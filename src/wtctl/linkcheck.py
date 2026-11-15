"""Integrity (docs/interface.md, Integrity): a record still names the worktree it was made for.

Every read is through no-follow descriptors anchored at a recorded identity. A mismatch is the
refusal ``unlinked``. With ``rebind`` (``baseline accept``) a recorded *identity* that differs from
the current one does not refuse (a file system may present a new ``st_dev`` after a reboot): the
current identities are returned and the caller shows and re-records them. Everything else
(pointers, symlinks, mount points on the chain) still refuses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from . import fsafe
from .errors import WtctlError

MAX_POINTER = 4096


@dataclass
class Links:
    common_dir: str
    common_id: str  # current "<dev>:<ino>"
    dir_id: str
    root: tuple[str, str | None, str] | None  # (base_dir, recorded identity, current), owned only


def _unlinked(message: str) -> WtctlError:
    return WtctlError("refused", message, reason="unlinked")


def _ident(text: str | None) -> fsafe.Identity | None:
    if not text:
        return None
    dev, _, ino = text.partition(":")
    return fsafe.Identity(int(dev), int(ino))


def _text(i: fsafe.Identity) -> str:
    return f"{i.dev}:{i.ino}"


def _read_small(anchor: fsafe.Anchor, dir_fd: int, name: str) -> str:
    fd = fsafe.open_file(anchor, dir_fd, name)
    try:
        data = os.read(fd, MAX_POINTER + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_POINTER:
        raise fsafe.FsafeError("limit", f"{name} is too large")
    return data.decode("utf-8", "surrogateescape")


def _pointer_value(text: str, prefix: str = "") -> str | None:
    if prefix:
        if not text.startswith(prefix):
            return None
        text = text[len(prefix) :]
    if text.endswith("\n"):
        text = text[:-1]
    return None if "\n" in text else text


def verify_links(record: dict[str, Any], state: Any, *, rebind: bool = False) -> Links:
    path, common = record["path"], record["common_dir"]
    admin, owned = record["admin_dir"], bool(record["owned"])
    try:
        return _verify(record, state, path, common, admin, owned, rebind)
    except fsafe.FsafeError as exc:
        raise _unlinked(f"{path} is not the recorded worktree ({exc.kind})") from exc


def _verify(
    record: dict[str, Any],
    state: Any,
    path: str,
    common: str,
    admin: str | None,
    owned: bool,
    rebind: bool,
) -> Links:
    if not common or not path:
        raise _unlinked(f"{record['id']} has no recorded location")
    common_expected = None if rebind else _ident(record["common_id"])
    dir_expected = None if rebind else _ident(record["dir_id"])
    with fsafe.open_anchor(common, expected=common_expected) as canchor:
        base = record["base_dir"]
        root_info: tuple[str, str | None, str] | None = None
        if owned and base:
            recorded_root = state.anchor_identity(base)
            with fsafe.open_anchor(
                base, expected=None if rebind else _ident(recorded_root)
            ) as ranchor:
                root_info = (base, recorded_root, _text(ranchor.identity))
                rel = os.path.relpath(path, base)
                if rel.startswith("..") or os.path.isabs(rel) or rel == ".":
                    raise _unlinked(f"{path} is not below its root {base}")
                fd = fsafe.open_chain(
                    ranchor,
                    ranchor.fd,
                    rel.split(os.sep),
                    expected=dir_expected,
                    start_path=ranchor.path,
                )
                try:
                    dir_id = _text(fsafe.identity_of(fd))
                    _pointers(ranchor, fd, canchor, path, common, admin)
                finally:
                    os.close(fd)
        else:
            with fsafe.open_anchor(path, expected=dir_expected) as wanchor:
                dir_id = _text(wanchor.identity)
                _pointers(wanchor, wanchor.fd, canchor, path, common, admin)
        return Links(common, _text(canchor.identity), dir_id, root_info)


def _pointers(
    wanchor: fsafe.Anchor,
    wfd: int,
    canchor: fsafe.Anchor,
    path: str,
    common: str,
    admin: str | None,
) -> None:
    if admin is None:
        # a main worktree: <path>/.git is the common dir itself
        dfd = fsafe.open_dir(wanchor, wfd, ".git")
        try:
            same = fsafe.identity_of(dfd) == canchor.identity
        finally:
            os.close(dfd)
        if not same or os.path.join(path, ".git") != common:
            raise _unlinked(f"{path}/.git is not the recorded common dir")
        return
    pointer = _read_small(wanchor, wfd, ".git")
    if _pointer_value(pointer, "gitdir: ") != admin:
        raise _unlinked(f"{path}/.git does not name the recorded admin dir")
    if not admin.startswith(canchor.path + "/worktrees/"):
        raise _unlinked(f"{admin} is not an admin entry of {common}")
    afd = fsafe.open_chain(
        canchor, canchor.fd, admin[len(canchor.path) + 1 :].split("/"), start_path=canchor.path
    )
    try:
        if _pointer_value(_read_small(canchor, afd, "gitdir")) != os.path.join(path, ".git"):
            raise _unlinked(f"{admin}/gitdir does not name {path}/.git")
        cd = _pointer_value(_read_small(canchor, afd, "commondir"))
        if cd is None or os.path.normpath(os.path.join(admin, cd)) != os.path.normpath(common):
            raise _unlinked(f"{admin}/commondir does not name the recorded common dir")
    finally:
        os.close(afd)
