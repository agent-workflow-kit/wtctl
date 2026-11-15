"""The process-group step runner in gateway.py (hooks, and later bootstrap steps)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from wtctl import gateway
from wtctl.gateway import run_step, sh_argv

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def alive(pid: int) -> bool:
    """True while ``pid`` exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return True
    return state != "Z"


def wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


@contextmanager
def reap_pid_file(path: Path) -> Iterator[None]:
    """Kill, by its exact recorded pid, a process a test left running."""
    try:
        yield
    finally:
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            pid = 0
        if pid > 1 and alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_captures_output_and_exit_status() -> None:
    r = run_step(
        py("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"),
        env=ENV,
        timeout=10,
    )
    assert (r.returncode, r.stdout, r.stderr) == (3, b"out\n", b"err\n")
    assert not r.timed_out
    assert r.start_error is None


def test_sh_argv_uses_a_shell_and_does_not_substitute() -> None:
    r = run_step(sh_argv("echo $0-{x}; exit 4"), env=ENV, timeout=10)
    assert r.returncode == 4
    assert r.stdout == b"/bin/sh-{x}\n"


def test_environment_is_exactly_what_was_passed(tmp_path: Path) -> None:
    os.environ["WTCTL_TEST_LEAK"] = "leak"
    try:
        r = run_step(
            py("import os, json; print(json.dumps(sorted(os.environ)))"),
            env={"ONLY": "1"},
            timeout=10,
        )
    finally:
        del os.environ["WTCTL_TEST_LEAK"]
    names = json.loads(r.stdout)
    assert "ONLY" in names
    assert "WTCTL_TEST_LEAK" not in names
    assert "PATH" not in names and "HOME" not in names


def test_cwd(tmp_path: Path) -> None:
    r = run_step(py("import os; print(os.getcwd())"), env=ENV, cwd=str(tmp_path), timeout=10)
    assert Path(r.stdout.decode().strip()).resolve() == tmp_path.resolve()


def test_runs_in_its_own_session_and_group_and_reports_the_leader_pid() -> None:
    seen: list[int] = []
    r = run_step(
        py("import os; print(os.getpid(), os.getpgrp(), os.getsid(0))"),
        env=ENV,
        timeout=10,
        on_start=seen.append,
    )
    pid, pgrp, sid = (int(x) for x in r.stdout.split())
    assert pid == pgrp == sid == r.pid == r.pgid
    assert seen == [pid]
    assert pgrp != os.getpgrp()


def test_stdin_larger_than_the_pipe_buffer_is_delivered() -> None:
    data = b"x" * 1_000_000
    r = run_step(
        py("import sys; print(len(sys.stdin.buffer.read()))"), env=ENV, stdin=data, timeout=20
    )
    assert r.stdout.strip() == b"1000000"


def test_a_child_that_never_reads_stdin_is_not_an_error() -> None:
    r = run_step(py("pass"), env=ENV, stdin=b"y" * 1_000_000, timeout=10)
    assert r.returncode == 0
    assert not r.timed_out


def test_no_stdin_means_closed_empty_input() -> None:
    r = run_step(py("import sys; print(repr(sys.stdin.read()))"), env=ENV, timeout=10)
    assert r.stdout.strip() == b"''"


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    cmd = f"sleep 300 & echo $! > {pidfile}; wait"
    with reap_pid_file(pidfile):
        t0 = time.monotonic()
        r = run_step(sh_argv(cmd), env=ENV, timeout=1.0)
        assert time.monotonic() - t0 < 10
        assert r.timed_out
        assert r.returncode == -signal.SIGKILL
        grandchild = int(pidfile.read_text())
        assert wait_gone(grandchild)
    assert r.pid is not None
    assert not alive(r.pid)


def test_timeout_kills_a_grandchild_that_ignores_term_and_closed_its_pipes(tmp_path: Path) -> None:
    pidfile = tmp_path / "gc.pid"
    quiet = ">/dev/null 2>&1 </dev/null"
    cmd = f"(trap '' TERM HUP; exec sleep 300) {quiet} & echo $! > {pidfile}; sleep 300"
    with reap_pid_file(pidfile):
        r = run_step(sh_argv(cmd), env=ENV, timeout=1.0)
        assert r.timed_out
        assert wait_gone(int(pidfile.read_text()))


def test_a_signalled_child_reports_the_signal() -> None:
    r = run_step(py("import os, signal; os.kill(os.getpid(), signal.SIGTERM)"), env=ENV, timeout=10)
    assert r.returncode == -signal.SIGTERM
    assert r.signal == signal.SIGTERM
    assert not r.timed_out


def test_start_failure_is_reported_not_raised(tmp_path: Path) -> None:
    r = run_step(py("pass"), env=ENV, cwd=str(tmp_path / "missing"), timeout=5)
    assert r.returncode is None
    assert r.start_error
    assert r.pid is None
    r = run_step(["/nonexistent/program"], env=ENV, timeout=5)
    assert r.returncode is None
    assert r.start_error


def test_stdout_cap_keeps_the_head_and_never_blocks_the_child() -> None:
    r = run_step(
        py("import sys; sys.stdout.write('a' * 5_000_000 + 'END')"),
        env=ENV,
        timeout=30,
        max_stdout=1000,
    )
    assert r.returncode == 0
    assert r.stdout == b"a" * 1000
    assert r.stdout_truncated
    assert not r.stderr_truncated


def test_tail_mode_keeps_the_last_bytes() -> None:
    r = run_step(
        py("import sys; sys.stderr.write('a' * 100_000 + 'END')"),
        env=ENV,
        timeout=30,
        max_stderr=10,
        tail=True,
    )
    assert r.stderr == b"aaaaaaaEND"
    assert r.stderr_truncated


def test_output_under_the_cap_is_not_marked_truncated() -> None:
    r = run_step(py("print('x' * 10)"), env=ENV, timeout=10, max_stdout=11)
    assert not r.stdout_truncated


def test_binary_garbage_output_is_returned_as_bytes() -> None:
    r = run_step(py("import sys; sys.stdout.buffer.write(bytes(range(256)))"), env=ENV, timeout=10)
    assert r.stdout == bytes(range(256))


def test_a_daemonizing_child_that_closed_its_output_does_not_delay_us(tmp_path: Path) -> None:
    pidfile = tmp_path / "daemon.pid"
    cmd = f"(sleep 300 >/dev/null 2>&1 </dev/null & echo $! > {pidfile}); echo done"
    with reap_pid_file(pidfile):
        t0 = time.monotonic()
        r = run_step(sh_argv(cmd), env=ENV, timeout=30)
        assert time.monotonic() - t0 < 5
        assert r.stdout == b"done\n"
        assert not r.timed_out
        assert not r.output_incomplete
        # We do not kill what a finished leader left behind.
        assert alive(int(pidfile.read_text()))


def test_a_child_that_keeps_our_pipes_open_after_the_leader_exits(tmp_path: Path) -> None:
    pidfile = tmp_path / "holder.pid"
    cmd = f"sleep 300 & echo $! > {pidfile}; echo early"
    with reap_pid_file(pidfile):
        t0 = time.monotonic()
        r = run_step(sh_argv(cmd), env=ENV, timeout=30, drain_grace=0.3)
        assert time.monotonic() - t0 < 5
        assert r.returncode == 0
        assert r.stdout == b"early\n"
        assert r.output_incomplete
        assert not r.timed_out


def test_on_tick_runs_while_waiting() -> None:
    ticks: list[int] = []
    code = py("import time; time.sleep(1.3)")
    run_step(code, env=ENV, timeout=10, on_tick=lambda: ticks.append(1))
    assert len(ticks) >= 1


def test_on_tick_failure_does_not_abort_the_step() -> None:
    def boom() -> None:
        raise RuntimeError("tick")

    r = run_step(py("import time; time.sleep(1.2)"), env=ENV, timeout=10, on_tick=boom)
    assert r.returncode == 0


def test_duration_is_measured() -> None:
    r = run_step(py("import time; time.sleep(0.3)"), env=ENV, timeout=10)
    assert 0.25 <= r.duration < 5


def test_default_gateway_exposes_the_step_runner() -> None:
    assert gateway.run_step is run_step


def test_sigterm_to_the_waiting_process_kills_the_step_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "hook.pid"
    ready = tmp_path / "ready"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    code = (
        "import sys; from wtctl.gateway import run_step, sh_argv; "
        f"run_step(sh_argv('echo $$ > {pidfile}; sleep 300'), env={ENV!r}, timeout=600, "
        f"on_start=lambda p: open({str(ready)!r}, 'w').write('x'))"
    )
    env = {
        **ENV,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    child = subprocess.Popen([sys.executable, "-c", code], env=env, start_new_session=True)
    hook_pid = 0
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if ready.exists() and pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        else:
            raise AssertionError("step never became ready")
        hook_pid = int(pidfile.read_text())
        os.kill(child.pid, signal.SIGTERM)
        assert child.wait(timeout=20) == 128 + signal.SIGTERM
        assert wait_gone(hook_pid, 10)
    finally:
        if hook_pid > 1 and alive(hook_pid):
            try:
                os.killpg(hook_pid, signal.SIGKILL)  # the hook leads its own group
            except ProcessLookupError:
                pass
        if child.poll() is None:
            child.kill()
            child.wait()


def test_an_inherited_sig_ign_is_not_overridden_by_the_step(tmp_path: Path) -> None:
    pidfile = tmp_path / "hook.pid"
    ready = tmp_path / "ready"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    code = (
        "import signal; signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "from wtctl.gateway import run_step, sh_argv\n"
        f"run_step(sh_argv('echo $$ > {pidfile}; sleep 2'), env={ENV!r}, timeout=600, "
        f"on_start=lambda p: open({str(ready)!r}, 'w').write('x'))"
    )
    env = {
        **ENV,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home),
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    child = subprocess.Popen([sys.executable, "-c", code], env=env, start_new_session=True)
    hook_pid = 0
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if ready.exists() and pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.05)
        else:
            raise AssertionError("step never became ready")
        hook_pid = int(pidfile.read_text())
        os.kill(child.pid, signal.SIGHUP)
        assert child.wait(timeout=20) == 0  # ignored SIGHUP: the step finished normally
    finally:
        if hook_pid > 1 and alive(hook_pid):
            try:
                os.killpg(hook_pid, signal.SIGKILL)  # the hook leads its own group
            except ProcessLookupError:
                pass
        if child.poll() is None:
            child.kill()
            child.wait()


def test_signal_handlers_are_restored_after_a_step() -> None:
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
    run_step(py("pass"), env=ENV, timeout=10)
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == before


def test_a_flooding_writer_outside_the_group_cannot_stall_the_post_timeout_collect() -> None:
    cmd = (
        f"setsid {sys.executable} -c "
        "\"import sys\nwhile True: sys.stderr.buffer.write(b'x' * 65536); sys.stderr.flush()\" & "
        "sleep 300"
    )
    t0 = time.monotonic()
    r = run_step(sh_argv(cmd), env=ENV, timeout=1)
    assert r.timed_out
    assert time.monotonic() - t0 < 5
