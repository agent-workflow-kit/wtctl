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
import re
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
class DeadlineResult:
    """Outcome of ``run_with_deadline``; ``returncode`` is None when the child was killed."""

    pid: int
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool


def run_with_deadline(
    argv: list[str],
    *,
    stdin: bytes = b"",
    timeout: float,
    env: Mapping[str, str] | None = None,
    on_start: Callable[[int], None] | None = None,
) -> DeadlineResult:
    """Run ``argv`` (no shell) and SIGKILL it on expiry.

    The kill goes to the exact PID of the ``Popen`` this call started (``Popen.kill`` signals
    only a child that has not been reaped), never a pattern or a process group.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=None if env is None else dict(env),
        close_fds=True,
    )
    try:
        if on_start is not None:
            on_start(proc.pid)
        try:
            out, err = proc.communicate(stdin, timeout=max(timeout, 0.0))
            return DeadlineResult(proc.pid, proc.returncode, out, err, False)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # still stuck in the kernel: report, never block
                out, err = b"", b""
            return DeadlineResult(proc.pid, None, out, err, True)
    except BaseException:
        # on_start raised or we were interrupted: never leave the child running
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass
        raise


@dataclass(frozen=True)
class MountEntry:
    """One mounted file system. ``mount_id`` is None where the platform has none (macOS)."""

    mount_id: int | None
    mount_point: str
    fstype: str
    source: str


class MountTableUnavailable(Exception):
    """The mount table or an fd's mount id could not be read on a platform that has them."""


_OCTAL = re.compile(r"\\([0-7]{3})")


def _unescape_mountinfo(field: str) -> str:
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> list[MountEntry]:
    """Parse ``/proc/self/mountinfo``; malformed lines are skipped."""
    entries: list[MountEntry] = []
    for line in text.splitlines():
        head, sep, tail = line.partition(" - ")
        fields = head.split(" ")
        rest = tail.split(" ")
        if not sep or len(fields) < 5 or len(rest) < 2 or not fields[0].isdigit():
            continue
        entries.append(
            MountEntry(
                int(fields[0]),
                _unescape_mountinfo(fields[4]),
                rest[0],
                _unescape_mountinfo(rest[1]),
            )
        )
    return entries


_MOUNT_LINE = re.compile(r"^(?P<src>.*?) on (?P<mp>/.*) \((?P<fs>[^,)]*)")


def parse_mount_command(text: str) -> list[MountEntry]:
    """Parse BSD/macOS ``mount`` output (``<source> on <point> (<fstype>, ...)``)."""
    entries: list[MountEntry] = []
    for line in text.splitlines():
        m = _MOUNT_LINE.match(line)
        if m:
            entries.append(MountEntry(None, m.group("mp"), m.group("fs"), m.group("src")))
    return entries


def read_mount_table() -> list[MountEntry] | None:
    """All mounts: Linux ``/proc/self/mountinfo``, macOS ``mount``; None on other platforms."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as f:
                return parse_mountinfo(f.read())
        except OSError as exc:
            raise MountTableUnavailable(f"cannot read /proc/self/mountinfo: {exc}") from exc
    if sys.platform == "darwin":
        try:
            r = run(["mount"], timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MountTableUnavailable(f"cannot run mount: {exc}") from exc
        if r.returncode != 0:
            raise MountTableUnavailable("mount failed")
        return parse_mount_command(r.stdout)
    return None


def fd_mount_id(fd: int) -> int | None:
    """Linux: the ``mnt_id`` of an open fd (``/proc/self/fdinfo/<fd>``); None elsewhere."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open(f"/proc/self/fdinfo/{fd}", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("mnt_id:"):
                    return int(line.split(":", 1)[1])
    except (OSError, ValueError) as exc:
        raise MountTableUnavailable(f"cannot read the mount id of fd {fd}: {exc}") from exc
    raise MountTableUnavailable(f"no mnt_id line for fd {fd}")


def chmod_opath_fd(fd: int, mode: int) -> None:
    """Linux: chmod the inode an ``O_PATH`` fd refers to (``/proc/self/fd/<fd>``)."""
    os.chmod(f"/proc/self/fd/{fd}", mode)


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
