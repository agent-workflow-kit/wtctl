from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import time
from pathlib import Path
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
        def scan(self, target: str | None = None) -> list[gateway.ProcessInfo]:
            return [proc]

    gw = gateway.Gateway(clock=FakeClock(), scanner=FakeScanner())
    assert gw.clock.now().year == 2026
    assert gw.scanner.scan() == [proc]


def test_unavailable_scanner_fails_closed() -> None:
    with pytest.raises(gateway.ScannerUnavailable):
        gateway.UnavailableScanner().scan()


def test_default_scanner_is_the_system_one() -> None:
    from wtctl import procscan

    assert isinstance(gateway.Gateway().scanner, procscan.ProcScanner | procscan.LsofScanner)


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


def test_ps_fallback_treats_unexecutable_ps_as_unreadable() -> None:
    import subprocess

    import pytest

    def make(code: int):
        def fake(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, code, "", "")

        return fake

    for code in (126, 127):
        with pytest.raises(gateway.ProcessUnreadable):
            gateway.PsProcessIdentity(runner=make(code)).start_time(5)
    assert gateway.PsProcessIdentity(runner=make(1)).start_time(5) is None


def test_run_git_delivers_all_stdin_to_a_child_that_reads_late(tmp_path: Path) -> None:
    out = tmp_path / "out"
    start = time.monotonic()
    r = gateway.run_git(
        ["sh", "-c", "sleep 0.5; wc -c"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        stdin=b"x" * 5_000_000,
        stdout_path=str(out),
        max_stdout_bytes=10**9,
        timeout=10,
    )
    assert not r.timed_out and r.returncode == 0
    assert out.read_text().strip() == "5000000"
    assert time.monotonic() - start < 5


def test_run_git_stdin_of_a_child_that_reads_late_without_an_output_file(tmp_path: Path) -> None:
    r = gateway.run_git(
        ["sh", "-c", "sleep 0.3; wc -c"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        stdin=b"y" * 1_000_000,
        timeout=10,
    )
    assert r.returncode == 0 and r.stdout.strip() == b"1000000"


def test_run_git_kills_and_reaps_the_child_on_an_interrupt(tmp_path: Path) -> None:
    import signal

    pidfile = tmp_path / "pid"
    script = f"echo $$ > {pidfile}; exec sleep 60"

    def interrupt(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    old = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        with pytest.raises(KeyboardInterrupt):
            gateway.run_git(["/bin/sh", "-c", script], env=ENV, cwd=str(tmp_path), timeout=30)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):  # dead and reaped: no zombie, no sleeping child
        os.kill(pid, 0)


def test_run_with_deadline_cancel_kills_the_exact_child() -> None:
    import threading

    cancel = threading.Event()
    started: list[int] = []

    def on_start(pid: int) -> None:
        started.append(pid)
        threading.Timer(0.3, cancel.set).start()

    t0 = time.monotonic()
    r = gateway.run_with_deadline(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        timeout=60,
        on_start=on_start,
        cancel=cancel,
    )
    assert r.cancelled and not r.timed_out and r.returncode is None
    assert time.monotonic() - t0 < 20
    with pytest.raises(ProcessLookupError):
        os.kill(r.pid, 0)


def test_run_with_deadline_cancel_unset_still_returns_output_and_expires() -> None:
    import threading

    cancel = threading.Event()
    ok = gateway.run_with_deadline(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        stdin=b"xyz",
        timeout=30,
        cancel=cancel,
    )
    assert (ok.stdout, ok.cancelled, ok.timed_out) == (b"xyz", False, False)
    slow = gateway.run_with_deadline(
        [sys.executable, "-c", "import time; time.sleep(600)"], timeout=0.5, cancel=cancel
    )
    assert slow.timed_out and not slow.cancelled


_STUBBORN = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "time.sleep(600)\n"
)  # ignores SIGTERM: only SIGKILL stops it


def test_run_with_deadline_kills_a_sigterm_ignoring_child_on_expiry() -> None:
    r = gateway.run_with_deadline([sys.executable, "-c", _STUBBORN], timeout=1.0)
    assert r.timed_out
    with pytest.raises(ProcessLookupError):
        os.kill(r.pid, 0)


def test_run_with_deadline_kills_a_sigterm_ignoring_child_on_cancel() -> None:
    import threading

    cancel = threading.Event()
    r = gateway.run_with_deadline(
        [sys.executable, "-c", _STUBBORN],
        timeout=60,
        on_start=lambda pid: threading.Timer(0.5, cancel.set).start(),
        cancel=cancel,
    )
    assert r.cancelled
    with pytest.raises(ProcessLookupError):
        os.kill(r.pid, 0)


_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen(['sleep', '20'])\n"  # inherits the pipes, outlives the killed child
    "open(sys.argv[1], 'w').write(str(g.pid))\n"
    "time.sleep(600)\n"
)


def _grandchild_run(
    tmp_path: Any, *, cancel_after_start: bool, timeout: float
) -> tuple[Any, float]:
    import threading

    pidfile = tmp_path / "gpid"
    cancel = threading.Event()
    cancelled_at: list[float] = []

    def on_start(pid: int) -> None:
        if not cancel_after_start:
            return

        def wait_then_cancel() -> None:
            end = time.monotonic() + 30
            while not pidfile.exists() and time.monotonic() < end:
                time.sleep(0.02)
            cancelled_at.append(time.monotonic())
            cancel.set()

        threading.Thread(target=wait_then_cancel, daemon=True).start()

    t0 = time.monotonic()
    try:
        r = gateway.run_with_deadline(
            [sys.executable, "-c", _GRANDCHILD, str(pidfile)],
            timeout=timeout,
            on_start=on_start,
            cancel=cancel,
        )
        end = time.monotonic()
        # cancel runs: measure from the moment the cancel was set, not from before Popen
        took = end - (cancelled_at[0] if cancelled_at else t0)
    finally:
        if pidfile.exists():  # the grandchild this test started: do not leave it sleeping
            try:
                os.kill(int(pidfile.read_text()), 9)
            except (ProcessLookupError, ValueError):
                pass
    assert pidfile.exists()
    return r, took


def test_cancel_reaps_within_the_short_bound_despite_a_grandchild(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "_REAP_TIMEOUT", 6.0)
    monkeypatch.setattr(gateway, "_CANCEL_REAP_TIMEOUT", 0.3)
    r, took = _grandchild_run(tmp_path, cancel_after_start=True, timeout=60)
    assert r.cancelled and r.stdout == b"" and r.returncode is None
    assert took < 0.3 + 3  # cancel-to-return: short bound plus margin; the long bound is 6 s
    with pytest.raises(ProcessLookupError):
        os.kill(r.pid, 0)  # the exact child is killed and reaped


def test_timeout_path_keeps_the_long_reap_bound(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway, "_REAP_TIMEOUT", 2.0)
    monkeypatch.setattr(gateway, "_CANCEL_REAP_TIMEOUT", 0.1)
    r, took = _grandchild_run(tmp_path, cancel_after_start=False, timeout=4)
    assert r.timed_out and not r.cancelled
    assert took >= 4 + 1.8  # the grandchild keeps the pipes open for the whole long bound


def test_reap_bounds_are_ten_seconds_and_one_second() -> None:
    assert gateway._REAP_TIMEOUT == 10.0 and gateway._CANCEL_REAP_TIMEOUT == 1.0
