"""``wtctl baseline accept <id|path> [--expect-digest D]`` (docs/interface.md, baseline accept).

A human re-records the baseline after inspecting the change. The CLI's actor gate (exit 13, before
record resolution) has already run. Under the per-record lease: integrity (a recorded anchor
identity that differs is shown and re-recorded, everything else still refuses), compute and show
the changed items, ask (unless ``--yes``), recompute and refuse if the digest moved while the human
decided, then write the rows, the digest and any re-recorded identities.

TODO(#23): ``clone_missing`` records (a moved clone is re-attached here as Reconcile does).
TODO(#5): audit the ``baseline_accept`` event (items and anchors).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from .. import baselinesvc, hooks, linkcheck, repobaseline
from ..context import Context, Result
from ..errors import UsageError, WtctlError
from ..locate import is_fuse
from ..state import record_lease
from . import clone as clone_cmd
from .create import record_object

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass
class Snapshot:
    links: linkcheck.Links
    anchors: list[dict[str, Any]]
    current: repobaseline.Baseline
    rows: list[dict[str, Any]]


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


def _anchors(
    record: dict[str, Any], links: linkcheck.Links, hooks_item: repobaseline.Item | None,
    hooks_recorded: str | None,
) -> list[dict[str, Any]]:  # fmt: skip
    out = [
        _anchor("common_dir", record["common_dir"], record["common_id"], links.common_id),
        _anchor("worktree_dir", record["path"], record["dir_id"], links.dir_id),
    ]
    if links.root is not None:
        out.append(_anchor("worktree_root", links.root[0], links.root[1], links.root[2]))
    if hooks_item is not None and hooks_item.anchor_id:
        out.append(_anchor("hooks_path", hooks_item.path, hooks_recorded, hooks_item.anchor_id))
    return [a for a in out if a is not None]


def _snapshot(ctx: Context, record: dict[str, Any]) -> Snapshot:
    config, state = ctx.config, ctx.state
    links = linkcheck.verify_links(record, state, rebind=True)
    recorded = baselinesvc.recorded_items(state, record["id"])
    hooks_id = baselinesvc.hooks_identity(recorded)
    spec = baselinesvc.spec_for(
        config,
        record,
        common_id=links.common_id,
        dir_id=links.dir_id,
        hooks_id=hooks_id,
        rebind_hooks=True,
    )
    current = baselinesvc.compute(spec)
    hooks_item = next((i for i in current.items if i.cls == "git" and i.anchor_id), None)
    anchors = _anchors(record, links, hooks_item, hooks_id)
    changed_anchors = [a for a in anchors if a["changed"]]
    if any(is_fuse(a["fs_type"]) for a in changed_anchors):
        raise WtctlError(
            "refused",
            f"an anchor of {record['id']} is on a FUSE file system; it cannot be re-accepted",
            reason="unlinked",
            details={"cause": "fuse_anchor", "anchors": anchors},
        )
    rows = repobaseline.compare(recorded, current.items)
    return Snapshot(links, anchors, current, rows)


def _question(record: dict[str, Any], snap: Snapshot) -> str:
    parts = [
        f"{r['class']} {r['path']}" + (f" {r['key']}" if r["key"] else "") for r in snap.rows[:20]
    ]
    more = f" (+{len(snap.rows) - 20} more)" if len(snap.rows) > 20 else ""
    moved = [a for a in snap.anchors if a["changed"]]
    ids = "; ".join(
        f"{a['anchor']} {a['path']}: recorded {a['recorded']}, now {a['current']} "
        f"({a['fs_type']} {a['mount_source']} on {a['mount_point']})"
        for a in moved
    )
    text = f"{record['name']}: accept the baseline change? " + ", ".join(parts) + more
    return text + (f" Anchors: {ids}." if ids else "")


def _check_state(record: dict[str, Any]) -> None:
    # TODO(#23): a clone_missing record is re-attached here (moved clone); until then it is
    # refused like every other state that is not active.
    baselinesvc.require_worktree(record, "baseline accept")


def run(ctx: Context) -> Result:
    a, config, state = ctx.args, ctx.config, ctx.state
    expect = a.expect_digest
    if expect is not None and not _DIGEST.fullmatch(expect):
        raise UsageError("--expect-digest must be 64 lowercase hex digits")
    record = state.resolve(a.target)
    if ctx.dry_run:
        return _decide(ctx, record, None)
    with state.lease(record_lease(record["id"]), wait=config.lease_wait_s):
        record = state.get_record(record["id"])
        if record is None:
            raise WtctlError("not_found", f"no record for {a.target}")
        _check_state(record)
        return _decide(ctx, record, state)


def _decide(ctx: Context, record: dict[str, Any], write_state: Any) -> Result:
    a, config, state = ctx.args, ctx.config, ctx.state
    _check_state(record)
    snap = _snapshot(ctx, record)
    old = baselinesvc.recorded_digest(record)
    if snap.current.has_detail():
        raise baselinesvc.refusal(record, snap.current, snap.rows)
    moved = any(x["changed"] for x in snap.anchors)
    if snap.current.digest == record["baseline_digest"] and not moved:
        # nothing would be recorded: nothing for --expect-digest to protect either
        return _result(ctx, record, old, snap.current.digest, [], snap.anchors, dry=ctx.dry_run)
    if a.expect_digest is not None and a.expect_digest != snap.current.digest:
        raise baselinesvc.refusal(record, snap.current, snap.rows)
    if ctx.dry_run:
        return _result(ctx, record, old, snap.current.digest, snap.rows, snap.anchors, dry=True)
    if not ctx.yes and not clone_cmd.confirm(_question(record, snap), tty=ctx.runtime.stdin_isatty):
        raise WtctlError(
            "confirmation_required", "accepting a baseline change needs confirmation (--yes)"
        )
    again = _snapshot(ctx, record)
    if again.current.has_detail() or again.current.digest != snap.current.digest:
        raise baselinesvc.refusal(record, again.current, again.rows)
    if again.links.common_id != snap.links.common_id or again.links.dir_id != snap.links.dir_id:
        raise baselinesvc.refusal(record, again.current, again.rows)
    updates: dict[str, Any] = {}
    if again.links.common_id != record["common_id"]:
        updates["common_id"] = again.links.common_id
    if again.links.dir_id != record["dir_id"]:
        updates["dir_id"] = again.links.dir_id
    new = state.transition(
        record["id"],
        "active",
        "active",
        baseline_digest=again.current.digest,
        baseline_items=baselinesvc.item_rows(again.current),
        **updates,
    )
    if again.links.root is not None and again.links.root[1] != again.links.root[2]:
        state.rerecord_anchor(again.links.root[0], again.links.root[2])
    if "common_id" in updates:
        state.put_clone(record["repo"], common_id=again.links.common_id)
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
    return Result(data, human=record["id"], stderr=summary)
