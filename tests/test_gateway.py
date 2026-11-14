from __future__ import annotations

import datetime as dt
import sys
from typing import Any

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


MOUNTINFO = (
    "22 28 0:21 / /sys rw,nosuid shared:7 - sysfs sysfs rw\n"
    "100 28 0:90 / /mnt/with\\040space rw,relatime shared:1 - tmpfs none rw,uid=1000\n"
    "101 28 0:91 / /mnt/fuse rw,nosuid,nodev - fuse.sshfs me@host:/ rw,user_id=1000\n"
    "102 28 8:1 /sub /mnt/bind rw - ext4 /dev/sda1 rw\n"
)


def test_parse_mountinfo_decodes_escapes_and_fields() -> None:
    entries = gateway.parse_mountinfo(MOUNTINFO)
    by_point = {e.mount_point: e for e in entries}
    assert by_point["/mnt/with space"].mount_id == 100
    assert by_point["/mnt/with space"].fstype == "tmpfs"
    assert by_point["/mnt/fuse"].fstype == "fuse.sshfs"
    assert by_point["/mnt/fuse"].source == "me@host:/"
    assert by_point["/mnt/bind"].source == "/dev/sda1"
    assert len(entries) == 4


def test_parse_mountinfo_skips_malformed_lines() -> None:
    assert gateway.parse_mountinfo("garbage\n\n1 2 3\n") == []


def test_parse_mount_command_output_macos_style() -> None:
    text = (
        "/dev/disk3s1 on / (apfs, local, journaled)\nmacfuse@0 on /Volumes/x y (macfuse, nodev)\n"
    )
    entries = gateway.parse_mount_command(text)
    assert [(e.mount_point, e.fstype) for e in entries] == [
        ("/", "apfs"),
        ("/Volumes/x y", "macfuse"),
    ]
    assert all(e.mount_id is None for e in entries)


def test_fd_mount_id_reads_fdinfo_on_linux(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import os

    if not sys.platform.startswith("linux"):
        pytest.skip("linux only")
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        mnt = gateway.fd_mount_id(fd)
    finally:
        os.close(fd)
    assert isinstance(mnt, int)


def test_run_with_deadline_returns_output() -> None:
    r = gateway.run_with_deadline(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        stdin=b"abc",
        timeout=30,
    )
    assert (r.returncode, r.stdout, r.timed_out) == (0, b"ABC", False)


def test_run_with_deadline_kills_exact_pid_on_expiry() -> None:
    import os

    started: list[int] = []
    r = gateway.run_with_deadline(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        timeout=0.5,
        on_start=started.append,
    )
    assert r.timed_out
    assert started == [r.pid]
    with pytest.raises(ProcessLookupError):  # reaped: the exact PID we started is gone
        os.kill(r.pid, 0)


def test_run_with_deadline_kills_the_child_when_on_start_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import signal
    import subprocess

    started: list[int] = []
    procs: list[Any] = []
    real_popen: Any = subprocess.Popen

    def capture(*a: Any, **k: Any) -> Any:
        p = real_popen(*a, **k)
        procs.append(p)
        return p

    monkeypatch.setattr(gateway.subprocess, "Popen", capture)

    def boom(pid: int) -> None:
        started.append(pid)
        raise RuntimeError("on_start failed")

    with pytest.raises(RuntimeError):
        gateway.run_with_deadline(
            [sys.executable, "-c", "import time; time.sleep(600)"], timeout=30, on_start=boom
        )
    alive = True
    try:
        os.kill(started[0], 0)
    except ProcessLookupError:
        alive = False
    if alive:  # only if the fix regressed: do not leak the sleeper
        os.kill(started[0], signal.SIGKILL)
    assert not alive
    p = procs[0]
    assert p.stdin is not None and p.stdout is not None and p.stderr is not None
    assert p.stdin.closed and p.stdout.closed and p.stderr.closed
