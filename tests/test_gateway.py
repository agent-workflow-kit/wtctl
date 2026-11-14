from __future__ import annotations

import datetime as dt
import sys

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


def test_system_process_identity_of_self_is_stable_and_unknown_pid_is_none() -> None:
    import os

    ident = gateway.system_process_identity()
    mine = ident.start_time(os.getpid())
    assert mine and mine == ident.start_time(os.getpid())
    assert ident.start_time(2**22 + 12345) is None


def test_system_process_identity_differs_between_processes() -> None:
    import os
    import subprocess

    ident = gateway.system_process_identity()
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        theirs = ident.start_time(p.pid)
        assert theirs and theirs != ident.start_time(os.getpid())
    finally:
        p.kill()
        p.wait()
    assert ident.start_time(p.pid) is None  # reaped


def test_zombie_counts_as_not_running() -> None:
    import subprocess
    import time

    ident = gateway.system_process_identity()
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and ident.start_time(p.pid) is not None:
            time.sleep(0.02)  # exited but not yet reaped: a zombie
        assert ident.start_time(p.pid) is None
    finally:
        p.wait()


def test_ps_fallback_parses_pins_tz_and_locale_and_reports_missing() -> None:
    import subprocess

    seen: list[list[str]] = []

    def fake(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        if argv[-1] == "5":
            return subprocess.CompletedProcess(argv, 0, "Ss   Mon Jan  5 10:00:00 2026\n", "")
        if argv[-1] == "7":
            return subprocess.CompletedProcess(argv, 0, "Z+   Mon Jan  5 10:00:00 2026\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "")

    ident = gateway.PsProcessIdentity(runner=fake)
    assert ident.start_time(5) == "Mon Jan  5 10:00:00 2026"
    assert ident.start_time(6) is None
    assert ident.start_time(7) is None  # zombie
    assert seen[0][:3] == ["env", "TZ=UTC", "LC_ALL=C"]
    assert seen[0][3:] == ["ps", "-o", "stat=,lstart=", "-p", "5"]
