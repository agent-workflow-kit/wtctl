"""``wtctl remove <id|path>`` (docs/interface.md, remove).

``--dry-run`` prints the ``status`` object plus ``dry_run: true`` and exits 0 whatever it found.
A worktree is removed by ``wtctl.remover`` (Remove order, Partial records), a throwaway by
``wtctl.throwaway_remove`` (Throwaway removal).
"""

from __future__ import annotations

from .. import remover
from ..context import Context, Result
from ..errors import UsageError
from ..state import check_holder
from ..throwaway_remove import RemoveOptions, ThrowawayRemover
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
        throwaway = RemoveOptions(opts.hook_flags, opts.override_escalation)
        return ThrowawayRemover(ctx, record["id"], throwaway).run()
    return remover.Remover(ctx, opts, record["id"]).run()
