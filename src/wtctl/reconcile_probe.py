"""Read-only probes reconcile makes of reality (docs/interface.md, Reconcile).

Every probe opens its target through no-follow descriptors anchored at a recorded identity. A
probe that cannot establish the answer says so (``unknown``, ``unlinked``): reconcile then changes
nothing about the data, because the data may exist elsewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from . import fsafe, fsafe_child, removal_delete, removalfs
from .errors import WtctlError
from .locate import Resolver


@dataclass
class Moved:
    """A re-resolved clone whose admin entry names the record's worktree."""

    clone_path: str
    common_dir: str
    common_id: str


@dataclass
class DirProbe:
    status: str  # present | absent | unknown
    detail: str = ""


def _ident(text: str | None) -> fsafe.Identity | None:
    if not text:
        return None
    dev, _, ino = text.partition(":")
    return fsafe.Identity(int(dev), int(ino))


def entry_args(state: Any, rec: dict[str, Any]) -> dict[str, Any]:
    """The ``entry_state`` request for the record's own directory: its parent chain is opened
    from the recorded root (worktree root, throwaway root) and only the last component is
    looked up."""
    if rec["object_type"] == "throwaway":
        base, root_id = rec.get("base_dir"), rec.get("root_id")
        if not base or not root_id or rec["path"] != os.path.join(base, rec["id"]):
            raise fsafe.FsafeError("identity", f"{rec['id']} has no usable recorded root")
        return {
            "anchor": {"path": base, "identity": removalfs.identity_list(root_id)},
            "chain": [],
            "name": rec["id"],
        }
    args = removal_delete.delete_args(state, rec)
    args.pop("root_identity", None)
    if not rec.get("owned"):
        # an adopted record has no recorded root: its parent counts as the place only if it is
        # not a mount point (a replaced or unmounted parent would look like a missing directory)
        args["same_device_parent"] = True
    return args


def probe_entry(state: Any, rec: dict[str, Any], budget: fsafe_child.Budget) -> DirProbe:
    """``absent`` only when the last component, or a component of the chain above it, is missing
    below an anchor that opened and matched its recorded identity (an adopted record's parent must
    also not be a mount point); any other failure (a link, an identity mismatch, an unopenable
    root) is ``unknown``."""
    try:
        args = entry_args(state, rec)
    except fsafe.FsafeError as exc:
        return DirProbe("unknown", exc.message)
    res = removalfs.run("entry_state", args, budget)
    if not res.ok or res.value is None:
        why = "deadline" if res.timed_out else (res.error.message if res.error else "failed")
        return DirProbe("unknown", f"cannot look at {rec['path']}: {why}")
    return DirProbe("absent" if res.value.get("state") == "absent" else "present")


def probe_clone(rec: dict[str, Any]) -> str:
    """``present``, ``gone`` (the common dir is not there) or ``unlinked`` (something else is:
    another identity, a link, a mount)."""
    common = rec.get("common_dir")
    if not common:
        return "unlinked"
    try:
        with fsafe.open_anchor(common, expected=_ident(rec.get("common_id"))):
            return "present"
    except fsafe.FsafeError as exc:
        return "gone" if exc.kind in ("missing", "not_dir") else "unlinked"


def moved_candidate(
    ctx_config: Any, state: Any, git: Any, rec: dict[str, Any], budget: fsafe_child.Budget
) -> Moved | None:
    """The clone a re-resolution (no auto-clone) finds for the record's repo when that clone
    names the record's worktree; otherwise None. Nothing is recorded or changed."""
    try:
        res, _ = Resolver(ctx_config, state, git, readonly=True).find(rec["repo"])
    except WtctlError:
        return None
    if res is None or res.common_dir == rec["common_dir"]:
        return None
    if rec["admin_dir"] is None:
        return (
            Moved(res.clone_path, res.common_dir, res.common_id)
            if res.clone_path == rec["path"]
            else None
        )
    args = {
        "anchor": {"path": res.common_dir, "identity": None},
        "chain": ["worktrees"],
        "name": os.path.basename(rec["admin_dir"]),
    }
    got = removalfs.run("admin_check", args, budget)
    if not got.ok or got.value is None or got.value.get("state") != "present":
        return None
    named = got.value.get("gitdir")
    if named != os.path.join(rec["path"], ".git"):
        return None
    return Moved(res.clone_path, res.common_dir, res.common_id)


def repair_args(
    state: Any,
    rec: dict[str, Any],
    *,
    write: bool,
    accept_dotgit: str | None = None,
    allow_adopted: bool = False,
) -> dict[str, Any]:
    """The ``repair_pointers`` request for an owned linked worktree: the worktree directory from
    its recorded root with the recorded ``dir_id``, the admin entry from the recorded common dir
    with the recorded ``common_id``."""
    base = rec.get("base_dir")
    owned_ok = rec.get("owned") and base
    if not (owned_ok or allow_adopted) or not rec.get("admin_dir") or not rec.get("dir_id"):
        raise fsafe.FsafeError("identity", f"{rec['id']} cannot be repaired by wtctl")
    walk = removalfs.walk_args(state, rec)
    admin = rec["admin_dir"]
    if os.path.dirname(admin) != os.path.join(rec["common_dir"], "worktrees"):
        raise fsafe.FsafeError("identity", f"{admin} is not an admin entry of the clone")
    return {
        **walk,
        "common": {
            "path": rec["common_dir"],
            "identity": removalfs.identity_list(rec["common_id"]),
        },
        "admin": os.path.basename(admin),
        "admin_dir": admin,
        "path": rec["path"],
        "write": write,
        "accept_dotgit": accept_dotgit,
    }


def repair_pointers(
    state: Any,
    rec: dict[str, Any],
    budget: fsafe_child.Budget,
    *,
    write: bool,
    accept_dotgit: str | None = None,
    allow_adopted: bool = False,
) -> str | None:
    """Rewrite the pointers; None on success, else why nothing (more) was written.
    ``accept_dotgit`` is a previous admin directory a ``.git`` file may still name (a clone that
    moved)."""
    try:
        args = repair_args(
            state, rec, write=write, accept_dotgit=accept_dotgit, allow_adopted=allow_adopted
        )
    except fsafe.FsafeError as exc:
        return exc.message
    res = removalfs.run("repair_pointers", args, budget)
    if res.ok:
        return None
    if res.timed_out:
        return "the pointer rewrite did not finish in time"
    return res.error.message if res.error else "the pointer rewrite failed"
