"""The deletions of ``remove``: a worktree directory and its admin entry, each by a child
operation of ``fsafe_child`` (fd-based, anchored at a recorded identity, under a deadline).

Nothing here follows a link or crosses a mount point; a path is never handed to ``rm`` or to
``git worktree remove``. Every function reports what it did as data and raises nothing for an
entry that could not be deleted (``remove`` turns that into a ``partial`` record).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import fsafe, fsafe_child, removalfs

# FsafeError kinds that are raised before the operation deleted anything: the recorded location
# is not what it was (or cannot be opened), so the record stays as it was.
NOTHING_DELETED = frozenset(
    {
        "identity",
        "mount_point",
        "symlink",
        "not_dir",
        "name",
        "owner",
        "permission",
        "unavailable",
        "bad_request",
        "missing",
    }
)


@dataclass
class TreeOutcome:
    """``status``: ``deleted`` (also a directory that was already gone), ``partial`` (something
    is left, or the deletion was cut off: ``remaining`` has the paths), or ``refused`` (nothing
    was deleted: ``error`` says why)."""

    status: str
    remaining: list[str] = field(default_factory=list)
    remaining_total: int = 0
    error: fsafe.FsafeError | None = None
    gone: bool = False


def _remaining(base: str, entries: list[dict[str, Any]]) -> list[str]:
    root = base.rstrip("/")
    return [
        fsafe.escape_for_details("/".join([root, *[str(x) for x in e["path"]]])) for e in entries
    ]


def delete_args(state: Any, record: dict[str, Any]) -> dict[str, Any]:
    """The request that deletes the record's directory: the anchor is the recorded root
    (``base_dir``, an owned worktree) or the directory's parent (an adopted one, whose chain from
    ``/`` is not compared), the chain the components between it and the directory's parent."""
    path = record["path"]
    base = record.get("base_dir")
    if base and record.get("owned"):
        identity = state.anchor_identity(base)
        if not identity:
            raise fsafe.FsafeError("identity", f"no identity is recorded for the root {base}")
        rel = os.path.relpath(path, base)
        if rel in (".", "..") or rel.startswith("../") or os.path.isabs(rel):
            raise fsafe.FsafeError("identity", f"{path} is not below its root {base}")
        parts = rel.split(os.sep)
        anchor: dict[str, Any] = {"path": base, "identity": removalfs.identity_list(identity)}
        chain, name = parts[:-1], parts[-1]
    else:
        parent, name = os.path.split(path)
        # an adopted worktree has no recorded root: its parent is opened no-follow from `/` and
        # the directory itself is verified by its recorded dir_id; the parent may be a mount's root
        anchor = {"path": parent, "identity": None, "precheck": False}
        chain = []
    return {
        "anchor": anchor,
        "chain": chain,
        "name": name,
        "root_identity": removalfs.identity_list(record.get("dir_id")),
    }


def delete_worktree(
    state: Any, record: dict[str, Any], budget: fsafe_child.Budget, *, allow_missing: bool = False
) -> TreeOutcome:
    """Delete the worktree directory, its ``.git`` pointer last."""
    path = record["path"]
    try:
        args = {**delete_args(state, record), "last": ".git"}
    except fsafe.FsafeError as exc:
        return TreeOutcome("refused", error=exc)
    res = removalfs.run("delete_tree", args, budget, fsafe_child.REMOVE_DEADLINE)
    if res.timed_out:
        return TreeOutcome("partial", [fsafe.escape_for_details(path)], 1, res.error)
    if not res.ok:
        err = res.error
        if err is not None and err.kind == "missing" and allow_missing:
            return TreeOutcome("deleted", gone=True)
        if err is not None and err.kind in NOTHING_DELETED:
            return TreeOutcome("refused", error=err)
        return TreeOutcome("partial", [fsafe.escape_for_details(path)], 1, err)
    value = res.value or {}
    total = int(value.get("remaining_total", 0))
    if total == 0:
        return TreeOutcome("deleted", gone=int(value.get("removed", 0)) == 0)
    return TreeOutcome("partial", _remaining(path, value.get("remaining", [])), total)


def admin_args(record: dict[str, Any], state: Any) -> dict[str, Any] | None:
    """The request for the record's admin entry (``None`` when it has none)."""
    admin, common = record.get("admin_dir"), record.get("common_dir")
    if not admin or not common:
        return None
    parent = os.path.join(common, "worktrees")
    if os.path.dirname(admin) != parent:
        raise fsafe.FsafeError("identity", f"{admin} is not an admin entry of {common}")
    return {
        "anchor": {"path": common, "identity": removalfs.identity_list(record.get("common_id"))},
        "chain": ["worktrees"],
        "name": os.path.basename(admin),
    }


@dataclass
class AdminState:
    present: bool
    gitdir: str | None = None
    locked: bool = False


def admin_check(
    state: Any, record: dict[str, Any], budget: fsafe_child.Budget
) -> AdminState | fsafe.FsafeError:
    """Open the admin entry from the recorded common dir; an error means nothing may be deleted."""
    try:
        args = admin_args(record, state)
    except fsafe.FsafeError as exc:
        return exc
    if args is None:
        return AdminState(False)
    res = removalfs.run("admin_check", args, budget)
    if not res.ok or res.value is None:
        return res.error or fsafe.FsafeError("deadline", "admin_check did not finish")
    if res.value["state"] == "absent":
        return AdminState(False)
    return AdminState(True, res.value.get("gitdir"), bool(res.value.get("locked")))


def delete_admin(
    state: Any, record: dict[str, Any], budget: fsafe_child.Budget
) -> tuple[str, list[str]]:
    """Delete the admin entry if its ``gitdir`` names ``<path>/.git``. Returns ``(state,
    remaining)``: ``deleted``, ``absent``, ``foreign`` (left alone), ``partial`` or ``failed``."""
    try:
        args = admin_args(record, state)
    except fsafe.FsafeError:
        return "failed", [fsafe.escape_for_details(record["admin_dir"])]
    if args is None:
        return "absent", []
    args["expect_gitdir"] = os.path.join(record["path"], ".git")
    res = removalfs.run("delete_admin", args, budget, fsafe_child.REMOVE_DEADLINE)
    entry = fsafe.escape_for_details(record["admin_dir"])
    if not res.ok or res.value is None:
        return "failed", [entry]
    state_ = str(res.value["state"])
    if state_ == "partial":
        return "partial", _remaining(record["admin_dir"], res.value.get("remaining", []))
    return state_, []
