from __future__ import annotations

import pytest

from tests.helpers import Result, install_handler, run_cli
from tests.schemacheck import validate
from wtctl import __version__
from wtctl.context import Context
from wtctl.errors import WtctlError
from wtctl.registry import COMMANDS

SID = "wt-abcdefgh"
SHA = "a" * 40

# One minimal valid argv per command key.
MINIMAL: dict[str, list[str]] = {
    "create": ["create", "--repo", "example.com/o/r"],
    "list": ["list"],
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
    "label_add": ["label", "add", SID, "k=v"],
    "label_remove": ["label", "remove", SID, "k=v"],
    "reconcile": ["reconcile"],
    "prune": ["prune", "--throwaways"],
    "export": ["export"],
    "cron_install": ["cron", "install"],
    "cron_uninstall": ["cron", "uninstall"],
}


def test_minimal_table_covers_every_registered_command() -> None:
    assert set(MINIMAL) == {c.key for c in COMMANDS}


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


@pytest.mark.parametrize("key", sorted(MINIMAL))
def test_unimplemented_commands_exit_1_internal(
    capsys: pytest.CaptureFixture[str], key: str
) -> None:
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


@pytest.mark.parametrize("argv", HUMAN_ONLY_COMMANDS)
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
    assert r.code == 1  # reached the handler as an automated actor


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
    r = run_cli(capsys, "--json", "--actor", "human:a", "remove", SID, "--force")
    assert r.code == 1
    assert r.json()["error"]["code"] == "internal"
    r = run_cli(capsys, "--json", "remove", SID, "--force", tty=True)
    assert r.code == 1


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
