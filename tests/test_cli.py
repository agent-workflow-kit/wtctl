from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest

from tests.helpers import Result, install_handler, run_cli
from tests.schemacheck import validate
from wtctl import __version__
from wtctl.cli import main
from wtctl.context import Context, Runtime
from wtctl.errors import WtctlError
from wtctl.registry import COMMANDS

SID = "wt-abcdefgh"
SHA = "a" * 40

# One minimal valid argv per command key.
MINIMAL: dict[str, list[str]] = {
    "create": ["create", "--repo", "example.com/o/r"],
    "list": ["list"],
    "browse": ["browse"],
    "show": ["show", SID],
    "status": ["status", SID],
    "remove": ["remove", SID],
    "bootstrap": ["bootstrap", SID],
    "extract": ["extract", SID, "--files-from", "-", "--dest", "/x"],
    "baseline_check": ["baseline", "check", SID],
    "baseline_accept": ["baseline", "accept", SID],
    "adopt": ["adopt", "/x"],
    "forget": ["forget", SID],
    "locate": ["locate", "example.com/o/r"],
    "clone": ["clone", "example.com/o/r"],
    "export": ["export"],
    "validate": ["validate"],
    "label_add": ["label", "add", SID, "k=v"],
    "label_remove": ["label", "remove", SID, "k=v"],
    "reconcile": ["reconcile"],
    "prune": ["prune", "--throwaways"],
    "cron_install": ["cron", "install"],
    "cron_uninstall": ["cron", "uninstall"],
}


def test_minimal_table_covers_every_registered_command() -> None:
    assert set(MINIMAL) == {c.key for c in COMMANDS}


# Assumes a uv-synced environment (`uv run` re-syncs the editable install when
# pyproject.toml changes).
def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text())["project"]["version"]

    assert __version__ == expected


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        run_cli(capsys, "--version")
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"wtctl {__version__}"


def test_no_arguments_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    r = run_cli(capsys)
    assert r.code == 2
    assert "required" in r.err


def test_usage_error_json(capsys: pytest.CaptureFixture[str]) -> None:
    r = run_cli(capsys, "--json", "nonsense")
    assert r.code == 2
    validate("wtctl/cli/error.json", r.json())
    assert r.json()["error"]["code"] == "usage"
    assert "nonsense" in r.err


def test_usage_error_json_even_when_flag_follows(capsys: pytest.CaptureFixture[str]) -> None:
    r = run_cli(capsys, "show", "--bogus", "--json")
    assert r.code == 2
    validate("wtctl/cli/error.json", r.json())


def test_abbreviated_flags_are_not_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "remove", SID, "--forc", "--actor", "human:a").code == 2


def _implemented(key: str) -> bool:
    return importlib.util.find_spec(f"wtctl.commands.{key}") is not None


@pytest.mark.parametrize("key", sorted(k for k in MINIMAL if not _implemented(k)))
def test_unimplemented_commands_exit_1_internal(
    capsys: pytest.CaptureFixture[str], key: str
) -> None:
    if importlib.util.find_spec(f"wtctl.commands.{key}") is not None:
        pytest.skip("implemented")
    r = run_cli(capsys, "--json", "--actor", "human:alice", *MINIMAL[key])
    assert r.code == 1
    obj = r.json()
    validate("wtctl/cli/error.json", obj)
    assert obj["error"]["code"] == "internal"
    assert "not implemented" in obj["error"]["message"]


def test_global_flags_are_accepted_before_and_after_the_command(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Context] = []

    def handler(ctx: Context) -> Result:
        seen.append(ctx)
        return Result({"ok": True})

    install_handler(monkeypatch, "reconcile", handler)
    a = run_cli(capsys, "--json", "--dry-run", "--yes", "--verbose", "reconcile")
    b = run_cli(capsys, "reconcile", "--json", "--dry-run", "--yes", "--verbose")
    assert a.code == b.code == 0
    for ctx in seen:
        assert (ctx.json, ctx.dry_run, ctx.yes, ctx.verbose, ctx.quiet) == (
            True,
            True,
            True,
            True,
            False,
        )


def test_config_and_decision_ids_reach_the_handler(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Context] = []
    install_handler(monkeypatch, "list", lambda ctx: (seen.append(ctx), Result({}))[1])
    r = run_cli(
        capsys,
        "--config",
        "/etc/x.yaml",
        "--decision-id",
        "a:1",
        "--decision-id",
        "b/2@x",
        "list",
    )
    assert r.code == 0
    assert seen[0].config_path == "/etc/x.yaml"
    assert seen[0].decision_ids == ["a:1", "b/2@x"]


def test_verbose_and_quiet_conflict(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "--verbose", "--quiet", "list").code == 2


def test_dry_run_rejected_where_not_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    r = run_cli(capsys, "--json", "--dry-run", "show", SID)
    assert r.code == 2
    assert r.json()["error"]["code"] == "usage"


def test_dry_run_accepted_where_documented() -> None:
    accepted = {
        "create", "bootstrap", "extract", "baseline_accept", "adopt", "forget", "label_add",
        "label_remove", "clone", "reconcile", "prune", "cron_install", "cron_uninstall", "remove",
    }  # fmt: skip
    assert {c.key for c in COMMANDS if c.dry_run} == accepted


# --- decision ids -----------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "a b", "a\nb", "x" * 129, "é", "a;b", "a$b"])
def test_bad_decision_id_exits_2(capsys: pytest.CaptureFixture[str], bad: str) -> None:
    r = run_cli(capsys, "--json", "--decision-id", bad, "list")
    assert r.code == 2
    validate("wtctl/cli/error.json", r.json())


def test_decision_id_limit(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_handler(monkeypatch, "list", lambda ctx: Result({}))
    ok = [x for i in range(32) for x in ("--decision-id", f"d{i}")]
    assert run_cli(capsys, *ok, "list").code == 0
    assert run_cli(capsys, *ok, "--decision-id", "one-too-many", "list").code == 2
    assert run_cli(capsys, "--decision-id", "x" * 128, "list").code == 0


# --- actor and human-only rules ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["human:", "bot:x", "human:a b", "human:" + "a" * 65])
def test_bad_actor_exits_2(capsys: pytest.CaptureFixture[str], bad: str) -> None:
    assert run_cli(capsys, "--actor", bad, "list").code == 2


def test_actor_default_follows_tty(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    install_handler(monkeypatch, "list", lambda ctx: (seen.append(ctx.actor.value), Result({}))[1])
    run_cli(capsys, "list", tty=True, me="alice")
    run_cli(capsys, "list", tty=False, me="alice")
    run_cli(capsys, "--actor", "auto:cron", "list", tty=True)
    assert seen == ["human:alice", "auto:unknown", "auto:cron"]


HUMAN_ONLY_COMMANDS = [
    ["baseline", "accept", SID],
    ["bootstrap", SID],
    ["forget", SID],
]


@pytest.mark.parametrize("argv", HUMAN_ONLY_COMMANDS)
def test_human_only_commands_exit_13_without_human_actor(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    for extra, tty in ([], False), (["--actor", "auto:bot"], True):
        r = run_cli(capsys, "--json", *extra, *argv, tty=tty)
        assert r.code == 13
        obj = r.json()
        validate("wtctl/cli/error.json", obj)
        assert obj["error"]["code"] == "confirmation_required"


# `bootstrap` (#16), `baseline accept` (#17) and `forget` (#18) are implemented: their own tests
# cover the handlers.
@pytest.mark.parametrize(
    "argv", [c for c in HUMAN_ONLY_COMMANDS if c[0] not in ("bootstrap", "baseline", "forget")]
)
@pytest.mark.parametrize(
    ("extra", "tty"), [(["--actor", "human:bob"], False), ([], True)], ids=["flag", "tty"]
)
def test_human_only_commands_reach_the_handler_with_a_human(
    capsys: pytest.CaptureFixture[str], argv: list[str], extra: list[str], tty: bool
) -> None:
    r = run_cli(capsys, "--json", *extra, *argv, tty=tty)
    assert r.code == 1  # past the gate, into the unimplemented handler
    assert r.json()["error"]["code"] == "internal"


def test_forget_with_holder_is_not_human_only(capsys: pytest.CaptureFixture[str]) -> None:
    r = run_cli(capsys, "--json", "forget", SID, "--holder", "h1")
    assert r.code == 10  # reached the handler as an automated actor (no such record)


def test_require_human_helper(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(ctx: Context) -> Result:
        ctx.require_human()
        return Result({"ok": 1})

    install_handler(monkeypatch, "clone", handler)
    assert run_cli(capsys, "--json", "clone", "example.com/o/r").code == 13
    assert run_cli(capsys, "--json", "--actor", "human:a", "clone", "example.com/o/r").code == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["remove", SID, "--force"],
        ["remove", SID, "--override-escalation"],
        ["create", "--repo", "example.com/o/r", "--override-escalation"],
    ],
)
def test_human_only_flags_exit_2_without_human_actor(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    r = run_cli(capsys, "--json", *argv)
    assert r.code == 2
    obj = r.json()
    validate("wtctl/cli/error.json", obj)
    assert obj["error"]["code"] == "usage"


def test_human_only_flags_still_exit_2_under_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "--dry-run", "remove", SID, "--force").code == 2


def test_human_only_flag_beats_human_only_command_gate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # usage (2) is checked before the command's own rules (13)
    assert run_cli(capsys, "remove", SID, "--force", tty=False).code == 2


def test_human_only_flags_pass_with_human_actor(capsys: pytest.CaptureFixture[str]) -> None:
    # past the flag gate: the handler runs and finds no such record
    r = run_cli(capsys, "--json", "--actor", "human:a", "remove", SID, "--force")
    assert r.code == 10
    assert r.json()["error"]["code"] == "not_found"
    r = run_cli(capsys, "--json", "remove", SID, "--force", tty=True)
    assert r.code == 10


# --- handlers, results and errors -------------------------------------------------------------


def test_handler_json_result(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_handler(
        monkeypatch,
        "export",
        lambda ctx: Result([{"a": 1}], human="a=1", stderr="1 record"),
    )
    r = run_cli(capsys, "--json", "export")
    assert (r.code, r.json()) == (0, [{"a": 1}])
    assert r.err == "1 record\n"
    validate("wtctl/cli/export.json", [])


def test_handler_human_result_and_quiet(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_handler(
        monkeypatch, "locate", lambda ctx: Result({"x": 1}, human="/some/path", stderr="note")
    )
    r = run_cli(capsys, "locate", "example.com/o/r")
    assert (r.code, r.out, r.err) == (0, "/some/path\n", "note\n")
    q = run_cli(capsys, "--quiet", "locate", "example.com/o/r")
    assert (q.out, q.err) == ("/some/path\n", "")


def test_handler_wtctl_error_maps_to_exit_and_object(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(ctx: Context) -> Result:
        raise WtctlError("refused", "tree is dirty", reason="dirty")

    install_handler(monkeypatch, "status", handler)
    r = run_cli(capsys, "--json", "status", SID)
    assert r.code == 20
    validate("wtctl/cli/error.json", r.json())
    assert r.err.strip() == "wtctl: error: tree is dirty (dirty)"
    h = run_cli(capsys, "status", SID)
    assert (h.code, h.out) == (20, "")


def test_uncaught_exception_is_internal_without_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(ctx: Context) -> Result:
        raise RuntimeError("boom \x1b[31m")

    install_handler(monkeypatch, "show", handler)
    r = run_cli(capsys, "--json", "show", SID)
    assert r.code == 1
    obj = r.json()
    validate("wtctl/cli/error.json", obj)
    assert obj["error"]["code"] == "internal" and "boom" in obj["error"]["message"]
    assert "Traceback" not in r.err and "\x1b" not in r.err
    v = run_cli(capsys, "--verbose", "show", SID)
    assert v.code == 1 and "Traceback" in v.err and "\x1b" not in v.err


def test_keyboard_interrupt_is_not_swallowed_as_internal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(ctx: Context) -> Result:
        raise KeyboardInterrupt

    install_handler(monkeypatch, "show", handler)
    with pytest.raises(KeyboardInterrupt):
        run_cli(capsys, "show", SID)


def test_handler_import_error_inside_module_is_internal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    mod = types.ModuleType("wtctl.commands.show")
    monkeypatch.setitem(sys.modules, "wtctl.commands.show", mod)  # no run()
    r = run_cli(capsys, "--json", "show", SID)
    assert r.code == 1
    assert r.json()["error"]["code"] == "internal"


def test_human_error_message_escapes_control_characters(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(ctx: Context) -> Result:
        raise WtctlError("not_found", "no \x1b[2Jrecord\nfake: line")

    install_handler(monkeypatch, "show", handler)
    r = run_cli(capsys, "show", SID)
    assert r.err.count("\n") == 1 and "\x1b" not in r.err
    assert "\\x1b[2J" in r.err


def test_process_hardening_runs_at_startup(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import gateway

    calls: list[int] = []
    monkeypatch.setattr(gateway, "disable_dumpable", lambda: calls.append(1) or True)
    run_cli(capsys, "list")
    assert calls == [1]


# --- review round 1 -----------------------------------------------------------------------------


def _capture_ctx(monkeypatch: pytest.MonkeyPatch, key: str = "list") -> list[Context]:
    seen: list[Context] = []
    install_handler(monkeypatch, key, lambda ctx: (seen.append(ctx), Result({}))[1])
    return seen


def test_decision_ids_before_and_after_the_command_are_merged_in_order(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_ctx(monkeypatch)
    r = run_cli(capsys, "--decision-id", "a", "--decision-id", "b", "list", "--decision-id", "c")
    assert r.code == 0
    assert seen[0].decision_ids == ["a", "b", "c"]


def test_decision_id_limit_applies_to_the_merged_list(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_ctx(monkeypatch)
    before = [x for i in range(20) for x in ("--decision-id", f"b{i}")]
    after = [x for i in range(20) for x in ("--decision-id", f"a{i}")]
    r = run_cli(capsys, "--json", *before, "list", *after)
    assert r.code == 2
    assert r.json()["error"]["code"] == "usage"


def test_bad_decision_id_after_the_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(capsys, "list", "--decision-id", "a b").code == 2


def test_usage_block_keeps_its_line_breaks(capsys: pytest.CaptureFixture[str]) -> None:
    r = run_cli(capsys, "show")
    assert r.code == 2
    assert "\\x0a" not in r.err
    assert r.err.splitlines()[0].startswith("usage: ")
    assert len(r.err.splitlines()) >= 2


def test_verbose_traceback_keeps_its_line_breaks(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(ctx: Context) -> Result:
        raise RuntimeError("boom")

    install_handler(monkeypatch, "show", handler)
    r = run_cli(capsys, "--verbose", "show", SID)
    assert "\\x0a" not in r.err
    assert any(line.startswith("Traceback") for line in r.err.splitlines())
    assert len(r.err.splitlines()) > 3


def test_json_with_nan_falls_back_to_internal_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_handler(monkeypatch, "export", lambda ctx: Result({"x": float("nan")}))
    r = run_cli(capsys, "--json", "export")
    assert r.code == 1
    obj = r.json()
    validate("wtctl/cli/error.json", obj)
    assert obj["error"]["code"] == "internal"
    assert "Traceback" not in r.err


def test_json_with_unserializable_result_falls_back_to_internal_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_handler(monkeypatch, "export", lambda ctx: Result({"x": {1, 2}}))
    r = run_cli(capsys, "--json", "export")
    assert r.code == 1 and r.out.count("\n") == 1
    assert r.json()["error"]["code"] == "internal"
    assert "Traceback" not in r.err


def test_error_object_that_cannot_be_serialized_becomes_internal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(ctx: Context) -> Result:
        raise WtctlError("partial", "x", details={"remaining_path": "/a", "n": float("nan")})

    install_handler(monkeypatch, "remove", handler)
    r = run_cli(capsys, "--json", "--actor", "human:a", "remove", SID)
    assert r.code == 1
    validate("wtctl/cli/error.json", r.json())
    assert r.json()["error"]["code"] == "internal"


def test_handler_human_and_stderr_text_is_escaped_but_keeps_newlines(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    install_handler(
        monkeypatch,
        "locate",
        lambda ctx: Result({}, human="a\x1b[31m‮\nsecond", stderr="e\x1b[2J\nline2"),
    )
    r = run_cli(capsys, "locate", "example.com/o/r")
    assert r.out == "a\\x1b[31m\\u202e\nsecond\n"
    assert r.err == "e\\x1b[2J\nline2\n"


def test_forget_with_empty_holder_is_still_human_only(capsys: pytest.CaptureFixture[str]) -> None:
    r = run_cli(capsys, "--json", "forget", SID, "--holder", "")
    assert r.code == 13


def test_json_prescan_ignores_tokens_after_double_dash_and_flag_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(capsys, "bogus", "--", "--json").out == ""
    assert run_cli(capsys, "--config", "--json", "bogus").out == ""
    assert run_cli(capsys, "--actor", "--json", "bogus").out == ""
    assert run_cli(capsys, "--decision-id", "--json", "bogus").out == ""
    assert run_cli(capsys, "--json", "bogus").json()["error"]["code"] == "usage"


def test_verbose_prescan_ignores_flag_values(capsys: pytest.CaptureFixture[str]) -> None:
    r = run_cli(capsys, "--config", "--verbose", "bogus")
    assert r.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["--verbose", "list", "--quiet"],
        ["--quiet", "list", "--verbose"],
        ["list", "--verbose", "--quiet"],
    ],
)
def test_verbose_with_quiet_on_any_sides_is_usage(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert run_cli(capsys, "--json", *argv).code == 2


class _BrokenStream:
    def __init__(self) -> None:
        self.writes = 0

    def write(self, text: str) -> int:
        self.writes += 1
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError


def test_broken_pipe_on_stdout_is_a_quiet_exit(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    install_handler(monkeypatch, "export", lambda ctx: Result({"a": 1}))
    broken = _BrokenStream()
    monkeypatch.setattr(sys, "stdout", broken)
    code = main(["--json", "export"], runtime=Runtime(stdin_isatty=False))
    assert code == 1
    assert broken.writes == 1
    assert capsys.readouterr().err == ""


def test_broken_pipe_on_stderr_alone_is_a_quiet_exit(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    broken = _BrokenStream()
    monkeypatch.setattr(sys, "stderr", broken)
    assert main(["--json", "bogus"], runtime=Runtime(stdin_isatty=False)) == 1
    assert broken.writes >= 1
    assert capsys.readouterr().err == ""


def test_broken_pipe_on_flush_is_a_quiet_exit(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import sys

    class FlushBroken(io.StringIO):
        def flush(self) -> None:
            raise BrokenPipeError

    install_handler(monkeypatch, "export", lambda ctx: Result({"a": 1}))
    monkeypatch.setattr(sys, "stdout", FlushBroken())
    assert main(["--json", "export"], runtime=Runtime(stdin_isatty=False)) == 1
    assert capsys.readouterr().err == ""


def test_closed_stdout_does_not_raise(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    install_handler(monkeypatch, "export", lambda ctx: Result({"a": 1}, human="x"))
    monkeypatch.setattr(sys, "stdout", None)
    assert main(["export"], runtime=Runtime(stdin_isatty=False)) == 0
    monkeypatch.setattr(sys, "stdout", None)
    assert main(["--json", "bogus"], runtime=Runtime(stdin_isatty=False)) == 2


def test_unknown_flag_is_named_when_the_command_is_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    r = run_cli(capsys, "--json", "-x")
    assert r.code == 2
    assert "-x" in r.json()["error"]["message"]
    assert "unrecognized" in r.json()["error"]["message"]
    assert "required" in run_cli(capsys, "--json").json()["error"]["message"]


# --- config / state / default-actor seams -----------------------------------------------------


def test_config_and_state_are_lazy_and_cached(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def loader(path: str | None) -> object:
        calls.append(f"config:{path}")
        return {"cfg": 1}

    def opener(ctx: Context) -> object:
        calls.append("state")
        return {"db": 1}

    def handler(ctx: Context) -> Result:
        assert calls == []  # not built by main
        assert ctx.config is ctx.config and ctx.state is ctx.state
        return Result({})

    install_handler(monkeypatch, "list", handler)
    rt = Runtime(stdin_isatty=False, config_loader=loader, state_opener=opener)
    assert main(["--config", "/c.yaml", "list"], runtime=rt) == 0
    assert calls == ["config:/c.yaml", "state"]


def test_default_config_seam_reads_the_private_config(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The config seam is implemented (#3): the default loader reads the real (private) config.
    install_handler(monkeypatch, "list", lambda ctx: Result({"d": ctx.config.search_depth}))
    r = run_cli(capsys, "--json", "list")
    assert r.code == 0
    assert r.json() == {"d": 3}


def test_default_state_seam_raises_not_implemented_internal(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Whatever modules exist now, behave as if state defined no seam function.
    monkeypatch.setattr("wtctl.context._seam", lambda module, attr: None)
    install_handler(monkeypatch, "list", lambda ctx: Result({"s": ctx.state}))
    r = run_cli(capsys, "--json", "list")
    assert "not implemented yet (see #4)" in r.json()["error"]["message"]


def test_default_actor_name_comes_from_the_seam_only_with_a_tty(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_ctx(monkeypatch)
    asked: list[str | None] = []

    def source(path: str | None) -> str:
        asked.append(path)
        return "carol"

    main(["--config", "/c", "list"], runtime=Runtime(stdin_isatty=True, me_name_source=source))
    main(["list"], runtime=Runtime(stdin_isatty=False, me_name_source=source))
    assert [c.actor.value for c in seen] == ["human:carol", "auto:unknown"]
    assert asked == ["/c"]


def test_dry_run_flag_later_one_wins_and_repeated_flags_are_fine(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture_ctx(monkeypatch, "reconcile")
    run_cli(capsys, "--config", "/a", "reconcile", "--config", "/b")
    assert seen[0].config_path == "/b"
