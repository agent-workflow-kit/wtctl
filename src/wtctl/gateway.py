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
import subprocess
import sys
import time
from collections.abc import Callable
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


# --- crontab (#8) -------------------------------------------------------------------------------


def _find_executable(name: str, path: str) -> str | None:
    """``name`` in the absolute entries of ``path`` (relative and empty entries are ignored, so
    a planted program in the working directory is never run; design D49)."""
    for entry in path.split(os.pathsep):
        candidate = os.path.join(entry, name)
        if os.path.isabs(entry) and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def run_crontab(args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run ``crontab <args>`` with bytes in and out (a crontab is rewritten without re-encoding),
    in the C locale so its "no crontab" message can be recognised.

    Raises ``FileNotFoundError`` when there is no ``crontab`` on an absolute ``PATH`` entry.
    """
    program = _find_executable("crontab", os.environ.get("PATH", ""))
    if program is None:
        raise FileNotFoundError("crontab")
    return subprocess.run(
        [program, *args],
        input=stdin,
        capture_output=True,
        timeout=30,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
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
    crontab_runner: Callable[..., subprocess.CompletedProcess[bytes]] = field(
        default_factory=lambda: run_crontab  # looked up at call time, so tests can patch it
    )

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return self.runner(argv, **kwargs)

    def crontab(
        self, args: list[str], stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return self.crontab_runner(args, stdin)


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
