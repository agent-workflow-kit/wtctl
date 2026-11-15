"""The command table: one entry per command, with its arguments and rules.

Adding a handler never edits this file: the dispatcher imports ``wtctl.commands.<key>`` and calls
its ``run(ctx: Context) -> Result`` (see ``load_handler``). A missing module means the command is
not implemented yet (exit 1, ``internal``). ``owner`` names the issue that provides the handler.
"""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from .context import Context, Result
from .errors import WtctlError

Handler = Callable[[Context], Result]
HumanOnly = bool | Callable[[argparse.Namespace], bool]

# Flags only a human actor may use (dest -> flag); using one otherwise exits 2.
HUMAN_ONLY_FLAGS = {"force": "--force", "override_escalation": "--override-escalation"}


@dataclass(frozen=True)
class Command:
    key: str  # handler module name and test id, e.g. "label_add"
    path: tuple[str, ...]  # words on the command line, e.g. ("label", "add")
    help: str
    owner: str  # issue that implements the handler
    add_args: Callable[[argparse.ArgumentParser], None]
    dry_run: bool = False  # accepts --dry-run
    human_only: HumanOnly = False  # exits 13 without a human actor


# --- argument groups --------------------------------------------------------------------------


def _target(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", metavar="<id|path>", help="record id (or unique prefix) or path")


def _label(p: argparse.ArgumentParser) -> None:
    p.add_argument("--label", action="append", default=[], metavar="k=v", help="repeatable")


def _hook(p: argparse.ArgumentParser) -> None:
    p.add_argument("--hook", action="append", default=[], metavar="NAME=CMD", help="repeatable")


def _override_escalation(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--override-escalation",
        action="store_true",
        help="count a hook's exit 13 as allow (human only)",
    )


def _none(p: argparse.ArgumentParser) -> None:
    pass


def _create(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", required=True, metavar="<repo-id>")
    p.add_argument("--throwaway", action="store_true")
    branch = p.add_mutually_exclusive_group()
    branch.add_argument("--branch")
    branch.add_argument("--detach", action="store_true")
    p.add_argument("--base", metavar="BRANCH")
    p.add_argument("--ref", metavar="SHA")
    p.add_argument("--name")
    p.add_argument("--holder")
    p.add_argument("--no-bootstrap", action="store_true")
    p.add_argument("--copy-env", action="store_true")
    p.add_argument("--source", choices=["auto", "local", "remote"])
    p.add_argument("--depth", type=int)
    _label(p)
    _hook(p)
    _override_escalation(p)


def _list(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", metavar="<id>")
    _label(p)
    kind = p.add_mutually_exclusive_group()
    kind.add_argument("--throwaways", action="store_true")
    kind.add_argument("--worktrees", action="store_true")
    p.add_argument("--older-than", metavar="DURATION")
    p.add_argument("--state", action="append", default=[], metavar="S", help="repeatable")
    p.add_argument("--all", action="store_true", help="include foreign worktrees")


def _status(p: argparse.ArgumentParser) -> None:
    _target(p)
    p.add_argument("--merged-head", metavar="SHA")
    p.add_argument("--holder")


def _remove(p: argparse.ArgumentParser) -> None:
    _status(p)
    p.add_argument("--keep-branch", action="store_true")
    p.add_argument("--force", action="store_true", help="override some refusals (human only)")
    _hook(p)
    _override_escalation(p)


def _bootstrap(p: argparse.ArgumentParser) -> None:
    _target(p)
    p.add_argument("--copy-env", action="store_true")


def _extract(p: argparse.ArgumentParser) -> None:
    _target(p)
    p.add_argument("--files-from", metavar="FILE|-")
    p.add_argument("--delete-from", metavar="FILE|-")
    p.add_argument("--dest", required=True, metavar="DIR")
    p.add_argument("--max-size", type=int, metavar="BYTES")


def _baseline_accept(p: argparse.ArgumentParser) -> None:
    _target(p)
    p.add_argument("--expect-digest", metavar="D")


def _adopt(p: argparse.ArgumentParser) -> None:
    p.add_argument("path")
    p.add_argument("--repo", metavar="<id>")
    p.add_argument("--name")
    p.add_argument("--holder")
    _label(p)


def _forget(p: argparse.ArgumentParser) -> None:
    _target(p)
    p.add_argument("--holder")


def _repo(p: argparse.ArgumentParser) -> None:
    p.add_argument("repo", metavar="<repo-id>")


def _label_edit(p: argparse.ArgumentParser) -> None:
    _target(p)
    p.add_argument("labels", nargs="+", metavar="k=v")


def _prune(p: argparse.ArgumentParser) -> None:
    p.add_argument("--throwaways", action="store_true", required=True)
    p.add_argument("--older-than", metavar="DURATION")
    p.add_argument("--if-configured", action="store_true")
    _label(p)


def _export(p: argparse.ArgumentParser) -> None:
    p.add_argument("--since", metavar="TIME")
    which = p.add_mutually_exclusive_group()
    which.add_argument("--audit", action="store_true")
    which.add_argument("--log", action="store_true")


def _cron_install(p: argparse.ArgumentParser) -> None:
    p.add_argument("--env-file", metavar="FILE")


# --- the table --------------------------------------------------------------------------------

COMMANDS: tuple[Command, ...] = (
    Command("create", ("create",), "create a worktree or throwaway", "#15", _create, dry_run=True),
    Command("list", ("list",), "list records", "#7", _list),
    Command("show", ("show",), "print one record", "#7", _target),
    Command("status", ("status",), "report what remove would do", "#19", _status),
    Command("remove", ("remove",), "remove a worktree or throwaway", "#20", _remove, dry_run=True),
    Command(
        "bootstrap",
        ("bootstrap",),
        "re-run bootstrap",
        "#16",
        _bootstrap,
        dry_run=True,
        human_only=True,
    ),
    Command(
        "extract", ("extract",), "extract files from a throwaway", "#22", _extract, dry_run=True
    ),
    Command(
        "baseline_check", ("baseline", "check"), "compare the repo-config baseline", "#17", _target
    ),
    Command(
        "baseline_accept",
        ("baseline", "accept"),
        "re-record the baseline",
        "#17",
        _baseline_accept,
        dry_run=True,
        human_only=True,
    ),
    Command("adopt", ("adopt",), "record an existing worktree", "#18", _adopt, dry_run=True),
    Command(
        "forget",
        ("forget",),
        "drop a worktree record",
        "#18",
        _forget,
        dry_run=True,
        human_only=lambda a: not a.holder,
    ),
    Command("locate", ("locate",), "resolve a repo id to its clone", "#11", _repo),
    Command("clone", ("clone",), "clone a repo", "#11", _repo, dry_run=True),
    Command("label_add", ("label", "add"), "add labels", "#7", _label_edit, dry_run=True),
    Command("label_remove", ("label", "remove"), "remove labels", "#7", _label_edit, dry_run=True),
    Command("reconcile", ("reconcile",), "reconcile state with disk", "#23", _none, dry_run=True),
    Command("prune", ("prune",), "remove old throwaways", "#21", _prune, dry_run=True),
    Command("export", ("export",), "print audit or log records", "#5", _export),
    Command(
        "cron_install",
        ("cron", "install"),
        "install cron entries",
        "#8",
        _cron_install,
        dry_run=True,
    ),
    Command(
        "cron_uninstall", ("cron", "uninstall"), "remove cron entries", "#8", _none, dry_run=True
    ),
)

BY_KEY = {c.key: c for c in COMMANDS}


def is_human_only(cmd: Command, args: argparse.Namespace) -> bool:
    rule = cmd.human_only
    return rule(args) if callable(rule) else rule


def load_handler(cmd: Command) -> Handler:
    """Import ``wtctl.commands.<key>``; a command with no module is unimplemented (exit 1)."""
    name = f"wtctl.commands.{cmd.key}"
    try:
        module: Any = importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:  # a missing import *inside* the handler is a real bug
            raise
        raise WtctlError(
            "internal", f"`wtctl {' '.join(cmd.path)}` is not implemented yet (see {cmd.owner})"
        ) from None
    handler = getattr(module, "run", None)
    if not callable(handler):
        raise WtctlError("internal", f"{name} defines no run(ctx) function")
    return cast(Handler, handler)
