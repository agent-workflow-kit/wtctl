"""``wtctl remove <id|path>`` (docs/interface.md, remove).

Only ``--dry-run`` is implemented here: it prints the ``status`` object plus ``dry_run: true`` and
exits 0 whatever it found. The removal itself (lease, mirror refresh, confirmation, hooks,
re-check and fd-based deletion, branch, record) is TODO(#20).
"""

from __future__ import annotations

from ..context import Context, Result
from ..errors import WtctlError
from . import status


def run(ctx: Context) -> Result:
    if not ctx.dry_run:
        # TODO(#20): Remove order steps 1 to 11.
        raise WtctlError("internal", "`wtctl remove` is not implemented yet (see #20)")
    data = {**status.collect(ctx), "dry_run": True}
    return Result(data, human=status.human(data))
