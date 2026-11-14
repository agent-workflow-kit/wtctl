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
        raise FsafeError("unavailable", str(exc)) from exc


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
    the anchor refuses it without opening it; an unreadable table fails closed. The chain from
    ``/`` is not compared for mounts (it may legitimately cross them).
    """
    _require_dir_fd_support()
    fs = fs or DEFAULT_FS
    comps = _split_absolute(path)
    norm = "/" + "/".join(comps)
    if precheck:
        try:
            table = fs.mount_table()
        except gateway.MountTableUnavailable as exc:
            raise FsafeError("unavailable", str(exc), path=path) from exc
        if table is not None:
            mounts = mountpoints_under(norm, table)
            if mounts:
                raise FsafeError(
                    "mount_point",
                    f"mount point at or below the anchor {norm}: {', '.join(mounts)}",
                    path=norm,
                    mounts=mounts,
                )
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
        return Anchor(fd, ident, _mount_id(fs, fd), norm, fs)
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
) -> int:
    """Open ``components`` below ``start_fd`` one at a time and return the final directory's fd.

    Every directory is verified against the anchor's device and mount; the final one against
    ``expected`` (its recorded ``dir_id``) when given. Returns a duplicate of ``start_fd`` for an
    empty chain. The caller closes the result; no fd leaks on failure.
    """
    fd = os.dup(start_fd)
    try:
        walked: list[str] = []
        for c in components:
            walked.append(c)
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
    ``mount`` (on another device or mount; never opened or descended into), ``changed`` (swapped
    while being opened) and ``unreadable``."""

    path: tuple[str, ...]
    kind: str
    nlink: int = 1


def walk_tree(
    anchor: Anchor,
    root_fd: int,
    *,
    limit: int | None = None,
    descend: Callable[[Entry], bool] | None = None,
) -> Iterator[Entry]:
    """Depth-first, pre-order walk below ``root_fd`` without following links or crossing mounts.

    Iterative (a hostile 100k-deep tree cannot exhaust the interpreter stack). Non-regular
    entries are reported by type only. ``descend(entry)`` may veto entering a directory (for
    example to stop at a nested ``.git``). More than ``limit`` entries raises
    ``FsafeError("limit")``.
    A file that is a bind-mount over another file on the same device is only detected when it is
    opened (``open_file``), since a walk does not open regular files.
    """
    fs = anchor.fs
    count = 0
    stack: list[tuple[int, tuple[str, ...], deque[str], bool]] = []
    names = deque(sorted(os.listdir(root_fd)))
    stack.append((root_fd, (), names, False))
    try:
        while stack:
            fd, path, pending, _owned = stack[-1]
            if not pending:
                stack.pop()
                if _owned:
                    os.close(fd)
                continue
            name = pending.popleft()
            epath = (*path, name)
            try:
                st = fs.lstat_at(fd, name)
            except FileNotFoundError:
                continue
            except OSError:
                yield Entry(epath, "unreadable")
                continue
            count += 1
            if limit is not None and count > limit:
                raise FsafeError("limit", f"more than {limit} entries")
            kind = kind_of_mode(st.mode)
            if kind != "dir":
                if st.dev != anchor.identity.dev:
                    yield Entry(epath, "mount")
                else:
                    yield Entry(epath, kind, st.nlink)
                continue
            try:
                child = open_dir(anchor, fd, name)
            except FsafeError as exc:
                if exc.kind == "mount_point":
                    yield Entry(epath, "mount")
                elif exc.kind == "missing":
                    pass
                elif exc.kind in ("symlink", "not_dir"):
                    yield Entry(epath, "changed")
                else:
                    yield Entry(epath, "unreadable")
                continue
            cst = fs.fstat(child)
            if (cst.dev, cst.ino) != (st.dev, st.ino):
                os.close(child)
                yield Entry(epath, "changed")
                continue
            entry = Entry(epath, "dir", st.nlink)
            yield entry
            if descend is not None and not descend(entry):
                os.close(child)
                continue
            try:
                stack.append((child, epath, deque(sorted(os.listdir(child))), True))
            except OSError:
                os.close(child)
                yield Entry(epath, "unreadable")
    finally:
        for fd, _path, _pending, owned in stack:
            if owned:
                os.close(fd)


# ---- deletion ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Remaining:
    """An entry deletion left behind. ``reason``: mount_point, raced, not_empty or error."""

    path: tuple[str, ...]
    reason: str
    errno: int | None = None


@dataclass
class DeleteResult:
    removed: int = 0
    remaining: list[Remaining] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.remaining


Hook = Callable[[str, tuple[str, ...]], None]


def _open_dir_for_delete(anchor: Anchor, parent_fd: int, name: str, lst: FdStat, owner: int) -> int:
    """Open a directory for emptying; grant ``u+rwx`` on one we own that lacks it."""
    try:
        fd = open_dir(anchor, parent_fd, name)
    except FsafeError as exc:
        if exc.kind != "permission" or lst.uid != owner or not hasattr(os, "O_PATH"):
            raise
        # An owned directory without read permission (mode 000): open it by O_PATH, check it is
        # the very inode we classified, and chmod through the fd (never by name).
        try:
            pfd = os.open(
                name, os.O_PATH | _O_NOFOLLOW | _O_DIRECTORY | _O_CLOEXEC, dir_fd=parent_fd
            )
        except OSError as exc2:
            raise _map_oserror(exc2, name) from exc2
        try:
            pst = anchor.fs.fstat(pfd)
            if (pst.dev, pst.ino) != (lst.dev, lst.ino) or pst.uid != owner:
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
        if st.uid == owner and stat.S_IMODE(st.mode) & 0o700 != 0o700:
            os.fchmod(fd, stat.S_IMODE(st.mode) | 0o700)
    except OSError as exc:
        os.close(fd)
        raise _map_oserror(exc, name) from exc
    return fd


@dataclass
class _Frame:
    fd: int
    path: tuple[str, ...]
    names: list[str]
    name: str
    parent_fd: int
    dirty: bool = False


def delete_tree(
    anchor: Anchor,
    parent_fd: int,
    name: str,
    *,
    hook: Hook | None = None,
    expected: Identity | None = None,
    owner_uid: int | None = None,
) -> DeleteResult:
    """Delete directory ``name`` of ``parent_fd`` and everything below it, fd-based.

    Never follows a link (a symlink found is unlinked itself), never descends into a directory on
    another device or mount (reported ``mount_point``), grants ``u+rwx`` only on directories owned
    by ``owner_uid`` (default: the effective user), and returns everything it could not remove
    instead of raising. A root that is a symlink or not a directory, or whose identity differs
    from ``expected`` (the recorded ``dir_id``), raises before anything is deleted.

    ``hook(stage, path)`` is a test seam called after an entry was classified (``classified``)
    and after a directory was opened (``opened``).
    """
    check_component(name)
    fs = anchor.fs
    owner = os.geteuid() if owner_uid is None else owner_uid
    result = DeleteResult()
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
    try:
        root = _open_dir_for_delete(anchor, parent_fd, name, rst, owner)
    except FsafeError as exc:
        if exc.kind == "mount_point":
            result.remaining.append(Remaining((), "mount_point"))
            return result
        raise
    try:
        got = identity_of(root, fs)
        if (got.dev, got.ino) != (rst.dev, rst.ino):
            raise FsafeError("identity", f"{name}: changed while opening", path=name)
        if expected is not None and got != expected:
            raise FsafeError(
                "identity",
                f"{name} has identity {got.dev}:{got.ino}, recorded {expected.dev}:{expected.ino}",
                path=name,
            )
        names = os.listdir(root)
    except BaseException:
        os.close(root)
        raise
    stack = [_Frame(root, (), names, name, parent_fd)]
    try:
        while stack:
            f = stack[-1]
            if f.names:
                _delete_entry(anchor, stack, f.names.pop(), result, owner, hook)
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
                reason = "not_empty" if exc.errno in (errno.ENOTEMPTY, errno.EEXIST) else "error"
                result.remaining.append(Remaining(f.path, reason, exc.errno))
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
) -> None:
    f = stack[-1]
    path = (*f.path, name)

    def fail(reason: str, err: int | None = None) -> None:
        result.remaining.append(Remaining(path, reason, err))
        f.dirty = True

    try:
        st = anchor.fs.lstat_at(f.fd, name)
    except FileNotFoundError:
        return
    except OSError as exc:
        fail("error", exc.errno)
        return
    if hook is not None:
        hook("classified", path)
    if stat.S_ISDIR(st.mode):
        try:
            child = _open_dir_for_delete(anchor, f.fd, name, st, owner)
        except FsafeError as exc:
            if exc.kind == "mount_point":
                fail("mount_point")
                return
            if exc.kind == "missing":
                return
            if exc.kind not in ("symlink", "not_dir"):
                fail("error", exc.errno)
                return
            # swapped for a non-directory after it was classified: unlink that entry below
        else:
            cst = anchor.fs.fstat(child)
            if (cst.dev, cst.ino) != (st.dev, st.ino):
                os.close(child)
                fail("raced")
                return
            if hook is not None:
                hook("opened", path)
            try:
                names = os.listdir(child)
            except OSError as exc:
                os.close(child)
                fail("error", exc.errno)
                return
            stack.append(_Frame(child, path, names, name, f.fd))
            return
    try:
        os.unlink(name, dir_fd=f.fd)
        result.removed += 1
    except FileNotFoundError:
        pass
    except OSError as exc:
        fail("error", exc.errno)


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
