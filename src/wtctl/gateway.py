"""Gateway: the single boundary for subprocess, ``/proc`` and clock access.

Every call to an external program (or HTTP endpoint) goes through this module so that
tests can substitute fakes (see design guide §14). Commands are always run with an argv
list, never through a shell. The clock and the process scanner are seams: ``Gateway`` holds
the ones a command uses, and tests pass fakes.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, Protocol, cast


def run(
    argv: list[str],
    *,
    stdin: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` without a shell and capture its text output."""
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# TODO(#9): the hardened git runner (clean environment, overrides) builds on ``run``.
# TODO(#16): the step runner (process groups, timeouts) is added here.


class Clock(Protocol):
    def now(self) -> dt.datetime:
        """Current time, timezone-aware."""
        ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class ProcessInfo:
    """One process found by a scan (see docs/interface.md, In-use detection)."""

    pid: int
    command: str
    cwd: str | None
    open_paths: tuple[str, ...] = ()


class ScannerUnavailable(Exception):
    """The platform's process information could not be read."""


class ProcessScanner(Protocol):
    def scan(self) -> list[ProcessInfo]: ...


class UnavailableScanner:
    """Placeholder until the in-use detection lands: scanning always fails closed."""

    # TODO(#19): read /proc (Linux) or run lsof (macOS) here.
    def scan(self) -> list[ProcessInfo]:
        raise ScannerUnavailable("process scanning is not implemented yet")


@dataclass
class Gateway:
    clock: Clock = field(default_factory=SystemClock)
    scanner: ProcessScanner = field(default_factory=UnavailableScanner)
    runner: Callable[..., subprocess.CompletedProcess[str]] = run

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return self.runner(argv, **kwargs)


_PR_SET_DUMPABLE = 4


def disable_dumpable() -> bool:
    """Linux: ``prctl(PR_SET_DUMPABLE, 0)`` so other same-uid processes cannot read our memory.

    Best effort; returns whether the flag was cleared (False elsewhere or on failure).
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------------------------
# Step runner (issue #6). A separate block on purpose: other issues edit the top of this file.
#
# ``run_step`` runs one command in its own session and process group, with a timeout, capped
# output and an exact environment. Hooks use it now; bootstrap steps (#16) and token commands
# reuse it. It never uses a shell unless the caller passes ``sh_argv(cmd)``.
#
# Signalling: only the group this function created, by the leader pid captured at launch. The
# leader is waited for without reaping (``waitid`` with ``WNOWAIT``) while we may still need to
# signal, so the group id cannot be recycled under us. Nothing is ever killed by pattern.
# ---------------------------------------------------------------------------------------------

_READ_CHUNK = 65536
_TICK = 0.05


@dataclass(frozen=True)
class StepResult:
    """Outcome of ``run_step``.

    ``returncode`` is None when the command could not be started (``start_error`` says why);
    a negative value means the leader died of that signal (``signal``). ``timed_out`` means the
    leader was still running at the deadline and its whole group was sent SIGKILL.
    ``output_incomplete`` means the leader exited but another process kept our pipes open, so
    we stopped reading after ``drain_grace`` seconds (that process is left alone).
    """

    returncode: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    output_incomplete: bool = False
    duration: float = 0.0
    pid: int | None = None
    pgid: int | None = None
    start_error: str | None = None

    @property
    def signal(self) -> int | None:
        return -self.returncode if self.returncode is not None and self.returncode < 0 else None


def sh_argv(cmd: str) -> list[str]:
    """``/bin/sh -c cmd`` (no template substitution; the caller owns the string)."""
    return ["/bin/sh", "-c", cmd]


class _Capture:
    def __init__(self, cap: int, tail: bool) -> None:
        self.cap = cap
        self.tail = tail
        self.buf = bytearray()
        self.truncated = False

    def add(self, data: bytes) -> None:
        if self.tail:
            self.buf += data
            if len(self.buf) > self.cap:
                del self.buf[: len(self.buf) - self.cap]
                self.truncated = True
            return
        room = self.cap - len(self.buf)
        if len(data) > room:
            self.truncated = True
        if room > 0:
            self.buf += data[:room]


def _leader_exited(proc: subprocess.Popen[bytes]) -> bool:
    """True once the leader has exited. Does not reap it where ``waitid`` exists."""
    if proc.returncode is not None:
        return True
    if hasattr(os, "waitid"):
        try:
            return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
    return proc.poll() is not None


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


_COLLECT_SECONDS = 0.2
_COLLECT_BYTES = 1 << 20


def _raise_system_exit(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def run_step(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None = None,
    stdin: bytes | None = None,
    timeout: float = 30.0,
    max_stdout: int = 65536,
    max_stderr: int = 65536,
    tail: bool = False,
    drain_grace: float = 1.0,
    on_start: Callable[[int], None] | None = None,
    on_tick: Callable[[], None] | None = None,
) -> StepResult:
    """Run ``argv`` (no shell) in a new session; see ``StepResult`` for the outcome.

    ``env`` is the complete environment (nothing is inherited). ``stdin`` is written without
    blocking (a child that never reads it is fine); None means an empty, closed input.
    ``max_stdout``/``max_stderr`` cap what is kept (the head, or the last bytes with
    ``tail=True``); the rest is read and discarded so the child never blocks on a full pipe.
    ``on_start`` receives the leader pid (= process group id) right after launch, for callers
    that record it. ``on_tick`` is called about once a second while waiting (lease renewal);
    its exceptions are ignored. A failure to start is a result, not an exception.
    """
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        return StepResult(None, start_error=f"{type(exc).__name__}: {exc}")
    pid = proc.pid
    out, err = _Capture(max_stdout, tail), _Capture(max_stderr, tail)
    timed_out = incomplete = False
    sel = selectors.DefaultSelector()
    saved: dict[int, object] = {}
    try:
        if threading.current_thread() is threading.main_thread():
            # A SIGTERM/SIGHUP to wtctl must not orphan the child's group: turn it into
            # SystemExit so the BaseException path below kills the group.
            for signum in (signal.SIGTERM, signal.SIGHUP):
                saved[signum] = signal.signal(signum, _raise_system_exit)
        if on_start is not None:
            on_start(pid)
        assert proc.stdout is not None and proc.stderr is not None
        for stream, cap in ((proc.stdout, out), (proc.stderr, err)):
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_READ, cap)
        pending = memoryview(stdin) if stdin else None
        if proc.stdin is not None:
            if pending:
                os.set_blocking(proc.stdin.fileno(), False)
                sel.register(proc.stdin, selectors.EVENT_WRITE, None)
            else:
                proc.stdin.close()
        deadline = t0 + timeout
        exited_at: float | None = None
        last_tick = t0
        while True:
            now = time.monotonic()
            if on_tick is not None and now - last_tick >= 1.0:
                last_tick = now
                try:
                    on_tick()
                except Exception:  # noqa: BLE001 - a heartbeat must not abort the step
                    pass
            if exited_at is None and _leader_exited(proc):
                exited_at = now
            if exited_at is not None and not sel.get_map():
                break
            if exited_at is not None and now - exited_at >= drain_grace:
                incomplete = True
                break
            if now >= deadline:
                if exited_at is None:
                    timed_out = True
                    _kill_group(pid)
                else:
                    incomplete = True
                break
            if not sel.get_map():
                time.sleep(_TICK)
                continue
            for key, _ in sel.select(timeout=_TICK):
                f = cast(IO[bytes], key.fileobj)
                if key.data is None:  # stdin
                    assert pending is not None
                    try:
                        n = os.write(f.fileno(), pending[:_READ_CHUNK])
                        pending = pending[n:]
                        done = not pending
                    except BlockingIOError:
                        done = False
                    except OSError:  # EPIPE: the child closed its stdin
                        done = True
                    if done:
                        sel.unregister(f)
                        f.close()
                    continue
                try:
                    data = os.read(f.fileno(), _READ_CHUNK)
                except BlockingIOError:
                    continue
                except OSError:
                    data = b""
                if data:
                    key.data.add(data)
                else:
                    sel.unregister(f)
                    f.close()
        if timed_out:
            # Collect what the dead group had already written, bounded in time and bytes (a
            # writer that escaped the group could otherwise keep the pipe full).
            collect_deadline = time.monotonic() + _COLLECT_SECONDS
            budget = _COLLECT_BYTES
            for key in list(sel.get_map().values()):
                if key.data is not None:
                    try:
                        while budget > 0 and time.monotonic() < collect_deadline:
                            data = os.read(key.fileobj.fileno(), _READ_CHUNK)  # type: ignore[union-attr]
                            if not data:
                                break
                            budget -= len(data)
                            key.data.add(data)
                    except OSError:
                        pass
    except BaseException:
        _kill_group(pid)
        raise
    finally:
        for signum, previous in saved.items():
            signal.signal(signum, previous if previous is not None else signal.SIG_DFL)  # type: ignore[arg-type]
        sel.close()
        for f in (proc.stdin, proc.stdout, proc.stderr):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        proc.wait()
    return StepResult(
        proc.returncode,
        stdout=bytes(out.buf),
        stderr=bytes(err.buf),
        stdout_truncated=out.truncated,
        stderr_truncated=err.truncated,
        timed_out=timed_out,
        output_incomplete=incomplete,
        duration=time.monotonic() - t0,
        pid=pid,
        pgid=pid,
    )


# Runs ``argv`` as a child and, past the timeout, SIGKILLs the whole group, itself included. It
# is the group leader (we start it in a new session), so the group is exactly the one we created.
_SUPERVISOR = """
import os, signal, subprocess, sys
p = subprocess.Popen(sys.argv[2:])
try:
    p.wait(timeout=float(sys.argv[1]))
except subprocess.TimeoutExpired:
    os.killpg(os.getpgrp(), signal.SIGKILL)
"""


def spawn_detached(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None = None,
    stdin: bytes | None = None,
    timeout: float = 30.0,
) -> int:
    """Start ``argv`` in its own session and return at once with the group id.

    Output is discarded and the result is never observed; ``timeout`` is enforced by a small
    supervisor that is the group leader, so a hung command and its descendants are killed even
    after we exit. Raises OSError if the supervisor cannot start.
    """
    with tempfile.TemporaryFile() as payload:
        payload.write(stdin or b"")
        payload.seek(0)
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _SUPERVISOR, repr(float(timeout)), *argv],
            stdin=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
        )
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it if we outlive it
    return proc.pid
