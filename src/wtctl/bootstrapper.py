"""Bootstrap of a worktree (docs/interface.md, Bootstrap): snapshots, env files, steps.

The steps come from the user's config, never from the repository: no repository file is read,
sourced or executed by wtctl itself (a step's own command line may of course run a repo script;
that is the config author's choice). Everything wtctl reads or writes inside the worktree goes
through a no-follow file-descriptor chain anchored at the worktree directory's recorded
identity (``dir_id``), and the clone's main worktree (the ``copy_env`` source) is opened the same
way.

This module returns what happened (``Outcome``); the calling command stores it
(``State.record_bootstrap``) and decides about leases, hooks and exit codes.

TODO(#5): log the ``bootstrap`` events (step runs, copied files) when the log lands.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import fnmatch
import hashlib
import heapq
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from . import fsafe, fsafe_child, gateway, removalfs
from .errors import WtctlError
from .hardgit import Git, PorcelainError, parse_status_v2

# -- bounds (fixed constants, not configurable) -----------------------------------------------

STEP_TAIL_BYTES = 4096  # the output_tail of a step (interface: Bootstrap step 5)
STEP_CAPTURE_BYTES = 64 * 1024  # kept before masking, so a secret cannot straddle the cut
TERM_GRACE = 10.0  # seconds between SIGTERM and SIGKILL of a timed-out step
HASH_FILE_CAP = 256 * 1024 * 1024  # a larger untracked file is recorded as `unhashed:<size>`
HASH_TOTAL_CAP = 1024 * 1024 * 1024  # bytes hashed per snapshot
ENV_FILE_MAX_BYTES = 1024 * 1024  # an env file larger than this is not copied
ENV_FILE_MAX_COUNT = 1000
ENV_WALK_LIMIT = 2_000_000  # directory entries the copy_env source walk reads (docs/design.md)
ENV_WALK_DEPTH = 255  # directory levels the copy_env source walk enters
ENV_BATCH_BYTES = 8 * 1024 * 1024  # content one copy_env read child returns (it may exceed it by
# one file, at most ENV_FILE_MAX_BYTES: a batch ends after the first file that reaches the cap)
ENV_MATCH_RETURN_MAX = 10_000  # matches the walk child returns (the first ones in sorted order)
ENV_MATCH_RETURN_BYTES = 4 * 1024 * 1024  # bytes of paths the walk child returns
ENV_WARN_CAP = 200  # warnings the copy_env walk child returns (the rest are counted in one)
_CHUNK = 1 << 16

Warn = Callable[[str], None]


def _esc(text: str) -> str:
    """A path or name from the repository, safe inside a one-line warning."""
    return fsafe.escape_for_details(text)


# -- the worktree handle ----------------------------------------------------------------------


def parse_identity(text: str) -> fsafe.Identity:
    dev, sep, ino = text.partition(":")
    if not sep or not dev.isdigit() or not ino.isdigit():
        raise fsafe.FsafeError("identity", f"not an identity: {text!r}")
    return fsafe.Identity(int(dev), int(ino))


@dataclasses.dataclass
class Worktree:
    """The open worktree directory: an anchor verified against the recorded ``dir_id``."""

    path: str
    admin_dir: str
    anchor: fsafe.Anchor

    @property
    def fd(self) -> int:
        return self.anchor.fd

    @property
    def cwd(self) -> str:
        """Where a step starts: the directory this handle holds open (immune to a later swap of
        the path), where ``/proc`` offers that, else the path."""
        proc = f"/proc/self/fd/{self.anchor.fd}"
        return proc if os.path.isdir(proc) else self.path


@contextlib.contextmanager
def open_worktree(path: str, admin_dir: str, dir_id: str) -> Iterator[Worktree]:
    """Open ``path`` through a no-follow chain and check its identity (``FsafeError``: a symlink
    on the chain, a mount point, or an identity other than the recorded ``dir_id``)."""
    anchor = fsafe.open_anchor(path, expected=parse_identity(dir_id))
    try:
        yield Worktree(path, admin_dir, anchor)
    finally:
        anchor.close()


# -- snapshots (Bootstrap steps 1 and 4) ------------------------------------------------------


@dataclasses.dataclass
class Snapshot:
    """What ``git status`` lists. ``untracked`` maps an exact path to its digest (the SHA-256 hex
    of a regular file's content; ``symlink:<sha256 of the target>``; ``special:<type>``;
    ``unhashed:<size>`` past a cap). ``untracked_dirs`` are nested repositories (git lists them
    as one directory). ``ignored`` maps a path to whether it is a directory. ``tracked`` are the
    tracked paths with changes. ``skipped`` counts paths that are not valid UTF-8."""

    untracked: dict[str, str] = dataclasses.field(default_factory=dict)
    untracked_dirs: set[str] = dataclasses.field(default_factory=set)
    ignored: dict[str, bool] = dataclasses.field(default_factory=dict)
    tracked: set[str] = dataclasses.field(default_factory=set)
    skipped: int = 0


@dataclasses.dataclass
class HashBudget:
    left: int

    def __init__(self) -> None:
        self.left = HASH_TOTAL_CAP


class _DirCache:
    """The directory fd of the previous entry's parent (status output is sorted by path)."""

    def __init__(self, wt: Worktree) -> None:
        self.wt = wt
        self.parts: tuple[str, ...] | None = None
        self.fd = -1

    def get(self, parts: tuple[str, ...]) -> int:
        if self.parts == parts and self.fd >= 0:
            return self.fd
        self.close()
        self.fd = fsafe.open_chain(
            self.wt.anchor, self.wt.fd, parts, start_path=self.wt.anchor.path
        )
        self.parts = parts
        return self.fd

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
        self.fd, self.parts = -1, None


def _hash_fd(fd: int, size: int) -> str:
    h = hashlib.sha256()
    left = size
    while True:
        data = os.read(fd, _CHUNK)
        if not data:
            break
        left -= len(data)
        h.update(data)
        if left < -_CHUNK * 1024:  # the file kept growing while we read
            break
    return h.hexdigest()


def _hash_entry(wt: Worktree, cache: _DirCache, rel: str, budget: HashBudget) -> str | None:
    """The digest of the untracked entry ``rel`` (never follows a link; never opens a FIFO or
    device). None when the entry vanished or cannot be read safely."""
    parts = tuple(rel.split("/"))
    try:
        dirfd = cache.get(parts[:-1])
        name = parts[-1]
        try:
            pre = wt.anchor.fs.lstat_at(dirfd, name)
        except OSError:
            return None
        if stat.S_ISLNK(pre.mode):
            target = os.readlink(name, dir_fd=dirfd)
            return "symlink:" + hashlib.sha256(os.fsencode(target)).hexdigest()
        if not stat.S_ISREG(pre.mode):
            return "special:" + fsafe.kind_of_mode(pre.mode)
        fd = fsafe.open_file(wt.anchor, dirfd, name)
    except (fsafe.FsafeError, OSError):
        return None
    try:
        size = os.fstat(fd).st_size
        if size > HASH_FILE_CAP or size > budget.left:
            return f"unhashed:{size}"
        budget.left -= size
        return _hash_fd(fd, size)
    except OSError:
        return None
    finally:
        os.close(fd)


def _run_status(git: Git, wt: Worktree, args: Sequence[str]) -> list[Any]:
    res = git.run(
        ["status", "--porcelain=v2", "-z", *args],
        git_dir=wt.admin_dir,
        work_tree=wt.path,
        honor_filters=True,
    )
    if res.timed_out or res.returncode != 0:
        raise WtctlError("internal", f"git status failed in the worktree (exit {res.returncode})")
    try:
        return list(parse_status_v2(res.stdout))
    except PorcelainError as exc:
        raise WtctlError("internal", f"unreadable git status output: {exc}") from exc


def _text(path: str) -> str | None:
    """``path`` if it is valid UTF-8 text (state.db stores text), else None."""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return path


def take_snapshot(git: Git, wt: Worktree) -> Snapshot:
    """Two hardened ``git status --porcelain=v2 -z`` runs: ``--untracked-files=all
    --ignored=no`` for the untracked files (each hashed from an fd opened no-follow) and the
    changed tracked paths, and ``--untracked-files=normal --ignored=traditional`` for the
    ignored entries (an ignored directory is one entry)."""
    snap = Snapshot()
    budget = HashBudget()
    cache = _DirCache(wt)
    try:
        for e in _run_status(git, wt, ["--untracked-files=all", "--ignored=no"]):
            if e.kind in ("1", "2", "u"):
                p = _text(e.path)
                if p is None:
                    snap.skipped += 1
                else:
                    snap.tracked.add(p)
                continue
            if e.kind != "?":
                continue
            p = _text(e.path)
            if p is None or "\0" in p:
                snap.skipped += 1
                continue
            if p.endswith("/"):  # a nested repository
                snap.untracked_dirs.add(p.rstrip("/"))
                continue
            digest = _hash_entry(wt, cache, p, budget)
            if digest is not None:
                snap.untracked[p] = digest
    finally:
        cache.close()
    for e in _run_status(git, wt, ["--untracked-files=normal", "--ignored=traditional"]):
        if e.kind != "!":
            continue
        p = _text(e.path)
        if p is None:
            snap.skipped += 1
            continue
        snap.ignored[p.rstrip("/")] = p.endswith("/")
    return snap


def tracked_paths(git: Git, wt: Worktree) -> set[str]:
    """Every path in the worktree's index (``git ls-files -z``), as text."""
    res = git.run(["ls-files", "-z"], git_dir=wt.admin_dir, work_tree=wt.path)
    if res.timed_out or res.returncode != 0:
        raise WtctlError("internal", f"git ls-files failed in the worktree (exit {res.returncode})")
    out: set[str] = set()
    for raw in res.stdout.split(b"\0"):
        if raw:
            p = _text(raw.decode("utf-8", "surrogateescape"))
            if p is not None:
                out.add(p)
    return out


# -- env files (Bootstrap step 2) -------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Copied:
    path: str  # relative to both roots
    digest: str  # SHA-256 hex of the content written


_GLOB_CHARS = frozenset("*?[")


def _valid_pattern(pattern: str) -> list[str] | None:
    """The components of a copy_env glob, or None when it is not a plain relative pattern."""
    if not pattern or "\0" in pattern or "\\" in pattern or pattern.startswith("/"):
        return None
    comps = pattern.split("/")
    for c in comps:
        if c in ("", ".", ".."):
            return None
        if not (_GLOB_CHARS & set(c)) and fsafe.is_dotgit(c):
            return None
    return comps


def _list_dir(dirfd: int, budget: list[int]) -> tuple[list[str], list[str]]:
    """``(subdirectories, others)`` of an open directory by name, without following links."""
    dirs: list[str] = []
    others: list[str] = []
    with os.scandir(dirfd) as it:
        for e in it:
            budget[0] -= 1
            if budget[0] < 0:
                raise fsafe.FsafeError("limit", "too many directory entries")
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            (dirs if is_dir else others).append(e.name)
    return sorted(dirs), sorted(others)


_Task = tuple["str | None", tuple[str, ...]]


class BoundedMatches(set[tuple[str, ...]]):
    """The walk's matches, bounded: past ``2 * keep`` entries it drops all but the ``keep``
    smallest (sorted order) and remembers that it did (``cut``). The smallest ``keep`` of the
    whole walk are always among those kept, so the result equals sorting the full set and taking
    its first ``keep``, without holding every match of a huge tree."""

    def __init__(self, keep: int) -> None:
        super().__init__()
        self.keep = max(keep, 1)
        self.cut = False

    def add(self, item: tuple[str, ...]) -> None:
        super().add(item)
        if len(self) > 2 * self.keep:
            kept = heapq.nsmallest(self.keep, self)
            super().clear()
            super().update(kept)
            self.cut = True

    def update(self, *others: Any) -> None:
        for other in others:
            for item in other:
                self.add(item)

    def clear(self) -> None:
        super().clear()
        self.cut = False


def _match(
    anchor: fsafe.Anchor,
    root_fd: int,
    comps: Sequence[str],
    out: set[tuple[str, ...]],
    budget: list[int],
    warn: Warn,
) -> None:
    """Collect the relative paths of non-directory entries matching ``comps`` below ``root_fd``.

    Iterative (an explicit stack of open-directory frames), so a hostile deep tree cannot exhaust
    the interpreter stack. Each child is opened no-follow relative to its parent's fd and the fd
    is carried down the walk, so the cost is linear in the number of directories; at most
    ``ENV_WALK_DEPTH`` + 1 fds are open. Directories deeper than ``ENV_WALK_DEPTH`` are not entered.
    """
    base = anchor.path.rstrip("/")
    mounts = frozenset(anchor.mounts_below)
    # frame: [dirfd, rel, path, tasks]; a task is (child name or None for this directory, comps)
    frames: list[tuple[int, tuple[str, ...], str, list[_Task]]] = [
        (root_fd, (), base, [(None, tuple(comps))])
    ]
    too_deep = False
    try:
        while frames:
            dirfd, rel, path, tasks = frames[-1]
            if not tasks:
                frames.pop()
                if frames:  # the root frame's fd is the caller's
                    os.close(dirfd)
                continue
            name, cs = tasks.pop()
            if name is None:
                _match_dir(anchor, dirfd, rel, cs, out, budget, tasks)
                continue
            crel = (*rel, name)
            if len(crel) > ENV_WALK_DEPTH:
                if not too_deep:
                    too_deep = True
                    warn(
                        f"copy_env: not entering directories deeper than {ENV_WALK_DEPTH} "
                        "levels; files below are not copied"
                    )
                continue
            cpath = path + "/" + name
            try:
                if cpath in mounts:
                    raise fsafe.FsafeError("mount_point", f"{cpath} is a mount point")
                cfd = fsafe.open_dir(anchor, dirfd, name)
            except fsafe.FsafeError as exc:
                if exc.kind != "missing":
                    warn(f"copy_env: not entering {_esc('/'.join(crel))}: {exc.kind}")
                continue
            frames.append((cfd, crel, cpath, [(None, cs)]))
    finally:
        for fd, *_ in frames[1:]:
            os.close(fd)


def _match_dir(
    anchor: fsafe.Anchor,
    dirfd: int,
    rel: tuple[str, ...],
    comps: tuple[str, ...],
    out: set[tuple[str, ...]],
    budget: list[int],
    stack: list[_Task],
) -> None:
    """One directory of the walk: add the matches here, push the tasks to run (this directory
    again with fewer components, or a child directory by name)."""
    comp, rest = comps[0], comps[1:]
    if comp == "**":
        if rest:
            stack.append((None, rest))
        dirs, others = _list_dir(dirfd, budget)
        if not rest:
            out.update((*rel, n) for n in others if not fsafe.is_dotgit(n))
        stack.extend((n, comps) for n in reversed(dirs) if not fsafe.is_dotgit(n))
        return
    if _GLOB_CHARS & set(comp):
        dirs, others = _list_dir(dirfd, budget)
        subdirs = set(dirs)
        for name in sorted([*dirs, *others]):
            if not fnmatch.fnmatchcase(name, comp) or fsafe.is_dotgit(name):
                continue
            if not rest:
                out.add((*rel, name))
            elif name in subdirs:
                stack.append((name, rest))
        return
    if fsafe.is_dotgit(comp):
        return
    if rest:
        stack.append((comp, rest))
        return
    try:
        anchor.fs.lstat_at(dirfd, comp)
    except OSError:
        return  # a literal name that does not exist is no match (and no warning)
    out.add((*rel, comp))


def _read_source(anchor: fsafe.Anchor, root_fd: int, rel: tuple[str, ...], max_bytes: int) -> bytes:
    """The content of one source file: every component opened no-follow below the anchor, a
    regular file with link count 1, read from the opened fd (``FsafeError`` otherwise)."""
    dirfd = fsafe.open_chain(anchor, root_fd, rel[:-1], start_path=anchor.path)
    try:
        fd = fsafe.open_file(anchor, dirfd, rel[-1], single_link=True)
    finally:
        os.close(dirfd)
    try:
        size = os.fstat(fd).st_size
        if size > max_bytes:
            raise fsafe.FsafeError("limit", f"too large ({size} bytes)")
        chunks: list[bytes] = []
        got = 0
        while True:
            data = os.read(fd, _CHUNK)
            if not data:
                break
            got += len(data)
            if got > max_bytes:
                raise fsafe.FsafeError("limit", "too large (it grew while being read)")
            chunks.append(data)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_dest(wt: Worktree, rel: tuple[str, ...], data: bytes) -> None:
    """Create the file exclusively (never overwrite, never follow) with mode 0600."""
    fd = os.dup(wt.fd)
    try:
        for comp in rel[:-1]:
            nfd, _created = fsafe.ensure_dir(wt.anchor, fd, comp)
            os.close(fd)
            fd = nfd
        out = fsafe.create_new_file(fd, rel[-1], mode=0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(out, view) :]
            os.fsync(out)
        except OSError as exc:
            # the name is ours and unrecorded: do not leave a partial file behind
            try:
                os.unlink(rel[-1], dir_fd=fd)
            except OSError:
                exc.strerror = f"{exc.strerror}; the partial file could not be removed"
            raise
        finally:
            os.close(out)
    finally:
        os.close(fd)


def _tracked_conflict(rel: tuple[str, ...], folded: set[str]) -> bool:
    """Whether the path, or a directory prefix of it, is a tracked path (compared case-folded,
    so a case-insensitive file system cannot map the copy onto a tracked file)."""
    return any("/".join(rel[: i + 1]).casefold() in folded for i in range(len(rel)))


# -- the child operations (fsafe_child ``env_match`` and ``env_read``) -------------------------


def env_match_op(args: dict[str, Any]) -> dict[str, Any]:
    """Child: open the clone's main worktree, check its ``.git`` identity (``common_id``) and walk
    it for the ``plans`` (lists of glob components). Returns the root ``identity``, the sorted
    ``matches``, the capped ``warnings`` and ``refuse`` (a final warning when the root must not
    be used). An anchor that cannot be opened raises ``FsafeError``."""
    spec = args["anchor"]
    plans = [[str(c) for c in plan] for plan in args["plans"]]
    limit = int(args["limit"])
    common_id = args.get("common_id")
    return_max = int(args.get("return_max", ENV_MATCH_RETURN_MAX))
    return_bytes = int(args.get("return_bytes", ENV_MATCH_RETURN_BYTES))
    anchor = fsafe.open_anchor(str(spec["path"]), expected=None)
    warnings: list[str] = []
    dropped = [0]

    def warn(text: str) -> None:
        if len(warnings) < ENV_WARN_CAP:
            warnings.append(text)
        else:
            dropped[0] += 1

    try:
        root = fsafe.identity_of(anchor.fd)
        out: dict[str, Any] = {
            "identity": [root.dev, root.ino],
            "matches": [],
            "warnings": warnings,
            "refuse": None,
        }
        if common_id:
            try:
                gfd = fsafe.open_dir(anchor, anchor.fd, ".git")
            except fsafe.FsafeError as exc:
                out["refuse"] = (
                    f"copy_env: the clone's .git cannot be checked ({exc.kind}); nothing copied"
                )
                return out
            try:
                ident = fsafe.identity_of(gfd)
            finally:
                os.close(gfd)
            if f"{ident.dev}:{ident.ino}" != str(common_id):
                out["refuse"] = (
                    "copy_env: the clone's main worktree is not the recorded clone; nothing copied"
                )
                return out
        matches = BoundedMatches(return_max)
        budget = [limit]
        try:
            for comps in plans:
                _match(anchor, anchor.fd, comps, matches, budget, warn)
        except (fsafe.FsafeError, OSError, RecursionError) as exc:
            # A walk that does not finish copies nothing: its matches would be an arbitrary
            # prefix of the tree (interface: Bootstrap step 2).
            warn(f"copy_env: the source walk stopped early ({getattr(exc, 'kind', 'io')})")
            matches.clear()
        kept: list[list[str]] = []
        used = 0
        warned = False
        for m in sorted(matches):
            size = sum(len(os.fsencode(c)) + 1 for c in m)
            if len(kept) >= return_max or used + size > return_bytes:
                warn(f"copy_env: only the first {len(kept)} matches are considered")
                warned = True
                break
            kept.append(list(m))
            used += size
        if matches.cut and not warned:  # more matches were dropped during the walk
            warn(f"copy_env: only the first {len(kept)} matches are considered")
        if dropped[0]:
            warnings.append(f"copy_env: {dropped[0]} more warnings were suppressed")
        out["matches"] = kept
        return out
    finally:
        anchor.close()


def env_read_op(args: dict[str, Any]) -> dict[str, Any]:
    """Child: read the files ``paths`` (lists of components) below the clone's main worktree,
    re-opened against the identity ``env_match`` reported. Stops after ``batch_bytes`` of content
    (at least one file is read). ``files`` has one entry per file read: ``{"data": base64}`` or
    ``{"kind", "message"}``; ``next`` is the index of the first file not read."""
    spec = args["anchor"]
    paths = args["paths"]
    max_bytes = int(args["max_bytes"])
    batch_bytes = int(args["batch_bytes"])
    raw = spec.get("identity")
    expected = None
    if raw is not None:
        if not (isinstance(raw, list) and len(raw) == 2 and all(isinstance(i, int) for i in raw)):
            raise fsafe.FsafeError("bad_request", f"not an identity: {raw!r}")
        expected = fsafe.Identity(int(raw[0]), int(raw[1]))
    anchor = fsafe.open_anchor(str(spec["path"]), expected=expected)
    files: list[dict[str, Any]] = []
    total = 0
    try:
        for parts in paths:
            if files and total >= batch_bytes:
                break
            try:
                data = _read_source(anchor, anchor.fd, tuple(str(c) for c in parts), max_bytes)
            except fsafe.FsafeError as exc:
                files.append({"kind": exc.kind, "message": exc.message})
            except OSError as exc:
                files.append({"kind": "io", "message": exc.strerror or str(exc)})
            else:
                total += len(data)
                files.append({"data": base64.b64encode(data).decode("ascii")})
    finally:
        anchor.close()
    return {"files": files, "next": len(files)}


# -- copy_env ---------------------------------------------------------------------------------


def _child_failure(res: Any) -> str:
    return "deadline" if res.timed_out else (res.error.kind if res.error is not None else "failed")


def copy_env_files(
    wt: Worktree,
    *,
    source_root: str,
    patterns: Sequence[str],
    tracked: set[str],
    warn: Warn,
    common_dir: str | None = None,
    common_id: str | None = None,
    on_tick: Callable[[], bool | None] | None = None,
) -> list[Copied]:
    """Copy the files matching ``patterns`` from the clone's main worktree into ``wt``.

    Everything that touches the source (the anchor open, the ``.git`` identity check, the walk and
    every read) runs in child processes under the 60 s walk deadline (``fsafe_child`` ``env_match``
    and ``env_read``); a walk that does not finish copies nothing, and a read that does not finish
    copies nothing more (files of earlier, completed reads stay). The destination is written here,
    in the parent, from the verified bytes. Nothing is ever overwritten or followed; any file that
    cannot be copied under the rules is skipped with a warning. A source root that cannot be opened
    safely copies nothing. ``on_tick`` (the lease renewal) runs before each child; a True result
    (lease lost) stops the copy.
    """
    if not patterns:
        return []
    plans: list[list[str]] = []
    for pat in patterns:
        comps = _valid_pattern(pat)
        if comps is None:
            warn(f"copy_env: pattern {_esc(pat)!r} is not a plain relative glob; skipped")
        else:
            plans.append(comps)
    if not plans:
        return []

    def lost() -> bool:
        if on_tick is not None and on_tick():
            warn("copy_env: the lease was lost; nothing more copied")
            return True
        return False

    if lost():
        return []
    use_common = (
        common_dir is not None and common_id and common_dir == source_root.rstrip("/") + "/.git"
    )
    res = removalfs.run(
        "env_match",
        {
            "anchor": {"path": source_root, "identity": None},
            "plans": plans,
            "limit": ENV_WALK_LIMIT,
            "return_max": ENV_MATCH_RETURN_MAX,
            "return_bytes": ENV_MATCH_RETURN_BYTES,
            "common_id": common_id if use_common else None,
        },
    )
    if not res.ok or res.value is None:
        if res.timed_out:
            warn(
                f"copy_env: the source walk exceeded its {fsafe_child.WALK_DEADLINE:g} s deadline "
                "and was killed; nothing copied"
            )
        else:
            warn(
                "copy_env: the clone's main worktree cannot be opened "
                f"({_child_failure(res)}); nothing copied"
            )
        return []
    value = res.value
    for text in value.get("warnings", []):
        warn(str(text))
    if value.get("refuse"):
        warn(str(value["refuse"]))
        return []
    root_id = value["identity"]
    matches = sorted(tuple(str(c) for c in m) for m in value["matches"][:ENV_MATCH_RETURN_MAX])
    folded = {t.casefold() for t in tracked}
    eligible = [
        rel
        for rel in matches
        if _text("/".join(rel)) is not None
        and "\0" not in "/".join(rel)
        and not _tracked_conflict(rel, folded)
    ]
    position = {rel: i for i, rel in enumerate(eligible)}
    results: dict[tuple[str, ...], dict[str, Any]] = {}
    budget = fsafe_child.Budget(fsafe_child.WALK_DEADLINE)  # all reads together: one walk

    def fetch(rel: tuple[str, ...]) -> bool:
        """Read the next batch of files from ``rel`` on into ``results``."""
        if lost():
            return False
        start = position[rel]
        window = eligible[start : start + ENV_FILE_MAX_COUNT]
        got = removalfs.run(
            "env_read",
            {
                "anchor": {"path": source_root, "identity": root_id},
                "paths": [list(r) for r in window],
                "max_bytes": ENV_FILE_MAX_BYTES,
                "batch_bytes": ENV_BATCH_BYTES,
            },
            budget=budget,
        )
        files = got.value.get("files") if got.ok and got.value is not None else None
        if not files:
            if got.timed_out and got.pid is None:
                warn("copy_env: the read time budget was used up; nothing more copied")
            elif got.timed_out:
                warn(
                    f"copy_env: reading the source exceeded its {fsafe_child.WALK_DEADLINE:g} s "
                    "deadline and was killed; nothing more copied"
                )
            else:
                kind = _child_failure(got)
                warn(f"copy_env: reading the source failed ({kind}); nothing more copied")
            return False
        for k, entry in enumerate(files[: len(window)]):
            results[window[k]] = entry
        return True

    copied: list[Copied] = []
    skipped = [0]

    def skip(text: str) -> None:
        """A per-file warning, at most ``ENV_WARN_CAP`` of them (the rest are counted)."""
        if skipped[0] < ENV_WARN_CAP:
            warn(text)
        skipped[0] += 1

    try:
        _copy_matches(wt, matches, folded, results, fetch, skip, copied, warn)
    finally:
        if skipped[0] > ENV_WARN_CAP:
            warn(f"copy_env: {skipped[0] - ENV_WARN_CAP} more warnings were suppressed")
    return copied


def _copy_matches(
    wt: Worktree,
    matches: list[tuple[str, ...]],
    folded: set[str],
    results: dict[tuple[str, ...], dict[str, Any]],
    fetch: Callable[[tuple[str, ...]], bool],
    warn: Warn,
    copied: list[Copied],
    raw_warn: Warn,
) -> None:
    """``warn`` is the capped per-file helper; ``raw_warn`` is for the once-only count cap."""
    for rel in matches:
        shown = _esc("/".join(rel))
        text = "/".join(rel)
        if _text(text) is None or "\0" in text:
            warn(f"copy_env: {shown!r} is not valid UTF-8; skipped")
            continue
        if len(copied) >= ENV_FILE_MAX_COUNT:
            raw_warn(f"copy_env: more than {ENV_FILE_MAX_COUNT} matches; the rest are skipped")
            break
        if _tracked_conflict(rel, folded):
            warn(f"copy_env: {shown!r} is tracked in the worktree; skipped")
            continue
        if rel not in results and not fetch(rel):
            break
        entry = results.pop(rel, None)
        if entry is None:
            continue
        data = _entry_data(entry)
        if data is None:
            kind = str(entry.get("kind", "io"))
            if kind == "missing":
                continue  # raced away between the walk and the read: silently skipped
            why = {"hardlink": "it is a hard link", "symlink": "it is a symlink"}.get(
                kind, str(entry.get("message", kind)) if kind in ("limit", "io") else kind
            )
            warn(f"copy_env: {shown!r} not copied: {_esc(why)}")
            continue
        try:
            _write_dest(wt, rel, data)
        except fsafe.FsafeError as exc:
            if exc.kind == "missing":
                continue  # raced away: not a match any more
            why = "it already exists" if exc.kind == "exists" else f"destination {exc.kind}"
            warn(f"copy_env: {shown!r} not copied: {why}")
            continue
        except OSError as exc:
            warn(f"copy_env: {shown!r} not copied: {exc.strerror}")
            continue
        copied.append(Copied(text, hashlib.sha256(data).hexdigest()))


def _entry_data(entry: Mapping[str, Any]) -> bytes | None:
    """The bytes of a read result, or None when it is an error entry (or malformed)."""
    raw = entry.get("data")
    if not isinstance(raw, str):
        return None
    try:
        data = base64.b64decode(raw, validate=True)
    except ValueError:
        return None
    return data if len(data) <= ENV_FILE_MAX_BYTES else None


# -- steps (Bootstrap steps 3 and 5) ----------------------------------------------------------


def _safe_path(value: str) -> str:
    """``PATH`` without empty and relative entries: they would resolve a bare command name in
    the worktree, i.e. run repository content (the hardened git applies the same rule)."""
    return ":".join(p for p in value.split(":") if p.startswith("/"))


def step_env(
    config: Any,
    environ: Mapping[str, str],
    settings: Any,
    *,
    record_id: str,
    path: str,
    repo: str,
) -> dict[str, str]:
    """The caller's environment minus every ``hosts.*.token_env`` variable, plus the caller's
    values of ``bootstrap.pass_env``, plus ``bootstrap.env``, plus ``WTCTL_ID``, ``WTCTL_PATH``
    and ``WTCTL_REPO``. Accident prevention, not a boundary (interface: Residual risks)."""
    tokens = {h.token_env for h in config.hosts.values() if h.token_env}
    env = {k: v for k, v in environ.items() if k not in tokens}
    for name in settings.bootstrap.pass_env:
        if name in environ:
            env[name] = environ[name]
    env.update(settings.bootstrap.env)
    env.update(WTCTL_ID=record_id, WTCTL_PATH=path, WTCTL_REPO=repo)
    if "PATH" in env:
        safe = _safe_path(env["PATH"])
        if safe:
            env["PATH"] = safe
        else:  # no absolute entry left: the key goes, so no default can mean "here"
            del env["PATH"]
    return env


def mask_names(config: Any) -> list[str]:
    """Variables whose values never appear in a step's output tail: ``mask_env`` and, as a second
    line of defence, every ``hosts.*.token_env`` (a token passed on by ``pass_env``)."""
    names = list(config.mask_env)
    names += [h.token_env for h in config.hosts.values() if h.token_env]
    return list(dict.fromkeys(names))


def mask_values(config: Any, *environs: Mapping[str, str]) -> set[str]:
    """The values to mask: those of every ``mask_names`` variable in each of ``environs``.

    Callers pass the caller's own environment (a ``token_env`` variable is dropped from the step
    environment, so only the caller's holds it) and the step environment (a value that reached the
    step through ``bootstrap.env`` or ``pass_env``)."""
    return {e[n] for e in environs for n in mask_names(config) if e.get(n)}


def _mask(data: bytes, values: Sequence[str]) -> bytes:
    for v in sorted({v.encode("utf-8", "replace") for v in values if v}, key=len, reverse=True):
        data = data.replace(v, b"***")
    return data


def lease_ticker(lease: Any) -> Callable[[], bool]:
    """The ``on_tick`` of a bootstrap run: renew ``lease`` and return True once it is lost, which
    stops the running step. Any other failure to renew is retried at the next tick."""

    def tick() -> bool:
        try:
            lease.renew()
        except Exception:  # noqa: BLE001 - a lost lease shows in ``lease.lost``
            pass
        return bool(lease.lost)

    return tick


def run_steps(
    steps: Sequence[Any],
    *,
    env: Mapping[str, str],
    cwd: str,
    mask: Sequence[str],
    warn: Warn,
    runner: Callable[..., gateway.StepResult] = gateway.run_step,
    on_tick: Callable[[], bool | None] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Run ``steps`` in order; stop at the first failure. Returns ``(results, failed)``.

    Each result is a ``bootstrap_step`` object: the configured argv, the exit status (None when
    the step was killed, timed out or could not start), the duration and the last 4096 bytes of
    the combined output with the ``mask`` values masked (``mask_values`` computes them).
    ``on_tick`` returning True stops the running step (a lost lease).
    """
    values = list(mask)
    results: list[dict[str, Any]] = []
    for step in steps:
        argv = list(step.argv)
        run_argv = gateway.sh_argv(argv[0]) if step.sh else argv
        res = runner(
            run_argv,
            env=env,
            cwd=cwd,
            timeout=float(step.timeout),
            max_stdout=STEP_CAPTURE_BYTES,
            tail=True,
            term_grace=TERM_GRACE,
            merge_stderr=True,
            on_tick=on_tick,
        )
        raw = res.stdout if res.start_error is None else res.start_error.encode()
        text = _mask(raw, values)[-STEP_TAIL_BYTES:].decode("utf-8", "replace")
        ok_exit = res.returncode if res.returncode is not None and res.returncode >= 0 else None
        exit_ = None if res.timed_out else ok_exit
        if res.output_incomplete:
            warn("a step left a process holding its output open; the output may be incomplete")
        results.append(
            {
                "argv": argv,
                "exit": exit_,
                "duration_ms": int(res.duration * 1000),
                "output_tail": text,
            }
        )
        if exit_ != 0:
            return results, True
    return results, False


# -- nested repositories (Bootstrap step 4) ---------------------------------------------------


def find_nested(wt: Worktree, dirs: Sequence[str], *, limit: int, warn: Warn) -> list[str]:
    """Every ``.git`` file or directory inside the bootstrap-created directories ``dirs``
    (relative paths), found by a no-follow walk that crosses no mount point and never descends
    into a ``.git``. Each walk runs in a child process under the 60 s walk deadline
    (``fsafe_child`` ``nested_scan``). A walk that reaches ``limit`` entries, or fails, stops with
    a warning and uses up the shared budget (fail safe: what it did not list is not exempt)."""
    found: list[str] = []
    left = limit  # one budget for all the directories
    ident = wt.anchor.identity
    for rel in dirs:
        parts = rel.split("/")
        args = {
            "anchor": {"path": wt.anchor.path, "identity": [ident.dev, ident.ino]},
            "chain": parts,
            "limit": left,
        }
        res = removalfs.run("nested_scan", args)
        if not res.ok or res.value is None:
            kind = res.error.kind if res.error is not None else "failed"
            if kind in ("missing", "not_dir", "symlink") and not res.timed_out:
                continue  # the directory is gone or was swapped: nothing to list
            left = 0
            warn(
                f"nested-repository scan of {_esc(rel)!r} stopped ({kind}); "
                "entries it did not list are not exempt"
            )
            continue
        left -= int(res.value["count"])
        for g in res.value["gits"]:
            if g["path"][-1] == ".git" and g["kind"] in ("file", "dir"):
                text = _text("/".join((*parts, *g["path"])))
                if text is not None:
                    found.append(text)
    return sorted(set(found))


# -- the run ----------------------------------------------------------------------------------


@dataclasses.dataclass
class Outcome:
    """What one bootstrap run did. ``state`` is ``ok`` or ``failed``; ``paths`` and ``nested`` are
    the bootstrap-created sets to add (``State.record_bootstrap``)."""

    state: str
    steps: list[dict[str, Any]]
    failed_step: dict[str, Any] | None
    paths: list[tuple[str, str, str | None]]
    nested: list[str]
    tracked_changes: list[str]
    copied: list[Copied]


_UNSTORABLE = ("unhashed:", "special:")


def run_bootstrap(
    *,
    git: Git,
    config: Any,
    settings: Any,
    record: Mapping[str, Any],
    copy_env: bool,
    run_the_steps: bool,
    rerun: bool,
    warn: Warn,
    runner: Callable[..., gateway.StepResult] = gateway.run_step,
    on_tick: Callable[[], bool | None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Outcome:
    """Bootstrap steps 1 to 5 on the worktree of ``record``.

    A create records every new untracked file; a re-run (``rerun``) records only new ignored
    entries and the env files this run itself copied (a re-run never adopts other untracked
    files, which an agent may have written).
    """
    caller = os.environ if environ is None else environ
    env = step_env(
        config,
        caller,
        settings,
        record_id=record["id"],
        path=record["path"],
        repo=record["repo"],
    )
    with open_worktree(record["path"], record["admin_dir"], record["dir_id"]) as wt:
        before = take_snapshot(git, wt)
        copied: list[Copied] = []
        if copy_env:
            if not settings.copy_env:
                warn("--copy-env: no copy_env patterns are configured; nothing was copied")
            else:
                copied = copy_env_files(
                    wt,
                    source_root=record["clone_path"],
                    patterns=settings.copy_env,
                    tracked=tracked_paths(git, wt),
                    warn=warn,
                    common_dir=record.get("common_dir"),
                    common_id=record.get("common_id"),
                    on_tick=on_tick,
                )
        results: list[dict[str, Any]] = []
        failed = False
        if run_the_steps:
            results, failed = run_steps(
                settings.bootstrap.steps,
                env=env,
                cwd=wt.cwd,
                mask=sorted(mask_values(config, caller, env)),
                warn=warn,
                runner=runner,
                on_tick=on_tick,
            )
        after = take_snapshot(git, wt)
        for snap in (before, after):
            if snap.skipped:
                warn(f"{snap.skipped} path(s) that are not valid UTF-8 were not recorded")
        paths: dict[str, tuple[str, str, str | None]] = {}
        new_ignored = {p: d for p, d in after.ignored.items() if p not in before.ignored}
        for p, is_dir in sorted(new_ignored.items()):
            paths[p] = (p, "ignored_dir" if is_dir else "ignored_file", None)
        if not rerun:
            for p, digest in sorted(after.untracked.items()):
                if p in before.untracked:
                    continue
                if digest.startswith(_UNSTORABLE):
                    if digest.startswith("unhashed:"):
                        warn(f"{_esc(p)!r} is too large to hash and is not recorded as created")
                    continue
                paths[p] = (p, "untracked", digest)
        for c in copied:
            paths[c.path] = (c.path, "untracked", c.digest)
        scan = [p for p, d in sorted(new_ignored.items()) if d]
        if not rerun:
            scan += sorted(after.untracked_dirs - before.untracked_dirs)
        nested = find_nested(wt, scan, limit=config.remove.nested_scan_limit, warn=warn)
        tracked = sorted(after.tracked - before.tracked)
    return Outcome(
        state="failed" if failed else "ok",
        steps=results,
        failed_step=results[-1] if failed else None,
        paths=list(paths.values()),
        nested=nested,
        tracked_changes=tracked,
        copied=copied,
    )


__all__ = [
    "Copied",
    "Outcome",
    "Snapshot",
    "Worktree",
    "copy_env_files",
    "find_nested",
    "mask_names",
    "mask_values",
    "open_worktree",
    "run_bootstrap",
    "run_steps",
    "step_env",
    "take_snapshot",
    "tracked_paths",
]
