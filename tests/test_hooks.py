"""Hook execution: order, veto, escalation, override, timeouts, decision ids, payloads."""

from __future__ import annotations

import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.schemacheck import validate
from tests.test_step_runner import alive, reap_pid_file, wait_gone
from wtctl import gateway, hooks
from wtctl.actor import Actor
from wtctl.config import Config, load_config
from wtctl.errors import UsageError, WtctlError
from wtctl.hooks import HookSpec

HUMAN = Actor("human:alice")
AUTO = Actor("auto:policy")
ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def spec(cmd: str, timeout: int = 10, is_async: bool = False) -> HookSpec:
    return HookSpec(cmd=cmd, timeout=timeout, is_async=is_async)


def py(code: str) -> str:
    return f"{sys.executable} -c {json.dumps(code)}"


def pre(
    specs: list[HookSpec],
    *,
    actor: Actor = AUTO,
    override: bool = False,
    existing: tuple[str, ...] = (),
    payload: dict[str, Any] | None = None,
) -> hooks.PreHookOutcome:
    return hooks.run_pre_hooks(
        "pre-create",
        specs,
        payload if payload is not None else {"tool": "wtctl"},
        env=ENV,
        actor=actor,
        override_escalation=override,
        decision_ids=existing,
    )


def err_of(specs: list[HookSpec], **kw: Any) -> WtctlError:
    with pytest.raises(WtctlError) as ei:
        pre(specs, **kw)
    return ei.value


# --- configuration, order --------------------------------------------------------------------


def test_configured_hooks_use_config_order_then_flag_hooks(private_home: Path) -> None:
    base = private_home / ".config" / "wtctl"
    (base / "config.d").mkdir(parents=True)
    (base / "config.yaml").write_text(
        "hooks:\n  pre-create: [{cmd: one, timeout: 5}]\n  post-create: [{cmd: p1, async: true}]\n"
    )
    (base / "config.d" / "a.yaml").write_text("hooks:\n  pre-create: [{cmd: two}, {cmd: three}]\n")
    (base / "config.d" / "b.yaml").write_text("hooks:\n  pre-create: [{cmd: four, timeout: 7}]\n")
    cfg = load_config(None)
    got = hooks.configured_hooks(cfg, "pre-create", extra=["five", "six"])
    assert [(h.cmd, h.timeout) for h in got] == [
        ("one", 5),
        ("two", 30),
        ("three", 30),
        ("four", 7),
        ("five", 30),
        ("six", 30),
    ]
    post = hooks.configured_hooks(cfg, "post-create")
    assert [(h.cmd, h.is_async) for h in post] == [("p1", True)]
    assert hooks.configured_hooks(cfg, "post-remove", extra=["x"]) == [spec("x", 30)]


@pytest.mark.parametrize("name", list(hooks.HOOK_NAMES))
def test_every_hook_name_maps_to_a_config_list(name: str) -> None:
    assert hooks.configured_hooks(Config(), name) == []


def test_unknown_hook_name_is_a_programmer_error() -> None:
    with pytest.raises(ValueError):
        hooks.configured_hooks(Config(), "on-create")


def test_hook_flags_parse_name_equals_command() -> None:
    got = hooks.parse_hook_flags(["pre-create=a b", "pre-create=c", "post-remove=x=y"])
    assert got == {"pre-create": ["a b", "c"], "post-remove": ["x=y"]}


@pytest.mark.parametrize(
    "bad",
    ["", "pre-create", "pre-create=", "on-create=x", "=x", "PRE-CREATE=x", "pre-create=\x00x"],
)
def test_bad_hook_flags_are_usage_errors(bad: str) -> None:
    with pytest.raises(UsageError):
        hooks.parse_hook_flags([bad])


def test_hooks_run_in_order_and_receive_the_payload_on_stdin(tmp_path: Path) -> None:
    log = tmp_path / "log"
    code = f"import sys; open({str(log)!r}, 'a').write(sys.argv[1] + sys.stdin.read() + '\\n')"
    specs = [spec(f"{sys.executable} -c {json.dumps(code)} {n}") for n in "abc"]
    out = pre(specs, payload={"tool": "wtctl", "k": "vé\n"})
    payload = json.dumps({"tool": "wtctl", "k": "vé\n"})
    assert log.read_text().splitlines() == [f"{n}{payload}" for n in "abc"]
    assert [r.exit_code for r in out.runs] == [0, 0, 0]


def test_payload_is_ascii_json_even_for_hostile_text(tmp_path: Path) -> None:
    got = tmp_path / "in"
    code = f"import sys; open({str(got)!r}, 'wb').write(sys.stdin.buffer.read())"
    pre([spec(py(code))], payload={"tool": "wtctl", "x": "\x1b[2J‮\ud800"})
    raw = got.read_bytes()
    assert raw.isascii()
    assert json.loads(raw)["x"] == "\x1b[2J‮\ud800"


# --- outcomes --------------------------------------------------------------------------------


def test_exit_zero_continues() -> None:
    out = pre([spec("exit 0"), spec("true")])
    assert out.decision_ids == ()
    assert len(out.runs) == 2


@pytest.mark.parametrize("code", [1, 2, 11, 12, 14, 127, 255])
def test_any_other_nonzero_exit_vetoes_with_exit_11(code: int) -> None:
    e = err_of([spec(f"echo no >&2; exit {code}")])
    assert (e.code, e.exit_code) == ("vetoed", 11)
    assert e.fields["hook"] == f"echo no >&2; exit {code}"
    assert e.fields["hook_stderr"] == "no\n"


def test_veto_stops_later_hooks(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    e = err_of([spec("exit 1"), spec(f"touch {marker}")])
    assert e.code == "vetoed"
    assert not marker.exists()


def test_a_signalled_hook_vetoes() -> None:
    e = err_of([spec("kill -TERM $$")])
    assert e.code == "vetoed"


def test_a_hook_that_cannot_start_vetoes_for_a_pre_hook() -> None:
    def runner(*a: Any, **kw: Any) -> gateway.StepResult:
        return gateway.StepResult(None, start_error="OSError: boom")

    with pytest.raises(WtctlError) as ei:
        hooks.run_pre_hooks(
            "pre-create", [spec("x")], {}, env=ENV, actor=AUTO, override_escalation=False,
            runner=runner,
        )  # fmt: skip
    assert ei.value.code == "vetoed"
    assert "boom" in ei.value.fields["hook_stderr"]


def test_exit_13_escalates_after_running_the_remaining_hooks(tmp_path: Path) -> None:
    marker = tmp_path / "later"
    e = err_of([spec("echo first >&2; exit 13"), spec(f"touch {marker}"), spec("exit 13")])
    assert (e.code, e.exit_code) == ("escalated", 13)
    assert marker.exists()
    assert e.fields["escalated_hooks"] == ["echo first >&2; exit 13", "exit 13"]
    assert e.fields["hook"] == "echo first >&2; exit 13"
    assert e.fields["hook_stderr"] == "first\n"


def test_a_veto_after_an_escalation_wins() -> None:
    e = err_of([spec("exit 13"), spec("echo stop >&2; exit 1")])
    assert e.code == "vetoed"
    assert e.fields["hook_stderr"] == "stop\n"


def test_override_escalation_counts_13_as_allow_for_a_human() -> None:
    out = pre([spec("exit 13"), spec("exit 0")], actor=HUMAN, override=True)
    assert [r.exit_code for r in out.runs] == [13, 0]
    assert any("escalation" in w and "overrid" in w for w in out.warnings)


def test_override_escalation_does_not_override_a_veto() -> None:
    e = err_of([spec("exit 13"), spec("exit 1")], actor=HUMAN, override=True)
    assert e.code == "vetoed"


def test_override_escalation_does_not_override_a_timeout_veto() -> None:
    e = err_of([spec("sleep 30", timeout=1)], actor=HUMAN, override=True)
    assert e.code == "vetoed"


def test_override_escalation_without_a_human_actor_is_refused_before_running(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "ran"
    with pytest.raises(UsageError):
        pre([spec(f"touch {marker}")], actor=AUTO, override=True)
    assert not marker.exists()


def test_without_the_flag_a_human_actor_still_escalates() -> None:
    assert err_of([spec("exit 13")], actor=HUMAN).code == "escalated"


def test_veto_after_an_earlier_hook_exposes_runs_and_warnings() -> None:
    e = err_of([spec("echo not-json-but-fine; echo '{\"decision_id\": 5}'"), spec("exit 1")])
    assert e.code == "vetoed"
    runs = e.hook_runs  # type: ignore[attr-defined]
    assert len(runs) == 2
    assert runs[0].exit_code == 0 and runs[1].exit_code == 1
    assert isinstance(e.hook_warnings, tuple)  # type: ignore[attr-defined]
    assert "hook_runs" not in e.to_object()["error"]


def test_escalation_exposes_runs_and_warnings() -> None:
    e = err_of([spec("true"), spec("exit 13")])
    assert e.code == "escalated" and len(e.hook_runs) == 2  # type: ignore[attr-defined]
    assert e.hook_warnings == ()  # type: ignore[attr-defined]


# --- timeouts --------------------------------------------------------------------------------


def test_pre_hook_timeout_vetoes_and_kills_the_whole_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "gc.pid"
    with reap_pid_file(pidfile):
        t0 = time.monotonic()
        e = err_of([spec(f"sleep 300 & echo $! > {pidfile}; wait", timeout=1)])
        assert time.monotonic() - t0 < 10
        assert e.code == "vetoed"
        assert "timed out" in e.fields["hook_stderr"]
        assert wait_gone(int(pidfile.read_text()))


def test_post_hook_timeout_is_a_warning_and_kills_the_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "gc.pid"
    with reap_pid_file(pidfile):
        out = hooks.run_post_hooks(
            "post-create", [spec(f"sleep 300 & echo $! > {pidfile}; wait", timeout=1)], {},
            env=ENV,
        )  # fmt: skip
        assert out.runs[0].timed_out
        assert any("timed out" in w for w in out.warnings)
        assert wait_gone(int(pidfile.read_text()))


# --- post hooks ------------------------------------------------------------------------------


def test_post_hook_failures_are_warnings_and_later_hooks_still_run(tmp_path: Path) -> None:
    marker = tmp_path / "later"
    out = hooks.run_post_hooks(
        "post-remove", [spec("echo bad >&2; exit 1"), spec("exit 13"), spec(f"touch {marker}")], {},
        env=ENV,
    )  # fmt: skip
    assert marker.exists()
    assert len(out.warnings) == 2
    assert all("post-remove" in w for w in out.warnings)


def test_post_hook_start_failure_is_a_warning() -> None:
    out = hooks.run_post_hooks(
        "post-remove", [spec("x")], {}, env=ENV,
        runner=lambda *a, **k: gateway.StepResult(None, start_error="OSError: boom"),
    )  # fmt: skip
    assert out.warnings and "boom" in out.warnings[0]


def test_post_hook_start_failure_text_is_not_repeated() -> None:
    out = hooks.run_post_hooks(
        "post-remove", [spec("x")], {}, env=ENV,
        runner=lambda *a, **k: gateway.StepResult(None, start_error="OSError: boom"),
    )  # fmt: skip
    assert out.warnings[0].count("boom") == 1


def test_post_hook_incomplete_output_is_a_warning(tmp_path: Path) -> None:
    pidfile = tmp_path / "holder.pid"
    with reap_pid_file(pidfile):
        out = hooks.run_post_hooks(
            "post-create", [spec(f"sleep 300 & echo $! > {pidfile}")], {}, env=ENV,
        )  # fmt: skip
        assert any("output incomplete" in w for w in out.warnings)


def test_async_post_hook_that_cannot_be_spawned_is_a_warning() -> None:
    out = hooks.run_post_hooks("post-create", [spec("echo a\0b", is_async=True)], {}, env=ENV)
    assert len(out.warnings) == 1 and "did not start" in out.warnings[0]
    assert out.runs[0].start_error is not None and not out.runs[0].launched_async


def test_async_post_hook_overflowing_timeout_is_a_warning() -> None:
    def spawner(*a: Any, **k: Any) -> int:
        raise OverflowError("too big")

    out = hooks.run_post_hooks(
        "post-create", [spec("x", is_async=True)], {}, env=ENV, spawner=spawner
    )
    assert "did not start" in out.warnings[0]


def test_post_hooks_collect_decision_ids() -> None:
    out = hooks.run_post_hooks("post-create", [spec('echo \'{"decision_id": "d1"}\'')], {}, env=ENV)
    assert out.decision_ids == ("d1",)


def test_async_hook_is_launched_detached_with_the_payload(tmp_path: Path) -> None:
    got = tmp_path / "got"
    code = f"import sys; open({str(got)!r}, 'w').write(sys.stdin.read())"
    t0 = time.monotonic()
    out = hooks.run_post_hooks(
        "post-create", [spec(f"sleep 5; {py(code)}", is_async=True)], {"tool": "wtctl"},
        env=ENV,
    )  # fmt: skip
    # The hook sleeps 5 s before writing; returning long before that proves it was detached.
    assert time.monotonic() - t0 < 3
    assert not got.exists()
    assert out.runs[0].launched_async
    assert out.warnings == ()
    deadline = time.monotonic() + 20
    while not got.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    assert json.loads(got.read_text()) == {"tool": "wtctl"}


def test_async_hook_timeout_kills_its_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "gc.pid"
    with reap_pid_file(pidfile):
        hooks.run_post_hooks(
            "post-create", [spec(f"sleep 300 & echo $! > {pidfile}; wait", 1, is_async=True)], {},
            env=ENV,
        )  # fmt: skip
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert wait_gone(int(pidfile.read_text()), 10)


def test_async_is_never_used_for_pre_hooks(tmp_path: Path) -> None:
    marker = tmp_path / "done"
    with pytest.raises(WtctlError):
        pre([spec(f"sleep 0.3; touch {marker}; exit 1", is_async=True)])
    assert marker.exists()


# --- environment -----------------------------------------------------------------------------


def test_environment_drops_every_token_env_variable_and_sets_the_wtctl_ones() -> None:
    cfg = Config.model_validate(
        {
            "hosts": {
                "github.com": {"token_env": "GH_TOK"},
                "git.example.org": {"token_env": "OTHER_TOK", "token_user": "u"},
                "plain.example.org": {},
            }
        }
    )
    environ = {
        "PATH": "/bin",
        "GH_TOK": "secret1",
        "OTHER_TOK": "secret2",
        "KEEP": "k",
        "WTCTL_HOOK": "stale",
        "WTCTL_ID": "stale",
    }
    env = hooks.hook_env(
        cfg, environ, hook="pre-create", object_id="wt-abcdefgh", object_type="worktree"
    )
    assert env == {
        "PATH": "/bin",
        "KEEP": "k",
        "WTCTL_HOOK": "pre-create",
        "WTCTL_ID": "wt-abcdefgh",
        "WTCTL_OBJECT_TYPE": "worktree",
    }
    assert "GH_TOK" in environ  # the caller's mapping is not modified


def test_environment_for_a_hook_without_an_object_id() -> None:
    env = hooks.hook_env(
        Config(), {}, hook="post-reconcile", object_id=None, object_type="reconcile"
    )
    assert env["WTCTL_ID"] == ""
    assert env["WTCTL_OBJECT_TYPE"] == "reconcile"


def test_a_running_hook_sees_no_token_variables(tmp_path: Path) -> None:
    cfg = Config.model_validate({"hosts": {"github.com": {"token_env": "WTCTL_T_TOK"}}})
    os.environ["WTCTL_T_TOK"] = "secret"
    try:
        env = hooks.hook_env(
            cfg, os.environ, hook="pre-create", object_id="wt-aaaaaaaa", object_type="worktree"
        )
    finally:
        del os.environ["WTCTL_T_TOK"]
    out = tmp_path / "env"
    code = f"import os, json; open({str(out)!r}, 'w').write(json.dumps(dict(os.environ)))"
    hooks.run_pre_hooks(
        "pre-create", [spec(py(code))], {}, env=env, actor=AUTO, override_escalation=False
    )
    seen = json.loads(out.read_text())
    assert "WTCTL_T_TOK" not in seen
    assert seen["WTCTL_HOOK"] == "pre-create"
    assert "secret" not in out.read_text()


# --- decision ids ----------------------------------------------------------------------------


def test_decision_id_string_and_list_are_captured_from_every_hook() -> None:
    out = pre(
        [
            spec('echo \'{"decision_id": "a.1"}\''),
            spec('echo \'{"decision_id": ["b:2", "c/3"], "other": 1}\''),
            spec("echo not json"),
            spec("echo '{}'"),
        ],
        existing=("cli-1",),
    )
    assert out.decision_ids == ("cli-1", "a.1", "b:2", "c/3")


def test_invalid_decision_ids_are_dropped_with_a_warning() -> None:
    bad = ["", "x y", "a" * 129, "x\ny", "é", 5, None, ["nested"], "ok-1"]
    line = json.dumps({"decision_id": bad})
    out = pre([spec(f"printf %s {shlex.quote(line)}")])
    assert out.decision_ids == ("ok-1",)
    assert len(out.warnings) >= 1


def test_decision_id_of_the_wrong_type_is_ignored_with_a_warning() -> None:
    out = pre([spec('echo \'{"decision_id": {"a": 1}}\''), spec("echo '{\"decision_id\": 7}'")])
    assert out.decision_ids == ()
    assert len(out.warnings) == 2


def test_a_boundary_length_decision_id_is_accepted() -> None:
    ok = "a" * 128
    out = pre([spec(f'echo \'{{"decision_id": "{ok}"}}\'')])
    assert out.decision_ids == (ok,)


def test_decision_ids_beyond_32_in_total_are_dropped_with_a_warning() -> None:
    existing = tuple(f"e{i}" for i in range(30))
    line = json.dumps({"decision_id": ["n1", "n2", "n3", "n4"]})
    out = pre([spec(f"printf %s {shlex.quote(line)}")], existing=existing)
    assert out.decision_ids == (*existing, "n1", "n2")
    assert any("32" in w for w in out.warnings)


def test_duplicate_decision_ids_are_recorded_once() -> None:
    out = pre([spec('echo \'{"decision_id": "d"}\''), spec('echo \'{"decision_id": ["d"]}\'')])
    assert out.decision_ids == ("d",)


def test_decision_ids_from_a_vetoing_hook_are_reported_in_the_error() -> None:
    e = err_of([spec('echo \'{"decision_id": "d9"}\'; echo why >&2; exit 1')], existing=("cli",))
    assert e.fields["decision_ids"] == ["cli", "d9"]


def test_error_has_no_decision_ids_when_hooks_returned_none() -> None:
    assert "decision_ids" not in err_of([spec("exit 1")], existing=("cli",)).fields


def test_decision_ids_in_the_escalation_error() -> None:
    e = err_of([spec('echo \'{"decision_id": "esc"}\'; exit 13')])
    assert e.fields["decision_ids"] == ["esc"]


def test_json_with_surrounding_whitespace_is_accepted() -> None:
    out = pre([spec('printf \'\\n  {"decision_id": "w"}  \\n\\n\'')])
    assert out.decision_ids == ("w",)


@pytest.mark.parametrize(
    "stdout",
    [
        "{",
        '{"decision_id": "a"',
        "[1,2]",
        '"decision_id"',
        "null",
        '{"decision_id": NaN}',
        '{"decision_id": "a"} trailing',
        "[" * 100000,
    ],
)
def test_invalid_json_output_yields_no_ids_and_never_crashes(stdout: str) -> None:
    out = pre([spec(f"printf %s {json.dumps(stdout)}")])
    assert out.decision_ids == ()


def test_huge_stdout_is_capped_and_its_ids_are_ignored() -> None:
    code = "import sys; sys.stdout.write('{\"decision_id\": \"' + 'a' * 5_000_000 + '\"}')"
    out = pre([spec(py(code))])
    assert out.decision_ids == ()
    assert any("truncated" in w or "too large" in w for w in out.warnings)
    assert out.runs[0].exit_code == 0


def test_output_just_under_the_cap_still_parses() -> None:
    pad = " " * (hooks.MAX_STDOUT - 40)
    code = f'import sys; sys.stdout.write({pad!r} + \'{{"decision_id": "edge"}}\')'
    out = pre([spec(py(code))])
    assert out.decision_ids == ("edge",)


def test_hostile_stderr_is_escaped_and_cut_to_4096_bytes() -> None:
    code = (
        "import sys; sys.stderr.buffer.write(b'\\x1b[2J\\x07\\xff\\xfe' + "
        "'\\u202e'.encode() + b'A' * 100000)"
    )
    e = err_of([spec(f"{py(code)}; exit 1")])
    text = e.fields["hook_stderr"]
    assert len(text.encode()) <= 4096
    assert "\x1b" not in text and "\x07" not in text and "‮" not in text
    assert text.startswith("\\x1b[2J\\x07")


def test_hook_stderr_of_a_veto_keeps_newlines_but_no_control_characters() -> None:
    e = err_of([spec("printf 'line1\\nline2\\r\\n\\x00z' >&2; exit 1")])
    text = e.fields["hook_stderr"]
    assert text.startswith("line1\nline2")
    assert "\r" not in text and "\x00" not in text


def test_a_hook_that_forks_and_closes_stdout_does_not_hang_or_mislead(tmp_path: Path) -> None:
    pidfile = tmp_path / "bg.pid"
    with reap_pid_file(pidfile):
        t0 = time.monotonic()
        out = pre(
            [
                spec(
                    f"(sleep 300 >/dev/null 2>&1 </dev/null & echo $! > {pidfile}); "
                    'echo \'{"decision_id": "fk"}\''
                )
            ]
        )
        assert time.monotonic() - t0 < 5
        assert out.decision_ids == ("fk",)
        assert alive(int(pidfile.read_text()))


def test_a_hook_that_keeps_the_pipes_open_after_exiting_is_bounded(tmp_path: Path) -> None:
    pidfile = tmp_path / "holder.pid"
    with reap_pid_file(pidfile):
        t0 = time.monotonic()
        out = pre([spec(f"sleep 300 & echo $! > {pidfile}")])
        assert time.monotonic() - t0 < 10
        assert out.runs[0].exit_code == 0
        assert any("output" in w for w in out.warnings)


# --- payloads --------------------------------------------------------------------------------

SHA = "a" * 40
TS = "2026-01-02T03:04:05Z"


def record(object_type: str = "worktree") -> dict[str, Any]:
    wt = object_type == "worktree"
    return {
        "id": "wt-abcdefgh",
        "object_type": object_type,
        "owned": True if wt else None,
        "state": "active",
        "repo": "git.example.org/team/proj",
        "clone_path": "/clones/proj" if wt else None,
        "common_dir": "/clones/proj/.git" if wt else None,
        "admin_dir": "/clones/proj/.git/worktrees/w" if wt else None,
        "common_id": "1:2" if wt else None,
        "dir_id": "1:3",
        "base_dir": "/wt",
        "root_id": None if wt else "1:4",
        "path": "/wt/w",
        "name": "w" if wt else None,
        "branch": "wt/w" if wt else None,
        "created_branch": True if wt else None,
        "base": "main" if wt else None,
        "base_sha": SHA if wt else None,
        "head_at_create": SHA if wt else None,
        "ref": None if wt else SHA,
        "source": None if wt else "local",
        "depth": None if wt else 50,
        "fetch_url": "https://git.example.org/team/proj.git",
        "remote": "origin" if wt else None,
        "labels": [],
        "holders": [],
        "sparse": None,
        "baseline_digest": "0" * 64 if wt else None,
        "bootstrap": {"state": "none", "steps": [], "created_paths_count": 0} if wt else None,
        "created": TS,
        "updated": TS,
        "missing_since": None,
        "removed_at": None,
        "last_error": None,
    }


def base(hook: str, object_type: str = "worktree") -> dict[str, Any]:
    return {
        "object_type": object_type,
        "object_id": "wt-abcdefgh",
        "repo": "git.example.org/team/proj",
        "labels": ["a=b", "c=d"],
    }


def test_payload_builders_produce_schema_valid_payloads() -> None:
    cases: dict[str, dict[str, Any]] = {
        "pre-create": {
            **base("pre-create"),
            "clone_path": "/clones/proj",
            "path": "/wt/w",
            "branch": "wt/w",
            "branch_action": "new",
            "base": "main",
            "ref": None,
            "source": None,
            "actor": "auto:x",
            "decision_ids": ["d1"],
        },
        "post-create": {**base("post-create"), "record": record(), "bootstrap": None},
        "pre-remove": {
            **base("pre-remove"),
            "record": record(),
            "checks": {},
            "merged_head": None,
            "overridden": ["held"],
            "actor": "human:alice",
            "decision_ids": [],
        },
        "post-remove": {
            **base("post-remove"),
            "record": record(),
            "branch_deleted": True,
            "branch_kept_reason": None,
            "overridden": [],
        },
        "post-baseline-accept": {
            **base("post-baseline-accept"),
            "record": record(),
            "items": [],
            "actor": "human:alice",
        },
        "post-reconcile": {
            "object_type": "reconcile",
            "object_id": None,
            "repo": None,
            "labels": [],
            "changes": [{"id": "wt-abcdefgh", "from": "active", "to": "gone", "action": "repair"}],
            "reports": [],
        },
    }
    assert set(cases) == set(hooks.HOOK_NAMES)
    for name, fields in cases.items():
        payload = hooks.build_payload(name, **fields)
        assert payload["tool"] == "wtctl"
        assert payload["hook"] == name
        validate(f"wtctl/hooks/{name}.json", payload)
        json.dumps(payload, allow_nan=False)


def test_a_throwaway_payload_is_schema_valid() -> None:
    payload = hooks.build_payload(
        "pre-create",
        **{
            **base("pre-create", "throwaway"),
            "clone_path": None,
            "path": "/tw/x",
            "branch": None,
            "branch_action": None,
            "base": None,
            "ref": SHA,
            "source": "auto",
            "actor": "auto:x",
            "decision_ids": [],
        },
    )
    validate("wtctl/hooks/pre-create.json", payload)


def test_payload_builder_rejects_missing_and_extra_fields() -> None:
    with pytest.raises(ValueError, match="missing"):
        hooks.build_payload("post-remove", **base("post-remove"), record=record())
    with pytest.raises(ValueError, match="unexpected"):
        hooks.build_payload(
            "post-reconcile", object_type="reconcile", object_id=None, repo=None, labels=[],
            changes=[], reports=[], extra=1,
        )  # fmt: skip
    with pytest.raises(ValueError):
        hooks.build_payload("nope", **base("x"))


def test_an_invalid_payload_is_caught_by_the_schema_check() -> None:
    bad = hooks.build_payload(
        "post-reconcile", object_type="reconcile", object_id=None, repo=None, labels=[],
        changes=[], reports=[],
    )  # fmt: skip
    with pytest.raises(Exception):  # noqa: B017 - jsonschema.ValidationError
        validate("wtctl/hooks/post-reconcile.json", bad)  # minItems: 1 on changes


def test_sigkill_constant_is_what_the_runner_uses() -> None:
    # Documentation test: the timeout path is SIGKILL (see gateway.run_step).
    r = gateway.run_step(["/bin/sh", "-c", "sleep 30"], env=ENV, timeout=0.5)
    assert r.returncode == -signal.SIGKILL
