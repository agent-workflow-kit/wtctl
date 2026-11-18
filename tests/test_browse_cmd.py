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
    assert a[:3] == [sys.executable, "-m", "wtctl"]
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


_ = Run
