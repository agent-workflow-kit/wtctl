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

import contextlib
import dataclasses
import fnmatch
import hashlib
import os
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

from . import fsafe, gateway
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
ENV_WALK_LIMIT = 200_000  # directory entries the copy_env source walk reads
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


def _match(
    anchor: fsafe.Anchor,
    dirfd: int,
    rel: tuple[str, ...],
    comps: Sequence[str],
    out: set[tuple[str, ...]],
    budget: list[int],
    warn: Warn,
) -> None:
    """Collect the relative paths of non-directory entries matching ``comps`` below ``dirfd``."""
    comp, rest = comps[0], comps[1:]

    def descend(name: str, sub: Sequence[str]) -> None:
        if fsafe.is_dotgit(name):
            return
        try:
            child = fsafe.open_dir(anchor, dirfd, name)
        except fsafe.FsafeError as exc:
            if exc.kind != "missing":
                warn(f"copy_env: not entering {_esc('/'.join((*rel, name)))}: {exc.kind}")
            return
        try:
            _match(anchor, child, (*rel, name), sub, out, budget, warn)
        finally:
            os.close(child)

    if comp == "**":
        if rest:
            _match(anchor, dirfd, rel, rest, out, budget, warn)
        dirs, others = _list_dir(dirfd, budget)
        if not rest:
            out.update((*rel, n) for n in others if not fsafe.is_dotgit(n))
        for name in dirs:
            descend(name, comps)
        return
    if _GLOB_CHARS & set(comp):
        dirs, others = _list_dir(dirfd, budget)
        names = [n for n in sorted([*dirs, *others]) if fnmatch.fnmatchcase(n, comp)]
        subdirs = set(dirs)
    else:
        names, subdirs = [comp], {comp}
    for name in names:
        if fsafe.is_dotgit(name):
            continue
        if rest:
            if name in subdirs:
                descend(name, rest)
        else:
            out.add((*rel, name))


def _read_source(anchor: fsafe.Anchor, root_fd: int, rel: tuple[str, ...]) -> bytes:
    """The content of one source file: every component opened no-follow below the anchor, a
    regular file with link count 1, read from the opened fd (``FsafeError`` otherwise)."""
    dirfd = fsafe.open_chain(anchor, root_fd, rel[:-1], start_path=anchor.path)
    try:
        fd = fsafe.open_file(anchor, dirfd, rel[-1], single_link=True)
    finally:
        os.close(dirfd)
    try:
        size = os.fstat(fd).st_size
        if size > ENV_FILE_MAX_BYTES:
            raise fsafe.FsafeError("limit", f"too large ({size} bytes)")
        chunks: list[bytes] = []
        got = 0
        while True:
            data = os.read(fd, _CHUNK)
            if not data:
                break
            got += len(data)
            if got > ENV_FILE_MAX_BYTES:
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
        finally:
            os.close(out)
    finally:
        os.close(fd)


def _tracked_conflict(rel: tuple[str, ...], folded: set[str]) -> bool:
    """Whether the path, or a directory prefix of it, is a tracked path (compared case-folded,
    so a case-insensitive file system cannot map the copy onto a tracked file)."""
    return any("/".join(rel[: i + 1]).casefold() in folded for i in range(len(rel)))


def copy_env_files(
    wt: Worktree,
    *,
    source_root: str,
    patterns: Sequence[str],
    tracked: set[str],
    warn: Warn,
    common_dir: str | None = None,
    common_id: str | None = None,
) -> list[Copied]:
    """Copy the files matching ``patterns`` from the clone's main worktree into ``wt``.

    Nothing is ever overwritten or followed; any file that cannot be copied under the rules is
    skipped with a warning. A source root that cannot be opened safely copies nothing.
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
    try:
        src = fsafe.open_anchor(source_root)
    except fsafe.FsafeError as exc:
        warn(f"copy_env: the clone's main worktree cannot be opened ({exc.kind}); nothing copied")
        return []
    try:
        if common_dir is not None and common_id and common_dir == source_root.rstrip("/") + "/.git":
            try:
                gfd = fsafe.open_dir(src, src.fd, ".git")
            except fsafe.FsafeError as exc:
                warn(f"copy_env: the clone's .git cannot be checked ({exc.kind}); nothing copied")
                return []
            try:
                ident = fsafe.identity_of(gfd)
            finally:
                os.close(gfd)
            if f"{ident.dev}:{ident.ino}" != common_id:
                warn(
                    "copy_env: the clone's main worktree is not the recorded clone; nothing copied"
                )
                return []
        matches: set[tuple[str, ...]] = set()
        budget = [ENV_WALK_LIMIT]
        try:
            for comps in plans:
                _match(src, src.fd, (), comps, matches, budget, warn)
        except (fsafe.FsafeError, OSError) as exc:
            warn(f"copy_env: the source walk stopped early ({getattr(exc, 'kind', 'io')})")
        folded = {t.casefold() for t in tracked}
        copied: list[Copied] = []
        for rel in sorted(matches):
            shown = _esc("/".join(rel))
            text = "/".join(rel)
            if _text(text) is None or "\0" in text:
                warn(f"copy_env: {shown!r} is not valid UTF-8; skipped")
                continue
            if len(copied) >= ENV_FILE_MAX_COUNT:
                warn(f"copy_env: more than {ENV_FILE_MAX_COUNT} matches; the rest are skipped")
                break
            if _tracked_conflict(rel, folded):
                warn(f"copy_env: {shown!r} is tracked in the worktree; skipped")
                continue
            try:
                data = _read_source(src, src.fd, rel)
            except fsafe.FsafeError as exc:
                why = {"hardlink": "it is a hard link", "symlink": "it is a symlink"}.get(
                    exc.kind, exc.message if exc.kind == "limit" else exc.kind
                )
                warn(f"copy_env: {shown!r} not copied: {why}")
                continue
            except OSError as exc:
                warn(f"copy_env: {shown!r} not copied: {exc.strerror}")
                continue
            try:
                _write_dest(wt, rel, data)
            except fsafe.FsafeError as exc:
                why = "it already exists" if exc.kind == "exists" else f"destination {exc.kind}"
                warn(f"copy_env: {shown!r} not copied: {why}")
                continue
            except OSError as exc:
                warn(f"copy_env: {shown!r} not copied: {exc.strerror}")
                continue
            copied.append(Copied(text, hashlib.sha256(data).hexdigest()))
        return copied
    finally:
        src.close()


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
        env["PATH"] = _safe_path(env["PATH"])
    return env


def mask_names(config: Any) -> list[str]:
    """Variables whose values never appear in a step's output tail: ``mask_env`` and, as a second
    line of defence, every ``hosts.*.token_env`` (a token passed on by ``pass_env``)."""
    names = list(config.mask_env)
    names += [h.token_env for h in config.hosts.values() if h.token_env]
    return list(dict.fromkeys(names))


def mask_values(config: Any, env: Mapping[str, str]) -> set[str]:
    return {env[n] for n in mask_names(config) if env.get(n)}


def _mask(data: bytes, values: Sequence[str]) -> bytes:
    for v in sorted({v.encode("utf-8", "replace") for v in values if v}, key=len, reverse=True):
        data = data.replace(v, b"***")
    return data


def run_steps(
    steps: Sequence[Any],
    *,
    env: Mapping[str, str],
    cwd: str,
    mask: Sequence[str],
    warn: Warn,
    runner: Callable[..., gateway.StepResult] = gateway.run_step,
    on_tick: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Run ``steps`` in order; stop at the first failure. Returns ``(results, failed)``.

    Each result is a ``bootstrap_step`` object: the configured argv, the exit status (None when
    the step was killed, timed out or could not start), the duration and the last 4096 bytes of
    the combined output with the values of the ``mask`` variables (read from ``env``) masked.
    """
    values = [env[n] for n in mask if env.get(n)]
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
    into a ``.git``. A walk that reaches ``limit`` entries stops with a warning (fail safe: what
    it did not list is not exempt)."""
    found: list[str] = []
    for rel in dirs:
        parts = tuple(rel.split("/"))
        try:
            fd = fsafe.open_chain(wt.anchor, wt.fd, parts, start_path=wt.anchor.path)
        except fsafe.FsafeError:
            continue
        root_path = wt.anchor.path.rstrip("/") + "/" + rel
        try:
            for e in fsafe.walk_tree(
                wt.anchor,
                fd,
                limit=limit,
                descend=lambda ent: ent.path[-1] != ".git",
                root_path=root_path,
            ):
                if e.path[-1] == ".git" and e.kind in ("file", "dir"):
                    text = _text("/".join((*parts, *e.path)))
                    if text is not None:
                        found.append(text)
        except fsafe.FsafeError as exc:
            warn(
                f"nested-repository scan of {_esc(rel)!r} stopped ({exc.kind}); "
                "entries it did not list are not exempt"
            )
        finally:
            os.close(fd)
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
    on_tick: Callable[[], None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Outcome:
    """Bootstrap steps 1 to 5 on the worktree of ``record``.

    A create records every new untracked file; a re-run (``rerun``) records only new ignored
    entries and the env files this run itself copied (a re-run never adopts other untracked
    files, which an agent may have written).
    """
    env = step_env(
        config,
        os.environ if environ is None else environ,
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
                )
        results: list[dict[str, Any]] = []
        failed = False
        if run_the_steps:
            results, failed = run_steps(
                settings.bootstrap.steps,
                env=env,
                cwd=wt.cwd,
                mask=mask_names(config),
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
