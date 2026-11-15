"""``wtctl locate <repo-id>``: resolve a repo id to its local clone (docs/interface.md, locate)."""

from __future__ import annotations

from ..context import Context, Result
from ..locate import resolver_for


def run(ctx: Context) -> Result:
    res = resolver_for(ctx).locate(ctx.args.repo)
    return Result(res.to_json(), human=res.clone_path, stderr="\n".join(res.stderr_warnings()))
