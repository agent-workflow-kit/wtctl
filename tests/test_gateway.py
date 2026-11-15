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
