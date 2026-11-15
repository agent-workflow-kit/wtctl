"""``wtctl cron install`` (docs/interface.md); the logic lives in ``wtctl.cron``."""

from __future__ import annotations

from ..context import Context, Result
from ..cron import install


def run(ctx: Context) -> Result:
    return install(ctx)
