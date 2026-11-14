from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from wtctl import gateway


def test_run_uses_argv_without_a_shell() -> None:
    r = gateway.run([sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME; echo x"])
    assert r.returncode == 0
    assert r.stdout.strip() == "$HOME; echo x"


def test_run_passes_stdin_and_does_not_raise_on_failure() -> None:
    r = gateway.run(
        [sys.executable, "-c", "import sys; sys.exit(sys.stdin.read() == 'hi')"], stdin="hi"
    )
    assert r.returncode == 1


def test_default_gateway_has_system_clock() -> None:
    gw = gateway.Gateway()
    assert gw.clock.now().tzinfo is not None
    assert gw.clock.monotonic() <= gw.clock.monotonic()


def test_fake_clock_and_scanner_can_be_substituted() -> None:
    class FakeClock:
        def now(self) -> dt.datetime:
            return dt.datetime(2026, 1, 2, tzinfo=dt.UTC)

        def monotonic(self) -> float:
            return 5.0

    proc = gateway.ProcessInfo(pid=7, command="sh", cwd="/w", open_paths=("/w/a",))

    class FakeScanner:
        def scan(self) -> list[gateway.ProcessInfo]:
            return [proc]

    gw = gateway.Gateway(clock=FakeClock(), scanner=FakeScanner())
    assert gw.clock.now().year == 2026
    assert gw.scanner.scan() == [proc]


def test_default_scanner_reports_unavailable_until_implemented() -> None:
    with pytest.raises(gateway.ScannerUnavailable):
        gateway.Gateway().scanner.scan()


def test_disable_dumpable_clears_the_flag_in_a_child_process() -> None:
    # A child process, so the pytest process itself stays dumpable (conftest no-ops the call).
    if not sys.platform.startswith("linux"):
        pytest.skip("Linux only")
    code = (
        "import ctypes, sys; from wtctl import gateway; ok = gateway.disable_dumpable();"
        "libc = ctypes.CDLL(None); print(ok, libc.prctl(3, 0, 0, 0, 0))"
    )
    r = gateway.run([sys.executable, "-c", code])
    assert r.stdout.split() == ["True", "0"], r.stderr


# -- run_git: bounded waiting after a kill (review round 1, item 6) ------------------------------

needs_setsid = pytest.mark.skipif(not shutil.which("setsid"), reason="needs setsid")
ENV = {"PATH": "/usr/bin:/bin"}


def _kill_recorded(pidfile: Path) -> None:
    # The pid was written by the script this test launched; signal exactly that one.
    try:
        os.kill(int(pidfile.read_text()), 9)
    except (FileNotFoundError, ValueError, ProcessLookupError):
        pass


@needs_setsid
def test_escaped_descendant_cannot_hang_a_timed_out_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "KILL_GRACE", 0.5)
    pidfile = tmp_path / "escaped.pid"
    script = f"setsid sleep 60 & echo $! > {pidfile}; echo partial; exec sleep 60"
    try:
        start = time.monotonic()
        r = gateway.run_git(["/bin/sh", "-c", script], env=ENV, cwd=str(tmp_path), timeout=1.0)
        assert time.monotonic() - start < 15
        assert r.timed_out
        assert b"partial" in r.stdout  # what was read before the kill is kept
    finally:
        _kill_recorded(pidfile)


@needs_setsid
def test_git_that_exited_before_the_deadline_is_not_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "KILL_GRACE", 0.5)
    pidfile = tmp_path / "escaped.pid"
    script = f"setsid sleep 60 & echo $! > {pidfile}; echo done; exit 0"
    try:
        r = gateway.run_git(["/bin/sh", "-c", script], env=ENV, cwd=str(tmp_path), timeout=1.0)
        assert (r.returncode, r.timed_out) == (0, False)
        assert b"done" in r.stdout
    finally:
        _kill_recorded(pidfile)
