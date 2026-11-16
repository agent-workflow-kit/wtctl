"""``wtctl reconcile [--dry-run]`` (docs/interface.md, reconcile): the handler of
``wtctl.reconciler``, which holds the rules."""

from __future__ import annotations

from ..context import Context, Result
from ..reconciler import Reconciler


def run(ctx: Context) -> Result:
    return Reconciler(ctx).run()
