"""``browse``'s on-demand ``status``: a child ``wtctl --json status`` with a deadline.

``status`` can refresh the mirror (network), so ``browse`` never runs it on the UI thread or in
the process that owns the state database handle: a worker thread runs a child process through
``gateway.run_with_deadline`` (exact PID, SIGKILL on expiry or cancel) and the UI polls for the
result. The child is the ordinary command, so its behaviour is exactly ``wtctl status``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from typing import Any

from . import gateway
from .browse_model import StatusOutcome
from .commands import status as status_cmd

STATUS_DEADLINE = 90.0  # seconds; the mirror refresh and the content checks are bounded inside
MAX_OUTPUT = 1 << 20  # bytes of the child's output that are parsed


def parse_status(
    returncode: int | None,
    stdout: bytes,
    stderr: bytes,
    *,
    timed_out: bool = False,
    cancelled: bool = False,
) -> StatusOutcome:
    """The child's result as display lines (unsanitized: the renderer sanitizes)."""
    if cancelled:
        return StatusOutcome(False, ("cancelled",))
    if timed_out or returncode is None:
        return StatusOutcome(False, (f"status did not finish within {STATUS_DEADLINE:.0f}s",))
    data: Any = None
    try:
        data = json.loads(stdout[:MAX_OUTPUT].decode("utf-8", "replace") or "null")
    except ValueError:
        data = None
    if returncode == 0 and isinstance(data, dict):
        try:
            return StatusOutcome(True, tuple(status_cmd.human(data).split("\n")))
        except (KeyError, TypeError, AttributeError):
            return StatusOutcome(False, ("status returned an object of an unexpected shape",))
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        err = data["error"]
        reason = f" ({err['reason']})" if err.get("reason") else ""
        return StatusOutcome(
            False, (f"{err.get('code', 'error')}: {err.get('message', '')}{reason}",)
        )
    tail = stderr[:2000].decode("utf-8", "replace").strip().split("\n")[0]
    return StatusOutcome(False, (f"status failed (exit {returncode}): {tail}",))


class _Job:
    def __init__(self, argv: list[str], timeout: float) -> None:
        self._cancel = threading.Event()
        self._outcome: StatusOutcome | None = None
        self._lock = threading.Lock()
        self._pid: int | None = None
        self._thread = threading.Thread(target=self._run, args=(argv, timeout), daemon=True)
        self._thread.start()

    def _run(self, argv: list[str], timeout: float) -> None:
        try:
            r = gateway.run_with_deadline(
                argv, timeout=timeout, cancel=self._cancel, on_start=self._started
            )
            outcome = parse_status(
                r.returncode, r.stdout, r.stderr, timed_out=r.timed_out, cancelled=r.cancelled
            )
        except Exception as exc:  # noqa: BLE001 - a worker must always report something
            outcome = StatusOutcome(False, (f"cannot run status: {type(exc).__name__}: {exc}",))
        with self._lock:
            self._outcome = outcome

    def _started(self, pid: int) -> None:
        self._pid = pid

    def poll(self) -> StatusOutcome | None:
        with self._lock:
            return self._outcome

    def cancel(self) -> None:
        self._cancel.set()

    def close(self, join_timeout: float = 2.0) -> None:
        """Cancel and make sure the child is gone: the worker is a daemon thread, so on quit the
        interpreter could exit before it polls the cancel event. SIGKILL goes to the exact
        recorded child pid only, and only if the worker has not finished."""
        self._cancel.set()
        self._thread.join(join_timeout)
        pid = self._pid
        if self._thread.is_alive() and pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class SubprocessStatus:
    """A ``StatusStarter``: ``SubprocessStatus(config_path)(record_id)`` starts a job."""

    def __init__(self, config_path: str | None, timeout: float = STATUS_DEADLINE) -> None:
        self._config = config_path
        self._timeout = timeout

    def argv(self, target: str) -> list[str]:
        cfg = ["--config", self._config] if self._config else []
        return [sys.executable, "-I", "-m", "wtctl", "--json", *cfg, "status", "--", target]

    def __call__(self, target: str) -> _Job:
        return _Job(self.argv(target), self._timeout)
