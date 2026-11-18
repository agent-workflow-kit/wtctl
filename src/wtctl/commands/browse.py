"""``wtctl browse``: a read-only full-screen view over ``list``, ``show`` and ``status``.

Records come from ``list``'s own handler; ``status`` runs on demand in a child process. The view
changes nothing and offers no action (it displays the command to run).
"""

from __future__ import annotations

from typing import Any

from .. import browse_ui
from ..browse_model import Browser
from ..browse_status import SubprocessStatus
from ..context import Context, Result
from ..errors import UsageError
from . import list as list_cmd


def run(ctx: Context) -> Result:
    if ctx.json:
        raise UsageError("`wtctl browse` is interactive and has no --json output")
    browse_ui.check_interactive(stdin_isatty=ctx.runtime.stdin_isatty)

    def load() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        data = list_cmd.run(ctx).data  # the same filters, state reads and git as `list`
        return data["records"], data["foreign"]

    browser = Browser(load, SubprocessStatus(ctx.config_path), now=ctx.gateway.clock.now)
    browse_ui.run(browser)
    return Result(data=None)
