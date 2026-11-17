"""Undo a worktree that wtctl itself just made and could not finish (docs/interface.md, Create
order step 15, Reconcile): the directory is deleted fd-based and anchored to its recorded
identity, then its admin entry, then the branch only if a compare-and-swap on the tip this create
made still holds. Nothing is touched when a guard refuses; a deletion that stops part-way reports
what is left and the caller keeps the record ``partial`` (the existing finishing path).

The guards are the removal ones (``removal_delete``, ``inuse``, ``linkcheck``): this module
adds no new way to delete a directory.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from . import fsafe, fsafe_child, inuse, linkcheck, removal_delete, worktrees
from .context import Context
from .errors import WtctlError
from .hardgit import Git
from .state import State, clone_lease

UNWOUND = "unwound"
REFUSED = "refused"  # a guard said no: nothing was deleted
PARTIAL = "partial"  # a deletion stopped part-way: ``remaining`` is what is left


@dataclasses.dataclass
class Outcome:
    status: str
    reason: str = ""
    remaining: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    branch_deleted: bool = False
    tree_deleted: bool = False  # the worktree directory is gone (an admin entry may be left)

    @property
    def done(self) -> bool:
        return self.status == UNWOUND


def positions(baseline_items: list[Any], limit: int = 5) -> list[dict[str, str]]:
    """The baseline items that could not be computed, as ``{path, detail}`` (at most ``limit``)."""
    return [
        {"path": fsafe.escape_for_details(i.path), "detail": str(i.detail)}
        for i in baseline_items
        if i.detail
    ][:limit]


def remedy(items: list[dict[str, str]]) -> str:
    """What to do about positions that blocked the baseline."""
    if any(i["detail"] == "mount_point" for i in items):
        return (
            "make the listed paths live on the clone's file system (a symlink to another device "
            "or mount is refused at these positions), or remove the symlink, then run it again"
        )
    return "raise remove.nested_scan_limit or shrink the listed trees, then run it again"


def incomplete_message(prefix: str, items: list[dict[str, str]], *, total: int) -> str:
    shown = ", ".join(f"{i['path']} ({i['detail']})" for i in items)
    more = f" and {total - len(items)} more" if total > len(items) else ""
    return f"{prefix}: {shown}{more}; {remedy(items)}"


def unwind_worktree(
    ctx: Context,
    git: Git,
    rec: dict[str, Any],
    *,
    lease_wait: float,
    branch_sha: str | None,
    written: dict[str, worktrees.UpstreamKey],
) -> Outcome:
    """Delete the worktree of ``rec`` (``admin_dir`` and ``dir_id`` set), its admin entry and, when
    ``branch_sha`` is given (the sha the branch was created at: only a branch this create made),
    the branch by compare-and-swap. The caller holds the record lease and made the worktree in this
    very command: nobody else has had its path (``reconcile`` never calls this, it deletes no
    file)."""
    started = [False]  # set once a deletion may have begun
    try:
        return _unwind(ctx, git, rec, lease_wait, branch_sha, written, started)
    except WtctlError as exc:
        if exc.code == "busy" and not started[0]:
            return Outcome(REFUSED, "the clone is busy")
        why = exc.message
    except Exception as exc:  # noqa: BLE001 - any failure must reach the ``partial`` fallback
        why = f"unexpected {type(exc).__name__}: {exc}"
    if started[0]:
        return Outcome(PARTIAL, f"the deletion stopped ({why})", remaining=[rec["path"]])
    return Outcome(REFUSED, f"the unwind could not start ({why})")


def _unwind(
    ctx: Context,
    git: Git,
    rec: dict[str, Any],
    lease_wait: float,
    branch_sha: str | None,
    written: dict[str, worktrees.UpstreamKey],
    started: list[bool],
) -> Outcome:
    state: State = ctx.state
    try:
        linkcheck.verify_links(rec, state)
    except WtctlError as exc:
        return Outcome(REFUSED, f"not the recorded worktree: {exc.message}")
    budget = fsafe_child.Budget()
    ids, _ = inuse.collect_dir_ids(
        state, rec, limit=ctx.config.remove.nested_scan_limit, budget=budget
    )
    use = inuse.check_in_use(ctx.gateway, rec["path"], ids)
    if use.status != "pass":
        return Outcome(REFUSED, "in use, or the process scanner is unavailable")
    del_budget = fsafe_child.Budget()
    with state.lease(clone_lease(rec["clone_path"]), wait=lease_wait):
        admin = removal_delete.admin_check(state, rec, del_budget)
        if isinstance(admin, fsafe.FsafeError) or not admin.present:
            return Outcome(REFUSED, "the admin entry cannot be opened")
        if admin.gitdir != os.path.join(rec["path"], ".git"):
            return Outcome(REFUSED, "the admin entry names another path")
        if admin.locked:
            return Outcome(REFUSED, "the worktree is locked")
        started[0] = True
        tree = removal_delete.delete_worktree(state, rec, del_budget)
        if tree.status == "refused":
            started[0] = False
            why = tree.error.kind if tree.error else "unknown"
            return Outcome(REFUSED, f"the directory is not the recorded one ({why})")
        if tree.status == "partial":
            return Outcome(PARTIAL, "the deletion stopped part-way", remaining=tree.remaining)
        entry, left = removal_delete.delete_admin(state, rec, del_budget)
        out = Outcome(UNWOUND, tree_deleted=True)
        if entry in ("partial", "failed"):
            return Outcome(
                PARTIAL, "the admin entry could not be deleted", remaining=left, tree_deleted=True
            )
        if entry in ("foreign", "changed"):
            out.notes.append(f"the admin entry {rec['admin_dir']} was left ({entry})")
        _branch(git, rec, branch_sha, written, out)
    return out


def _branch(
    git: Git,
    rec: dict[str, Any],
    sha: str | None,
    written: dict[str, worktrees.UpstreamKey],
    out: Outcome,
) -> None:
    """Only a branch this create made, only at the sha it was made at; the unwind never fails
    because of it (a branch left is a note)."""
    branch, common = rec["branch"], rec["common_dir"]
    if not rec.get("created_branch") or not branch or sha is None:
        return
    try:
        if worktrees.delete_created_branch(git, common, branch, sha):
            out.branch_deleted = True
        else:
            out.notes.append(f"branch {branch} was left (it moved, or could not be deleted)")
            return
        out.notes += worktrees.remove_upstream(git, common, branch, written)
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
        out.notes.append(f"branch {branch} was left ({exc})")
