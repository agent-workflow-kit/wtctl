"""``wtctl baseline check <id|path>`` (docs/interface.md, baseline check).

Integrity first, then the recomputed baseline against the recorded one. Takes no lease and is not
human-only. Any change in either class is the refusal ``repo_config_changed``.
"""

from __future__ import annotations

from typing import Any

from .. import baselinesvc
from ..context import Context, Result
from ..gitsetup import open_git
from ..output import escape_control


def run(ctx: Context) -> Result:
    record = ctx.state.resolve(ctx.args.target)
    baselinesvc.require_worktree(record, "baseline check")
    git = baselinesvc.LazyGit(lambda: open_git(ctx.config, ctx.gateway))
    _links, current = baselinesvc.verify_unchanged(ctx.config, ctx.state, record, git)
    data: dict[str, Any] = {
        "id": record["id"],
        "changed": False,
        "digest": current.digest,
        "recorded_digest": baselinesvc.recorded_digest(record),
    }
    notice = f"{record['name']}: baseline unchanged"
    if not baselinesvc.skips_linked(ctx.state, record["id"]):
        # a format 1 or 2 baseline: unchanged by its own rules; say what accept would newly cover
        data["format"] = ctx.state.baseline_format(record["id"])
        added = baselinesvc.new_coverage(ctx.config, ctx.state, record, current, git)
        if added:
            data["new_coverage"] = added
            notice += (
                f"\n{record['name']}: {len(added)} items would be added or changed by the current "
                "baseline format; run `baseline accept` to record them"
            )
    blind = current.uncovered()
    if blind:
        # a documented, bounded gap, always shown: never part of a refusal
        data["uncovered"] = blind
        notice += (
            f"\n{record['name']}: {len(blind)} symlink targets are on another device and their "
            "content is not covered by the baseline: "
            + ", ".join(escape_control(p) for p in blind[:20])
            + (f" (+{len(blind) - 20} more)" if len(blind) > 20 else "")
        )
    return Result(data, human=record["id"], stderr=notice)
