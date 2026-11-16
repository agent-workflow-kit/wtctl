"""The destination side of ``extract`` (docs/interface.md, Extraction rules: Destination).

A :class:`Dest` holds an open directory file descriptor. Every directory below it is opened one
component at a time with ``O_NOFOLLOW``, relative to its parent's descriptor, on the anchor's
device and mount; a path is never resolved by name, so a directory swapped for a symlink after
validation redirects nothing. Files are written to a temporary ``.wtctl-extract-<random>`` in the
target's directory and renamed over the target; deletions remove regular files only.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from . import fsafe

NEW_FILE_MODE = 0o644
NEW_DIR_MODE = 0o755
_CONFLICT_KINDS = frozenset(
    {"symlink", "not_dir", "mount_point", "not_regular", "identity", "owner", "name", "permission"}
)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class DestConflict(Exception):
    """The destination does not allow the operation (a link, a directory, a mount point...)."""


@dataclass
class Probe:
    exists: bool
    mode: int | None  # permission bits of an existing regular file


def _ident(text: str | None) -> fsafe.Identity:
    dev, _, ino = (text or "").partition(":")
    if not (dev.isdigit() and ino.isdigit()):
        raise fsafe.FsafeError("identity", "no identity is recorded")
    return fsafe.Identity(int(dev), int(ino))


class Dest:
    def __init__(self, anchor: fsafe.Anchor, fd: int, path: str) -> None:
        self.anchor = anchor
        self.fd = fd
        self.path = path

    def close(self) -> None:
        os.close(self.fd)
        self.anchor.close()

    def __enter__(self) -> Dest:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- opening ------------------------------------------------------------------------------

    @classmethod
    def open(cls, real: str, throwaway: dict[str, Any] | None) -> Dest:
        """Open the destination directory ``real`` (a resolved absolute path). Inside a
        throwaway, from the throwaway root down: the root and directory must have the record's
        identities and every directory below must be on the root's device and mount. Raises
        ``FsafeError`` (the caller reports ``dest_conflict``)."""
        if throwaway is None:
            anchor = fsafe.open_anchor(real, precheck=False)
            return cls(anchor, os.dup(anchor.fd), real)
        base, rid, tpath = throwaway["base_dir"], throwaway["id"], throwaway["path"]
        if not base or tpath != os.path.join(base, rid):
            raise fsafe.FsafeError("identity", "the destination throwaway has no recorded root")
        anchor = fsafe.open_anchor(base, expected=_ident(throwaway["root_id"]))
        try:
            tfd = fsafe.open_chain(
                anchor,
                anchor.fd,
                [rid],
                expected=_ident(throwaway["dir_id"]),
                start_path=anchor.path,
            )
            try:
                rest = [c for c in os.path.relpath(real, tpath).split(os.sep) if c != "."]
                fd = fsafe.open_chain(anchor, tfd, rest, start_path=tpath)
            finally:
                os.close(tfd)
        except BaseException:
            anchor.close()
            raise
        return cls(anchor, fd, real)

    def _open_parent(self, parts: Sequence[str], *, create: bool) -> int | None:
        """The directory fd of ``parts`` (the components above a target), or None when a
        component is missing and ``create`` is false. Raises ``DestConflict``."""
        fd = os.dup(self.fd)
        walked = self.path
        try:
            for c in parts:
                full = walked.rstrip("/") + "/" + c
                if full in self.anchor.mounts_below:
                    raise DestConflict(f"{c}: is a mount point")
                try:
                    nfd = self._open_or_create(fd, c, create)
                except fsafe.FsafeError as exc:
                    if exc.kind == "missing":
                        os.close(fd)
                        return None
                    if exc.kind in _CONFLICT_KINDS:
                        raise DestConflict(exc.message) from exc
                    raise
                os.close(fd)
                fd = nfd
                walked = full
            return fd
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def _open_or_create(self, fd: int, name: str, create: bool) -> int:
        try:
            return fsafe.open_dir(self.anchor, fd, name, forbid_git=True)
        except fsafe.FsafeError as exc:
            if exc.kind != "missing" or not create:
                raise
        try:
            os.mkdir(name, NEW_DIR_MODE, dir_fd=fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise fsafe.FsafeError("io", f"{name}: {exc.strerror}", errno_=exc.errno) from exc
        nfd = fsafe.open_dir(self.anchor, fd, name, forbid_git=True)
        try:
            os.fchmod(nfd, NEW_DIR_MODE)  # not at the mercy of the umask
        except OSError:
            pass
        return nfd

    # -- probing (validation, nothing is changed) ---------------------------------------------

    def probe(self, parts: Sequence[str]) -> Probe:
        """Whether the target is absent or a regular file; ``DestConflict`` otherwise."""
        parent = self._open_parent(parts[:-1], create=False)
        if parent is None:
            return Probe(False, None)
        try:
            return self._probe_in(parent, parts[-1])
        finally:
            os.close(parent)

    def _probe_in(self, parent: int, name: str) -> Probe:
        try:
            st = self.anchor.fs.lstat_at(parent, name)
        except FileNotFoundError:
            return Probe(False, None)
        except NotADirectoryError as exc:
            raise DestConflict(f"{name}: parent is not a directory") from exc
        except OSError as exc:
            raise DestConflict(f"{name}: {exc.strerror}") from exc
        if not stat.S_ISREG(st.mode):
            raise DestConflict(f"{name}: is a {fsafe.kind_of_mode(st.mode)}")
        return Probe(True, stat.S_IMODE(st.mode) & 0o777)

    # -- changes ------------------------------------------------------------------------------

    def write(self, parts: Sequence[str], data: bytes) -> tuple[int, bool]:
        """Write ``data`` to the target; returns ``(mode, created)``. Raises ``FsafeError`` or
        ``OSError`` (an I/O error: the caller reports ``io_error``)."""
        name = parts[-1]
        fsafe.check_component(name, forbid_git=True)
        parent = self._open_parent(parts[:-1], create=True)
        assert parent is not None
        try:
            try:
                probe = self._probe_in(parent, name)
            except DestConflict as exc:
                raise fsafe.FsafeError("not_regular", str(exc), path=name) from exc
            mode = probe.mode if probe.mode is not None else NEW_FILE_MODE
            self._atomic(parent, name, data, mode)
            return mode, not probe.exists
        finally:
            os.close(parent)

    @staticmethod
    def _atomic(parent: int, name: str, data: bytes, mode: int) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC
        tmp, fd = "", -1
        for _ in range(5):
            tmp = f".wtctl-extract-{secrets.token_hex(8)}"
            try:
                fd = os.open(tmp, flags, 0o600, dir_fd=parent)
                break
            except FileExistsError:
                continue
        if fd < 0:
            raise fsafe.FsafeError("exists", "could not create a temporary file", path=tmp)
        try:
            try:
                os.fchmod(fd, mode)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=parent)
            except OSError:
                pass
            raise
        try:
            os.fsync(parent)
        except OSError:
            pass

    def delete(self, parts: Sequence[str]) -> str:
        """``deleted`` or ``absent``; only a regular file is removed (``FsafeError`` otherwise)."""
        name = parts[-1]
        parent = self._open_parent(parts[:-1], create=False)
        if parent is None:
            return "absent"
        try:
            try:
                probe = self._probe_in(parent, name)
            except DestConflict as exc:
                raise fsafe.FsafeError("not_regular", str(exc), path=name) from exc
            if not probe.exists:
                return "absent"
            os.unlink(name, dir_fd=parent)
            return "deleted"
        finally:
            os.close(parent)
