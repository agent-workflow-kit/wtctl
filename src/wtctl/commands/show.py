"""``wtctl show <id|path>``: one record from state.db, running no git."""

from __future__ import annotations

from typing import Any

from ..context import Context, Result
from ..output import escape_control
from ..record_view import record_object


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return escape_control(str(value))


def render_human(obj: dict[str, Any]) -> str:
    """``key: value`` lines. Every value is escaped here, because the dispatcher keeps ``\\n`` as
    a line break and a path, branch or error text may contain one."""
    lines = []
    for key, value in obj.items():
        if key == "bootstrap":
            if value is None:
                continue
            lines.append(
                f"bootstrap: {value['state']} ({len(value['steps'])} steps, "
                f"{value['created_paths_count']} created paths)"
            )
            for i, step in enumerate(value["steps"], 1):
                argv = " ".join(step["argv"])
                code = "killed" if step["exit"] is None else _scalar(step["exit"])
                lines.append(f"  step {i}: {_scalar(argv)} exit={code}")
                if step["exit"] != 0 and step["output_tail"]:
                    lines.append(f"    output: {_scalar(step['output_tail'])}")
        elif value is None:
            continue
        elif isinstance(value, list):
            lines.append(f"{key}: " + (", ".join(_scalar(v) for v in value) or "-"))
        else:
            lines.append(f"{key}: {_scalar(value)}")
    return "\n".join(lines)


def run(ctx: Context) -> Result:
    state = ctx.state
    # A full id also finds a tombstone; prefixes and paths never do (State.resolve).
    record = state.resolve(ctx.args.target, allow_tombstone=True)
    obj = record_object(state, record)
    return Result(data=obj, human=render_human(obj))
