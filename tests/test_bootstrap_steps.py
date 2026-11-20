"""Bootstrap step 3 and 5: the environment, the steps, timeouts, output (docs/interface.md)."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tests.bootlib import Repo
from tests.test_step_runner import alive, reap_pid_file, wait_gone
from wtctl import bootstrapper as b
from wtctl import gateway


@pytest.fixture
def r(tmp_path: Path, private_home: Path) -> Repo:
    return Repo(tmp_path, private_home)


def cfg(tokens: tuple[str, ...] = ("GH_TOKEN",), mask: tuple[str, ...] = ()) -> NS:
    return NS(
        hosts={f"h{i}": NS(token_env=t) for i, t in enumerate(tokens)} | {"x": NS(token_env=None)},
        mask_env=list(mask),
    )


def settings(steps=(), pass_env=(), env=None) -> NS:  # noqa: ANN001
    return NS(bootstrap=NS(steps=list(steps), pass_env=list(pass_env), env=dict(env or {})))


def step(argv: list[str], sh: bool = False, timeout: int = 30) -> NS:
    return NS(argv=argv, sh=sh, timeout=timeout)


def py(code: str, **kw) -> NS:  # noqa: ANN003
    return step([sys.executable, "-c", code], **kw)


def run(r: Repo, steps: list[NS], env: dict[str, str] | None = None, **kw):  # noqa: ANN003, ANN201
    base = env if env is not None else {"PATH": os.environ["PATH"]}
    warns: list[str] = []
    names = kw.pop("mask", ())
    caller = kw.pop("caller", base)
    values = sorted(b.mask_values(kw.pop("config", cfg((), names)), caller, base))
    with r.open() as wt:
        out = b.run_steps(steps, env=base, cwd=wt.cwd, mask=values, warn=warns.append, **kw)
    return out, warns


# -- the environment -----------------------------------------------------------------------


def test_environment_drops_every_token_variable_and_keeps_the_rest() -> None:
    env = b.step_env(
        cfg(("GH_TOKEN", "OTHER_TOKEN")),
        {"GH_TOKEN": "s1", "OTHER_TOKEN": "s2", "KEEP": "k", "PATH": "/usr/bin"},
        settings(),
        record_id="wt-aaaaaaaa",
        path="/w/x",
        repo="o/r",
    )
    assert "GH_TOKEN" not in env and "OTHER_TOKEN" not in env
    assert env["KEEP"] == "k"
    assert env["WTCTL_ID"] == "wt-aaaaaaaa" and env["WTCTL_PATH"] == "/w/x"
    assert env["WTCTL_REPO"] == "o/r"


def test_pass_env_gives_back_named_variables_only_when_set() -> None:
    environ = {"GH_TOKEN": "s1", "PATH": "/usr/bin"}
    env = b.step_env(
        cfg(),
        environ,
        settings(pass_env=["GH_TOKEN", "NOT_SET"]),
        record_id="wt-aaaaaaaa",
        path="/w",
        repo="r",
    )
    assert env["GH_TOKEN"] == "s1" and "NOT_SET" not in env


def test_bootstrap_env_adds_and_wtctl_variables_win() -> None:
    env = b.step_env(
        cfg(),
        {"WTCTL_ID": "evil", "A": "caller"},
        settings(env={"A": "cfg", "WTCTL_PATH": "evil"}),
        record_id="wt-aaaaaaaa",
        path="/w",
        repo="r",
    )
    assert env["A"] == "cfg"
    assert env["WTCTL_ID"] == "wt-aaaaaaaa" and env["WTCTL_PATH"] == "/w"


def test_relative_and_empty_path_entries_are_dropped() -> None:
    env = b.step_env(
        cfg(),
        {"PATH": ".:/usr/bin::bin:/bin:./x"},
        settings(),
        record_id="wt-aaaaaaaa",
        path="/w",
        repo="r",
    )
    assert env["PATH"] == "/usr/bin:/bin"


def test_a_repo_executable_is_not_found_through_a_relative_path_entry(r: Repo) -> None:
    exe = r.write("mytool", "#!/bin/sh\necho PWNED > pwned\n")
    exe.chmod(0o755)
    env = b.step_env(
        cfg(),
        {"PATH": f".:{os.environ['PATH']}"},
        settings(),
        record_id="wt-aaaaaaaa",
        path=str(r.wt),
        repo="r",
    )
    out, _ = run(r, [step(["mytool"])], env=env)
    steps, failed = out
    assert failed and steps[0]["exit"] is None  # not found: never executed
    assert not (r.wt / "pwned").exists()


def test_no_repository_file_is_read_or_sourced(r: Repo) -> None:
    for name in (".env", ".envrc", ".tool-versions", "Makefile", "package.json"):
        r.write(name, "FROM_REPO=1\n$(touch pwned)\nexport EVIL=1\n")
    code = "import os; print('EVIL' in os.environ, 'FROM_REPO' in os.environ)"
    (steps, failed), _ = run(r, [py(code)])
    assert not failed and steps[0]["output_tail"].strip() == "False False"
    assert not (r.wt / "pwned").exists()


# -- running steps -------------------------------------------------------------------------


def test_steps_run_in_order_in_the_worktree_with_empty_stdin(r: Repo) -> None:
    code = "import os,sys; print(os.getcwd()); print(repr(sys.stdin.read()))"
    (steps, failed), _ = run(r, [py(code), py("print('two')")])
    assert not failed and [s["exit"] for s in steps] == [0, 0]
    lines = steps[0]["output_tail"].splitlines()
    assert lines[0] == str(r.wt) and lines[1] == "''"
    assert steps[1]["output_tail"] == "two\n"
    assert all(isinstance(s["duration_ms"], int) and s["duration_ms"] >= 0 for s in steps)


def test_step_records_the_configured_argv(r: Repo) -> None:
    (steps, _), _ = run(r, [step(["echo", "hi"])])
    assert steps[0]["argv"] == ["echo", "hi"]


def test_sh_true_runs_the_string_through_a_shell(r: Repo) -> None:
    (steps, failed), _ = run(r, [step(["echo $((1+2)) && exit 3"], sh=True)])
    assert failed and steps[0]["exit"] == 3 and steps[0]["output_tail"] == "3\n"
    assert steps[0]["argv"] == ["echo $((1+2)) && exit 3"]


def test_without_sh_there_is_no_shell_expansion(r: Repo) -> None:
    (steps, _), _ = run(r, [step(["echo", "$HOME;", "`id`"])])
    assert steps[0]["output_tail"] == "$HOME; `id`\n"


def test_the_first_failing_step_stops_the_rest(r: Repo) -> None:
    (steps, failed), _ = run(r, [py("print(1)"), py("raise SystemExit(5)"), py("print(3)")])
    assert failed
    assert [s["exit"] for s in steps] == [0, 5]


def test_stderr_and_stdout_are_one_combined_stream(r: Repo) -> None:
    code = "import sys; print('o1'); sys.stdout.flush(); print('e1', file=sys.stderr); print('o2')"
    (steps, _), _ = run(r, [py(code)])
    assert steps[0]["output_tail"] == "o1\ne1\no2\n"


def test_a_command_that_cannot_start_fails_the_step(r: Repo) -> None:
    (steps, failed), _ = run(r, [step(["/no/such/program"])])
    assert failed and steps[0]["exit"] is None and "No such file" in steps[0]["output_tail"]


def test_a_step_killed_by_a_signal_has_no_exit_status(r: Repo) -> None:
    (steps, failed), _ = run(r, [py("import os,signal; os.kill(os.getpid(), signal.SIGKILL)")])
    assert failed and steps[0]["exit"] is None


# -- output -----------------------------------------------------------------------------------


def test_output_tail_is_the_last_4096_bytes(r: Repo) -> None:
    (steps, _), _ = run(r, [py("print('a' * 100000); print('END')")])
    tail = steps[0]["output_tail"]
    assert len(tail.encode()) == 4096 and tail.endswith("a\nEND\n")


def test_binary_and_hostile_output_stays_a_valid_string(r: Repo) -> None:
    code = "import sys; sys.stdout.buffer.write(b'\\xff\\xfe\\x1b[31m\\x00\\xe2\\x80')"
    (steps, _), _ = run(r, [py(code)])
    assert isinstance(steps[0]["output_tail"], str)
    steps[0]["output_tail"].encode("utf-8")  # no lone surrogates


def test_a_flood_of_output_does_not_stall_or_fill_memory(r: Repo) -> None:
    t0 = time.monotonic()
    (steps, _), _ = run(r, [py("import sys\nfor _ in range(200):\n sys.stdout.write('x'*100000)")])
    assert time.monotonic() - t0 < 30 and len(steps[0]["output_tail"]) <= 4096


def test_mask_env_values_are_masked_everywhere_in_the_tail(r: Repo) -> None:
    env = {"PATH": os.environ["PATH"], "SECRET_A": "hunter2-token", "SECRET_B": "other-secret"}
    code = "import os; print(os.environ['SECRET_A']); print('x' + os.environ['SECRET_B'] + 'y')"
    (steps, _), _ = run(r, [py(code)], env=env, mask=("SECRET_A", "SECRET_B", "UNSET"))
    tail = steps[0]["output_tail"]
    assert "hunter2" not in tail and "other-secret" not in tail and "***" in tail


def test_a_secret_straddling_the_tail_boundary_is_not_partly_shown(r: Repo) -> None:
    secret = "S3CRETVALUE" * 4
    env = {"PATH": os.environ["PATH"], "SEC": secret}
    code = "import os; print('z' * (4096 - 20) + os.environ['SEC'] + 'tail')"
    (steps, _), _ = run(r, [py(code)], env=env, mask=("SEC",))
    tail = steps[0]["output_tail"]
    assert "S3CRET" not in tail and "CRETVAL" not in tail and "***" in tail
    assert len(tail.encode()) <= 4096


def test_token_values_are_masked_even_when_passed_and_not_listed() -> None:
    values = b.mask_values(cfg(("GH_TOKEN",)), {"GH_TOKEN": "tok-123", "A": "a"})
    assert "tok-123" in values and "a" not in values


def test_mask_values_reads_the_caller_environ_and_the_step_env() -> None:
    config = cfg(("GH_TOKEN",), mask=("SEC",))
    caller = {"GH_TOKEN": "caller-token", "SEC": "caller-sec"}
    step_env = {"GH_TOKEN": "cfg-token", "SEC": "cfg-sec", "OTHER": "o"}
    assert b.mask_values(config, caller, step_env) == {
        "caller-token", "caller-sec", "cfg-token", "cfg-sec"
    }  # fmt: skip


def test_a_token_env_value_in_the_caller_environ_is_masked_via_pass_env(r: Repo) -> None:
    config = cfg(("GH_TOKEN",))
    caller = {"PATH": os.environ["PATH"], "GH_TOKEN": "tok-pass-123"}
    env = b.step_env(
        config,
        caller,
        settings(pass_env=["GH_TOKEN"]),
        record_id="wt-aaaaaaaa",
        path="/w",
        repo="r",
    )
    (steps, _), _ = run(
        r,
        [py("import os; print('t=' + os.environ['GH_TOKEN'])")],
        env=env,
        caller=caller,
        config=config,
    )
    assert "tok-pass-123" not in steps[0]["output_tail"] and "t=***" in steps[0]["output_tail"]


def test_a_token_env_value_set_by_bootstrap_env_is_masked(r: Repo) -> None:
    config = cfg(("GH_TOKEN",))
    caller = {"PATH": os.environ["PATH"], "GH_TOKEN": "caller-tok-1"}
    env = b.step_env(
        config,
        caller,
        settings(env={"GH_TOKEN": "cfg-tok-2"}),
        record_id="wt-aaaaaaaa",
        path="/w",
        repo="r",
    )
    code = "import os; print(os.environ['GH_TOKEN'])"
    (steps, _), _ = run(
        r,
        [py(code), py("print('caller-tok-1')")],
        env=env,
        caller=caller,
        config=config,
    )
    assert [s["output_tail"] for s in steps] == ["***\n", "***\n"]


def test_a_token_straddling_the_cut_stays_masked_when_from_the_caller(r: Repo) -> None:
    secret = "TOKVALUE9" * 5
    caller = {"PATH": os.environ["PATH"], "GH_TOKEN": secret}
    env = b.step_env(
        cfg(("GH_TOKEN",)),
        caller,
        settings(pass_env=["GH_TOKEN"]),
        record_id="wt-aaaaaaaa",
        path="/w",
        repo="r",
    )
    code = "import os; print('z' * (4096 - 20) + os.environ['GH_TOKEN'] + 'tail')"
    (steps, _), _ = run(r, [py(code)], env=env, caller=caller, config=cfg(("GH_TOKEN",)))
    tail = steps[0]["output_tail"]
    assert "TOKVAL" not in tail and "KVALUE9" not in tail and len(tail.encode()) <= 4096


# -- R1: an emptied PATH -------------------------------------------------------------------


@pytest.mark.parametrize("value", ["bin:./x", ".:rel", "", ":"])
def test_a_path_with_no_absolute_entry_is_removed_from_the_step_env(value: str) -> None:
    for how in ("environ", "bootstrap_env"):
        env = b.step_env(
            cfg(),
            {"PATH": value} if how == "environ" else {},
            settings(env={"PATH": value} if how == "bootstrap_env" else None),
            record_id="wt-aaaaaaaa",
            path="/w",
            repo="r",
        )
        assert "PATH" not in env


@pytest.mark.parametrize("sh", [False, True])
@pytest.mark.parametrize("value", ["bin:./x", ".:rel"])
def test_a_repo_executable_named_like_the_command_does_not_run_without_a_path(
    r: Repo, sh: bool, value: str
) -> None:
    exe = r.write("mytool", "#!/bin/sh\necho PWNED > pwned\n")
    exe.chmod(0o755)
    env = b.step_env(
        cfg(),
        {"PATH": value},
        settings(),
        record_id="wt-aaaaaaaa",
        path=str(r.wt),
        repo="r",
    )
    (steps, failed), _ = run(r, [step(["mytool"], sh=sh)], env=env)
    assert failed and steps[0]["exit"] != 0
    assert not (r.wt / "pwned").exists()


# -- timeout and process groups --------------------------------------------------------------


def test_a_timeout_terminates_then_kills_the_whole_group(
    r: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "TERM_GRACE", 1.0)
    gc = tmp_path / "gc.pid"
    cmd = (
        f"(trap '' TERM; exec sleep 300) >/dev/null 2>&1 </dev/null & echo $! > {gc}; "
        "trap '' TERM; while :; do sleep 0.1; done"
    )
    with reap_pid_file(gc):
        t0 = time.monotonic()
        (steps, failed), _ = run(r, [step([cmd], sh=True, timeout=1), py("print('never')")])
        assert failed and len(steps) == 1 and steps[0]["exit"] is None
        assert time.monotonic() - t0 < 15
        assert wait_gone(int(gc.read_text()))


def test_a_step_that_handles_term_gets_its_grace(r: Repo, tmp_path: Path) -> None:
    marker = tmp_path / "cleaned"
    cmd = f"trap 'echo done > {marker}; exit 9' TERM; while :; do sleep 0.1; done"
    (steps, failed), _ = run(r, [step([cmd], sh=True, timeout=1)])
    assert failed and marker.exists()


def test_a_grandchild_left_behind_after_a_clean_exit_is_not_chased(r: Repo, tmp_path: Path) -> None:
    pid = tmp_path / "bg.pid"
    cmd = f"(exec sleep 300) >/dev/null 2>&1 </dev/null & echo $! > {pid}"
    with reap_pid_file(pid):
        (steps, failed), _ = run(r, [step([cmd], sh=True)])
        assert not failed and alive(int(pid.read_text()))  # only a timeout kills the group


def test_on_tick_runs_during_a_long_step(r: Repo, tmp_path: Path) -> None:
    # The step stays alive until the second tick has fired (bounded at 60s), so a stalled test
    # process only delays the ticks instead of letting the step end before they happen.
    ticks: list[int] = []
    gate = tmp_path / "two-ticks"

    def tick() -> None:
        ticks.append(1)
        if len(ticks) >= 2:
            gate.touch()

    script = (
        "import os, time\n"
        "end = time.monotonic() + 60\n"
        f"while not os.path.exists({str(gate)!r}) and time.monotonic() < end:\n"
        "    time.sleep(0.05)\n"
    )
    run(r, [py(script)], on_tick=tick)
    assert len(ticks) >= 2


def test_the_runner_gets_term_grace_and_merged_output(r: Repo) -> None:
    seen: dict = {}

    def fake(argv, **kw):  # noqa: ANN001, ANN202
        seen.update(kw)
        return gateway.StepResult(0, stdout=b"ok\n", pid=1, pgid=1)

    run(r, [step(["x"], timeout=77)], runner=fake)
    assert seen["term_grace"] == b.TERM_GRACE and seen["merge_stderr"] is True
    assert seen["timeout"] == 77 and seen["cwd"]
    assert signal  # keep the import used


# -- R8: a lost lease ----------------------------------------------------------------------


class FakeLease:
    def __init__(self) -> None:
        self.lost = False

    def renew(self) -> None:
        from wtctl.errors import WtctlError

        self.lost = True
        raise WtctlError("busy", "lease was lost to another process")


def test_lease_ticker_reports_a_lost_lease_and_steps_stop(r: Repo) -> None:
    lease = FakeLease()
    t0 = time.monotonic()
    (steps, failed), _ = run(r, [py("import time; time.sleep(60)")], on_tick=b.lease_ticker(lease))
    assert lease.lost and failed and steps[0]["exit"] is None
    assert time.monotonic() - t0 < 20


def test_lease_ticker_is_quiet_while_the_lease_is_held() -> None:
    class Held:
        lost = False

        def renew(self) -> None:
            pass

    assert b.lease_ticker(Held())() is False
