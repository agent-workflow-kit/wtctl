"""Command-line entry point for wtctl: parsing, the actor and human-only rules, dispatch."""

from __future__ import annotations

import argparse
import os
import re
import sys
import traceback
from typing import NoReturn, TextIO

from . import __version__, gateway
from .actor import ActorError, parse_actor, resolve_actor
from .context import Context, Result, Runtime
from .errors import UsageError, WtctlError
from .output import escape_text_lines, render_error, render_json
from .registry import BY_KEY, COMMANDS, HUMAN_ONLY_FLAGS, is_human_only, load_handler

PROG = "wtctl"
DESCRIPTION = "Git worktrees, throwaway checkouts, and the mapping from repo ids to local clones."

DECISION_ID_RE = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")
MAX_DECISION_IDS = 32


def _actor_arg(value: str) -> str:
    try:
        parse_actor(value)
    except ActorError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return value


def _decision_id_arg(value: str) -> str:
    if not DECISION_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"invalid decision id {value!r}: expected 1-128 of [A-Za-z0-9._:@/-]"
        )
    return value


class _Parser(argparse.ArgumentParser):
    """Raises ``UsageError`` instead of printing and exiting, so ``--json`` can report it."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(message, usage=self.format_usage())


def _global_flags(*, top: bool) -> _Parser:
    """The global flags. Subcommands get them with suppressed defaults, so they work on either
    side of the command without a subcommand's default overwriting a value given before it."""
    p = _Parser(add_help=False, allow_abbrev=False)
    g = p.add_argument_group("global flags")

    def default(value: object) -> object:
        return value if top else argparse.SUPPRESS

    g.add_argument("--json", action="store_true", default=default(False), help="JSON output")
    g.add_argument("--config", metavar="PATH", default=default(None), help="config file")
    noise = g.add_mutually_exclusive_group()
    noise.add_argument("--verbose", action="store_true", default=default(False))
    noise.add_argument("--quiet", action="store_true", default=default(False))
    g.add_argument("--dry-run", action="store_true", default=default(False))
    g.add_argument("--yes", action="store_true", default=default(False), help="skip prompts")
    g.add_argument("--actor", type=_actor_arg, metavar="ACTOR", default=default(None))
    g.add_argument(
        "--decision-id",
        action="append",
        type=_decision_id_arg,
        metavar="ID",
        # The subcommand's values go to their own dest; `main` appends them to the top-level ones.
        dest="decision_id" if top else "decision_id_after",
        default=[] if top else argparse.SUPPRESS,
        help="repeatable",
    )
    return p


def build_parser() -> argparse.ArgumentParser:
    top = _global_flags(top=True)
    sub_flags = _global_flags(top=False)
    parser = _Parser(prog=PROG, description=DESCRIPTION, parents=[top], allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    root = parser.add_subparsers(
        dest="_word0", metavar="<command>", required=True, parser_class=_Parser
    )
    groups: dict[str, argparse._SubParsersAction[argparse.ArgumentParser]] = {}
    for cmd in COMMANDS:
        if len(cmd.path) == 1:
            p = root.add_parser(cmd.path[0], help=cmd.help, parents=[sub_flags], allow_abbrev=False)
        else:
            group = cmd.path[0]
            if group not in groups:
                gp = root.add_parser(group, parents=[sub_flags], allow_abbrev=False)
                groups[group] = gp.add_subparsers(
                    dest="_word1", metavar="<subcommand>", required=True, parser_class=_Parser
                )
            p = groups[group].add_parser(
                cmd.path[1], help=cmd.help, parents=[sub_flags], allow_abbrev=False
            )
        cmd.add_args(p)
        p.set_defaults(_key=cmd.key)
    return parser


def _check_usage(args: argparse.Namespace, actor_is_human: bool) -> None:
    """Usage rules beyond argparse (stage 1 of the check order: exit 2)."""
    cmd = BY_KEY[args._key]
    if args.dry_run and not cmd.dry_run:
        raise UsageError(f"--dry-run is not accepted by `{PROG} {' '.join(cmd.path)}`")
    if args.verbose and args.quiet:
        raise UsageError("--verbose and --quiet cannot be used together")
    if len(args.decision_id) > MAX_DECISION_IDS:
        raise UsageError(f"at most {MAX_DECISION_IDS} --decision-id values are allowed")
    if not actor_is_human:
        for dest, flag in HUMAN_ONLY_FLAGS.items():
            if getattr(args, dest, False):
                raise UsageError(
                    f"{flag} needs a human actor (a TTY on stdin or --actor human:<user>)"
                )


class _OutputClosed(BaseException):
    """Our own write to stdout or stderr hit a broken pipe (BaseException: not an `internal`)."""


def _write(stream: TextIO | None, text: str) -> None:
    if stream is None:  # a detached stream (sys.stdout is None) is skipped
        return
    try:
        stream.write(text)
    except BrokenPipeError:
        raise _OutputClosed from None


def _emit_error(err: WtctlError, *, as_json: bool, verbose: bool = False) -> int:
    try:
        out, errtext = render_error(err, as_json=as_json)
    except (TypeError, ValueError):  # e.g. a NaN or a set in `details`
        err = WtctlError("internal", f"could not render the {err.code} error")
        out, errtext = render_error(err, as_json=as_json)
    if isinstance(err, UsageError) and err.usage and not as_json:
        errtext = escape_text_lines(err.usage) + errtext
    if out:
        _write(sys.stdout, out + "\n")
    _write(sys.stderr, errtext)
    if verbose and err.__cause__ is not None:
        _write(sys.stderr, escape_text_lines(_trace(err.__cause__)))
    return err.exit_code


def _trace(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


def _emit_result(result: Result, ctx: Context) -> None:
    if ctx.json:
        _write(sys.stdout, render_json(result.data) + "\n")
    elif result.human:
        _write(sys.stdout, escape_text_lines(result.human.rstrip("\n")) + "\n")
    if result.stderr and not ctx.quiet:
        _write(sys.stderr, escape_text_lines(result.stderr.rstrip("\n")) + "\n")


_VALUE_FLAGS = frozenset({"--config", "--actor", "--decision-id"})


def _prescan(argv: list[str]) -> tuple[bool, bool]:
    """``(--json, --verbose)`` present, for errors raised before parsing succeeds. Tokens after a
    bare ``--`` and the values of the flags that take one are not flags."""
    as_json = verbose = False
    skip = False
    for token in argv:
        if skip:
            skip = False
        elif token == "--":
            break
        elif token in _VALUE_FLAGS:
            skip = True
        elif token == "--json":
            as_json = True
        elif token == "--verbose":
            verbose = True
    return as_json, verbose


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = build_parser()
    try:
        return parser.parse_args(argv)
    except UsageError as err:
        # argparse reports the missing command before the unknown argument; name the latter.
        if "required: <command>" in str(err):
            try:
                _, extra = parser.parse_known_args([*argv, "list"])
            except UsageError:
                extra = []
            if extra:
                raise UsageError(
                    f"unrecognized arguments: {' '.join(extra)}", usage=err.usage
                ) from None
        raise


def _silence_stdout() -> None:
    """After a broken pipe, point stdout at /dev/null so the exit-time flush cannot fail too."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (AttributeError, ValueError, OSError):
        pass


def main(argv: list[str] | None = None, *, runtime: Runtime | None = None) -> int:
    try:
        code = _run(list(sys.argv[1:] if argv is None else argv), runtime or Runtime())
        for stream in (sys.stdout, sys.stderr):
            if stream is not None:
                stream.flush()
        return code
    except (_OutputClosed, BrokenPipeError):  # BrokenPipeError: from the flush
        _silence_stdout()
        return 1


def _needs_me_name(args: argparse.Namespace, runtime: Runtime) -> bool:
    return args.actor is None and runtime.stdin_isatty


def _run(argv: list[str], runtime: Runtime) -> int:
    gateway.disable_dumpable()
    as_json, verbose = _prescan(argv)
    try:
        args = _parse(argv)
        as_json, verbose = args.json, args.verbose
        args.decision_id = [*args.decision_id, *getattr(args, "decision_id_after", [])]
        actor = resolve_actor(
            args.actor,
            stdin_isatty=runtime.stdin_isatty,
            me_name=runtime.resolve_me_name(args.config) if _needs_me_name(args, runtime) else "",
        )
        _check_usage(args, actor.is_human)
        cmd = BY_KEY[args._key]
        if is_human_only(cmd, args) and not actor.is_human:
            raise WtctlError(
                "confirmation_required",
                f"`{PROG} {' '.join(cmd.path)}` needs a human actor "
                "(a TTY on stdin or --actor human:<user>)",
            )
        ctx = Context(
            args=args,
            actor=actor,
            runtime=runtime,
            json=args.json,
            config_path=args.config,
            verbose=args.verbose,
            quiet=args.quiet,
            dry_run=args.dry_run,
            yes=args.yes,
            decision_ids=list(args.decision_id),
        )
        _emit_result(load_handler(cmd)(ctx), ctx)
        return 0
    except WtctlError as err:
        return _emit_error(err, as_json=as_json, verbose=verbose)
    except Exception as exc:  # noqa: BLE001 - anything uncaught is exit 1, `internal`
        err = WtctlError("internal", f"unexpected {type(exc).__name__}: {exc}")
        err.__cause__ = exc
        return _emit_error(err, as_json=as_json, verbose=verbose)
