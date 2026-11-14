"""What a command handler receives and returns."""

from __future__ import annotations

import argparse
import getpass
import importlib
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
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


def _seam(module: str, attr: str) -> Callable[..., Any] | None:
    """``wtctl.<module>.<attr>`` if that module defines it, else None.

    The config (#3) and state (#4) modules plug in by defining these functions; nothing in
    ``cli.py`` or this file is edited for it (the same convention as ``load_handler``).
    A module that exists but fails to import is a real bug and propagates.
    """
    name = f"wtctl.{module}"
    try:
        mod = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        return None
    fn = getattr(mod, attr, None)
    return fn if callable(fn) else None


def _not_implemented(what: str, owner: str) -> WtctlError:
    return WtctlError("internal", f"{what} is not implemented yet (see {owner})")


def _default_config_loader(config_path: str | None) -> Any:
    fn = _seam("config", "load_config")
    if fn is None:
        raise _not_implemented("loading the configuration", "#3")
    return fn(config_path)


def _default_state_opener(ctx: Context) -> Any:
    fn = _seam("state", "open_state")
    if fn is None:
        raise _not_implemented("opening state.db", "#4")
    return fn(ctx)


def _default_me_name(config_path: str | None) -> str:
    fn = _seam("config", "me_name")
    return str(fn(config_path)) if fn is not None else _os_user()


@dataclass
class Runtime:
    """Facts and seams of the environment the CLI runs in; tests pass their own.

    ``config_loader(config_path)`` and ``state_opener(ctx)`` back ``Context.config`` and
    ``Context.state``; ``me_name_source(config_path)`` gives the default actor's name. Their
    defaults look up ``wtctl.config.load_config``, ``wtctl.state.open_state`` and
    ``wtctl.config.me_name`` (docs/design.md, Internal architecture).
    """

    stdin_isatty: bool = field(default_factory=_stdin_isatty)
    me_name: str | None = None  # explicit override (tests); else ``me_name_source``
    gateway: Gateway = field(default_factory=Gateway)
    config_loader: Callable[[str | None], Any] = _default_config_loader
    state_opener: Callable[[Context], Any] = _default_state_opener
    me_name_source: Callable[[str | None], str] = _default_me_name

    def resolve_me_name(self, config_path: str | None) -> str:
        return self.me_name if self.me_name is not None else self.me_name_source(config_path)


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

    @cached_property
    def config(self) -> Any:
        """The validated configuration, loaded on first use (a seam, see ``Runtime``)."""
        return self.runtime.config_loader(self.config_path)

    @cached_property
    def state(self) -> Any:
        """The ``state.db`` handle, opened on first use (a seam, see ``Runtime``)."""
        return self.runtime.state_opener(self)

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
