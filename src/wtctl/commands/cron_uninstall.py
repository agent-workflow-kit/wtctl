"""``wtctl cron uninstall`` (docs/interface.md); the logic lives in ``wtctl.cron``."""

from __future__ import annotations

from ..context import Context, Result
from ..cron import uninstall


def run(ctx: Context) -> Result:
    return uninstall(ctx)
