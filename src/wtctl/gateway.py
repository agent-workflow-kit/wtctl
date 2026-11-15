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


@dataclass(frozen=True)
class TokenCommandResult:
    """Outcome of a ``token_command``: the stdout is a secret, so it is kept out of ``repr``.

    ``failure`` is a category only (``not_found``, ``os_error``, ``timeout``, ``too_large``,
    ``exit``), never the program's own text: its stderr is discarded unread and its stdout is the
    token.
    """

    returncode: int | None
    stdout: bytes = field(default=b"", repr=False)
    failure: str | None = None


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the group this call created. The leader is unreaped (no ``poll``/``wait`` yet),
    so its pid still names that group even if the leader is a zombie."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_token_command(
    argv: list[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    max_bytes: int,
) -> TokenCommandResult:
    """Run a credential helper without a shell and return at most ``max_bytes`` of its stdout.

    stdin is ``/dev/null`` and stderr is discarded unread (it may hold secrets and is never
    echoed). The child is a process-group leader; on timeout, or output beyond ``max_bytes``,
    the whole group gets SIGKILL through the pid captured at launch.
    """
    # TODO(#16): switch to the step runner (run_step) once it lands; this block is only the
    # credentials seam (bounded stdout capture) it does not yet offer.
    try:
        proc = subprocess.Popen(
            argv,
            env=dict(env),
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return TokenCommandResult(None, failure="not_found")
    except (OSError, ValueError):
        return TokenCommandResult(None, failure="os_error")
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    failure: str | None = None
    fd = proc.stdout.fileno()
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            if not sel.select(remaining):
                continue
            data = os.read(fd, 65536)
            if not data:
                break
            size += len(data)
            if size > max_bytes:
                failure = "too_large"
                break
            chunks.append(data)
    if failure is None:
        # EOF: the program closed stdout; it may still run.
        try:
            proc.wait(timeout=max(deadline - time.monotonic(), 0.0))
        except subprocess.TimeoutExpired:
            failure = "timeout"
    if failure is not None:
        _kill_group(proc)
    try:
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass  # a leader that survives SIGKILL is reported, not chased
    proc.stdout.close()
    if failure is not None:
        return TokenCommandResult(proc.returncode, failure=failure)
    if proc.returncode != 0:
        return TokenCommandResult(proc.returncode, failure="exit")
    return TokenCommandResult(0, b"".join(chunks))


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
    git_runner: Callable[..., GitResult] = run_git
    token_runner: Callable[..., TokenCommandResult] = run_token_command

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return self.runner(argv, **kwargs)

    def git(self, argv: list[str], **kwargs: object) -> GitResult:
        """Run a fully built git command line (see ``wtctl.hardgit``, the only caller)."""
        return self.git_runner(argv, **kwargs)

    def token_command(self, argv: list[str], **kwargs: object) -> TokenCommandResult:
        """Run a ``token_command`` (see ``wtctl.credentials``, the only caller)."""
        return self.token_runner(argv, **kwargs)


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
