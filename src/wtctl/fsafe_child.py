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


def _open_root(args: dict[str, object], *, parent: bool) -> tuple[fsafe.Anchor, int, str]:
    """Open the anchor and the chain; returns ``(anchor, fd, absolute path of fd)``."""
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
        fd = fsafe.open_chain(
            anchor,
            anchor.fd,
            [str(c) for c in chain],
            expected=expected,
            start_path=anchor.path,
        )
    except BaseException:
        anchor.close()
        raise
    return anchor, fd, "/".join([anchor.path.rstrip("/"), *[str(c) for c in chain]]) or "/"


def op_walk(args: dict[str, object]) -> dict[str, object]:
    """Walk the tree; report counts, ``.git`` aliases, non-file entries and mount points."""
    limit = args.get("limit", fsafe.DEFAULT_SCAN_LIMIT)
    if not isinstance(limit, int):
        raise fsafe.FsafeError("bad_request", "limit must be an integer")
    stop_at_git = bool(args.get("stop_at_git", False))
    dirs_only = bool(args.get("dirs_only", False))
    list_limit = args.get("list_limit", 0)  # entries returned by path (the confirmation shows them)
    if not isinstance(list_limit, int):
        raise fsafe.FsafeError("bad_request", "list_limit must be an integer")
    anchor, root, root_path = _open_root(args, parent=False)
    count = 0
    listed: list[list[str]] = []
    mounts: list[list[str]] = []
    special: list[list[object]] = []
    unreadable: list[list[object]] = []  # entries the walk could not read: an incomplete walk
    dotgit: list[list[str]] = []

    def descend(e: fsafe.Entry) -> bool:
        return not (stop_at_git and fsafe.is_dotgit(e.path[-1]))

    try:
        for e in fsafe.walk_tree(
            anchor, root, limit=limit, descend=descend, dirs_only=dirs_only, root_path=root_path
        ):
            count += 1
            if len(listed) < list_limit:
                listed.append(list(e.path))
            if fsafe.is_dotgit(e.path[-1]):
                dotgit.append(list(e.path))
            if e.kind == "mount":
                mounts.append(list(e.path))
            elif e.kind in ("unreadable", "changed"):
                unreadable.append([list(e.path), e.kind, e.errno])
            elif e.kind not in ("file", "dir"):
                special.append([list(e.path), e.kind])
    finally:
        os.close(root)
        anchor.close()
    return {
        "count": count,
        "mounts": mounts,
        "special": special,
        "unreadable": unreadable,
        "dotgit": dotgit,
        "entries": listed,
    }


def op_delete_tree(args: dict[str, object]) -> dict[str, object]:
    """Delete directory ``name`` below the chain; nothing is deleted if a check fails."""
    name = args.get("name")
    if not isinstance(name, str):
        raise fsafe.FsafeError("bad_request", "name must be a string")
    last = args.get("last")
    if last is not None and not isinstance(last, str):
        raise fsafe.FsafeError("bad_request", "last must be a string")
    root_identity = _identity(args.get("root_identity"))
    anchor, parent, parent_path = _open_root(args, parent=True)
    try:
        res = fsafe.delete_tree(
            anchor, parent, name, expected=root_identity, parent_path=parent_path, last=last
        )
    finally:
        os.close(parent)
        anchor.close()
    return {
        "removed": res.removed,
        "remaining_total": res.remaining_total,
        "remaining": [
            {"path": list(r.path), "reason": r.reason, "errno": r.errno} for r in res.remaining
        ],
    }


ADMIN_READ = 4096  # bytes of an admin entry's ``gitdir`` file kept


def _admin_entry(
    anchor: fsafe.Anchor, parent: int, name: str
) -> tuple[fsafe.Identity, bool, str | None] | None:
    """``(identity, locked, gitdir text)`` of the admin entry ``name``, opened no-follow from the
    ``worktrees`` directory fd and verified against the anchor's device and mount; None when it
    does not exist."""
    try:
        anchor.fs.lstat_at(parent, name)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise fsafe.FsafeError("io", f"{name}: {exc.strerror}", errno_=exc.errno) from exc
    fd = fsafe.open_dir(anchor, parent, name)
    try:
        ident = fsafe.identity_of(fd, anchor.fs)
        try:
            anchor.fs.lstat_at(fd, "locked")
            locked = True
        except FileNotFoundError:
            locked = False
        gitdir: str | None = None
        try:
            gfd = fsafe.open_file(anchor, fd, "gitdir")
        except fsafe.FsafeError:
            gfd = -1
        if gfd >= 0:
            try:
                gitdir = os.read(gfd, ADMIN_READ).decode("utf-8", "surrogateescape")
            finally:
                os.close(gfd)
        return ident, locked, gitdir
    finally:
        os.close(fd)


def _gitdir_value(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:-1] if text.endswith("\n") else text


def op_admin_check(args: dict[str, object]) -> dict[str, object]:
    """The state of the admin entry ``name`` below the ``worktrees`` directory (the chain), opened
    from the common dir's recorded identity. Reports ``absent`` or ``present`` with the entry's
    identity, whether it has a ``locked`` entry and the value of its ``gitdir`` file."""
    name = args.get("name")
    if not isinstance(name, str):
        raise fsafe.FsafeError("bad_request", "name must be a string")
    spec = args.get("anchor")
    if not isinstance(spec, dict):
        raise fsafe.FsafeError("bad_request", "anchor must be an object")
    try:
        anchor, parent, _ = _open_root(args, parent=True)
    except fsafe.FsafeError as exc:
        if exc.kind != "missing":
            raise
        # an anchor that is gone is a mismatch (the open below raises); a missing `worktrees`
        # directory below a good anchor is an absent entry
        fsafe.open_anchor(
            str(spec["path"]),
            expected=_identity(spec.get("identity")),
            precheck=bool(spec.get("precheck", True)),
        ).close()
        return {"state": "absent"}
    try:
        found = _admin_entry(anchor, parent, name)
    finally:
        os.close(parent)
        anchor.close()
    if found is None:
        return {"state": "absent"}
    ident, locked, gitdir = found
    return {
        "state": "present",
        "identity": [ident.dev, ident.ino],
        "locked": locked,
        "gitdir": _gitdir_value(gitdir),
    }


def op_delete_admin(args: dict[str, object]) -> dict[str, object]:
    """Delete the admin entry ``name`` of the ``worktrees`` directory (the chain) fd-based, but
    only if its ``gitdir`` file names ``expect_gitdir``. Returns ``{"state": "absent" |
    "foreign" | "deleted" | "partial", "remaining": [...]}``: a ``foreign`` entry (it names
    another path) is left alone."""
    name = args.get("name")
    expect = args.get("expect_gitdir")
    if not isinstance(name, str) or not isinstance(expect, str):
        raise fsafe.FsafeError("bad_request", "name and expect_gitdir must be strings")
    anchor, parent, parent_path = _open_root(args, parent=True)
    try:
        found = _admin_entry(anchor, parent, name)
        if found is None:
            return {"state": "absent", "remaining": [], "remaining_total": 0}
        ident, _, gitdir = found
        if _gitdir_value(gitdir) != expect:
            return {"state": "foreign", "remaining": [], "remaining_total": 0}
        res = fsafe.delete_tree(anchor, parent, name, expected=ident, parent_path=parent_path)
    finally:
        os.close(parent)
        anchor.close()
    return {
        "state": "deleted" if res.complete else "partial",
        "remaining_total": res.remaining_total,
        "remaining": [
            {"path": list(r.path), "reason": r.reason, "errno": r.errno} for r in res.remaining
        ],
    }


def op_entry_state(args: dict[str, object]) -> dict[str, object]:
    """Whether ``name`` exists below the chain, opened from the anchor's recorded identity:
    ``{"state": "absent"}`` or ``{"state": "present", "kind": ...}`` (never followed, never
    opened). A missing or changed anchor or chain is the usual error, not "absent"."""
    name = args.get("name")
    if not isinstance(name, str):
        raise fsafe.FsafeError("bad_request", "name must be a string")
    fsafe.check_component(name)
    anchor, parent, _ = _open_root(args, parent=True)
    try:
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return {"state": "absent"}
        except OSError as exc:
            raise fsafe.FsafeError("io", f"{name}: {exc.strerror}") from exc
    finally:
        os.close(parent)
        anchor.close()
    return {"state": "present", "kind": fsafe.kind_of_mode(st.st_mode)}


def op_dir_ids(args: dict[str, object]) -> dict[str, object]:
    """``(st_dev, st_ino)`` of the walk root and every directory below it, as a flat list
    ``[dev, ino, dev, ino, ...]`` (the in-use detection matches process cwds against them).

    A walk that reaches ``limit`` stops with ``complete: false`` (what it listed still counts)."""
    limit = args.get("limit", fsafe.DEFAULT_SCAN_LIMIT)
    if not isinstance(limit, int):
        raise fsafe.FsafeError("bad_request", "limit must be an integer")
    anchor, root, root_path = _open_root(args, parent=False)
    ids: list[int] = []
    complete = True
    mounts = 0
    try:
        st = anchor.fs.fstat(root)
        ids += [st.dev, st.ino]
        try:
            for e in fsafe.walk_tree(
                anchor, root, limit=limit, dirs_only=True, root_path=root_path
            ):
                if e.kind == "dir":
                    ids += [e.dev, e.ino]
                elif e.kind == "mount":
                    mounts += 1
                elif e.kind in ("unreadable", "changed"):
                    complete = False
        except fsafe.FsafeError as exc:
            if exc.kind != "limit":
                raise
            complete = False
    finally:
        os.close(root)
        anchor.close()
    return {"ids": ids, "complete": complete, "mounts": mounts}


NESTED_READ = 4096  # bytes of a nested ``.git`` file kept (a ``gitdir:`` pointer)


def _read_pointer(
    anchor: fsafe.Anchor, root: int, root_path: str, path: tuple[str, ...]
) -> str | None:
    try:
        parent = fsafe.open_chain(anchor, root, list(path[:-1]), start_path=root_path)
    except fsafe.FsafeError:
        return None
    try:
        fd = fsafe.open_file(anchor, parent, path[-1])
        try:
            return os.read(fd, NESTED_READ).decode("utf-8", "surrogateescape")
        finally:
            os.close(fd)
    except (fsafe.FsafeError, OSError):
        return None
    finally:
        os.close(parent)


def op_nested_scan(args: dict[str, object]) -> dict[str, object]:
    """Every ``.git`` (or alias of that name) below the walk root, without descending into it.

    Reports ``gits`` (path, kind, and for a regular file the start of its content), ``mounts``
    (mount points below the root, never crossed) and ``unreadable`` (entries the walk could not
    read: the scan is incomplete). More than ``limit`` entries raises ``FsafeError("limit")``."""
    limit = args.get("limit", fsafe.DEFAULT_SCAN_LIMIT)
    if not isinstance(limit, int):
        raise fsafe.FsafeError("bad_request", "limit must be an integer")
    anchor, root, root_path = _open_root(args, parent=False)
    gits: list[dict[str, object]] = []
    mounts: list[list[str]] = []
    unreadable: list[list[str]] = []
    count = 0
    try:
        for e in fsafe.walk_tree(
            anchor,
            root,
            limit=limit,
            descend=lambda ent: not fsafe.is_dotgit(ent.path[-1]),
            root_path=root_path,
        ):
            count += 1
            if e.kind == "mount":
                mounts.append(list(e.path))
            elif e.kind in ("unreadable", "changed"):
                unreadable.append(list(e.path))
            elif fsafe.is_dotgit(e.path[-1]):
                content = (
                    _read_pointer(anchor, root, root_path, e.path)
                    if e.kind == "file" and len(e.path) > 1
                    else None
                )
                gits.append({"path": list(e.path), "kind": e.kind, "content": content})
    finally:
        os.close(root)
        anchor.close()
    return {"count": count, "gits": gits, "mounts": mounts, "unreadable": unreadable}


def op_baseline_git(args: dict[str, object]) -> dict[str, object]:
    """The ``git``-class repo-config baseline items (``wtctl.repobaseline``)."""
    from wtctl import repobaseline

    return repobaseline.op_baseline_git(args)


def op_baseline_claude(args: dict[str, object]) -> dict[str, object]:
    """The ``claude``-class repo-config baseline items (``wtctl.repobaseline``)."""
    from wtctl import repobaseline

    return repobaseline.op_baseline_claude(args)


def op_extract_read(args: dict[str, object]) -> dict[str, object]:
    from wtctl import extraction

    return extraction.read_op(args)


def _read_small(anchor: fsafe.Anchor, dir_fd: int, name: str, limit: int = ADMIN_READ) -> str | None:
    """A small regular file of ``dir_fd`` (no-follow), or None when it is absent or not one."""
    try:
        fd = fsafe.open_file(anchor, dir_fd, name)
    except fsafe.FsafeError:
        return None
    try:
        return os.read(fd, limit).decode("utf-8", "surrogateescape")
    finally:
        os.close(fd)


def _strip_nl(text: str | None) -> str | None:
    return _gitdir_value(text)


def _refs_hold_files(anchor: fsafe.Anchor, entry_fd: int) -> bool:
    """Whether ``refs/`` of an admin entry holds anything but directories (per-worktree refs).
    Anything unreadable, over the limit or not a plain directory counts as holding some."""
    try:
        anchor.fs.lstat_at(entry_fd, "refs")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        rfd = fsafe.open_dir(anchor, entry_fd, "refs")
    except fsafe.FsafeError:
        return True
    try:
        for e in fsafe.walk_tree(anchor, rfd, limit=10_000):
            if e.kind != "dir":
                return True
    except fsafe.FsafeError:
        return True
    finally:
        os.close(rfd)
    return False


def op_admin_inspect(args: dict[str, object]) -> dict[str, object]:
    """What reconcile must know of the admin entry ``name`` before deleting it: whether it has a
    ``locked`` entry, a ``modules`` entry or files below ``refs``, and the text of ``gitdir`` and
    ``commondir``. Opened from the recorded common dir, no-follow, owned by the invoking user."""
    name = args.get("name")
    if not isinstance(name, str):
        raise fsafe.FsafeError("bad_request", "name must be a string")
    try:
        anchor, parent, _ = _open_root(args, parent=True)
    except fsafe.FsafeError as exc:
        if exc.kind != "missing":
            raise
        spec = args["anchor"]
        assert isinstance(spec, dict)
        # a missing `worktrees` directory below an anchor that opens and matches is an absent entry
        fsafe.open_anchor(
            str(spec["path"]),
            expected=_identity(spec.get("identity")),
            precheck=bool(spec.get("precheck", True)),
        ).close()
        return {"state": "absent"}
    try:
        try:
            anchor.fs.lstat_at(parent, name)
        except FileNotFoundError:
            return {"state": "absent"}
        fd = fsafe.open_dir(anchor, parent, name, owner_uid=os.geteuid())
        try:
            def exists(entry: str) -> bool:
                try:
                    anchor.fs.lstat_at(fd, entry)
                except FileNotFoundError:
                    return False
                return True

            return {
                "state": "present",
                "locked": exists("locked"),
                "has_modules": exists("modules"),
                "refs_files": _refs_hold_files(anchor, fd),
                "gitdir": _strip_nl(_read_small(anchor, fd, "gitdir")),
                "commondir": _strip_nl(_read_small(anchor, fd, "commondir")),
            }
        finally:
            os.close(fd)
    finally:
        os.close(parent)
        anchor.close()


SCAN_ENTRIES = 5000


def op_admin_scan(args: dict[str, object]) -> dict[str, object]:
    """The ``gitdir`` text of every entry of the ``worktrees`` directory (the chain), bounded."""
    try:
        anchor, parent, _ = _open_root(args, parent=False)
    except fsafe.FsafeError as exc:
        if exc.kind != "missing":
            raise
        spec = args["anchor"]
        assert isinstance(spec, dict)
        fsafe.open_anchor(
            str(spec["path"]),
            expected=_identity(spec.get("identity")),
            precheck=bool(spec.get("precheck", True)),
        ).close()
        return {"entries": [], "complete": True}
    entries: list[list[str | None]] = []
    complete = True
    try:
        with os.scandir(parent) as it:
            for e in it:
                if len(entries) >= SCAN_ENTRIES:
                    complete = False
                    break
                text: str | None = None
                try:
                    if e.is_dir(follow_symlinks=False):
                        efd = fsafe.open_dir(anchor, parent, e.name)
                        try:
                            text = _strip_nl(_read_small(anchor, efd, "gitdir"))
                        finally:
                            os.close(efd)
                except (fsafe.FsafeError, OSError):
                    complete = False
                entries.append([e.name, text])
    finally:
        os.close(parent)
        anchor.close()
    return {"entries": entries, "complete": complete}


def op_repair_pointers(args: dict[str, object]) -> dict[str, object]:
    """Rewrite the two pointers of a worktree: ``<path>/.git`` and ``<admin>/gitdir``.

    ``anchor``/``chain``/``root_identity`` reach the worktree directory (its recorded ``dir_id``);
    ``common`` (path, identity) and ``admin`` (the entry name) reach the admin entry through a
    no-follow chain owned by the invoking user. ``<path>/.git`` must be missing or a regular file
    naming exactly ``admin_dir``; anything else is refused and nothing is written. With
    ``write`` false nothing is written either (a dry run). Both files are replaced atomically
    relative to their directory fds and nothing is ever written anywhere else.
    """
    name, path, admin_dir = args.get("admin"), args.get("path"), args.get("admin_dir")
    common = args.get("common")
    if not (
        isinstance(name, str)
        and isinstance(path, str)
        and isinstance(admin_dir, str)
        and isinstance(common, dict)
    ):
        raise fsafe.FsafeError("bad_request", "admin, path, admin_dir and common are required")
    uid = os.geteuid()
    want_dotgit = f"gitdir: {admin_dir}\n".encode()
    want_gitdir = f"{path}/.git\n".encode()
    wanchor, wfd, _ = _open_root(args, parent=False)
    try:
        canchor = fsafe.open_anchor(
            str(common["path"]), expected=_identity(common.get("identity")), precheck=True
        )
        try:
            afd = fsafe.open_chain(
                canchor, canchor.fd, ["worktrees", name], owner_uid=uid, start_path=canchor.path
            )
            try:
                return _repair(wanchor, wfd, canchor, afd, want_dotgit, want_gitdir,
                               admin_dir, bool(args.get("write", True)))  # fmt: skip
            finally:
                os.close(afd)
        finally:
            canchor.close()
    finally:
        os.close(wfd)
        wanchor.close()


def _repair(
    wanchor: fsafe.Anchor,
    wfd: int,
    canchor: fsafe.Anchor,
    afd: int,
    want_dotgit: bytes,
    want_gitdir: bytes,
    admin_dir: str,
    write: bool,
) -> dict[str, object]:
    try:
        st = wanchor.fs.lstat_at(wfd, ".git")
    except FileNotFoundError:
        have_dotgit: bytes | None = None
    else:
        if fsafe.kind_of_mode(st.mode) != "file":
            raise fsafe.FsafeError("not_regular", ".git is not a regular file", path=".git")
        text = _read_small(wanchor, wfd, ".git")
        if text is None or _strip_nl(text) != f"gitdir: {admin_dir}":
            raise fsafe.FsafeError("identity", ".git names another git directory", path=".git")
        have_dotgit = text.encode("utf-8", "surrogateescape")
    have_gitdir = _read_small(canchor, afd, "gitdir")
    wrote: list[str] = []
    if have_dotgit != want_dotgit:
        if write:
            fsafe.write_file_atomic(wfd, ".git", want_dotgit)
        wrote.append(".git")
    if have_gitdir is None or have_gitdir.encode("utf-8", "surrogateescape") != want_gitdir:
        if write:
            fsafe.write_file_atomic(afd, "gitdir", want_gitdir)
        wrote.append("gitdir")
    return {"state": "written" if write else "would_write", "wrote": wrote}



OPS: dict[str, Callable[[dict[str, object]], dict[str, object]]] = {
    "extract_read": op_extract_read,
    "walk": op_walk,
    "delete_tree": op_delete_tree,
    "admin_check": op_admin_check,
    "delete_admin": op_delete_admin,
    "entry_state": op_entry_state,
    "admin_inspect": op_admin_inspect,
    "admin_scan": op_admin_scan,
    "repair_pointers": op_repair_pointers,
    "dir_ids": op_dir_ids,
    "nested_scan": op_nested_scan,
    "baseline_git": op_baseline_git,
    "baseline_claude": op_baseline_claude,
}


def main() -> int:
    fsafe.raise_fd_limit()
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
