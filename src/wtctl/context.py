"""What a command handler receives and returns."""

from __future__ import annotations

import argparse
import getpass
import sys
from dataclasses import dataclass, field
from typing import Any

from .actor import Actor
from .errors import WtctlError
from .gateway import Gateway


def _stdin_isatty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except ValueError:  # closed stdin
        return False


def _os_user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


@dataclass
class Runtime:
    """Facts about the environment the CLI runs in; tests pass their own."""

    stdin_isatty: bool = field(default_factory=_stdin_isatty)
    # TODO(#3): the default actor's name is `me.name` from the config (default: the OS user).
    me_name: str = field(default_factory=_os_user)
    gateway: Gateway = field(default_factory=Gateway)


@dataclass
class Context:
    """The parsed invocation. Handlers read ``args`` for their own command's arguments."""

    args: argparse.Namespace
    actor: Actor
    runtime: Runtime
    json: bool = False
    config_path: str | None = None
    verbose: bool = False
    quiet: bool = False
    dry_run: bool = False
    yes: bool = False
    decision_ids: list[str] = field(default_factory=list)
    # TODO(#3): `config` (the validated configuration) is added here.
    # TODO(#4): `state` (the state.db handle) is added here.

    @property
    def gateway(self) -> Gateway:
        return self.runtime.gateway

    def require_human(self) -> None:
        """For commands that are human-only only in some cases (e.g. `remove` of an adopted one)."""
        if not self.actor.is_human:
            raise WtctlError(
                "confirmation_required",
                "this action needs a human actor (a TTY on stdin or --actor human:<user>)",
            )


@dataclass
class Result:
    """A handler's success output.

    ``data`` is printed with ``--json``; ``human`` goes to stdout without it; ``stderr`` is the
    summary line(s) on stderr (suppressed by ``--quiet``).
    """

    data: Any
    human: str = ""
    stderr: str = ""
