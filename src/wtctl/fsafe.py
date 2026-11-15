"""Filesystem-safety primitives: no-follow opens, anchors, mount checks, fd-based deletion.

Everything here works on file descriptors, one path component at a time, so a directory swapped
for a symlink (or a mount point placed over a directory) after a check cannot redirect an
operation. The rules are docs/interface.md "Mount points" and docs/design.md D51, D52, D55, D56.

* Every walk has an **anchor**: a directory whose ``(st_dev, st_ino)`` identity wtctl recorded
  earlier. ``open_anchor`` opens it through a no-follow chain, reads the mount table first (Linux)
  and compares the opened fd with the recorded identity before anything else.
* Every directory and regular file opened below the anchor is compared by ``fstat`` of the opened
  fd (never a ``stat`` by name before opening) with the anchor's ``st_dev`` and, on Linux, mount
  id. A directory on another mount is never descended into, read or deleted from.
* Opens are non-blocking and never follow a link. An entry that is neither a regular file nor a
  directory (FIFO, socket, device) is recorded by type only and never opened for reading.
* Walks that must be bounded in time run in a child process (``wtctl.fsafe_child``).

Platforms: Linux and macOS (``openat``-style ``dir_fd`` support is required; elsewhere
``FsafeError("unavailable")``). The mount id (``/proc/self/fdinfo``) and the mountinfo pre-check
exist on Linux only; macOS compares ``st_dev``.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import stat
import sys
import unicodedata
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

from wtctl import gateway

# Fixed bound from docs/design.md D55; ``remove.nested_scan_limit`` overrides it per call.
DEFAULT_SCAN_LIMIT = 2_000_000

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
OPEN_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
OPEN_FILE_FLAGS = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC


class FsafeError(Exception):
    """A refused or failed primitive.

    ``kind`` is one of: ``name`` (unsafe path component or path), ``missing``, ``symlink``,
    ``not_dir``, ``not_regular``, ``hardlink``, ``mount_point``, ``identity``, ``owner``,
    ``exists``, ``permission``, ``limit``, ``deadline``, ``unavailable``, ``bad_request``, ``io``.
    """

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        path: str | None = None,
        errno_: int | None = None,
        mounts: Sequence[str] = (),
    ) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.path = path
        self.errno = errno_
        self.mounts = list(mounts)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "message": self.message,
            "path": self.path,
            "errno": self.errno,
            "mounts": self.mounts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> FsafeError:
        mounts = d.get("mounts")
        path = d.get("path")
        err = d.get("errno")
        return cls(
            str(d.get("kind", "io")),
            str(d.get("message", "")),
            path=path if isinstance(path, str) else None,
            errno_=err if isinstance(err, int) else None,
            mounts=[str(m) for m in mounts] if isinstance(mounts, list) else [],
        )


# ---- names ------------------------------------------------------------------------------

_HFS_IGNORABLE = frozenset(
    [*range(0x200C, 0x2010), *range(0x202A, 0x202F), *range(0x206A, 0x2070), 0xFEFF]
)
_SHORT_NAME = re.compile(r"git~\d+")


def is_dotgit(name: str) -> bool:
    """Whether ``name`` is ``.git`` or an alias of it on a case-folding, NTFS or HFS+ volume.

    Case folding (``.GIT``), NTFS trailing dots and spaces (``.git.``), NTFS alternate data
    streams (``.git::$INDEX_ALLOCATION``), the 8.3 short name (``GIT~1``; any ``git~<n>`` is
    refused), HFS+ ignorable code points and Unicode compatibility forms (fullwidth letters).
    Deliberately conservative: a false positive only refuses a write.
    """
    s = "".join(c for c in name if ord(c) not in _HFS_IGNORABLE)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").casefold()
    s = s.split(":", 1)[0].rstrip(" .")
    return s == ".git" or _SHORT_NAME.fullmatch(s) is not None


def check_component(name: str, *, forbid_git: bool = False) -> None:
    """Refuse a name that is not one plain path component (and, optionally, a ``.git`` alias)."""
    if name in ("", ".", "..") or "/" in name or "\0" in name:
        raise FsafeError("name", f"not a plain path component: {name!r}", path=name)
    if forbid_git and is_dotgit(name):
        raise FsafeError("name", f"refused: {name!r} is a .git name", path=name)


def _split_absolute(path: str) -> list[str]:
    if not path.startswith("/") or "\0" in path:
        raise FsafeError("name", f"not an absolute path: {path!r}", path=path)
    comps = [c for c in path.split("/") if c != ""]
    if any(c in (".", "..") for c in comps):
        raise FsafeError("name", f"path has '.' or '..' components: {path!r}", path=path)
    return comps


# ---- stat and mount seam ----------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """The ``(st_dev, st_ino)`` identity of a directory, as recorded by wtctl."""

    dev: int
    ino: int


@dataclass(frozen=True)
class FdStat:
    dev: int
    ino: int
    mode: int
    uid: int
    nlink: int


def _fdstat(st: os.stat_result) -> FdStat:
    return FdStat(st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_nlink)


class Fs:
    """Every ``fstat``, mount id and mount-table read of this module goes through one object.

    Tests substitute a subclass (``adjust`` fakes ``st_dev``; ``mount_id`` fakes a bind mount).
    """

    def adjust(self, st: FdStat) -> FdStat:
        return st

    def fstat(self, fd: int) -> FdStat:
        return self.adjust(_fdstat(os.fstat(fd)))

    def lstat_at(self, dir_fd: int, name: str) -> FdStat:
        return self.adjust(_fdstat(os.stat(name, dir_fd=dir_fd, follow_symlinks=False)))

    def mount_id(self, fd: int) -> int | None:
        return gateway.fd_mount_id(fd)

    def mount_table(self) -> list[gateway.MountEntry] | None:
        return gateway.read_mount_table()


DEFAULT_FS = Fs()


def kind_of_mode(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "chardev"
    if stat.S_ISBLK(mode):
        return "blockdev"
    return "other"


def identity_of(fd: int, fs: Fs = DEFAULT_FS) -> Identity:
    st = fs.fstat(fd)
    return Identity(st.dev, st.ino)


def raise_fd_limit() -> None:
    """An fd-based walk holds one fd per level of depth; raise the soft limit to the hard one.

    Best effort (a failure leaves the limit alone; the walk then reports ``EMFILE`` per entry).
    """
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = hard if hard != resource.RLIM_INFINITY else 1_048_576
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(want, 1_048_576), hard))
    except (ImportError, ValueError, OSError):
        pass


def _require_dir_fd_support() -> None:
    needed = (os.open, os.stat, os.unlink, os.rmdir, os.mkdir, os.rename)
    if not all(f in os.supports_dir_fd for f in needed) or not _O_DIRECTORY or not _O_NOFOLLOW:
        raise FsafeError("unavailable", f"{sys.platform} lacks the openat family wtctl needs")


def _map_oserror(
    exc: OSError, path: str, *, dir_fd: int | None = None, name: str | None = None
) -> FsafeError:
    e = exc.errno
    if e == errno.ENOENT:
        return FsafeError("missing", f"{path}: no such file or directory", path=path, errno_=e)
    if e == errno.EEXIST:
        return FsafeError("exists", f"{path}: already exists", path=path, errno_=e)
    if e in (errno.ELOOP, errno.ENOTDIR):
        if e == errno.ELOOP:
            return FsafeError("symlink", f"{path}: is a symbolic link", path=path, errno_=e)
        if dir_fd is not None and name is not None:
            try:
                if stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
                    return FsafeError("symlink", f"{path}: is a symbolic link", path=path, errno_=e)
            except OSError:
                pass
        return FsafeError("not_dir", f"{path}: not a directory", path=path, errno_=e)
    if e in (errno.EACCES, errno.EPERM):
        return FsafeError("permission", f"{path}: permission denied", path=path, errno_=e)
    return FsafeError("io", f"{path}: {exc.strerror or exc}", path=path, errno_=e)


# ---- mount points -----------------------------------------------------------------------


def mountpoints_under(path: str, table: Sequence[gateway.MountEntry]) -> list[str]:
    """Mount points at or below ``path`` (a normalized absolute path), in table order."""
    prefix = path.rstrip("/") + "/"
    return [
        m.mount_point for m in table if m.mount_point == path or m.mount_point.startswith(prefix)
    ]


def is_fuse_fstype(fstype: str) -> bool:
    """FUSE file systems: ``fuse``, ``fuseblk``, ``fuse.<x>``, ``macfuse``, ``osxfuse``, ..."""
    return "fuse" in fstype.lower()


@dataclass(frozen=True)
class MountDescription:
    """What a human sees before re-accepting an anchor (docs/design.md D56 (4))."""

    fstype: str
    source: str
    mount_point: str
    is_fuse: bool


def describe_mount(anchor: Anchor) -> MountDescription | None:
    """The anchor's file-system type, mount source and mount point.

    Linux: the mountinfo entry with the anchor's mount id. Elsewhere (and as a fallback): the
    entry with the longest mount point that contains the anchor's path. None if no table exists.
    """
    try:
        table = anchor.fs.mount_table()
    except gateway.MountTableUnavailable:
        return None
    if not table:
        return None
    best: gateway.MountEntry | None = None
    if anchor.mnt_id is not None:
        best = next((m for m in table if m.mount_id == anchor.mnt_id), None)
    if best is None:
        prefix = anchor.path.rstrip("/") + "/"
        contain = [m for m in table if prefix.startswith(m.mount_point.rstrip("/") + "/")]
        best = max(contain, key=lambda m: len(m.mount_point), default=None)
    if best is None:
        return None
    return MountDescription(best.fstype, best.source, best.mount_point, is_fuse_fstype(best.fstype))


# ---- anchors ----------------------------------------------------------------------------


def _mount_id(fs: Fs, fd: int) -> int | None:
    try:
        return fs.mount_id(fd)
    except gateway.MountTableUnavailable as exc:
        raise FsafeError(
            "unavailable", str(exc), errno_=getattr(exc.__cause__, "errno", None)
        ) from exc


@dataclass
class Anchor:
    """An open directory with a verified identity, device and mount id.

    ``verify_fd`` is the check every other fd opened below it must pass.
    """

    fd: int
    identity: Identity
    mnt_id: int | None
    path: str
    fs: Fs = field(default_factory=Fs, repr=False)
    mounts_below: tuple[str, ...] = ()
    """Absolute mount points strictly below the anchor (Linux mount table; empty without one)."""

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> Anchor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def verify_fd(self, fd: int, path: str = "") -> FdStat:
        """``fstat`` the opened fd and compare ``st_dev`` and mount id with the anchor's."""
        st = self.fs.fstat(fd)
        if st.dev != self.identity.dev:
            raise FsafeError(
                "mount_point", f"{path or '<fd>'} is on another device (mount point)", path=path
            )
        if self.mnt_id is not None and _mount_id(self.fs, fd) != self.mnt_id:
            raise FsafeError(
                "mount_point", f"{path or '<fd>'} is on another mount (mount point)", path=path
            )
        return st


def _open_dir_nofollow(parent_fd: int, name: str, display: str) -> int:
    try:
        return os.open(name, OPEN_DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _map_oserror(exc, display, dir_fd=parent_fd, name=name) from exc


def open_anchor(
    path: str, *, expected: Identity | None = None, fs: Fs | None = None, precheck: bool = True
) -> Anchor:
    """Open ``path`` through a no-follow chain from ``/`` and verify it against ``expected``.

    ``expected`` is the *recorded* identity (None only for locations wtctl never recorded).
    With ``precheck`` (the default) the mount table is read first and any mount point at or below
    the anchor path refuses it without opening it; an unreadable table fails closed. Mounts
    strictly below the anchor do not refuse it: they are kept in ``Anchor.mounts_below`` so walks
    skip and report them (the fd checks refuse a mount on the chain to a walk root). The chain from
    ``/`` is not compared for mounts (it may legitimately cross them).
    """
    _require_dir_fd_support()
    fs = fs or DEFAULT_FS
    comps = _split_absolute(path)
    norm = "/" + "/".join(comps)
    below: tuple[str, ...] = ()
    if precheck:
        try:
            table = fs.mount_table()
        except gateway.MountTableUnavailable as exc:
            raise FsafeError("unavailable", str(exc), path=path) from exc
        if table is not None:
            mounts = mountpoints_under(norm, table)
            if norm in mounts:
                raise FsafeError(
                    "mount_point",
                    f"the anchor {norm} is a mount point",
                    path=norm,
                    mounts=[norm],
                )
            below = tuple(mounts)
    fd = os.open("/", os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        walked = ""
        for c in comps:
            walked += "/" + c
            nfd = _open_dir_nofollow(fd, c, walked)
            os.close(fd)
            fd = nfd
        st = fs.fstat(fd)
        ident = Identity(st.dev, st.ino)
        if expected is not None and ident != expected:
            raise FsafeError(
                "identity",
                f"{norm} has identity {ident.dev}:{ident.ino}, "
                f"recorded {expected.dev}:{expected.ino}",
                path=norm,
            )
        return Anchor(fd, ident, _mount_id(fs, fd), norm, fs, below)
    except BaseException:
        os.close(fd)
        raise


# ---- opens below an anchor --------------------------------------------------------------


def open_dir(
    anchor: Anchor,
    parent_fd: int,
    name: str,
    *,
    owner_uid: int | None = None,
    forbid_git: bool = False,
) -> int:
    """Open directory ``name`` of ``parent_fd``: no-follow, non-blocking, verified by ``anchor``.

    With ``owner_uid``, the directory must be owned by that user.
    """
    check_component(name, forbid_git=forbid_git)
    fd = _open_dir_nofollow(parent_fd, name, name)
    try:
        st = anchor.verify_fd(fd, name)
        if not stat.S_ISDIR(st.mode):
            raise FsafeError("not_dir", f"{name}: not a directory", path=name)
        if owner_uid is not None and st.uid != owner_uid:
            raise FsafeError("owner", f"{name}: not owned by uid {owner_uid}", path=name)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_chain(
    anchor: Anchor,
    start_fd: int,
    components: Sequence[str],
    *,
    owner_uid: int | None = None,
    forbid_git: bool = False,
    expected: Identity | None = None,
    start_path: str | None = None,
) -> int:
    """Open ``components`` below ``start_fd`` one at a time and return the final directory's fd.

    Every directory is verified against the anchor's device and mount; the final one against
    ``expected`` (its recorded ``dir_id``) when given. Returns a duplicate of ``start_fd`` for an
    empty chain. The caller closes the result; no fd leaks on failure. With ``start_path`` (the
    absolute path of ``start_fd``) a component that is one of the anchor's recorded
    ``mounts_below`` is refused (``mount_point``) before it is opened.
    """
    mounts = frozenset(anchor.mounts_below) if start_path is not None else frozenset()
    fd = os.dup(start_fd)
    try:
        walked: list[str] = []
        for c in components:
            walked.append(c)
            if mounts and start_path is not None:
                full = _abs_join(start_path, walked[:-1], c)
                if full in mounts:
                    raise FsafeError(
                        "mount_point", f"{full} is a mount point", path="/".join(walked)
                    )
            nfd = open_dir(anchor, fd, c, owner_uid=owner_uid, forbid_git=forbid_git)
            os.close(fd)
            fd = nfd
        ident = identity_of(fd, anchor.fs)
        if not components:
            anchor.verify_fd(fd, "<start>")
        if expected is not None and ident != expected:
            raise FsafeError(
                "identity",
                f"{'/'.join(walked) or '.'} has identity {ident.dev}:{ident.ino}, "
                f"recorded {expected.dev}:{expected.ino}",
                path="/".join(walked),
            )
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_file(
    anchor: Anchor,
    parent_fd: int,
    name: str,
    *,
    single_link: bool = False,
    forbid_git: bool = False,
) -> int:
    """Open a regular file read-only: no-follow, non-blocking, never a FIFO, socket or device.

    The type is checked by ``lstat`` first so a special file is never opened, then again by
    ``fstat`` of the opened fd (authoritative). With ``single_link`` a hard-linked file
    (``st_nlink > 1``) is refused.
    """
    check_component(name, forbid_git=forbid_git)
    try:
        pre = anchor.fs.lstat_at(parent_fd, name)
    except OSError as exc:
        raise _map_oserror(exc, name) from exc
    if stat.S_ISLNK(pre.mode):
        raise FsafeError("symlink", f"{name}: is a symbolic link", path=name)
    if not stat.S_ISREG(pre.mode):
        raise FsafeError("not_regular", f"{name}: is a {kind_of_mode(pre.mode)}", path=name)
    try:
        fd = os.open(name, OPEN_FILE_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _map_oserror(exc, name, dir_fd=parent_fd, name=name) from exc
    try:
        st = anchor.verify_fd(fd, name)
        if not stat.S_ISREG(st.mode):
            raise FsafeError("not_regular", f"{name}: is a {kind_of_mode(st.mode)}", path=name)
        if single_link and st.nlink > 1:
            raise FsafeError("hardlink", f"{name}: has {st.nlink} hard links", path=name)
        return fd
    except BaseException:
        os.close(fd)
        raise


# ---- walking ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One walked entry. ``kind``: file, dir, symlink, fifo, socket, chardev, blockdev, other, or
    ``mount`` (on another device or mount, or a recorded mount point; never opened or descended
    into), ``changed`` (swapped while being opened) and ``unreadable`` (``errno`` when known)."""

    path: tuple[str, ...]
    kind: str
    nlink: int = 1
    errno: int | None = None


def _abs_join(root: str, parts: Sequence[str], name: str) -> str:
    return root.rstrip("/") + "/" + "/".join((*parts, name))


def walk_tree(
    anchor: Anchor,
    root_fd: int,
    *,
    limit: int | None = None,
    descend: Callable[[Entry], bool] | None = None,
    dirs_only: bool = False,
    root_path: str | None = None,
) -> Iterator[Entry]:
    """Depth-first, pre-order walk below ``root_fd`` without following links or crossing mounts.

    Iterative (a hostile 100k-deep tree cannot exhaust the interpreter stack) and linear in
    memory (one shared name stack; a path tuple is built only for the entry emitted). Non-regular
    entries are reported by type only. ``descend(entry)`` may veto entering a directory (for
    example to stop at a nested ``.git``). More than ``limit`` entries raises
    ``FsafeError("limit")``, checked while each directory is read, so a huge directory is never
    listed whole. With ``dirs_only`` non-directories are skipped before they are counted.
    ``root_path`` (the absolute path of ``root_fd``) enables skipping the anchor's recorded
    ``mounts_below`` by path, without opening them.
    A file that is a bind-mount over another file on the same device is only detected when it is
    opened (``open_file``), since a walk does not open regular files.
    """
    if root_path is None and anchor.mounts_below:
        raise FsafeError("bad_request", "root_path is required: the anchor has mounts below it")
    raise_fd_limit()
    fs = anchor.fs
    count = 0
    read = 0
    mounts = frozenset(anchor.mounts_below) if root_path is not None else frozenset()
    mount_names = {m.rsplit("/", 1)[-1] for m in mounts}

    def read_names(fd: int) -> deque[str]:
        nonlocal read
        names: list[str] = []
        with os.scandir(fd) as it:
            for e in it:
                if dirs_only:
                    try:
                        if not e.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                read += 1
                if limit is not None and read > limit:
                    raise FsafeError("limit", f"more than {limit} entries")
                names.append(e.name)
        names.sort()
        return deque(names)

    stack: list[tuple[int, deque[str], bool]] = [(root_fd, read_names(root_fd), False)]
    parts: list[str] = []  # names of the open owned directories, root excluded
    try:
        while stack:
            fd, pending, owned = stack[-1]
            if not pending:
                stack.pop()
                if owned:
                    os.close(fd)
                    parts.pop()
                continue
            name = pending.popleft()
            if (
                name in mount_names
                and root_path is not None
                and _abs_join(root_path, parts, name) in mounts
            ):
                yield Entry((*parts, name), "mount")
                continue
            try:
                st = fs.lstat_at(fd, name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                yield Entry((*parts, name), "unreadable", errno=exc.errno)
                continue
            count += 1
            if limit is not None and count > limit:
                raise FsafeError("limit", f"more than {limit} entries")
            kind = kind_of_mode(st.mode)
            if kind != "dir":
                if st.dev != anchor.identity.dev:
                    yield Entry((*parts, name), "mount")
                else:
                    yield Entry((*parts, name), kind, st.nlink)
                continue
            try:
                child = open_dir(anchor, fd, name)
            except FsafeError as exc:
                if exc.kind == "mount_point":
                    yield Entry((*parts, name), "mount")
                elif exc.kind == "missing":
                    pass
                elif exc.kind in ("symlink", "not_dir"):
                    yield Entry((*parts, name), "changed")
                else:
                    yield Entry((*parts, name), "unreadable", errno=exc.errno)
                continue
            cst = fs.fstat(child)
            if (cst.dev, cst.ino) != (st.dev, st.ino):
                os.close(child)
                yield Entry((*parts, name), "changed")
                continue
            entry = Entry((*parts, name), "dir", st.nlink)
            try:
                yield entry
                enter = descend is None or descend(entry)
            except BaseException:
                os.close(child)
                raise
            if not enter:
                os.close(child)
                continue
            try:
                names = read_names(child)
            except OSError as exc:
                os.close(child)
                yield Entry((*parts, name), "unreadable", errno=exc.errno)
                continue
            except BaseException:
                os.close(child)
                raise
            stack.append((child, names, True))
            parts.append(name)
    finally:
        for fd, _pending, owned in stack:
            if owned:
                os.close(fd)


# ---- deletion ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Remaining:
    """An entry deletion left behind. ``reason``: mount_point, raced, not_empty or error."""

    path: tuple[str, ...]
    reason: str
    errno: int | None = None


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def escape_for_details(text: str) -> str:
    """Replace control characters (newline included) by ``\\xNN`` for output fields."""
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


REMAINING_CAP = 100  # docs/interface.md: details.remaining holds at most 100 entries


@dataclass
class DeleteResult:
    """``remaining`` holds the first ``REMAINING_CAP`` entries left; ``remaining_total`` counts all.

    Names are raw (they may contain a newline or other control characters); the consumer escapes
    them when it builds ``details.remaining``.
    """

    removed: int = 0
    remaining: list[Remaining] = field(default_factory=list)
    remaining_total: int = 0

    def add(self, item: Remaining) -> None:
        self.remaining_total += 1
        if len(self.remaining) < REMAINING_CAP:
            self.remaining.append(item)

    @property
    def complete(self) -> bool:
        return self.remaining_total == 0

    def remaining_paths(self, root: str) -> list[str]:
        """The entries left as absolute paths for ``details.remaining``: control characters
        (a newline included) are escaped, as the ``abs_path`` schema forbids a newline."""
        base = root.rstrip("/")
        return [escape_for_details("/".join([base, *r.path])) for r in self.remaining]


Hook = Callable[[str, tuple[str, ...]], None]


def _open_dir_for_delete(
    anchor: Anchor,
    parent_fd: int,
    name: str,
    lst: FdStat,
    owner: int,
    expected: Identity | None = None,
) -> int:
    """Open a directory for emptying; grant ``u+rwx`` on one we own that lacks it.

    Nothing is chmodded before the opened inode is verified: ``(dev, ino)`` equal to ``lst`` (what
    the caller classified) and to ``expected`` (the recorded id) when given, else
    ``FsafeError("identity")`` and the mode stays as it was.
    """

    def check(st: FdStat) -> None:
        if (st.dev, st.ino) != (lst.dev, lst.ino):
            raise FsafeError("identity", f"{name}: changed while opening", path=name)
        if expected is not None and Identity(st.dev, st.ino) != expected:
            raise FsafeError(
                "identity",
                f"{name} has identity {st.dev}:{st.ino}, recorded {expected.dev}:{expected.ino}",
                path=name,
            )

    try:
        fd = open_dir(anchor, parent_fd, name)
    except FsafeError as exc:
        if exc.kind != "permission" or lst.uid != owner or not hasattr(os, "O_PATH"):
            raise
        # An owned directory without read permission (mode 000): open it by O_PATH, check it is
        # the very inode we classified, on the anchor's device and mount, and chmod through the
        # fd (never by name).
        try:
            pfd = os.open(
                name, os.O_PATH | _O_NOFOLLOW | _O_DIRECTORY | _O_CLOEXEC, dir_fd=parent_fd
            )
        except OSError as exc2:
            raise _map_oserror(exc2, name) from exc2
        try:
            pst = anchor.verify_fd(pfd, name)
            check(pst)
            if pst.uid != owner:
                raise FsafeError(
                    "permission", f"{name}: changed while fixing permissions", path=name
                )
            gateway.chmod_opath_fd(pfd, stat.S_IMODE(pst.mode) | 0o700)
        except OSError as exc2:
            raise _map_oserror(exc2, name) from exc2
        finally:
            os.close(pfd)
        fd = open_dir(anchor, parent_fd, name)
    try:
        st = anchor.fs.fstat(fd)
        check(st)
        if st.uid == owner and stat.S_IMODE(st.mode) & 0o700 != 0o700:
            os.fchmod(fd, stat.S_IMODE(st.mode) | 0o700)
    except OSError as exc:
        os.close(fd)
        raise _map_oserror(exc, name) from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


@dataclass
class _Frame:
    fd: int
    names: list[str]
    name: str
    parent_fd: int
    dirty: bool = False


def _frame_path(stack: Sequence[_Frame]) -> tuple[str, ...]:
    """Names of the open directories below the root frame (built only when needed)."""
    return tuple(fr.name for fr in stack[1:])


def delete_tree(
    anchor: Anchor,
    parent_fd: int,
    name: str,
    *,
    hook: Hook | None = None,
    expected: Identity | None = None,
    owner_uid: int | None = None,
    parent_path: str | None = None,
) -> DeleteResult:
    """Delete directory ``name`` of ``parent_fd`` and everything below it, fd-based.

    Never follows a link (a symlink found is unlinked itself), never descends into a directory on
    another device or mount (reported ``mount_point``), grants ``u+rwx`` only on directories owned
    by ``owner_uid`` (default: the effective user), and returns everything it could not remove
    instead of raising. A root that is a symlink or not a directory, or whose identity differs
    from ``expected`` (the recorded ``dir_id``), raises before anything is deleted or chmodded.
    ``parent_path`` (the absolute path of ``parent_fd``) enables skipping the anchor's recorded
    ``mounts_below`` by path, without opening them.

    ``hook(stage, path)`` is a test seam called after an entry was classified (``classified``)
    and after a directory was opened (``opened``).
    """
    check_component(name)
    if parent_path is None and anchor.mounts_below:
        raise FsafeError("bad_request", "parent_path is required: the anchor has mounts below it")
    raise_fd_limit()
    fs = anchor.fs
    owner = os.geteuid() if owner_uid is None else owner_uid
    result = DeleteResult()
    mounts = frozenset(anchor.mounts_below) if parent_path is not None else frozenset()
    mount_names = {m.rsplit("/", 1)[-1] for m in mounts}

    def is_mount(stack: Sequence[_Frame], entry: str) -> bool:
        if parent_path is None or entry not in mount_names:
            return False
        return _abs_join(parent_path, [fr.name for fr in stack], entry) in mounts

    try:
        rst = fs.lstat_at(parent_fd, name)
    except FileNotFoundError:
        return result
    except OSError as exc:
        raise _map_oserror(exc, name) from exc
    if stat.S_ISLNK(rst.mode):
        raise FsafeError("symlink", f"{name}: is a symbolic link", path=name)
    if not stat.S_ISDIR(rst.mode):
        raise FsafeError("not_dir", f"{name}: not a directory", path=name)
    if parent_path is not None and is_mount([], name):
        result.add(Remaining((), "mount_point"))
        return result
    try:
        root = _open_dir_for_delete(anchor, parent_fd, name, rst, owner, expected)
    except FsafeError as exc:
        if exc.kind == "mount_point":
            result.add(Remaining((), "mount_point"))
            return result
        raise
    try:
        names = os.listdir(root)
    except BaseException:
        os.close(root)
        raise
    stack = [_Frame(root, names, name, parent_fd)]
    try:
        while stack:
            f = stack[-1]
            if f.names:
                _delete_entry(anchor, stack, f.names.pop(), result, owner, hook, is_mount)
                continue
            stack.pop()
            os.close(f.fd)
            f.fd = -1
            if f.dirty:
                if stack:
                    stack[-1].dirty = True
                continue
            try:
                os.rmdir(f.name, dir_fd=f.parent_fd)
                result.removed += 1
            except FileNotFoundError:
                pass
            except OSError as exc:
                if exc.errno == errno.EBUSY:
                    reason = "mount_point"
                elif exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    reason = "not_empty"
                else:
                    reason = "error"
                # the root frame (stack now empty) reports the empty path
                path = (*_frame_path(stack), f.name) if stack else ()
                result.add(Remaining(path, reason, exc.errno))
                if stack:
                    stack[-1].dirty = True
    finally:
        for f in stack:
            if f.fd >= 0:
                os.close(f.fd)
    return result


def _delete_entry(
    anchor: Anchor,
    stack: list[_Frame],
    name: str,
    result: DeleteResult,
    owner: int,
    hook: Hook | None,
    is_mount: Callable[[Sequence[_Frame], str], bool],
) -> None:
    f = stack[-1]

    def fail(reason: str, err: int | None = None) -> None:
        result.add(Remaining((*_frame_path(stack), name), reason, err))
        f.dirty = True

    if is_mount(stack, name):
        fail("mount_point")
        return
    try:
        st = anchor.fs.lstat_at(f.fd, name)
    except FileNotFoundError:
        return
    except OSError as exc:
        fail("error", exc.errno)
        return
    if hook is not None:
        hook("classified", (*_frame_path(stack), name))
    if stat.S_ISDIR(st.mode):
        try:
            child = _open_dir_for_delete(anchor, f.fd, name, st, owner)
        except FsafeError as exc:
            if exc.kind == "mount_point":
                fail("mount_point")
                return
            if exc.kind == "identity":  # not the directory we classified; nothing was chmodded
                fail("raced")
                return
            if exc.kind == "missing":
                return
            if exc.kind not in ("symlink", "not_dir"):
                fail("error", exc.errno)
                return
            # swapped for a non-directory after it was classified: unlink that entry below
        else:
            if hook is not None:
                hook("opened", (*_frame_path(stack), name))
            try:
                names = os.listdir(child)
            except OSError as exc:
                os.close(child)
                fail("error", exc.errno)
                return
            stack.append(_Frame(child, names, name, f.fd))
            return
    try:
        os.unlink(name, dir_fd=f.fd)
        result.removed += 1
    except FileNotFoundError:
        pass
    except OSError as exc:
        fail("mount_point" if exc.errno == errno.EBUSY else "error", exc.errno)


# ---- writes -----------------------------------------------------------------------------


def create_new_file(dir_fd: int, name: str, *, mode: int = 0o600, forbid_git: bool = True) -> int:
    """Create ``name`` exclusively and without following a link; returns a write-only fd.

    ``O_EXCL`` makes a pre-planted symlink (even a dangling one) fail with ``exists``. Refuses
    ``.git`` aliases by default (extraction and ``copy_env`` destinations).
    """
    check_component(name, forbid_git=forbid_git)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
    try:
        fd = os.open(name, flags, mode, dir_fd=dir_fd)
    except OSError as exc:
        raise _map_oserror(exc, name) from exc
    try:
        os.fchmod(fd, mode)
    except OSError as exc:
        os.close(fd)
        raise _map_oserror(exc, name) from exc
    return fd


def ensure_dir(
    anchor: Anchor, parent_fd: int, name: str, *, forbid_git: bool = True
) -> tuple[int, bool]:
    """``mkdirat`` (mode 0700) if missing, then open it no-follow. Returns ``(fd, created)``."""
    check_component(name, forbid_git=forbid_git)
    created = True
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        created = False
    except OSError as exc:
        raise _map_oserror(exc, name) from exc
    return open_dir(anchor, parent_fd, name), created


def write_file_atomic(dir_fd: int, name: str, data: bytes, *, mode: int = 0o644) -> None:
    """Replace ``name`` in the directory ``dir_fd`` atomically (temp file, fsync, rename).

    The temp file is created in the same directory with ``O_CREAT|O_EXCL|O_NOFOLLOW`` and renamed
    over the target relative to the directory fd; a symlink at the target is replaced, never
    written through. A directory at the target is refused. On failure the temp file is removed
    and the target is untouched.
    """
    check_component(name)
    try:
        if stat.S_ISDIR(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
            raise FsafeError("not_regular", f"{name}: is a directory", path=name)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise _map_oserror(exc, name) from exc
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
    tmp = ""
    fd = -1
    for _ in range(3):
        tmp = f".wtctl-{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(tmp, flags, mode, dir_fd=dir_fd)
            break
        except FileExistsError:
            continue
        except OSError as exc:
            raise _map_oserror(exc, tmp) from exc
    if fd < 0:
        raise FsafeError("exists", "could not create a unique temporary file", path=tmp)
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except OSError:
            pass
        raise _map_oserror(exc, name) from exc
    try:
        os.fsync(dir_fd)
    except OSError:
        pass


def rewrite_pointer(
    anchor: Anchor,
    start_fd: int,
    components: Sequence[str],
    name: str,
    data: bytes,
    *,
    owner_uid: int,
    expected: Identity | None = None,
    mode: int = 0o644,
) -> None:
    """Atomically rewrite pointer file ``name`` in the directory reached by ``components``.

    The directory is reached through the no-follow fd chain (each one on the anchor's device and
    mount and owned by ``owner_uid``, the last matching ``expected`` when given); nothing is
    written if any step fails.
    """
    fd = open_chain(anchor, start_fd, components, owner_uid=owner_uid, expected=expected)
    try:
        write_file_atomic(fd, name, data, mode=mode)
    finally:
        os.close(fd)


__all__ = [
    "DEFAULT_FS",
    "DEFAULT_SCAN_LIMIT",
    "Anchor",
    "DeleteResult",
    "Entry",
    "FdStat",
    "FsafeError",
    "Fs",
    "Identity",
    "MountDescription",
    "Remaining",
    "check_component",
    "create_new_file",
    "delete_tree",
    "describe_mount",
    "escape_for_details",
    "ensure_dir",
    "identity_of",
    "is_dotgit",
    "is_fuse_fstype",
    "kind_of_mode",
    "mountpoints_under",
    "open_anchor",
    "open_chain",
    "open_dir",
    "open_file",
    "rewrite_pointer",
    "walk_tree",
    "write_file_atomic",
]
