"""``wtctl browse``: the handler, the terminal checks, the status child (docs/interface.md)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import Run
from tests.listhelpers import cli, seed
from tests.schemacheck import validate
from tests.statehelpers import make_state
from wtctl import browse_status, browse_ui
from wtctl.browse_model import Browser
from wtctl.gateway import DeadlineResult
from wtctl.state import State


@pytest.fixture
def st(tmp_path: Path) -> State:
    s = make_state(tmp_path)
    seed(s, "wt-aaaaaaaa", repo="example.com/o/alpha", labels=["team=x"])
    seed(s, "wt-bbbbbbbb", state="partial", repo="example.com/o/beta")
    return s


# --- terminal checks ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "expect"),
    [
        ({"stdin_isatty": False}, "terminal"),
        ({"stdout_isatty": False}, "terminal"),
        ({"term": None}, "TERM"),
        ({"term": ""}, "TERM"),
        ({"term": "dumb"}, "TERM"),
        ({"term": "DUMB"}, "TERM"),
        ({"has_curses": False}, "curses"),
    ],
)
def test_unsupported_reasons(kw: dict[str, Any], expect: str) -> None:
    args: dict[str, Any] = {
        "stdin_isatty": True,
        "stdout_isatty": True,
        "term": "xterm",
        "has_curses": True,
    }
    args.update(kw)
    assert expect in (browse_ui.unsupported_reason(**args) or "")


def test_supported_when_everything_is_present() -> None:
    assert (
        browse_ui.unsupported_reason(
            stdin_isatty=True, stdout_isatty=True, term="xterm-256color", has_curses=True
        )
        is None
    )


def test_not_a_tty_exits_2_before_opening_anything(
    capsys: pytest.CaptureFixture[str], st: State
) -> None:
    r = cli(capsys, st, "--json", "browse")  # --json is refused first
    assert r.code == 2
    validate("wtctl/cli/error.json", r.json())
    r = cli(capsys, st, "browse")
    assert r.code == 2 and r.out == ""
    assert "interactive only" in r.err


def test_json_is_a_usage_error(capsys: pytest.CaptureFixture[str], st: State) -> None:
    r = cli(capsys, st, "--json", "browse")
    assert r.json()["error"]["code"] == "usage"
    assert "no --json" in r.json()["error"]["message"]


def _tty_cli(capsys: pytest.CaptureFixture[str], st: State, *argv: str) -> Run:
    from wtctl.cli import main
    from wtctl.context import Runtime

    rt = Runtime(stdin_isatty=True, me_name="a", gateway=st.gateway, state_opener=lambda c: st)
    code = main(list(argv), runtime=rt)
    c = capsys.readouterr()
    return Run(code, c.out, c.err)


def test_dumb_terminal_is_refused(
    capsys: pytest.CaptureFixture[str], st: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(browse_ui, "isatty", lambda s: True)
    r = _tty_cli(capsys, st, "browse")
    assert r.code == 2 and "TERM" in r.err


def test_missing_curses_is_refused(
    capsys: pytest.CaptureFixture[str], st: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr(browse_ui, "isatty", lambda s: True)
    monkeypatch.setattr(browse_ui, "have_curses", lambda: False)
    r = _tty_cli(capsys, st, "browse")
    assert r.code == 2 and "curses" in r.err


def test_dry_run_is_not_accepted(capsys: pytest.CaptureFixture[str], st: State) -> None:
    assert cli(capsys, st, "browse", "--dry-run").code == 2


# --- the handler feeds the browser through list's code path --------------------------------


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[Browser]:
    seen: list[Browser] = []
    monkeypatch.setattr(browse_ui, "check_interactive", lambda **kw: None)
    monkeypatch.setattr(browse_ui, "run", lambda b: seen.append(b))
    return seen


def test_browser_shows_the_same_records_as_list(
    capsys: pytest.CaptureFixture[str], st: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture(monkeypatch)
    listed = [r["id"] for r in cli(capsys, st, "--json", "list").json()["records"]]
    r = cli(capsys, st, "browse")
    assert r.code == 0 and r.out == ""
    assert seen[0].visible_ids() == listed and len(listed) == 2


def test_list_filters_apply_to_browse(
    capsys: pytest.CaptureFixture[str], st: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture(monkeypatch)
    cli(capsys, st, "browse", "--repo", "example.com/o/beta", "--state", "partial")
    assert seen[0].visible_ids() == ["wt-bbbbbbbb"]
    cli(capsys, st, "browse", "--label", "team=x")
    assert seen[1].visible_ids() == ["wt-aaaaaaaa"]


def test_bad_filter_is_a_usage_error_before_the_ui(
    capsys: pytest.CaptureFixture[str], st: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture(monkeypatch)
    r = cli(capsys, st, "browse", "--state", "nonsense")
    assert r.code == 2 and seen == []


def test_browse_writes_nothing(
    capsys: pytest.CaptureFixture[str], st: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture(monkeypatch)
    before = [dict(r) for r in st.records()]
    cli(capsys, st, "browse")
    assert seen
    seen[0].handle_key("r")
    assert [dict(r) for r in st.records()] == before


# --- status child -------------------------------------------------------------------------

OK = {
    "id": "wt-aaaaaaaa",
    "would_remove": False,
    "reason": "dirty",
    "checks": {"dirty": {"status": "fail", "details": ["x"]}},
    "human_required": False,
    "warnings": ["w1"],
}


def test_parse_status_success_uses_the_status_human_text() -> None:
    out = browse_status.parse_status(0, json.dumps(OK).encode(), b"")
    assert out.ok
    assert out.lines[0] == "wt-aaaaaaaa: would NOT be removed (dirty)"
    assert "  dirty: fail (1)" in out.lines and "  warning: w1" in out.lines


def test_parse_status_error_object() -> None:
    err = {"error": {"code": "not_found", "message": "no such record", "reason": "x"}}
    out = browse_status.parse_status(10, json.dumps(err).encode(), b"")
    assert not out.ok and out.lines == ("not_found: no such record (x)",)


@pytest.mark.parametrize("stdout", [b"", b"not json", b"[1]", b'{"id": 1}', b"\xff\xfe", b"null"])
def test_parse_status_garbage_never_raises(stdout: bytes) -> None:
    for rc in (0, 1, 12):
        out = browse_status.parse_status(rc, stdout, b"boom\nsecond")
        assert not out.ok and out.lines


def test_parse_status_timeout_and_cancel() -> None:
    assert "did not finish" in browse_status.parse_status(None, b"", b"", timed_out=True).lines[0]
    assert browse_status.parse_status(None, b"", b"", cancelled=True).lines == ("cancelled",)


def test_subprocess_status_builds_a_safe_argv() -> None:
    a = browse_status.SubprocessStatus("/etc/c.yaml").argv("--evil")
    assert a[:4] == [sys.executable, "-I", "-m", "wtctl"]
    assert a[-3:] == ["status", "--", "--evil"] and "--config" in a
    assert "--config" not in browse_status.SubprocessStatus(None).argv("wt-aaaaaaaa")


def test_status_job_reports_the_child_result(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    def fake(argv: list[str], **kw: Any) -> DeadlineResult:
        return DeadlineResult(1, 0, json.dumps(OK).encode(), b"", False)

    monkeypatch.setattr(browse_status.gateway, "run_with_deadline", fake)
    job = browse_status.SubprocessStatus(None)("wt-aaaaaaaa")
    end = time.monotonic() + 10
    while job.poll() is None and time.monotonic() < end:
        time.sleep(0.01)
    out = job.poll()
    assert out is not None and out.ok


def test_status_job_survives_a_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    def boom(argv: list[str], **kw: Any) -> DeadlineResult:
        raise OSError("no exec")

    monkeypatch.setattr(browse_status.gateway, "run_with_deadline", boom)
    job = browse_status.SubprocessStatus(None)("wt-aaaaaaaa")
    end = time.monotonic() + 10
    while job.poll() is None and time.monotonic() < end:
        time.sleep(0.01)
    out = job.poll()
    assert out is not None and not out.ok and "no exec" in out.lines[0]


def test_real_status_child_against_a_seeded_database(tmp_path: Path) -> None:
    """The actual `python -m wtctl --json status` child, with HOME and XDG isolated."""
    import time

    from wtctl.state import state_path

    s = State(state_path(), make_state(tmp_path / "x").gateway)  # HOME/XDG: conftest's private tree
    seed(s, "wt-aaaaaaaa")
    s.close()
    job = browse_status.SubprocessStatus(None, timeout=120)("wt-aaaaaaaa")
    end = time.monotonic() + 150
    while job.poll() is None and time.monotonic() < end:
        time.sleep(0.05)
    out = job.poll()
    assert out is not None and out.ok, out
    assert out.lines[0].startswith("wt-aaaaaaaa:")


def _wait(job: Any, seconds: float = 20) -> Any:
    import time

    end = time.monotonic() + seconds
    while job.poll() is None and time.monotonic() < end:
        time.sleep(0.02)
    return job.poll()


def _gone(pid: int, seconds: float = 10) -> bool:
    import os
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_status_child_ignores_a_decoy_package_in_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl.state import state_path

    s = State(state_path(), make_state(tmp_path / "x").gateway)
    seed(s, "wt-aaaaaaaa")
    s.close()
    decoy = tmp_path / "decoy"
    (decoy / "wtctl").mkdir(parents=True)
    marker = tmp_path / "marker"
    (decoy / "wtctl" / "__init__.py").write_text("")
    (decoy / "wtctl" / "__main__.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n")
    monkeypatch.chdir(decoy)
    job = browse_status.SubprocessStatus(None, timeout=120)("wt-aaaaaaaa")
    out = _wait(job, 150)
    assert out is not None and out.ok, out
    assert not marker.exists()


_SLEEPER = [sys.executable, "-c", "import time; time.sleep(600)"]


def _start_sleeper_job() -> Any:
    import time

    job = browse_status._Job(_SLEEPER, 120)
    end = time.monotonic() + 20
    while job._pid is None and time.monotonic() < end:
        time.sleep(0.02)
    assert job._pid is not None
    return job


def test_job_cancel_kills_the_real_child() -> None:
    job = _start_sleeper_job()
    pid = job._pid
    assert pid is not None
    job.cancel()
    out = _wait(job)
    assert out is not None and out.lines == ("cancelled",)
    assert _gone(pid)


def test_job_close_kills_the_real_child() -> None:
    job = _start_sleeper_job()
    pid = job._pid
    assert pid is not None
    job.close()
    assert _gone(pid)


def test_job_close_kills_the_recorded_pid_when_the_worker_does_not_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    import time

    proc = subprocess.Popen(_SLEEPER)  # a child this test started

    def stuck(argv: list[str], *, on_start: Any, cancel: Any, **kw: Any) -> DeadlineResult:
        on_start(proc.pid)
        time.sleep(6)  # a worker that is not polling its cancel event
        return DeadlineResult(proc.pid, None, b"", b"", False, True)

    monkeypatch.setattr(browse_status.gateway, "run_with_deadline", stuck)
    try:
        job = browse_status._Job(["x"], 60)
        end = time.monotonic() + 10
        while job._pid is None and time.monotonic() < end:
            time.sleep(0.02)
        job.close(join_timeout=0.3)
        assert proc.wait(timeout=10) == -9  # SIGKILL to exactly the recorded pid
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_cancel_reaches_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(argv: list[str], **kw: Any) -> DeadlineResult:
        seen.update(kw)
        return DeadlineResult(1, 0, json.dumps(OK).encode(), b"", False)

    monkeypatch.setattr(browse_status.gateway, "run_with_deadline", fake)
    job = browse_status.SubprocessStatus(None)("wt-aaaaaaaa")
    _wait(job)
    assert seen["cancel"] is job._cancel and callable(seen["on_start"])


def test_job_timed_out_result_says_it_did_not_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(argv: list[str], **kw: Any) -> DeadlineResult:
        return DeadlineResult(1, None, b"", b"", True, False)

    monkeypatch.setattr(browse_status.gateway, "run_with_deadline", fake)
    out = _wait(browse_status.SubprocessStatus(None)("wt-aaaaaaaa"))
    assert out is not None and not out.ok and "did not finish" in out.lines[0]


def test_parse_status_timeout_or_failure_exit_wins_over_a_valid_object() -> None:
    body = json.dumps(OK).encode()
    assert not browse_status.parse_status(0, body, b"", timed_out=True).ok
    out = browse_status.parse_status(1, body, b"oops")
    assert not out.ok and "exit 1" in out.lines[0]


def test_parse_status_truncates_output_before_parsing() -> None:
    body = b" " * browse_status.MAX_OUTPUT + json.dumps(OK).encode()
    assert not browse_status.parse_status(0, body, b"").ok


def test_check_interactive_needs_a_tty_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    from wtctl.errors import UsageError

    monkeypatch.setattr(browse_ui, "isatty", lambda s: True)
    monkeypatch.setattr(browse_ui, "have_curses", lambda: True)
    with pytest.raises(UsageError, match="terminal"):
        browse_ui.check_interactive(stdin_isatty=False, env={"TERM": "xterm"})


def test_unknown_term_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from wtctl.errors import UsageError

    monkeypatch.setattr(browse_ui, "isatty", lambda s: True)
    with pytest.raises(UsageError, match="nosuchterm"):
        browse_ui.check_interactive(stdin_isatty=True, env={"TERM": "nosuchterm"})


def test_run_treats_ctrl_c_during_setup_as_a_quit(
    st: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    import curses

    def boom(*a: Any, **k: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(curses, "wrapper", boom)
    closed: list[bool] = []
    b = Browser(lambda: ([], []), lambda t: None, now=st.gateway.clock.now)  # type: ignore[arg-type,return-value]
    monkeypatch.setattr(b, "close", lambda: closed.append(True))
    browse_ui.run(b)
    assert closed == [True]


def test_run_maps_an_init_error_to_a_usage_error(
    st: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    import curses

    from wtctl.errors import UsageError

    def boom(*a: Any, **k: Any) -> None:
        raise curses.error("setupterm failed")

    monkeypatch.setattr(curses, "wrapper", boom)
    b = Browser(lambda: ([], []), lambda t: None, now=st.gateway.clock.now)  # type: ignore[arg-type,return-value]
    with pytest.raises(UsageError, match="TERM"):
        browse_ui.run(b)


def test_run_restores_signal_handlers_and_exits_on_sigterm(
    st: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    import curses
    import signal

    seen: dict[str, Any] = {}

    def fake_wrapper(*a: Any, **k: Any) -> None:
        seen["term"] = signal.getsignal(signal.SIGTERM)
        seen["hup"] = signal.getsignal(signal.SIGHUP)
        handler: Any = seen["term"]
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(curses, "wrapper", fake_wrapper)
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
    b = Browser(lambda: ([], []), lambda t: None, now=st.gateway.clock.now)  # type: ignore[arg-type,return-value]
    with pytest.raises(SystemExit) as ex:
        browse_ui.run(b)
    assert ex.value.code == 128 + signal.SIGTERM
    assert seen["hup"] is seen["term"]
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == before


def test_output_encoding_follows_the_c_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browse_ui.locale, "nl_langinfo", lambda item: "ANSI_X3.4-1968")
    assert browse_ui._output_encoding() == "ANSI_X3.4-1968"
    monkeypatch.setattr(browse_ui.locale, "nl_langinfo", lambda item: "no-such-codec")
    assert browse_ui._output_encoding() == "ascii"


_ = Run
