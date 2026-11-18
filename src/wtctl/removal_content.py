"""The content checks of a removal (docs/interface.md, Content checks).

Every check returns ``{"status", "details"}`` (``schemas/defs.json#/$defs/checks``). A git command
that fails raises ``internal``: a check is never reported ``pass`` for lack of an answer. The
checks that need the remote's tips (``stash``, ``unpushed_head``, ``unpushed_branches``) are
``skipped`` when there are none, never ``fail``.

The worktree's index is agent-writable, and git's status trusts an index entry whose stat data
matches the file. ``tracked``, ``untracked`` and ``ignored`` therefore run against a private index
rebuilt without stat data (``ls-files -s -z`` into ``update-index --index-info``) and compare
every tracked file by content (SAFE2-3, docs/design.md D53).
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import os
import posixpath
import re
from collections.abc import Iterator, Sequence
from typing import Any

from . import bootstrapper, fsafe, removalfs, worktrees
from .errors import WtctlError
from .fsafe import escape_for_details
from .gitsetup import private_dir
from .hardgit import (
    Git,
    PorcelainError,
    StatusEntry,
    hidden_index_flag,
    parse_ls_files_tagged,
    parse_status_v2,
)
from .ignorematch import Disposable
from .mirror import Mirror, Pushed, Tips

SHA = re.compile(r"[0-9a-f]{40}")
# Config that makes status compare content, never stat data or a cache the agent could forge.
# Only ``core.ignoreStat`` and ``core.splitIndex`` are guarded by a test that fails without them
# (tests/test_removal_round1.py, R13/R14). The other three are belt and braces: the private index
# is rebuilt from ``ls-files -s`` and so holds no stat data and no untracked-cache extension that
# a ``core.checkStat``, ``core.trustctime`` or ``core.untrackedCache`` setting could act on, and
# no test can tell the difference (checked by dropping each: nothing observable changes).
PRIVATE_INDEX_OVERRIDES = (
    ("core.untrackedCache", "false"),
    ("core.checkStat", "default"),
    ("core.trustctime", "true"),
    ("core.ignoreStat", "false"),
    # a split index would write sharedindex.* files into the real git dir
    ("core.splitIndex", "false"),
)
REFLOG_MAX_BYTES = 64 * 1024 * 1024
STASH_REF = "refs/stash"


def check(status: str, details: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    return {"status": status, "details": list(details)}


def skipped() -> dict[str, Any]:
    return check("skipped")


def _fail(what: str, exit_code: int | None, err: str = "") -> WtctlError:
    tail = f": {err.strip().splitlines()[-1][:200]}" if err.strip() else ""
    return WtctlError("internal", f"git {what} failed in the worktree (exit {exit_code}){tail}")


@dataclasses.dataclass
class Inputs:
    """Everything the content checks read."""

    git: Git  # hardened git with the repo's allowed filter drivers
    state: Any
    record: dict[str, Any]
    config: Any
    disposable: list[str]
    mirror: Mirror | None = None
    tips: Tips | None = None
    merged_head: str | None = None
    budget: Any = None
    warnings: list[str] = dataclasses.field(default_factory=list)
    # unpushed_head details about submodules, found by nested_repos (which runs first)
    sub_unpushed: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    _pushed: dict[str, Pushed] = dataclasses.field(default_factory=dict)

    @property
    def gitdir(self) -> str:
        return self.record["admin_dir"] or self.record["common_dir"]

    @property
    def path(self) -> str:
        return self.record["path"]

    def run(self, args: list[str], *, what: str, **kw: Any) -> bytes:
        """A hardened git command in the worktree; a failure is ``internal``."""
        r = self.git.run(args, git_dir=self.gitdir, work_tree=self.path, honor_filters=True, **kw)
        if r.timed_out or r.returncode != 0:
            raise _fail(what, None if r.timed_out else r.returncode, r.err)
        return r.stdout

    def pushed(self, sha: str) -> Pushed:
        if self.mirror is None or self.tips is None:
            return Pushed(False, "no_tips")
        got = self._pushed.get(sha)
        if got is None:
            got = self.mirror.is_pushed(sha, self.tips)
            self._pushed[sha] = got
        return got


# -- the private index ---------------------------------------------------------------------------


@contextlib.contextmanager
def private_index(inp: Inputs) -> Iterator[str]:
    """A wtctl-private index with the entries of the worktree's index and no stat data."""
    staged = inp.run(["ls-files", "-s", "-z"], what="ls-files -s")
    with private_dir() as p:
        index = os.path.join(p, "index")
        r = inp.git.run(
            ["update-index", "-z", "--index-info"],
            stdin=staged,
            git_dir=inp.gitdir,
            work_tree=inp.path,
            index_file=index,
            overrides=PRIVATE_INDEX_OVERRIDES,
        )
        if r.timed_out or r.returncode != 0:
            raise _fail("update-index --index-info", None if r.timed_out else r.returncode, r.err)
        yield index


def _status(inp: Inputs, index: str, args: list[str]) -> list[StatusEntry]:
    r = inp.git.run(
        ["status", "--porcelain=v2", "-z", *args],
        git_dir=inp.gitdir,
        work_tree=inp.path,
        honor_filters=True,
        index_file=index,
        overrides=PRIVATE_INDEX_OVERRIDES,
    )
    if r.timed_out or r.returncode != 0:
        raise _fail("status", None if r.timed_out else r.returncode, r.err)
    try:
        return parse_status_v2(r.stdout)
    except PorcelainError as exc:
        raise WtctlError("internal", f"unreadable git status output: {exc}") from exc


def _text(raw: str) -> str:
    """A path as JSON-safe text (undecodable bytes become ``\\xNN`` escapes)."""
    return raw.encode("utf-8", "backslashreplace").decode("ascii", "replace") if _bad(raw) else raw


def _bad(raw: str) -> bool:
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def hidden_entries(inp: Inputs) -> list[str]:
    """Paths tagged assume-unchanged or skip-worktree in the worktree's own index."""
    out = inp.run(["ls-files", "-v", "-z"], what="ls-files -v")
    try:
        tagged = parse_ls_files_tagged(out)
    except PorcelainError as exc:
        raise WtctlError("internal", f"unreadable git ls-files output: {exc}") from exc
    return sorted({path for tag, path in tagged if hidden_index_flag(tag)})


# -- the bootstrap-created set -------------------------------------------------------------------


@dataclasses.dataclass
class Created:
    untracked: dict[str, str]  # exact path -> content digest
    ignored_files: set[str]
    ignored_dirs: list[str]
    nested: set[str]

    @classmethod
    def load(cls, state: Any, record_id: str) -> Created:
        rows = state.bootstrap_paths(record_id)
        return cls(
            untracked={p: d for p, k, d in rows if k == "untracked" and d},
            ignored_files={p for p, k, _ in rows if k == "ignored_file"},
            ignored_dirs=[p.rstrip("/") for p, k, _ in rows if k == "ignored_dir"],
            nested=set(state.bootstrap_nested(record_id)),
        )

    def under_ignored_dir(self, rel: str) -> bool:
        return any(rel == d or rel.startswith(d + "/") for d in self.ignored_dirs)


class Hasher:
    """Digests of the paths of the created set, read through no-follow descriptors anchored at
    the worktree directory's recorded identity (nothing else in the tree is ever read)."""

    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record
        self.budget = bootstrapper.HashBudget()
        self._wt: Any = None
        self._cm: Any = None
        self._cache: Any = None

    def __enter__(self) -> Hasher:
        self._cm = bootstrapper.open_worktree(
            self.record["path"],
            self.record["admin_dir"] or self.record["common_dir"],
            self.record["dir_id"],
        )
        self._wt = self._cm.__enter__()
        self._cache = bootstrapper._DirCache(self._wt)
        return self

    def __exit__(self, *exc: object) -> None:
        self._cache.close()
        self._cm.__exit__(*exc)

    def digest(self, rel: str) -> str | None:
        return bootstrapper._hash_entry(self._wt, self._cache, rel, self.budget)

    def gitlink_state(self, rel: str) -> str:
        """What stands at the tracked gitlink path ``rel``, without following links: ``absent``,
        ``empty`` (an empty directory, what an uninitialised submodule leaves), ``initialised``
        (a ``.git`` entry is there: the nested walk handles it), ``contents`` (files without a
        ``.git``, which git's status never sees), ``not_dir`` (a file, link or special file in
        place of the directory) or ``unreadable``."""
        parts = rel.split("/")
        try:
            parent = self._cache.get(tuple(parts[:-1]))
            try:
                st = self._wt.anchor.fs.lstat_at(parent, parts[-1])
            except FileNotFoundError:
                return "absent"
            if fsafe.kind_of_mode(st.mode) != "dir":
                return "not_dir"
            fd = fsafe.open_dir(self._wt.anchor, parent, parts[-1])
            try:
                # bounded: a lookup for ``.git``, then at most one entry of the listing (a hostile
                # directory may hold millions, and this runs in the wtctl process)
                try:
                    os.lstat(".git", dir_fd=fd)
                    return "initialised"
                except FileNotFoundError:
                    pass
                with os.scandir(fd) as it:
                    return "empty" if next(it, None) is None else "contents"
            finally:
                os.close(fd)
        except fsafe.FsafeError as exc:
            return "absent" if exc.kind == "missing" else "unreadable"
        except OSError:
            return "unreadable"


def gitlink_paths(inp: Inputs) -> list[str]:
    """The gitlink (mode 160000) paths of the worktree's index. Git's status, run with
    ``--ignore-submodules=all``, never looks below them, so wtctl does."""
    staged = inp.run(["ls-files", "-s", "-z"], what="ls-files -s")
    out: set[str] = set()
    for entry in staged.split(b"\0"):
        meta, _, path = entry.partition(b"\t")
        if meta.startswith(b"160000 ") and path:
            out.add(path.decode("utf-8", "surrogateescape"))
    return sorted(out)


def info_attributes_hide_changes(inp: Inputs) -> bool:
    """``<common>/info/attributes`` is read by git for every worktree of the clone and cannot be
    turned off (``* ident``, a filter or a ``-diff`` rule there can make an edit invisible to
    status). True unless the file is missing or empty; a file wtctl cannot read counts as
    non-empty."""
    common = inp.record["common_dir"]
    try:
        with fsafe.open_anchor(common, expected=_ident(inp.record["common_id"])) as anchor:
            try:
                fd = fsafe.open_chain(anchor, anchor.fd, ["info"], start_path=anchor.path)
            except fsafe.FsafeError as exc:
                return exc.kind != "missing"
            try:
                try:
                    anchor.fs.lstat_at(fd, "attributes")
                except FileNotFoundError:
                    return False
                afd = fsafe.open_file(anchor, fd, "attributes")
                try:
                    return bool(os.read(afd, 1))
                finally:
                    os.close(afd)
            finally:
                os.close(fd)
    except (fsafe.FsafeError, OSError):
        return True


# -- tracked, untracked, ignored -----------------------------------------------------------------


def tracked_untracked_ignored(inp: Inputs) -> dict[str, dict[str, Any]]:
    created = Created.load(inp.state, inp.record["id"])
    disposable = Disposable(inp.disposable)
    tracked: list[dict[str, Any]] = []
    untracked: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    hidden = hidden_entries(inp)
    gitlinks = gitlink_paths(inp)
    with private_index(inp) as index, Hasher(inp.record) as hasher:
        for rel in gitlinks:  # a file or link where a submodule's directory belongs
            if hasher.gitlink_state(rel) in ("not_dir", "unreadable"):
                tracked.append({"path": _text(rel), "detail": "changed"})
        for e in _status(inp, index, ["--untracked-files=all", "--ignored=no"]):
            if e.kind in ("1", "2", "u"):
                tracked.append({"path": _text(e.path), "detail": "changed"})
            elif e.kind == "?":
                rel = e.path
                want = created.untracked.get(rel)
                if want is not None and not rel.endswith("/") and hasher.digest(rel) == want:
                    continue
                untracked.append({"path": _text(rel)})
        # "matching" reports each ignored path at the level of the pattern that ignores it, where
        # "traditional" folds a directory of only-ignored entries into its untracked parent
        # (`pkg/` for `pkg/node_modules/`), which would hide which part is disposable. Every
        # ignored path is still covered by an entry, or by the directory entry above it.
        for e in _status(inp, index, ["--untracked-files=normal", "--ignored=matching"]):
            if e.kind != "!":
                continue
            rel = e.path.rstrip("/")
            is_dir = e.path.endswith("/")
            if _ignored_ok(rel, is_dir, created, disposable, hasher):
                continue
            ignored.append({"path": _text(e.path)})
    tracked += [{"path": _text(p), "detail": "hidden_changes"} for p in hidden]
    if info_attributes_hide_changes(inp):
        tracked.append({"path": "info/attributes", "detail": "attributes"})
    tracked.sort(key=lambda d: (d["path"], d["detail"]))
    untracked.sort(key=lambda d: d["path"])
    ignored.sort(key=lambda d: d["path"])
    return {
        "tracked": check("fail" if tracked else "pass", tracked),
        "untracked": check("fail" if untracked else "pass", untracked),
        "ignored": check("fail" if ignored else "pass", ignored),
    }


def _ignored_ok(
    rel: str, is_dir: bool, created: Created, disposable: Disposable, hasher: Hasher
) -> bool:
    if created.under_ignored_dir(rel):
        return True
    if not is_dir:
        if rel in created.ignored_files:
            return True
        want = created.untracked.get(rel)  # a copied env file is untracked-kind but ignored
        if want is not None and hasher.digest(rel) == want:
            return True
    return disposable.matches(rel, is_dir=is_dir)


# -- nested repositories -------------------------------------------------------------------------


def submodule_paths(inp: Inputs) -> set[str]:
    """The paths listed in ``.gitmodules`` at HEAD (read from the object database, never from the
    worktree's copy)."""
    listing = inp.run(["ls-tree", "-z", "HEAD", "--", ".gitmodules"], what="ls-tree")
    if not listing:
        return set()
    out = inp.run(["config", "--blob", "HEAD:.gitmodules", "--list", "-z"], what="config --blob")
    paths: set[str] = set()
    for entry in out.split(b"\0"):
        key, _, value = entry.partition(b"\n")
        k = key.decode("utf-8", "surrogateescape")
        if k.startswith("submodule.") and k.endswith(".path"):
            paths.add(posixpath.normpath(value.decode("utf-8", "surrogateescape")))
    return paths


def _points_into_modules(content: str | None, inp: Inputs, rel_dir: str) -> bool:
    if not content or not content.startswith("gitdir:"):
        return False
    target = content[len("gitdir:") :].strip()
    if not os.path.isabs(target):
        target = os.path.join(inp.path, rel_dir, target)
    target = posixpath.normpath(target)
    roots = [os.path.join(inp.record["common_dir"], "modules")]
    if inp.record["admin_dir"]:
        roots.append(os.path.join(inp.record["admin_dir"], "modules"))
    return any(target.startswith(r.rstrip("/") + "/") for r in roots)


def nested_repos(inp: Inputs) -> dict[str, Any]:
    limit = inp.config.remove.nested_scan_limit
    created = Created.load(inp.state, inp.record["id"])
    try:
        args = removalfs.walk_args(inp.state, inp.record, limit=limit)
        res = removalfs.run("nested_scan", args, inp.budget)
    except fsafe.FsafeError:
        return check("fail", [{"detail": "incomplete", "path": None}])
    if not res.ok or res.value is None:
        return check("fail", [{"detail": "incomplete", "path": None}])
    details: list[dict[str, Any]] = []
    if res.value["unreadable"]:
        details.append({"detail": "incomplete", "path": None})
    for parts in res.value["mounts"]:
        details.append({"detail": "mount_point", "path": _text("/".join(parts))})
    gits = [g for g in res.value["gits"] if g["path"] != [".git"]]
    gits = [g for g in gits if "/".join(g["path"]) not in created.nested]
    from . import removal_submodules

    sub = removal_submodules.check(inp, gits)
    inp.sub_unpushed = sub.unpushed
    details += sub.dirty
    subs: set[str] | None = None
    for g in gits:
        rel = "/".join(g["path"])
        if sub.covers(tuple(g["path"])):
            continue
        parent = "/".join(g["path"][:-1])
        kind = "nested_repo"
        if g["kind"] == "file" and g["path"][-1] == ".git":
            if subs is None:
                subs = submodule_paths(inp)
            if parent in subs and _points_into_modules(g["content"], inp, parent):
                kind = "submodule"
        details.append({"detail": kind, "path": _text(rel)})
    details += gitlink_contents(inp) + module_git_dirs(inp, sub.gitdirs)
    details.sort(key=lambda d: (d["detail"], d["path"] or ""))
    return check("fail" if details else "pass", details)


def gitlink_contents(inp: Inputs) -> list[dict[str, Any]]:
    """Files below a gitlink path whose directory has no ``.git`` (an uninitialised submodule
    somebody wrote into): git sees none of them, and removal would delete them."""
    try:
        gitlinks = gitlink_paths(inp)
        with Hasher(inp.record) as hasher:
            states = {rel: hasher.gitlink_state(rel) for rel in gitlinks}
    except fsafe.FsafeError:
        return [{"detail": "incomplete", "path": None}]
    out: list[dict[str, Any]] = []
    for rel, state in states.items():
        if state == "contents":
            out.append({"detail": "submodule", "path": _text(rel)})
        elif state == "unreadable":
            out.append({"detail": "incomplete", "path": None})
    return out


MODULES_MAX_DEPTH = 64


def module_git_dirs(
    inp: Inputs, accounted: frozenset[str] | set[str] = frozenset()
) -> list[dict[str, Any]]:
    """Git dirs below ``<admin_dir>/modules`` and ``<common>/modules``: a submodule's repository
    lives there, with any commits not pushed anywhere, and removal deletes the admin entry whether
    or not the submodule's checkout (and so its ``.git`` file) is still there."""
    common = inp.record["common_dir"]
    admin = inp.record["admin_dir"]
    roots = [(common, ["modules"])]
    if admin:
        roots.append((admin, [*os.path.relpath(admin, common).split(os.sep), "modules"]))
    limit = inp.config.remove.nested_scan_limit
    found: list[str] = []
    incomplete = False
    left = [limit]
    try:
        with fsafe.open_anchor(common, expected=_ident(inp.record["common_id"])) as anchor:
            for base, chain in roots:
                try:
                    fd = fsafe.open_chain(anchor, anchor.fd, chain, start_path=anchor.path)
                except fsafe.FsafeError as exc:
                    incomplete = incomplete or exc.kind != "missing"
                    continue
                try:
                    root_path = os.path.join(base, "modules")
                    incomplete |= not _walk_git_dirs(anchor, fd, root_path, 0, left, found)
                finally:
                    os.close(fd)
    except fsafe.FsafeError:
        incomplete = True
    out: list[dict[str, Any]] = [
        {"detail": "submodule", "path": _text(p)} for p in found if p not in accounted
    ]
    if incomplete:
        out.append({"detail": "incomplete", "path": None})
    return out


def _walk_git_dirs(
    anchor: fsafe.Anchor, fd: int, path: str, depth: int, left: list[int], found: list[str]
) -> bool:
    """Collect the git dirs (directories holding ``HEAD``) below ``fd``; False when the walk was
    cut short or something could not be read."""
    if depth > MODULES_MAX_DEPTH:
        return False
    try:
        names = sorted(os.listdir(fd))
    except OSError:
        return False
    left[0] -= len(names)
    if left[0] < 0:
        return False
    if "HEAD" in names:
        found.append(path)
        return True
    complete = True
    for name in names:
        try:
            st = anchor.fs.lstat_at(fd, name)
            if fsafe.kind_of_mode(st.mode) != "dir":
                continue
            sub = fsafe.open_dir(anchor, fd, name)
        except (fsafe.FsafeError, OSError):
            complete = False
            continue
        try:
            complete &= _walk_git_dirs(
                anchor, sub, os.path.join(path, name), depth + 1, left, found
            )
        finally:
            os.close(sub)
    return complete


# -- pushed state: stash, head, side branches ----------------------------------------------------


def head_sha(inp: Inputs) -> str:
    out = inp.run(["rev-parse", "--verify", "HEAD"], what="rev-parse HEAD").decode().strip()
    if not SHA.fullmatch(out):
        raise WtctlError("internal", "cannot read HEAD of the worktree")
    return out


def _is_ancestor(inp: Inputs, commit: str, tip: str) -> bool | None:
    r = inp.git.run(
        ["merge-base", "--is-ancestor", commit, tip], git_dir=inp.gitdir, work_tree=inp.path
    )
    if r.timed_out or r.returncode not in (0, 1):
        return None
    return r.returncode == 0


def _pushed_detail(p: Pushed) -> str:
    return "ancestry_unverified" if p.detail == "ancestry_unverified" else "not_reachable"


def stash(inp: Inputs, head: str) -> dict[str, Any]:
    common = inp.record["common_dir"]
    has = inp.git.run(
        ["rev-parse", "--verify", "-q", STASH_REF], git_dir=common, cwd=common, timeout=60
    )
    if has.timed_out or has.returncode not in (0, 1):
        raise _fail("rev-parse refs/stash", None if has.timed_out else has.returncode, has.err)
    if has.returncode == 1:  # `rev-parse --verify -q` exits 1 for a missing ref, nothing else
        return check("pass")
    r = inp.git.run(
        ["log", "-g", "--format=%H %P", STASH_REF, "--"], git_dir=common, cwd=common, timeout=60
    )
    if r.returncode != 0 or r.timed_out:
        raise _fail("log -g refs/stash", r.returncode, r.err)
    recorded = set(inp.state.stash_ids(inp.record["id"]))
    candidates: list[tuple[int, str, str]] = []
    for n, line in enumerate(x for x in r.out.split("\n") if x):
        fields = line.split(" ")
        sha, parents = fields[0], fields[1:]
        if not SHA.fullmatch(sha) or sha in recorded:
            continue
        first = parents[0] if parents and SHA.fullmatch(parents[0]) else None
        if first is None:
            candidates.append((n, sha, ""))  # no readable parent: cannot rule it out
            continue
        inside = first == head or _is_ancestor(inp, first, head)
        if inside is False:
            continue
        candidates.append((n, sha, first))  # in doubt (None) counts
    details: list[dict[str, Any]] = []
    for n, sha, first in candidates:
        if not first:
            details.append({"ref": f"stash@{{{n}}}", "sha": sha})
            continue
        if inp.tips is None:
            return skipped()
        if not inp.pushed(first).pushed:
            details.append({"ref": f"stash@{{{n}}}", "sha": sha})
    return check("fail" if details else "pass", details)


def merged_head_ok(inp: Inputs, head: str) -> bool:
    """``--merged-head`` exempts HEAD only when it equals HEAD and the tool-owned mirror has seen
    that commit (it was on the remote: reachable now, or fetched while its branch existed). A sha
    the caller made up, or a local-only commit, is not accepted."""
    mh = inp.merged_head
    if mh is None or mh != head:
        return False
    p = inp.pushed(mh)
    return p.pushed or p.detail == "not_ancestor"


def unpushed_head(inp: Inputs, head: str) -> dict[str, Any]:
    extra = [dict(d) for d in inp.sub_unpushed]
    if inp.tips is None:
        return check("fail", extra) if extra else skipped()
    p = inp.pushed(head)
    if p.pushed or merged_head_ok(inp, head):
        return check("fail", extra) if extra else check("pass")
    return check("fail", [{"sha": head, "detail": _pushed_detail(p)}, *extra])


def _epoch(ts: str) -> float | None:
    try:
        return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.UTC).timestamp()
    except ValueError:
        return None


def read_reflog(inp: Inputs) -> list[tuple[str, str, int]] | None:
    """The worktree's HEAD reflog as ``(old, new, timestamp)``; None when it is missing or cannot
    be read in full (fail-safe)."""
    common = inp.record["common_dir"]
    admin = inp.record["admin_dir"] or common
    rel = os.path.relpath(admin, common)
    chain = [] if rel == "." else rel.split(os.sep)
    try:
        with fsafe.open_anchor(common, expected=_ident(inp.record["common_id"])) as anchor:
            fd = fsafe.open_chain(anchor, anchor.fd, [*chain, "logs"], start_path=anchor.path)
            try:
                ffd = fsafe.open_file(anchor, fd, "HEAD")
            finally:
                os.close(fd)
            try:
                raw = os.read(ffd, REFLOG_MAX_BYTES + 1)
            finally:
                os.close(ffd)
    except (fsafe.FsafeError, OSError):
        return None
    if len(raw) > REFLOG_MAX_BYTES:
        return None
    out: list[tuple[str, str, int]] = []
    for line in raw.decode("utf-8", "surrogateescape").split("\n"):
        if not line:
            continue
        head = line.split("\t", 1)[0].split(" ")
        if len(head) < 5 or not SHA.fullmatch(head[0]) or not SHA.fullmatch(head[1]):
            return None
        try:
            out.append((head[0], head[1], int(head[-2])))
        except ValueError:
            return None
    return out


def _ident(text: str | None) -> fsafe.Identity | None:
    if not text:
        return None
    dev, _, ino = text.partition(":")
    return fsafe.Identity(int(dev), int(ino))


def reflog_tips(
    entries: list[tuple[str, str, int]], start_ts: float, start_sha: str | None
) -> set[str] | None:
    """The shas HEAD pointed at since the worktree was made, or None when neither anchor is
    usable. An entry is attributed when its timestamp is at or after the recorded start, or when
    it comes after the start entry (the first entry with the recorded sha at or before the start;
    so an entry backdated with ``GIT_COMMITTER_DATE`` is still attributed).

    Reflog timestamps are whole seconds and the recorded start has milliseconds, so both are
    compared at the start's second: an entry in that second counts as since the start, and the
    time anchor needs an entry strictly before it (an entry in the start second cannot be told
    from one made just after it)."""
    start_sec = int(start_ts // 1)
    start_idx = None
    if start_sha is not None:
        for i, (_old, new, ts) in enumerate(entries):
            if new == start_sha and ts <= start_sec:
                start_idx = i
                break
    time_anchor = any(ts < start_sec for _o, _n, ts in entries)
    if start_idx is None and not time_anchor:
        return None
    tips = {
        new
        for i, (_old, new, ts) in enumerate(entries)
        if ts >= start_sec or (start_idx is not None and i > start_idx)
    }
    if not tips and start_idx is None:
        return None  # a reflog that shows nothing since the start, with no start entry, is cut
    return tips


def unpushed_branches(inp: Inputs, head: str) -> dict[str, Any]:
    start = _epoch(inp.record["reflog_start_ts"] or "")
    entries = read_reflog(inp)
    tips_seen = (
        None
        if entries is None or start is None
        else reflog_tips(entries, start, inp.record["reflog_start_sha"])
    )
    if tips_seen is None:
        return check("fail", [{"branch": None, "sha": None, "detail": "reflog_unavailable"}])
    try:
        heads = worktrees.local_heads(inp.git, inp.record["common_dir"])
    except WtctlError as exc:
        raise WtctlError("internal", exc.message) from exc
    candidates = sorted(
        (ref, sha) for ref, sha in heads.items() if sha in tips_seen and sha != head
    )
    if candidates and inp.tips is None:
        return skipped()
    details: list[dict[str, Any]] = []
    for ref, sha in candidates:
        p = inp.pushed(sha)
        if not p.pushed:
            name = ref[len("refs/heads/") :]
            details.append(
                {"branch": escape_for_details(name), "sha": sha, "detail": _pushed_detail(p)}
            )
    return check("fail" if details else "pass", details)


def not_run() -> dict[str, dict[str, Any]]:
    """Every content check ``skipped`` (integrity failed, or a partial record)."""
    return {
        name: skipped()
        for name in (
            "tracked",
            "untracked",
            "ignored",
            "nested_repos",
            "stash",
            "unpushed_head",
            "unpushed_branches",
        )
    }


def run_all(inp: Inputs) -> dict[str, dict[str, Any]]:
    """All content checks of a worktree, in report order."""
    out = tracked_untracked_ignored(inp)
    out["nested_repos"] = nested_repos(inp)
    head = head_sha(inp)
    out["stash"] = stash(inp, head)
    out["unpushed_head"] = unpushed_head(inp, head)
    out["unpushed_branches"] = unpushed_branches(inp, head)
    return out
