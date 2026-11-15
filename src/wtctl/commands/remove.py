"""``wtctl remove <id|path>`` (docs/interface.md, remove).

``--dry-run`` prints the ``status`` object plus ``dry_run: true`` and exits 0 whatever it found.
A worktree is removed by ``wtctl.remover`` (Remove order, Partial records).

TODO(#21): removing a throwaway (Throwaway removal) is its own issue.
"""

from __future__ import annotations

from .. import remover
from ..context import Context, Result
from ..errors import UsageError, WtctlError
from ..state import check_holder
from . import status


def run(ctx: Context) -> Result:
    if ctx.dry_run:
        data = {**status.collect(ctx), "dry_run": True}
        return Result(data, human=status.human(data))
    a = ctx.args
    status.merged_head_arg(a.merged_head)
    if a.holder is not None:
        check_holder(a.holder)
    opts = remover.parse_options(ctx)
    record = ctx.state.resolve(a.target)
    if record["object_type"] == "throwaway":
        if a.merged_head or a.holder or a.force or a.keep_branch:
            raise UsageError("--merged-head, --holder, --force and --keep-branch are for worktrees")
        raise WtctlError("internal", "removing a throwaway is not implemented yet (see #21)")
    return remover.Remover(ctx, opts, record["id"]).run()
