"""``wtctl baseline accept <id|path> [--expect-digest D]`` (docs/interface.md, baseline accept).

A human re-records the baseline after inspecting the change. The CLI's actor gate (exit 13, before
record resolution) has already run. Under the per-record lease: integrity (a recorded anchor
identity that differs is shown and re-recorded, everything else still refuses), compute and show
the changed items, ask (unless ``--yes``), recompute and refuse if the digest moved while the human
decided, then write the rows, the digest and any re-recorded identities.

A ``clone_missing`` record whose re-resolved clone names its worktree is re-attached here (a moved
clone, ``_reattach``): the record and the clone's row follow the clone and the two pointers are
rewritten as Reconcile does it.
TODO(#5): audit the ``baseline_accept`` event (items and anchors).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from .. import (
    baselinesvc,
    fsafe_child,
    hooks,
    linkcheck,
    reconcile_probe,
    removal_integrity,
    repobaseline,
)
from ..context import Context, Result
from ..errors import UsageError, WtctlError
from ..gitsetup import open_git
from ..locate import is_fuse
from ..output import escape_control
from ..state import clone_lease, record_lease
from . import clone as clone_cmd
from .create import record_object

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass
class Snapshot:
    links: linkcheck.Links
    anchors: list[dict[str, Any]]
    current: repobaseline.Baseline
    rows: list[dict[str, Any]]
    clone_stale: bool = False  # the clone's row holds an identity other than the current one

    def moved(self) -> list[dict[str, Any]]:
        """The anchors to show with a refusal: the changed ones, or (only a stale clone row)
        the common dir."""
        out = [a for a in self.anchors if a["changed"]]
        if not out and self.clone_stale:
            out = [a for a in self.anchors if a["anchor"] == "common_dir"]
        return out


def _anchor(name: str, path: str, recorded: str | None, current: str) -> dict[str, Any] | None:
    if recorded is None:
        return None
    return {
        "anchor": name,
        "path": path,
        "recorded": recorded,
        "current": current,
        "changed": recorded != current,
        **baselinesvc.describe(path),
    }


def _known_anchors(record: dict[str, Any], links: linkcheck.Links) -> list[dict[str, Any]]:
    out = [
        _anchor("common_dir", record["common_dir"], record["common_id"], links.common_id),
        _anchor("worktree_dir", record["path"], record["dir_id"], links.dir_id),
    ]
    if links.root is not None:
        out.append(_anchor("worktree_root", links.root[0], links.root[1], links.root[2]))
    return [a for a in out if a is not None]


def _refuse_fuse(record: dict[str, Any], anchors: list[dict[str, Any]]) -> None:
    if any(is_fuse(a["fs_type"]) for a in anchors if a["changed"]):
        raise WtctlError(
            "refused",
            f"an anchor of {record['id']} is on a FUSE file system; it cannot be re-accepted",
            reason="unlinked",
            details={"cause": "fuse_anchor", "anchors": anchors},
        )


def _snapshot(ctx: Context, record: dict[str, Any]) -> Snapshot:
    config, state = ctx.config, ctx.state
    links = linkcheck.verify_links(record, state, rebind=True)
    known = _known_anchors(record, links)
    _refuse_fuse(record, known)  # before the (possibly long) computation
    recorded = baselinesvc.recorded_items(state, record["id"])
    hooks_id = baselinesvc.hooks_identity(recorded)
    hooks_path = baselinesvc.hooks_recorded_path(recorded)
    spec = baselinesvc.spec_for(
        config,
        record,
        common_id=links.common_id,
        dir_id=links.dir_id,
        hooks_id=hooks_id,
        hooks_path=hooks_path,
        rebind_hooks=True,
        git=open_git(config, ctx.gateway),
    )
    current = baselinesvc.compute(spec)
    anchors = known
    hooks_item = next((i for i in current.items if i.cls == "git" and i.anchor_id), None)
    # an identity is compared only for the path it was recorded for: another path is a new anchor
    if hooks_item is not None and hooks_path == hooks_item.path:
        extra = _anchor("hooks_path", hooks_item.path, hooks_id, hooks_item.anchor_id or "")
        if extra is not None:
            anchors = [*known, extra]
    _refuse_fuse(record, anchors)
    clone = state.get_clone(record["repo"])
    stale = (
        clone is not None
        and clone["common_dir"] == record["common_dir"]
        and clone["common_id"] != links.common_id
    )
    rows = repobaseline.compare(recorded, current.items)
    return Snapshot(links, anchors, current, rows, stale)


def _question(record: dict[str, Any], snap: Snapshot) -> str:
    """The confirmation prompt. Item paths, keys and mount facts come from the repository or the
    file system, so every piece is escaped: this prompt is the human gate."""
    esc = escape_control
    parts = [
        esc(f"{r['class']} {r['path']}" + (f" {r['key']}" if r["key"] else ""))
        for r in snap.rows[:20]
    ]
    more = f" (+{len(snap.rows) - 20} more)" if len(snap.rows) > 20 else ""
    moved = [a for a in snap.anchors if a["changed"]]
    ids = "; ".join(
        esc(
            f"{a['anchor']} {a['path']}: recorded {a['recorded']}, now {a['current']} "
            f"({a['fs_type']} {a['mount_source']} on {a['mount_point']})"
        )
        for a in moved
    )
    text = f"{esc(record['name'])}: accept the baseline change? " + ", ".join(parts) + more
    blind = [
        r["path"]
        for r in snap.rows
        if isinstance(r["after"], dict) and r["after"]["type"] == repobaseline.UNCOVERED_TYPE
    ]
    if blind:
        text += esc(
            " Symlink targets on another device are recorded by path only, their content is not "
            "covered: "
            + ", ".join(blind[:20])
            + (f" (+{len(blind) - 20} more)" if len(blind) > 20 else "")
        )
    if snap.clone_stale and not moved:
        text += " The clone's recorded identity is out of date."
    return text + (f" Anchors: {ids}." if ids else "")


def _check_state(record: dict[str, Any]) -> None:
    if record["state"] == "clone_missing" and record["object_type"] == "worktree":
        return
    baselinesvc.require_worktree(record, "baseline accept")


def _refused(message: str) -> WtctlError:
    return WtctlError("refused", message, reason="unlinked")


def _reattach(ctx: Context, record: dict[str, Any], *, dry: bool) -> Result:
    """A moved clone: re-resolve it (no auto-clone), apply the same test as Reconcile, and on
    acceptance rewrite the pointers under the per-clone lease, re-run integrity and record the
    new clone, identities and baseline in one transaction."""
    config, state = ctx.config, ctx.state
    git = open_git(config, ctx.gateway)
    budget = fsafe_child.Budget()
    moved = reconcile_probe.moved_candidate(config, state, git, record, budget)
    if moved is None:
        raise _refused(
            f"no re-resolved clone names {record['path']}; {record['id']} stays clone_missing"
        )
    old_admin = record["admin_dir"]
    new = {
        **record,
        "clone_path": moved.clone_path,
        "common_dir": moved.common_dir,
        "common_id": moved.common_id,
        "admin_dir": None
        if old_admin is None
        else os.path.join(moved.common_dir, "worktrees", os.path.basename(old_admin)),
    }
    anchor = _anchor("common_dir", moved.common_dir, record["common_id"], moved.common_id)
    note = f"{record['clone_path']} -> {moved.clone_path}"
    if dry:
        out = {**new, "state": "active"}
        digest = record["baseline_digest"] or "0" * 64
        anchors = [anchor] if anchor else []
        old = baselinesvc.recorded_digest(record)
        return _result(ctx, out, old, digest, [], anchors, dry=True, note=note)
    # the baseline of the worktree against the candidate clone, before anything is asked or
    # written: a baseline that cannot be recorded refuses with the pointers untouched
    recorded = baselinesvc.recorded_items(state, record["id"])
    preview = baselinesvc.compute(
        baselinesvc.spec_for(
            config,
            new,
            common_id=moved.common_id,
            dir_id=record["dir_id"],
            hooks_id=baselinesvc.hooks_identity(recorded),
            hooks_path=baselinesvc.hooks_recorded_path(recorded),
            rebind_hooks=True,
            git=git,
        )  # fmt: skip
    )
    if preview.has_detail():
        raise baselinesvc.refusal(record, preview, repobaseline.compare(recorded, preview.items))
    question = (
        f"{escape_control(record['name'] or record['id'])}: re-attach to the moved clone "
        f"{escape_control(moved.clone_path)} (was {escape_control(record['clone_path'] or '')})?"
    )
    if not ctx.yes and not clone_cmd.confirm(question, tty=ctx.runtime.stdin_isatty):
        raise WtctlError(
            "confirmation_required", "re-attaching a moved clone needs confirmation (--yes)"
        )
    with state.lease(clone_lease(moved.clone_path), wait=config.lease_wait_s):
        if old_admin is not None:
            why = reconcile_probe.repair_pointers(
                state, new, budget, write=True, accept_dotgit=old_admin, allow_adopted=True
            )
            if why is not None:
                raise _refused(why)
        snap = _snapshot(ctx, new)
        check, _ = removal_integrity.integrity_check(state, new, git)
        if check["status"] != "pass":
            raise _refused(f"{record['path']} does not match the moved clone after the rewrite")
        if snap.current.has_detail():
            raise baselinesvc.refusal(record, snap.current, snap.rows)
        updates: dict[str, Any] = {
            "clone_path": moved.clone_path,
            "common_dir": moved.common_dir,
            "common_id": snap.links.common_id,
            "admin_dir": new["admin_dir"],
            "dir_id": snap.links.dir_id,
            "move_clone": (
                record["repo"], record["common_dir"], moved.clone_path, moved.common_dir,
                snap.links.common_id,
            ),
        }  # fmt: skip
        root = snap.links.root
        if root is not None and root[1] != root[2]:
            updates["rerecord_anchor"] = (root[0], root[2])
        done = state.transition(
            record["id"], "clone_missing", "active",
            baseline_digest=snap.current.digest,
            baseline_items=baselinesvc.item_rows(snap.current), **updates,
        )  # fmt: skip
    result = _result(
        ctx, done, baselinesvc.recorded_digest(record), snap.current.digest, snap.rows,
        snap.anchors, dry=False, note=note,
    )  # fmt: skip
    _post_hook(ctx, config, done, snap.rows, result)
    return result


def run(ctx: Context) -> Result:
    a, config, state = ctx.args, ctx.config, ctx.state
    expect = a.expect_digest
    if expect is not None and not _DIGEST.fullmatch(expect):
        raise UsageError("--expect-digest must be 64 lowercase hex digits")
    record = state.resolve(a.target)
    if ctx.dry_run:
        _check_state(record)
        if record["state"] == "clone_missing":
            return _reattach(ctx, record, dry=True)
        return _decide(ctx, record, None)
    with state.lease(record_lease(record["id"]), wait=config.lease_wait_s):
        record = state.get_record(record["id"])
        if record is None:
            raise WtctlError("not_found", f"no record for {a.target}")
        _check_state(record)
        if record["state"] == "clone_missing":
            return _reattach(ctx, record, dry=False)
        return _decide(ctx, record, state)


def _decide(ctx: Context, record: dict[str, Any], write_state: Any) -> Result:
    a, config, state = ctx.args, ctx.config, ctx.state
    _check_state(record)
    snap = _snapshot(ctx, record)
    old = baselinesvc.recorded_digest(record)
    if snap.current.has_detail():
        raise baselinesvc.refusal(record, snap.current, snap.rows)
    moved = any(x["changed"] for x in snap.anchors) or snap.clone_stale
    if snap.current.digest == record["baseline_digest"] and not moved:
        # nothing would be recorded: nothing for --expect-digest to protect either
        return _result(ctx, record, old, snap.current.digest, [], snap.anchors, dry=ctx.dry_run)
    if a.expect_digest is not None and a.expect_digest != snap.current.digest:
        raise baselinesvc.refusal(record, snap.current, snap.rows, anchors=snap.moved())
    if ctx.dry_run:
        return _result(ctx, record, old, snap.current.digest, snap.rows, snap.anchors, dry=True)
    if not ctx.yes and not clone_cmd.confirm(_question(record, snap), tty=ctx.runtime.stdin_isatty):
        raise WtctlError(
            "confirmation_required", "accepting a baseline change needs confirmation (--yes)"
        )
    again = _snapshot(ctx, record)
    if (
        again.current.has_detail()
        or again.current.digest != snap.current.digest
        or again.links.common_id != snap.links.common_id
        or again.links.dir_id != snap.links.dir_id
    ):
        # it moved while the human decided (possibly back to the recorded state: then there are
        # no rows to show and the refusal is `busy`)
        raise baselinesvc.refusal(
            record, again.current, again.rows, anchors=again.moved(), confirming=True
        )
    updates: dict[str, Any] = {}
    if again.links.common_id != record["common_id"]:
        updates["common_id"] = again.links.common_id
    if again.links.dir_id != record["dir_id"]:
        updates["dir_id"] = again.links.dir_id
    root = again.links.root
    if root is not None and root[1] != root[2]:
        updates["rerecord_anchor"] = (root[0], root[2])
    if "common_id" in updates or again.clone_stale:
        # only a clones row of this very clone (same common dir) is touched
        updates["rebind_clone"] = (record["repo"], record["common_dir"], again.links.common_id)
    # one transaction: the record with its baseline rows, the anchor and the clone's identity
    new = state.transition(
        record["id"],
        "active",
        "active",
        baseline_digest=again.current.digest,
        baseline_items=baselinesvc.item_rows(again.current),
        **updates,
    )
    # TODO(#5): audit `baseline_accept` (the changed items and the anchors).
    result = _result(ctx, new, old, again.current.digest, snap.rows, again.anchors, dry=False)
    _post_hook(ctx, config, new, snap.rows, result)
    return result


def _post_hook(
    ctx: Context, config: Any, record: dict[str, Any], rows: list[dict[str, Any]], result: Result
) -> None:
    payload = hooks.build_payload(
        "post-baseline-accept",
        object_type="worktree",
        object_id=record["id"],
        repo=record["repo"],
        labels=record["labels"],
        record=result.data["record"],
        items=rows,
        actor=ctx.actor.value,
    )
    env = hooks.hook_env(
        config, os.environ, hook="post-baseline-accept", object_id=record["id"],
        object_type="worktree",
    )  # fmt: skip
    specs = hooks.configured_hooks(config, "post-baseline-accept")
    outcome = hooks.run_post_hooks(
        "post-baseline-accept", specs, payload, env=env, decision_ids=list(ctx.decision_ids)
    )
    if outcome.warnings:
        result.stderr = (
            "\n".join([result.stderr, *outcome.warnings])
            if result.stderr
            else ("\n".join(outcome.warnings))
        )


def _result(
    ctx: Context,
    record: dict[str, Any],
    old: str,
    new: str,
    rows: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    *,
    dry: bool,
    note: str | None = None,
) -> Result:
    out = record_object(record, ctx.state.bootstrap_summary(record["id"]))
    if dry and out["baseline_digest"] != new:
        out["baseline_digest"] = new
    data = {
        "record": out,
        "old_digest": old,
        "new_digest": new,
        "items": rows,
        "anchors": anchors,
        "dry_run": dry,
    }
    verb = "would accept" if dry else "accepted"
    summary = f"{verb} the baseline of {record['name']} ({len(rows)} changed items)"
    if note:
        summary += f"; moved clone {note}"
    return Result(data, human=record["id"], stderr=summary)
