"""In-use detection (docs/interface.md, In-use detection).

A worktree or throwaway is in use when a process of the invoking user has its cwd or an open file
inside the directory. Processes come from ``gateway.scanner`` (``/proc`` or ``lsof``, fakeable);
cwds are matched by the ``(st_dev, st_ino)`` of every directory inside the target (collected by a
no-follow, mount-free walk in a child process) and by path, open files by path only. wtctl's own
process, its children and its ancestor chain are excluded; an excluded ancestor whose cwd is inside
is reported as a warning. A scanner that cannot run makes the check ``unavailable`` (never a pass).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import fsafe, removalfs
from .gateway import Gateway, ProcessInfo, ScannerUnavailable

DELETED = " (deleted)"


def escape_path(text: str) -> str:
    """Non-printable bytes as ``\\xNN`` (undecodable bytes arrive as lone surrogates)."""
    out: list[str] = []
    for ch in text:
        o = ord(ch)
        if 0xDC80 <= o <= 0xDCFF:
            out.append(f"\\x{o - 0xDC00:02x}")
        elif o < 0x20 or o == 0x7F or 0xD800 <= o <= 0xDFFF or ch in "\u2028\u2029":
            out.append(f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}")
        else:
            out.append(ch)
    return "".join(out)


def inside(path: str, target: str) -> bool:
    """``path`` is ``target`` or below it, by path component (``wt2`` is not inside ``wt``)."""
    t = target.rstrip("/") or "/"
    if t == "/":
        return path.startswith("/")
    return path == t or path.startswith(t + "/")


def path_matches(path: str, target: str) -> bool:
    """With and without a trailing ``(deleted)`` marker (the kernel appends it to the link text)."""
    if inside(path, target):
        return True
    return path.endswith(DELETED) and inside(path[: -len(DELETED)], target)


@dataclass
class InUse:
    status: str  # pass | fail | unavailable
    processes: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def check(self) -> dict[str, Any]:
        return {"status": self.status, "details": self.processes}


def collect_dir_ids(
    state: Any, record: dict[str, Any], *, limit: int, budget: Any = None
) -> tuple[set[tuple[int, int]], str | None]:
    """The identities of every directory inside the target, and a warning when the list is
    partial (a limit, a deadline, an anchor that no longer matches: the path matches still cover
    the whole target)."""
    try:
        args = removalfs.walk_args(state, record, limit=limit)
        res = removalfs.run("dir_ids", args, budget)
    except fsafe.FsafeError as exc:
        return set(), f"directory walk of {escape_path(record['path'])} not possible ({exc.kind})"
    if not res.ok or res.value is None:
        why = "deadline" if res.timed_out else (res.error.kind if res.error else "failed")
        return set(), f"directory walk of {escape_path(record['path'])} incomplete ({why})"
    raw = res.value["ids"]
    ids = {(int(raw[i]), int(raw[i + 1])) for i in range(0, len(raw), 2)}
    warning = None
    if not res.value.get("complete", True):
        warning = (
            f"directory walk of {escape_path(record['path'])} stopped early; "
            "working directories are matched by path only for the rest"
        )
    return ids, warning


def _parent_of(info: ProcessInfo, gateway: Gateway) -> int | None:
    if info.ppid is not None:
        return info.ppid
    try:  # a scanner that does not report parents (lsof): ask ps
        r = gateway.run(["ps", "-o", "ppid=", "-p", str(info.pid)], timeout=10)
    except Exception:  # noqa: BLE001 - any failure leaves the ancestor unknown
        return None
    text = r.stdout.strip() if r.returncode == 0 else ""
    return int(text) if text.isdigit() else None


def excluded_pids(
    procs: list[ProcessInfo], gateway: Gateway, own_pid: int
) -> tuple[set[int], set[int]]:
    """``(own, ancestors)``: wtctl itself with its descendants, and its verified ancestor chain.

    An ancestor counts only when its PID and start time still match what the scan saw, so a PID
    reused by another process after the scan is not excluded."""
    by_pid = {p.pid: p for p in procs}
    own = {own_pid}
    children: dict[int, list[int]] = {}
    for p in procs:
        if p.ppid is not None:
            children.setdefault(p.ppid, []).append(p.pid)
    todo = [own_pid]
    while todo:
        for c in children.get(todo.pop(), []):
            if c not in own:
                own.add(c)
                todo.append(c)
    ancestors: set[int] = set()
    info = by_pid.get(own_pid)
    pid = _parent_of(info, gateway) if info is not None else os.getppid()
    seen = set(own)
    while pid is not None and pid > 0 and pid not in seen:
        seen.add(pid)
        cur = by_pid.get(pid)
        if cur is None:
            break
        try:
            alive = gateway.procs.start_time(pid)
        except Exception:  # noqa: BLE001 - cannot verify: not excluded
            break
        if alive is None or (cur.start is not None and cur.start != alive):
            break
        ancestors.add(pid)
        if pid == 1:
            break
        pid = _parent_of(cur, gateway)
    return own, ancestors


def _match(info: ProcessInfo, target: str, dir_ids: set[tuple[int, int]]) -> str | None:
    """The path that puts ``info`` inside ``target`` (its cwd first), or None."""
    if info.cwd is not None and path_matches(info.cwd, target):
        return info.cwd
    if info.cwd_id is not None and info.cwd_id in dir_ids:
        return info.cwd if info.cwd is not None else target
    for p in info.open_paths:
        if path_matches(p, target):
            return p
    return None


def check_in_use(
    gateway: Gateway,
    target: str,
    dir_ids: set[tuple[int, int]],
    *,
    own_pid: int | None = None,
) -> InUse:
    """Scan processes and report those with a cwd or open file inside ``target``."""
    try:
        procs = gateway.scanner.scan(target)
    except ScannerUnavailable as exc:
        return InUse("unavailable", warnings=[f"in-use check skipped: {escape_path(str(exc))}"])
    own, ancestors = excluded_pids(procs, gateway, os.getpid() if own_pid is None else own_pid)
    found: list[dict[str, Any]] = []
    warnings: list[str] = []
    foreign = 0  # processes of other users: not ours to inspect
    unreadable = 0  # processes of the invoking user whose cwd or descriptors could not be read
    for info in sorted(procs, key=lambda p: p.pid):
        if info.pid in own:
            continue
        if not info.inspected and info.pid not in ancestors:
            if info.other_user:
                foreign += 1
            else:
                unreadable += 1
        hit = _match(info, target, dir_ids)
        if hit is None:
            continue
        if info.pid in ancestors:
            warnings.append(
                f"wtctl's ancestor {info.pid} ({escape_path(info.command)}) has "
                f"{escape_path(hit)} open; it is not counted as in use"
            )
            continue
        found.append(
            {"pid": info.pid, "command": escape_path(info.command), "path": escape_path(hit)}
        )
    if foreign:
        warnings.append(f"{foreign} process(es) of other users were not inspected")
    if unreadable:
        warnings.append(
            f"{unreadable} process(es) of the invoking user could not be inspected, so a process "
            "using the directory cannot be ruled out"
        )
    if found:
        return InUse("fail", found, warnings)
    return InUse("unavailable" if unreadable else "pass", found, warnings)


def require_unused(
    gateway: Gateway, state: Any, record: dict[str, Any], *, limit: int
) -> list[str]:
    """Refuse ``in_use`` (exit 20) when a process has its cwd or an open file inside the record's
    directory; returns the warnings (an unavailable scanner is one: the callers that use this
    change nothing a process could lose, unlike ``remove``)."""
    from .errors import WtctlError

    ids, warning = collect_dir_ids(state, record, limit=limit)
    result = check_in_use(gateway, record["path"], ids)
    if result.status == "fail":
        raise WtctlError(
            "refused",
            f"{record['id']} is in use by {len(result.processes)} process(es)",
            reason="in_use",
            processes=result.processes,
        )
    return ([warning] if warning else []) + result.warnings
