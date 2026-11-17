"""Verified linked worktrees beneath the walked tree (docs/interface.md, Baseline).

The ``claude`` walk of a worktree would otherwise read the files of every linked worktree of the
same clone that sits beneath it (``<main>/.claude/worktrees/x``): an edit there would flip the
main record's baseline. A directory is skipped only when it is *verified* to be such a worktree,
and the decision is recorded as an item so that a planted directory can never hide a file:

- git lists it for the clone (``git worktree list``, run by the parent and handed in as
  ``Spec.listed``; a locked or prunable entry is not handed in),
- it is a real directory (no-follow) whose ``.git`` is a regular file saying
  ``gitdir: <common dir>/worktrees/<name>``,
- that admin entry exists, its ``gitdir`` names this directory's ``.git`` and its ``commondir``
  names the clone (the ``linkcheck`` verification),
- all of it read through no-follow descriptors anchored at the walk's anchor, and the directory's
  identity is the one the walk opened; the whole verification is repeated after the walk and a
  directory that differs the second time is an ``incomplete`` item, never a skip.

Everything else (a forged pointer, a ``.git`` directory or link, an unlisted directory) is walked
exactly as without this module. The item ``linked-worktree`` hashes the verified identity (the
directory's path and the admin entry's name), never any content.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass

from . import fsafe, linkcheck
from .errors import WtctlError

LINKED_KEY = "linked-worktree"  # never a config key: those always contain a dot
LINKED_TYPE = "linked_worktree"
_LINKED_VERSION = b"wtctl-linked-v1\n"


def is_admin_name(name: str) -> bool:
    """One plain path component: an admin entry's name must not be able to name a nested path
    (``worktrees/x/y``, which the pointer verification would open) or forge a line."""
    return name not in ("", ".", "..") and not any(c in name for c in "/\0\n\r")


@dataclass(frozen=True)
class Verified:
    rel: str  # the directory below the anchor
    identity: fsafe.Identity
    admin: str  # the admin entry's name


def identity_hash(rel: str, admin: str) -> str:
    h = hashlib.sha256(_LINKED_VERSION)
    for part in (rel, admin):
        b = part.encode("utf-8", "surrogateescape")
        h.update(b"%d:" % len(b) + b + b"\n")
    return h.hexdigest()


class LinkedWorktrees:
    """The verification of the directories the walk meets, done once per directory git listed."""

    def __init__(
        self,
        anchor: fsafe.Anchor,
        common_dir: str,
        common_id: str,
        listed: list[str],
        protected: Callable[[str], bool],
    ) -> None:
        self.anchor = anchor
        self.common_dir = common_dir
        self._common_id = common_id
        self.listed = frozenset(listed)
        self.protected = protected  # names that are never skipped (.claude, CLAUDE*.md, ...)
        self.verified: dict[str, Verified] = {}  # by absolute path
        self._refused: set[str] = set()
        self._canchor: fsafe.Anchor | None = None
        self._canchor_failed = False

    def close(self) -> None:
        if self._canchor is not None:
            self._canchor.close()
            self._canchor = None

    def _common(self) -> fsafe.Anchor | None:
        if self._canchor is None and not self._canchor_failed:
            dev, _, ino = self._common_id.partition(":")
            try:
                self._canchor = fsafe.open_anchor(
                    self.common_dir, expected=fsafe.Identity(int(dev), int(ino))
                )
            except (fsafe.FsafeError, ValueError):
                self._canchor_failed = True
        return self._canchor

    def check(self, root_path: str, e: fsafe.Entry) -> bool:
        """Whether the directory entry ``e`` of a walk rooted at ``root_path`` (a directory at or
        below the anchor) is a verified linked worktree that the walk must not enter."""
        if e.kind != "dir" or not e.path:
            return False
        absolute = root_path.rstrip("/") + "/" + "/".join(e.path)
        if absolute not in self.listed:
            return False  # not listed by git: the common case, no file system access at all
        if absolute in self.verified:
            return True
        if absolute in self._refused:
            return False
        got = None
        if not self.protected(e.path[-1]) and absolute.startswith(self.anchor.path + "/"):
            parts = tuple(absolute[len(self.anchor.path) + 1 :].split("/"))
            got = self._verify(parts)
            if got is not None and got.identity != fsafe.Identity(e.dev, e.ino):
                got = None  # not the directory the walk opened
        if got is None:
            self._refused.add(absolute)
            return False
        self.verified[absolute] = got
        return True

    def _verify(self, parts: tuple[str, ...]) -> Verified | None:
        anchor = self.anchor
        rel = "/".join(parts)
        absolute = anchor.path + "/" + rel
        canchor = self._common()
        if canchor is None:
            return None
        try:
            dfd = fsafe.open_chain(anchor, anchor.fd, list(parts), start_path=anchor.path)
        except fsafe.FsafeError:
            return None
        try:
            identity = fsafe.identity_of(dfd)
            named = linkcheck._pointer_value(  # pyright: ignore[reportPrivateUsage]
                linkcheck._read_small(anchor, dfd, ".git"),  # pyright: ignore[reportPrivateUsage]
                "gitdir: ",
            )
            prefix = self.common_dir.rstrip("/") + "/worktrees/"
            if named is None or not named.startswith(prefix):
                return None
            name = named[len(prefix) :]
            if not is_admin_name(name):
                return None
            linkcheck._pointers(  # pyright: ignore[reportPrivateUsage]
                anchor, dfd, canchor, absolute, self.common_dir, named
            )
            return Verified(rel, identity, name)
        except (fsafe.FsafeError, WtctlError, OSError):
            return None
        finally:
            os.close(dfd)

    def reverify(self) -> tuple[list[Verified], list[str]]:
        """After the walk: the directories that verify the same way again, and the paths of those
        that do not (replaced while the walk ran)."""
        good: list[Verified] = []
        bad: list[str] = []
        for v in sorted(self.verified.values(), key=lambda v: v.rel):
            again = self._verify(tuple(v.rel.split("/")))
            if again == v:
                good.append(v)
            else:
                bad.append(v.rel)
        return good, bad
