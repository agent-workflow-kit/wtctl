"""Anchored walks and deletions in a child process under a hard deadline.

A FUSE request that never answers blocks ``open`` or ``read`` in the kernel, which no byte bound
interrupts; only a child process killed with SIGKILL bounds the wait (docs/design.md D56). The
parent starts the child through ``gateway.run_with_deadline`` (exact PID, SIGKILL on expiry):

    python -m wtctl.fsafe_child      # request JSON on stdin, result JSON on stdout

The child re-opens the anchor itself from a path and the *recorded* identity, so it needs no
inherited fds. Operations (``OPS``) take plain JSON arguments:

* ``anchor``: ``{"path", "identity": [dev, ino] | null, "precheck": bool = true}``
* ``chain``: components below the anchor to the walk root (or, for ``delete_tree``, to the parent
  of the directory to delete)
* ``root_identity``: the recorded identity of the walk root / directory to delete, or null
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from wtctl import fsafe, gateway

WALK_DEADLINE = 60.0
REMOVE_DEADLINE = 300.0


class Budget:
    """The time left for all walks of one operation (``REMOVE_DEADLINE`` for one ``remove``)."""

    def __init__(
        self, total: float = REMOVE_DEADLINE, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.total = total
        self.clock = clock
        self.spent = 0.0

    def remaining(self) -> float:
        return max(0.0, self.total - self.spent)

    def charge(self, seconds: float) -> None:
        self.spent += max(0.0, seconds)


@dataclass
class OpResult:
    ok: bool
    value: dict[str, Any] | None = None
    error: fsafe.FsafeError | None = None
    timed_out: bool = False
    pid: int | None = None


def _identity(raw: object) -> fsafe.Identity | None:
    if raw is None:
        return None
    if isinstance(raw, list) and len(raw) == 2 and all(isinstance(i, int) for i in raw):
        return fsafe.Identity(int(raw[0]), int(raw[1]))
    raise fsafe.FsafeError("bad_request", f"not an identity: {raw!r}")


def _open_root(args: dict[str, object], *, parent: bool) -> tuple[fsafe.Anchor, int]:
    spec = args["anchor"]
    chain = args.get("chain", [])
    if not isinstance(spec, dict) or not isinstance(chain, list):
        raise fsafe.FsafeError("bad_request", "anchor must be an object and chain a list")
    anchor = fsafe.open_anchor(
        str(spec["path"]),
        expected=_identity(spec.get("identity")),
        precheck=bool(spec.get("precheck", True)),
    )
    try:
        expected = None if parent else _identity(args.get("root_identity"))
        fd = fsafe.open_chain(anchor, anchor.fd, [str(c) for c in chain], expected=expected)
    except BaseException:
        anchor.close()
        raise
    return anchor, fd


def op_walk(args: dict[str, object]) -> dict[str, object]:
    """Walk the tree; report counts, ``.git`` aliases, non-file entries and mount points."""
    limit = args.get("limit", fsafe.DEFAULT_SCAN_LIMIT)
    if not isinstance(limit, int):
        raise fsafe.FsafeError("bad_request", "limit must be an integer")
    stop_at_git = bool(args.get("stop_at_git", False))
    anchor, root = _open_root(args, parent=False)
    count = 0
    mounts: list[list[str]] = []
    special: list[list[object]] = []
    dotgit: list[list[str]] = []

    def descend(e: fsafe.Entry) -> bool:
        return not (stop_at_git and fsafe.is_dotgit(e.path[-1]))

    try:
        for e in fsafe.walk_tree(anchor, root, limit=limit, descend=descend):
            count += 1
            if fsafe.is_dotgit(e.path[-1]):
                dotgit.append(list(e.path))
            if e.kind == "mount":
                mounts.append(list(e.path))
            elif e.kind not in ("file", "dir"):
                special.append([list(e.path), e.kind])
    finally:
        os.close(root)
        anchor.close()
    return {"count": count, "mounts": mounts, "special": special, "dotgit": dotgit}


def op_delete_tree(args: dict[str, object]) -> dict[str, object]:
    """Delete directory ``name`` below the chain; nothing is deleted if a check fails."""
    name = args.get("name")
    if not isinstance(name, str):
        raise fsafe.FsafeError("bad_request", "name must be a string")
    root_identity = _identity(args.get("root_identity"))
    anchor, parent = _open_root(args, parent=True)
    try:
        res = fsafe.delete_tree(anchor, parent, name, expected=root_identity)
    finally:
        os.close(parent)
        anchor.close()
    return {
        "removed": res.removed,
        "remaining": [
            {"path": list(r.path), "reason": r.reason, "errno": r.errno} for r in res.remaining
        ],
    }


OPS: dict[str, Callable[[dict[str, object]], dict[str, object]]] = {
    "walk": op_walk,
    "delete_tree": op_delete_tree,
}


def _raise_fd_limit() -> None:
    """An fd-based walk holds one fd per level of depth; use the hard limit (best effort)."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = hard if hard != resource.RLIM_INFINITY else 1_048_576
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(want, 1_048_576), hard))
    except (ImportError, ValueError, OSError):
        pass


def main() -> int:
    _raise_fd_limit()
    try:
        req = json.loads(sys.stdin.read())
        op = req["op"]
        args = req["args"]
        if op not in OPS or not isinstance(args, dict):
            raise fsafe.FsafeError("bad_request", f"unknown operation or arguments: {op!r}")
        out: dict[str, object] = {"ok": True, "value": OPS[op](args)}
    except fsafe.FsafeError as exc:
        out = {"ok": False, "error": exc.to_dict()}
    except (KeyError, TypeError, ValueError) as exc:
        out = {"ok": False, "error": fsafe.FsafeError("bad_request", repr(exc)).to_dict()}
    except OSError as exc:
        out = {"ok": False, "error": fsafe.FsafeError("io", str(exc), errno_=exc.errno).to_dict()}
    sys.stdout.write(json.dumps(out))
    return 0


def run_op(
    op: str,
    args: dict[str, object],
    *,
    timeout: float = WALK_DEADLINE,
    budget: Budget | None = None,
    argv: list[str] | None = None,
    on_start: Callable[[int], None] | None = None,
) -> OpResult:
    """Run ``op`` in a child process; kill it by its exact PID when the deadline passes.

    The deadline is ``timeout`` capped by what ``budget`` has left (and the elapsed time is
    charged to it). An expired deadline is ``timed_out`` (callers fail safe); an exhausted budget
    starts nothing.
    """
    limit = timeout if budget is None else min(timeout, budget.remaining())
    if limit <= 0:
        return OpResult(False, error=fsafe.FsafeError("deadline", "no time left"), timed_out=True)
    cmd = argv or [sys.executable, "-I", "-m", "wtctl.fsafe_child"]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/")}
    start = budget.clock() if budget is not None else 0.0
    r = gateway.run_with_deadline(
        cmd,
        stdin=json.dumps({"op": op, "args": args}).encode("ascii"),
        timeout=limit,
        env=env,
        on_start=on_start,
    )
    if budget is not None:
        budget.charge(budget.clock() - start)
    if r.timed_out:
        return OpResult(
            False,
            error=fsafe.FsafeError("deadline", f"{op} exceeded {limit:g}s and was killed"),
            timed_out=True,
            pid=r.pid,
        )
    try:
        data = json.loads(r.stdout)
        if r.returncode != 0 or not isinstance(data, dict):
            raise ValueError("bad child output")
    except ValueError:
        tail = r.stderr.decode("utf-8", "replace")[-300:]
        err = fsafe.FsafeError("io", f"{op} child failed ({r.returncode}): {tail}")
        return OpResult(False, error=err, pid=r.pid)
    if data.get("ok") is True:
        value = data.get("value")
        return OpResult(True, value=value if isinstance(value, dict) else {}, pid=r.pid)
    raw = data.get("error")
    err = fsafe.FsafeError.from_dict(raw if isinstance(raw, dict) else {})
    return OpResult(False, error=err, pid=r.pid)


if __name__ == "__main__":
    sys.exit(main())
