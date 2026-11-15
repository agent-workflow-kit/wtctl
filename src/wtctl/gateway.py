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
    stdout: bytes = field(default=b"", repr=False, compare=False)
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
        leader_gone = False
        while failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            # Poll in short slices so a leader that exited is noticed even while a descendant
            # still holds the pipe open (end-of-file would then never come).
            if not sel.select(0 if leader_gone else min(remaining, 0.1)):
                if leader_gone:
                    break  # everything already written has been read
                leader_gone = proc.poll() is not None
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
    if failure is not None and proc.returncode is None:
        # Only while the leader is unreaped does its pid certainly still name our group.
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
    git_runner: Callable[..., GitResult] = run_git
    token_runner: Callable[..., TokenCommandResult] = run_token_command
    crontab_runner: Callable[..., subprocess.CompletedProcess[bytes]] = field(
        default_factory=lambda: run_crontab  # looked up at call time, so tests can patch it
    )

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return self.runner(argv, **kwargs)

    def git(self, argv: list[str], **kwargs: object) -> GitResult:
        """Run a fully built git command line (see ``wtctl.hardgit``, the only caller)."""
        return self.git_runner(argv, **kwargs)

    def token_command(self, argv: list[str], **kwargs: object) -> TokenCommandResult:
        """Run a ``token_command`` (see ``wtctl.credentials``, the only caller)."""
        return self.token_runner(argv, **kwargs)

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
    saved: dict[int, object] = {}
    proc: subprocess.Popen[bytes] | None = None
    sel = selectors.DefaultSelector()
    out, err = _Capture(max_stdout, tail), _Capture(max_stderr, tail)
    timed_out = incomplete = False
    try:
        if threading.current_thread() is threading.main_thread():
            # A SIGTERM/SIGHUP to wtctl must not orphan the child's group: turn it into
            # SystemExit so the BaseException path below kills the group. Installed before
            # the spawn (no unprotected window); an inherited SIG_IGN is respected.
            for signum in (signal.SIGTERM, signal.SIGHUP):
                previous = signal.getsignal(signum)
                if previous != signal.SIG_IGN:
                    saved[signum] = signal.signal(signum, _raise_system_exit)
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
            for key in list(sel.get_map().values()):
                if key.data is None:
                    continue
                collect_deadline = time.monotonic() + _COLLECT_SECONDS
                budget = _COLLECT_BYTES
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
        if proc is not None:
            _kill_group(proc.pid)
        raise
    finally:
        sel.close()
        if proc is not None:
            for f in (proc.stdin, proc.stdout, proc.stderr):
                if f is not None:
                    try:
                        f.close()
                    except OSError:
                        pass
            proc.wait()
        for signum, previous in saved.items():
            signal.signal(signum, previous if previous is not None else signal.SIG_DFL)  # type: ignore[arg-type]
    assert proc is not None
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
