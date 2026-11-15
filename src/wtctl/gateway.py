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
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol


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


@dataclass(frozen=True)
class GitResult:
    """Outcome of one git process: raw bytes (git output is not always text) and a timeout flag."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")


# Seconds to wait for the pipes to close after the process group was killed.
KILL_GRACE = 5.0


def _drain(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Output after a kill: the pipes close once the group is dead; else return what was read.

    A descendant that left the process group (``setsid``) can keep the pipes open for as long as
    it lives; waiting for end-of-file would then hang forever, so the wait is bounded and the
    pipes are closed from our side.
    """
    try:
        return proc.communicate(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired as e:
        out, err = e.output or b"", e.stderr or b""
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        proc.wait()
        return out, err


def run_git(
    argv: list[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    stdin: bytes | None = None,
    timeout: float | None = None,
) -> GitResult:
    """Run git with exactly ``env`` (nothing is inherited) in its own process group.

    The group is the one this call created (the child is its leader), so on expiry the whole
    group is killed through the leader's pid captured at launch, never by pattern.
    ``timed_out`` is set only when git itself was still running at expiry; a git that already
    exited (its pipes held open by a descendant) is returned with its own exit status.
    """
    # TODO(#13): stdout is buffered in memory; pack-objects --stdout needs stdout_path or an fd.
    proc = subprocess.Popen(
        argv,
        env=dict(env),
        cwd=cwd,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = proc.pid
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        was_running = proc.poll() is None
        if was_running:
            # Only while the leader is unreaped is its pid certainly still this group's id; once
            # it has exited, a recycled pid could name an unrelated group, so descendants that
            # outlive it are left to the bounded wait below.
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        out, err = _drain(proc)
        proc.wait()
        return GitResult(proc.returncode, out, err, timed_out=was_running)
    return GitResult(proc.returncode, out, err)


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


class ProcessUnreadable(Exception):
    """Whether a process exists could not be determined (as opposed to: it does not exist)."""


class ProcessIdentity(Protocol):
    def start_time(self, pid: int) -> str | None:
        """An opaque string that, with the PID, identifies one process for its lifetime.

        None when no such process runs (a zombie counts as not running); raises
        ``ProcessUnreadable`` when that cannot be told.
        """
        ...


class PsProcessIdentity:
    """Start time from ``ps -o stat=,lstart=`` (macOS and anywhere without ``/proc``).

    Run with ``TZ=UTC LC_ALL=C`` so the start time does not depend on the caller's time zone or
    locale; a zombie (stat starting with ``Z``) counts as not running.
    """

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = run) -> None:
        self._runner = runner

    def start_time(self, pid: int) -> str | None:
        try:
            r = self._runner(
                ["env", "TZ=UTC", "LC_ALL=C", "ps", "-o", "stat=,lstart=", "-p", str(pid)],
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProcessUnreadable(f"cannot run ps: {exc}") from exc
        if r.returncode in (126, 127):  # env could not exec ps: unknown, not "gone"
            raise ProcessUnreadable(f"cannot run ps (exit {r.returncode})")
        out = r.stdout.strip()
        if r.returncode != 0 or not out:
            return None
        parts = out.split(None, 1)
        if parts[0].startswith("Z"):
            return None
        if len(parts) < 2:
            raise ProcessUnreadable(f"unexpected ps output: {out!r}")
        return parts[1]


class ProcProcessIdentity:
    """Start time from ``/proc/<pid>/stat`` field 22 (clock ticks since boot; Linux)."""

    def start_time(self, pid: int) -> str | None:
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
        except (FileNotFoundError, ProcessLookupError):
            return None
        except OSError as exc:
            raise ProcessUnreadable(f"cannot read /proc/{pid}/stat: {exc}") from exc
        # The command name (field 2) is in parentheses and may itself contain spaces or ')'.
        rest = raw[raw.rfind(")") + 2 :].split()
        if len(rest) < 20:
            raise ProcessUnreadable(f"unexpected /proc/{pid}/stat format")
        if rest[0] in ("Z", "X"):  # zombie or dead: not running
            return None
        return rest[19]


def system_process_identity() -> ProcessIdentity:
    return ProcProcessIdentity() if sys.platform.startswith("linux") else PsProcessIdentity()


@dataclass
class Gateway:
    clock: Clock = field(default_factory=SystemClock)
    procs: ProcessIdentity = field(default_factory=system_process_identity)
    scanner: ProcessScanner = field(default_factory=UnavailableScanner)
    runner: Callable[..., subprocess.CompletedProcess[str]] = run
    git_runner: Callable[..., GitResult] = run_git

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return self.runner(argv, **kwargs)

    def git(self, argv: list[str], **kwargs: object) -> GitResult:
        """Run a fully built git command line (see ``wtctl.hardgit``, the only caller)."""
        return self.git_runner(argv, **kwargs)


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
