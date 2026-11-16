"""``wtctl baseline check <id|path>`` (docs/interface.md, baseline check).

Integrity first, then the recomputed baseline against the recorded one. Takes no lease and is not
human-only. Any change in either class is the refusal ``repo_config_changed``.
"""

from __future__ import annotations

from .. import baselinesvc
from ..context import Context, Result


def run(ctx: Context) -> Result:
    record = ctx.state.resolve(ctx.args.target)
    baselinesvc.require_worktree(record, "baseline check")
    _links, current = baselinesvc.verify_unchanged(ctx.config, ctx.state, record)
    data = {
        "id": record["id"],
        "changed": False,
        "digest": current.digest,
        "recorded_digest": baselinesvc.recorded_digest(record),
    }
    return Result(data, human=record["id"], stderr=f"{record['name']}: baseline unchanged")
