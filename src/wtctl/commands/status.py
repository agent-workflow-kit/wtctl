"""``wtctl status <id|path> [--merged-head SHA] [--holder H]`` (docs/interface.md, status).

Reports what ``remove`` would do. Takes no per-record lease, asks nothing and fires no hooks; its
only change is the mirror refresh. ``remove --dry-run`` prints the same object plus
``dry_run: true`` (``commands/remove.py``).
"""

from __future__ import annotations

import re
from typing import Any

from .. import removal_status
from ..context import Context, Result
from ..errors import UsageError
from ..output import escape_control

SHA = re.compile(r"[0-9a-f]{40}")


def merged_head_arg(value: str | None) -> str | None:
    if value is not None and not SHA.fullmatch(value):
        raise UsageError("--merged-head must be a full 40-hex lowercase commit sha")
    return value


def collect(ctx: Context) -> dict[str, Any]:
    a = ctx.args
    merged = merged_head_arg(a.merged_head)
    record = ctx.state.resolve(a.target)
    if record["object_type"] == "throwaway" and (merged or a.holder):
        raise UsageError("--merged-head and --holder are for worktrees")
    return removal_status.build(ctx, record, merged_head=merged, holder=a.holder)


def human(data: dict[str, Any]) -> str:
    would = data["would_remove"]
    verdict = {True: "would be removed", False: "would NOT be removed", None: "unknown"}[would]
    lines = [f"{data['id']}: {verdict}" + (f" ({data['reason']})" if data["reason"] else "")]
    for name, c in data["checks"].items():
        if c["status"] not in ("pass",):
            lines.append(f"  {name}: {c['status']} ({len(c['details'])})")
    if data["human_required"]:
        lines.append("  removal needs a human actor")
    lines += [f"  warning: {w}" for w in data["warnings"]]
    return "\n".join(escape_control(x) for x in lines)


def run(ctx: Context) -> Result:
    data = collect(ctx)
    return Result(data, human=human(data))
