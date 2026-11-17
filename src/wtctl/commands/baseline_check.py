"""``wtctl baseline check <id|path>`` (docs/interface.md, baseline check).

Integrity first, then the recomputed baseline against the recorded one. Takes no lease and is not
human-only. Any change in either class is the refusal ``repo_config_changed``.
"""

from __future__ import annotations

from typing import Any

from .. import baselinesvc
from ..context import Context, Result
from ..output import escape_control
from ..state import LEGACY_BASELINE_FORMAT


def run(ctx: Context) -> Result:
    record = ctx.state.resolve(ctx.args.target)
    baselinesvc.require_worktree(record, "baseline check")
    _links, current = baselinesvc.verify_unchanged(ctx.config, ctx.state, record)
    data: dict[str, Any] = {
        "id": record["id"],
        "changed": False,
        "digest": current.digest,
        "recorded_digest": baselinesvc.recorded_digest(record),
    }
    notice = f"{record['name']}: baseline unchanged"
    if not baselinesvc.follows_links(ctx.state, record["id"]):
        # a format 1 baseline: unchanged by its own rules; say what accept would newly cover
        data["format"] = LEGACY_BASELINE_FORMAT
        added = baselinesvc.new_coverage(ctx.config, ctx.state, record, current)
        if added:
            data["new_coverage"] = added
            notice += (
                f"\n{record['name']}: {len(added)} symlink-target items are not covered by this "
                "baseline; run `baseline accept` to record them"
            )
    blind = current.uncovered()
    if blind:
        # a documented, bounded gap, always shown: never part of a refusal
        data["uncovered"] = blind
        notice += (
            f"\n{record['name']}: {len(blind)} symlink targets are on another device and their "
            "content is not covered by the baseline: " + ", ".join(escape_control(p) for p in blind)
        )
    return Result(data, human=record["id"], stderr=notice)
