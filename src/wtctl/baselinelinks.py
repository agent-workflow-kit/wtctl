"""Resolving a symlink chain for the baseline, one component at a time, never following a link
the kernel would follow for us (docs/interface.md, Baseline, "Symlinks").

``resolve`` walks an absolute path from ``/`` through no-follow directory descriptors. A symlink
component (the final one or any other) is read with ``readlink`` and its target spliced into the
rest of the path, as the kernel does, so ``..`` after a link is physical. Every directory entered
below the reference anchor, and the final entry, must be on the anchor's device and mount; only
the anchor's own ancestors (its chain from ``/``) may be elsewhere. The result is one ``Chain``:
the hop strings and the final entry, opened (a regular file or a directory) or described
(``missing`` or a special type), never a FIFO, socket or device opened.

A target on another device or mount than the anchor is not an error: the walk stops at the first
entry that is, without opening it, and the result is an *uncovered* chain (``Chain.uncovered``):
the hops, the path and the type as far as ``lstat`` of that entry tells, and nothing read.
The one exception is the null device (``/dev/null``, matched by its resolved path and its device
numbers), which is described by type like any special node.
"""

from __future__ import annotations

import os
import stat
import sys
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from . import fsafe

MAX_HOPS = 16  # links followed per chain (the kernel allows 40; a longer chain is hostile)

# The null device's (major, minor), by platform; a platform without an entry has none, and a link
# to its null device is then uncovered like any other cross-device target.
_NULL_DEVICE = (1, 3) if sys.platform.startswith("linux") else None

_O_RDONLY_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)


@dataclass
class Chain:
    hops: tuple[str, ...]
    kind: str  # file, dir, missing, or a special type (fifo, socket, chardev, blockdev, other)
    path: str  # the physical absolute path of the final entry
    ident: tuple[int, int] | None  # (dev, ino) of the opened file or directory
    fd: int = -1  # open regular file or directory (verified), else -1
    executable: bool = False
    uncovered: bool = False  # on another device or mount: described, never opened

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> Chain:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def same_as(self, other: Chain) -> bool:
        return (self.hops, self.kind, self.path, self.ident, self.uncovered) == (
            other.hops,
            other.kind,
            other.path,
            other.ident,
            other.uncovered,
        )


def _is_ancestor(anchor: fsafe.Anchor, path: str) -> bool:
    return anchor.path == path or anchor.path.startswith(path.rstrip("/") + "/")


def resolve(
    anchor: fsafe.Anchor,
    path: str,
    charge: Callable[[], None],
    *,
    max_hops: int | None = None,
) -> Chain:
    """Resolve the absolute ``path``. ``charge`` is called once per component (the entry budget).
    Raises ``FsafeError`` (``link_depth``, ``link_loop``, ``identity``, ...); an entry on another
    device or mount ends the walk with an ``uncovered`` chain instead."""
    if not path.startswith("/"):
        raise fsafe.FsafeError("name", f"not an absolute path: {path!r}", path=path)
    limit = MAX_HOPS if max_hops is None else max_hops
    fs = anchor.fs
    pending: deque[str] = deque(path.split("/")[1:])
    root = os.open("/", _O_RDONLY_DIR)
    fds = [root]
    names: list[str] = []
    hops: list[str] = []
    seen: set[tuple[int, int, tuple[str, ...], tuple[str, ...]]] = set()

    def here(extra: str | None = None) -> str:
        return "/" + "/".join(names if extra is None else [*names, extra])

    def enter(dirfd: int, comp: str, dev: int) -> int | None:
        """The fd of directory ``comp``, or ``None`` when it is on another device or mount than
        the anchor (not opened at all when ``lstat`` or the mount table already says so)."""
        child = here(comp)
        if _is_ancestor(anchor, child):
            fd = fsafe.open_ancestor(dirfd, comp)
            if child == anchor.path and fsafe.identity_of(fd, fs) != anchor.identity:
                os.close(fd)
                raise fsafe.FsafeError("identity", f"{child} is not the anchor", path=child)
            return fd
        if child in anchor.mounts_below or dev != anchor.identity.dev:
            return None
        try:
            return fsafe.open_dir(anchor, dirfd, comp)
        except fsafe.FsafeError as exc:
            if exc.kind == "mount_point":  # another mount on the same device (a bind mount)
                return None
            raise

    def is_null(dirfd: int, comp: str, rest: list[str]) -> bool:
        """``/dev/null``: the resolved path is exactly that and the node is the null device."""
        if _NULL_DEVICE is None:
            return False
        if rest or here(comp) != "/dev/null":
            return False
        try:
            return fs.char_rdev_at(dirfd, comp) == _NULL_DEVICE
        except OSError:
            return False

    def beyond(kind: str, text: str) -> Chain:
        return Chain(tuple(hops), kind, text, None, uncovered=True)

    def missing() -> Chain:
        return Chain(tuple(hops), "missing", here(), None)

    try:
        while True:
            while pending and pending[0] in ("", "."):
                pending.popleft()
            if not pending:
                fd = os.dup(fds[-1])
                st = fs.fstat(fd)
                return Chain(tuple(hops), "dir", here(), (st.dev, st.ino), fd)
            comp = pending.popleft()
            charge()
            if comp == "..":
                if names:
                    names.pop()
                    os.close(fds.pop())
                continue
            fsafe.check_component(comp)
            dirfd = fds[-1]
            try:
                st = fs.lstat_at(dirfd, comp)
            except (FileNotFoundError, NotADirectoryError):
                return missing()
            except OSError as exc:
                raise fsafe.FsafeError(
                    "io", f"{comp}: {exc.strerror or exc}", path=comp, errno_=exc.errno
                ) from exc
            kind = fsafe.kind_of_mode(st.mode)
            if kind == "symlink":
                if len(hops) >= limit:
                    raise fsafe.FsafeError("link_depth", f"more than {limit} links", path=path)
                state = (st.dev, st.ino, tuple(names), tuple(pending))
                if state in seen:
                    raise fsafe.FsafeError("link_loop", "a link loop", path=path)
                seen.add(state)
                try:
                    target = os.readlink(comp, dir_fd=dirfd)
                except OSError as exc:
                    raise fsafe.FsafeError(
                        "io", f"{comp}: {exc.strerror or exc}", path=comp, errno_=exc.errno
                    ) from exc
                if target == "":
                    return missing()
                hops.append(target)
                if target.startswith("/"):
                    for fd in fds[1:]:
                        os.close(fd)
                    del fds[1:]
                    names.clear()
                pending.extendleft(reversed(target.split("/")))
                continue
            rest = [c for c in pending if c not in ("", ".")]
            final = not rest
            if kind == "dir":
                nfd = enter(dirfd, comp, st.dev)
                if nfd is None:
                    here_comp = here(comp)
                    if here_comp == "/dev" and rest == ["null"] and _NULL_DEVICE is not None:
                        # the null device sits on a device file system: look at the one node
                        charge()
                        ddir = fsafe.open_ancestor(dirfd, comp)
                        try:
                            if fs.char_rdev_at(ddir, "null") == _NULL_DEVICE:
                                return Chain(tuple(hops), "chardev", "/dev/null", None)
                        except OSError:
                            pass
                        finally:
                            os.close(ddir)
                    if final:
                        return beyond("dir", here_comp)
                    return beyond("unresolved", "/".join([here_comp, *rest]))
                fds.append(nfd)
                names.append(comp)
                continue
            if not final:
                return missing()  # a file or special entry used as a directory
            crossing = st.dev != anchor.identity.dev
            if kind != "file":
                # recorded by type only: it is never opened, so nothing of it leaks
                if crossing and not is_null(dirfd, comp, rest):
                    return beyond(kind, here(comp))
                return Chain(tuple(hops), kind, here(comp), None)
            if crossing:
                return beyond("file", here(comp))
            fd = fsafe.open_file(anchor, dirfd, comp)
            fst = fs.fstat(fd)
            return Chain(
                tuple(hops),
                "file",
                here(comp),
                (fst.dev, fst.ino),
                fd,
                bool(stat.S_IMODE(fst.mode) & 0o111),
            )
    finally:
        for fd in fds:
            os.close(fd)
