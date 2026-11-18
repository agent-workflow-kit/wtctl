"""Recursive submodule checks of a removal (docs/interface.md, Content checks, Submodules).

A submodule's checkout, ``.git`` file and git directory are all agent-writable, so nothing in
them is trusted:

* The ``.git`` file must name a directory below ``<admin_dir>/modules`` or ``<common>/modules``
  (for a nested submodule: below the parent's git directory ``modules``). The directory is then
  opened from a descriptor of the recorded common directory, one component at a time with
  ``O_NOFOLLOW``, on the same device and mount, owned by the invoking user; it must hold a
  regular ``HEAD`` and ``config``, no ``commondir``, no ``worktrees``, no alternates and no
  ``info/attributes`` content. A git directory two checkouts claim is refused. Anything else
  refuses the submodule (reason ``gitdir``), never guesses.
* Every git command goes through the hardened ``Git`` (clean environment, the full override set,
  protocols ``none``, explicit ``--git-dir`` and ``--work-tree``): no hook, filter, fsmonitor or
  network access happens in a submodule.
* "Pushed" is local and conservative: the submodule's ``HEAD`` and every local branch, and every
  commit their reflogs still reach, must lie within the history of the commit the superproject's
  ``HEAD`` records for it. Every branch, tag and remote-tracking ref must name a commit (a tag or
  ref of a blob or tree would keep an object only the git directory holds). The recorded commit
  must be reachable from a remote-tracking ref of the submodule, and so must every tag outside
  the recorded history. Remote-tracking refs are not authenticated (an agent can write any of
  them): they vouch for tags and for the recorded commit, which the superproject's own pushed
  check (the mirror) already vouches for, and never for the local ``HEAD`` or a local branch.
* Every git command is charged to the removal budget and capped by what is left of it.
* The recursion is bounded in depth, count and time.
"""

from __future__ import annotations

import dataclasses
import os
import posixpath
from typing import Any, cast

from . import fsafe, removal_content
from .errors import WtctlError
from .gateway import GitResult
from .hardgit import DEFAULT_TIMEOUT, Git
from .ignorematch import Disposable
from .removal_content import Inputs

MAX_DEPTH = 8
MAX_SUBMODULES = 128
MAX_REFS = 1000
MAX_TAG_RESOLVES = 100  # tags of tags resolved one by one; more are refused (``refs``)
MAX_REFLOG = 100_000  # reflog entries read for the pushed check; more is refused
POINTER_MAX = 4000  # the walk keeps 4096 bytes of a .git file: more is not a pointer


@dataclasses.dataclass
class Outcome:
    """What the submodule check found."""

    dirty: list[dict[str, Any]] = dataclasses.field(default_factory=list)  # nested_repos details
    unpushed: list[dict[str, Any]] = dataclasses.field(default_factory=list)  # unpushed_head
    handled: set[tuple[str, ...]] = dataclasses.field(default_factory=set)  # exact .git paths
    refused_dirs: set[tuple[str, ...]] = dataclasses.field(default_factory=set)
    gitdirs: set[str] = dataclasses.field(default_factory=set)  # verified absolute git dirs

    def covers(self, path: tuple[str, ...]) -> bool:
        """True for a ``.git`` entry this check reported on, or one below a refused submodule."""
        if path in self.handled:
            return True
        return any(path[: len(d)] == d for d in self.refused_dirs)


@dataclasses.dataclass
class _Ctx:
    inp: Inputs
    parts: tuple[str, ...]
    gitdir: str
    roots: list[str]
    depth: int


class _BudgetedGit:
    """The hardened ``Git`` with every command charged to the removal budget and capped by what
    is left of it; an exhausted budget refuses the submodule being checked (``deadline``)."""

    def __init__(self, inner: Git, budget: Any) -> None:
        self.inner = inner
        self.budget = budget

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def run(self, argv: Any, **kw: Any) -> GitResult:
        left = self.budget.remaining()
        if left <= 0:
            raise _Refuse("deadline")
        start = self.budget.clock()
        try:
            return self.inner.run(
                argv, **{**kw, "timeout": min(left, kw.get("timeout") or left, DEFAULT_TIMEOUT)}
            )
        finally:
            self.budget.charge(self.budget.clock() - start)


class _Refuse(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check(inp: Inputs, gits: list[dict[str, Any]]) -> Outcome:
    """Check every initialised submodule among ``gits`` (the nested ``.git`` entries of the
    worktree walk) and those below them."""
    out = Outcome()
    if not gits:
        return out
    if inp.budget is not None:
        inp = dataclasses.replace(inp, git=cast(Git, _BudgetedGit(inp.git, inp.budget)))
    by_path = {tuple(g["path"]): g for g in gits}
    try:
        with (
            removal_content.Hasher(inp.record) as hasher,
            fsafe.open_anchor(
                inp.record["common_dir"],
                expected=removal_content._ident(inp.record["common_id"]),
            ) as anchor,
        ):
            _Walk(inp, by_path, hasher, anchor, out).run()
    except (fsafe.FsafeError, _Refuse):  # _Refuse: the budget ran out outside any submodule
        out.dirty.append({"detail": "incomplete", "path": None})
    return out


class _Walk:
    def __init__(
        self,
        inp: Inputs,
        gits: dict[tuple[str, ...], dict[str, Any]],
        hasher: removal_content.Hasher,
        anchor: fsafe.Anchor,
        out: Outcome,
    ) -> None:
        self.inp = inp
        self.gits = gits
        self.hasher = hasher
        self.anchor = anchor
        self.out = out
        self.count = 0
        self.stopped = False
        self.seen: set[fsafe.Identity] = set()
        self.left = [inp.config.remove.nested_scan_limit]
        self.disposable = Disposable(inp.disposable)
        self.pending: list[tuple[str, list[str]]] = []

    def run(self) -> None:
        roots = [os.path.join(self.inp.record["common_dir"], "modules")]
        if self.inp.record["admin_dir"]:
            roots.insert(0, os.path.join(self.inp.record["admin_dir"], "modules"))
        self.repo(_Ctx(self.inp, (), self.inp.gitdir, roots, 0))
        for modules_path, chain in self.pending:
            self.find_orphans(modules_path, chain)

    # -- one repository: its initialised submodules --------------------------------------------

    def repo(self, ctx: _Ctx) -> None:
        gitlinks = removal_content.gitlink_paths(ctx.inp)
        wanted = [
            (gl, ctx.parts + tuple(gl.split("/")))
            for gl in gitlinks
            if ctx.parts + tuple(gl.split("/")) + (".git",) in self.gits
        ]
        if not wanted:
            return
        listed = removal_content.submodule_paths(ctx.inp)
        for gl, parts in wanted:
            g = self.gits[(*parts, ".git")]
            if g["kind"] != "file" or gl not in listed:
                continue  # an embedded git dir or an unlisted path: a nested_repo, as before
            self.submodule(ctx, gl, parts, g)

    def refuse(self, parts: tuple[str, ...], reason: str, *, silent: bool = False) -> None:
        self.out.handled.add((*parts, ".git"))
        self.out.refused_dirs.add(parts)
        if not silent:
            self.out.dirty.append(
                {"detail": "submodule", "path": removal_content._text("/".join(parts)),
                 "reason": reason}
            )  # fmt: skip

    def submodule(self, ctx: _Ctx, gl: str, parts: tuple[str, ...], g: dict[str, Any]) -> None:
        if self.stopped:
            return self.refuse(parts, "", silent=True)
        budget = self.inp.budget
        reason = None
        if ctx.depth + 1 > MAX_DEPTH:
            reason = "too_deep"
        elif self.count + 1 > MAX_SUBMODULES:
            reason = "too_many"
        elif budget is not None and budget.remaining() <= 0:
            reason = "deadline"
        if reason is not None:
            self.stopped = reason != "too_deep"
            return self.refuse(parts, reason)
        self.count += 1
        self.out.handled.add((*parts, ".git"))
        try:
            self.verified(ctx, gl, parts, g)
        except _Refuse as exc:
            self.stopped = self.stopped or exc.reason == "deadline"
            self.refuse(parts, exc.reason)
        except (WtctlError, fsafe.FsafeError, OSError):
            self.refuse(parts, "unreadable")

    # -- one submodule -------------------------------------------------------------------------

    def verified(self, ctx: _Ctx, gl: str, parts: tuple[str, ...], g: dict[str, Any]) -> None:
        rel = removal_content._text("/".join(parts))
        target = self.resolve(ctx, parts, g["content"])
        chain = os.path.relpath(target, self.inp.record["common_dir"]).split(os.sep)
        try:
            ident, problems, orphan_root = self.inspect_gitdir(chain, target)
        except fsafe.FsafeError as exc:
            raise _Refuse("gitdir") from exc
        if ident in self.seen:
            raise _Refuse("gitdir")  # two checkouts (or a cycle) claim one git directory
        self.seen.add(ident)
        self.out.gitdirs.add(target)
        sub = dataclasses.replace(
            ctx.inp,
            submodule=True,
            record={
                **ctx.inp.record,
                "path": os.path.join(self.inp.path, *parts),
                "admin_dir": target,
            },
        )
        reasons = list(problems)
        if "gitdir" in reasons:
            raise _Refuse("gitdir")
        recorded = self.recorded(ctx, gl)
        reasons += self.clean(sub, parts)
        reasons += self.refs(sub, recorded, rel)
        for r in dict.fromkeys(reasons):
            self.out.dirty.append({"detail": "submodule", "path": rel, "reason": r})
        self.repo(_Ctx(sub, parts, target, [target + "/modules"], ctx.depth + 1))
        self.orphans(orphan_root, chain)

    def resolve(self, ctx: _Ctx, parts: tuple[str, ...], content: str | None) -> str:
        """The git directory a ``.git`` file names, only when it lies below ``ctx.roots``."""
        if not content or len(content) >= POINTER_MAX or not content.startswith("gitdir: "):
            raise _Refuse("gitdir")
        body = content[len("gitdir: ") :]
        if body.endswith("\n"):
            body = body[:-1]
        if not body or body != body.strip() or "\n" in body or "\0" in body or "\r" in body:
            raise _Refuse("gitdir")
        if not os.path.isabs(body):
            body = os.path.join(self.inp.path, *parts, body)
        target = posixpath.normpath(body)
        for root in ctx.roots:
            prefix = root.rstrip("/") + "/"
            if target.startswith(prefix) and len(target) > len(prefix):
                try:
                    for c in target[len(prefix) :].split("/"):
                        fsafe.check_component(c)
                except fsafe.FsafeError as exc:
                    raise _Refuse("gitdir") from exc
                return target
        raise _Refuse("gitdir")

    def inspect_gitdir(
        self, chain: list[str], target: str
    ) -> tuple[fsafe.Identity, list[str], str]:
        """Open the git directory no-follow from the recorded common directory and look at what
        git would read from it. Returns its identity, the reasons it is refused or dirty, and the
        path of its ``modules`` directory."""
        anchor = self.anchor
        fd = fsafe.open_chain(
            anchor, anchor.fd, chain, owner_uid=os.geteuid(), start_path=anchor.path
        )
        problems: list[str] = []
        try:
            ident = fsafe.identity_of(fd, anchor.fs)
            for name in ("HEAD", "config"):
                if _kind(anchor, fd, name) != "file":
                    problems.append("gitdir")
            for name in ("commondir", "worktrees"):
                if _kind(anchor, fd, name) is not None:
                    problems.append("gitdir" if name == "commondir" else "worktrees")
            if _kind(anchor, fd, "objects") != "dir":
                problems.append("gitdir")
            if self.nonempty(fd, ["objects", "info"], "alternates"):
                problems.append("gitdir")
            if self.nonempty(fd, ["info"], "attributes"):
                problems.append("attributes")
        finally:
            os.close(fd)
        return ident, problems, target + "/modules"

    def nonempty(self, fd: int, chain: list[str], name: str) -> bool:
        """True when ``chain/name`` below ``fd`` exists and is not an empty regular file; a
        directory that cannot be opened (but exists) counts as non-empty."""
        anchor = self.anchor
        try:
            dfd = fsafe.open_chain(anchor, fd, chain, owner_uid=os.geteuid())
        except fsafe.FsafeError as exc:
            if exc.kind == "missing":
                return False
            return True
        try:
            try:
                st = anchor.fs.lstat_at(dfd, name)
            except FileNotFoundError:
                return False
            if fsafe.kind_of_mode(st.mode) != "file":
                return True
            ffd = fsafe.open_file(anchor, dfd, name)
            try:
                return bool(os.read(ffd, 1))
            finally:
                os.close(ffd)
        except (fsafe.FsafeError, OSError):
            return True
        finally:
            os.close(dfd)

    # -- what the superproject records ---------------------------------------------------------

    def recorded(self, ctx: _Ctx, gl: str) -> str | None:
        """The commit the parent's ``HEAD`` records for ``gl``, or None when the index disagrees
        (a staged pointer change that status, run with ignored submodules, never shows) or the
        entry is not in ``HEAD``."""
        r = ctx.inp.git.run(
            ["rev-parse", "--verify", "-q", f"HEAD:{gl}"],
            git_dir=ctx.inp.gitdir,
            work_tree=ctx.inp.path,
        )
        head = r.stdout.decode().strip() if r.returncode == 0 and not r.timed_out else ""
        staged = ctx.inp.run(["ls-files", "-s", "-z"], what="ls-files -s")
        index = None
        for entry in staged.split(b"\0"):
            meta, _, path = entry.partition(b"\t")
            if meta.startswith(b"160000 ") and path.decode("utf-8", "surrogateescape") == gl:
                index = meta.split(b" ")[1].decode()
        if not removal_content.SHA.fullmatch(head) or head != index:
            return None
        return head

    # -- clean ---------------------------------------------------------------------------------

    def clean(self, sub: Inputs, parts: tuple[str, ...]) -> list[str]:
        reasons: list[str] = []
        if removal_content.hidden_entries(sub):
            reasons.append("hidden_changes")
        with removal_content.private_index(sub) as index:
            for e in removal_content._status(sub, index, ["--untracked-files=all", "--ignored=no"]):
                if e.kind in ("1", "2", "u"):
                    reasons.append("tracked")
                elif e.kind == "?":
                    reasons.append("untracked")
            for e in removal_content._status(
                sub, index, ["--untracked-files=normal", "--ignored=matching"]
            ):
                if e.kind == "!" and not self.disposable.matches(
                    e.path.rstrip("/"), is_dir=e.path.endswith("/")
                ):
                    reasons.append("ignored")
        for gl in removal_content.gitlink_paths(sub):
            state = self.hasher.gitlink_state("/".join((*parts, *gl.split("/"))))
            if state in ("not_dir", "unreadable"):
                reasons.append("tracked")
            elif state == "contents":
                reasons.append("gitlink_contents")
        return reasons

    # -- pushed --------------------------------------------------------------------------------

    def refs(self, sub: Inputs, recorded: str | None, rel: str) -> list[str]:
        reasons: list[str] = []
        if recorded is None:
            reasons.append("gitlink_changed")
        head = sub.run(["rev-parse", "--verify", "HEAD^{commit}"], what="rev-parse HEAD").decode()
        head = head.strip()
        if not removal_content.SHA.fullmatch(head):
            raise _Refuse("unreadable")
        reasons += self.ref_names(sub)
        bad: list[str] = []
        if recorded is not None:
            have = sub.git.run(
                ["rev-parse", "--verify", "-q", f"{recorded}^{{commit}}"],
                git_dir=sub.gitdir,
                work_tree=sub.path,
            )
            if have.timed_out or have.returncode != 0:
                bad.append(head)  # the recorded commit is not here: nothing vouches for HEAD
            else:
                reflog = self.reflog_commits(sub)
                if reflog is None:
                    reasons.append("refs")
                # (a) HEAD, every local branch and every commit their reflogs reach lie within
                #     the recorded commit's history;
                # (b) so does every tag, or it is reachable from a remote-tracking ref;
                # (c) the recorded commit itself is reachable from a remote-tracking ref.
                for argv, stdin in (
                    (
                        ["rev-list", "--max-count=1", "--stdin", "--branches", head,
                         "--not", recorded],
                        b"".join(c.encode() + b"\n" for c in reflog or ()),
                    ),
                    (["rev-list", "--max-count=1", "--tags", "--not", "--remotes", recorded], None),
                    (["rev-list", "--max-count=1", recorded, "--not", "--remotes"], None),
                ):  # fmt: skip
                    r = sub.git.run(argv, git_dir=sub.gitdir, work_tree=sub.path, stdin=stdin)
                    out = r.stdout.decode().strip()
                    if (
                        r.timed_out
                        or r.returncode != 0
                        or (out and not removal_content.SHA.fullmatch(out))
                    ):
                        reasons.append("refs")
                    elif out:
                        bad.append(out)
        for sha in dict.fromkeys(bad):
            self.out.unpushed.append(
                {
                    "sha": sha,
                    "detail": "submodule_unpushed",
                    "submodule": removal_content._text(rel),
                }
            )
        return reasons

    def ref_names(self, sub: Inputs) -> list[str]:
        """Reasons from the refs themselves: a namespace nothing vouches for, a stash, too many
        refs, or a branch, tag or remote-tracking ref that does not name a commit (the rev-list
        checks never see a blob or tree that only such a ref keeps alive)."""
        reasons: list[str] = []
        listing = sub.run(
            [
                "for-each-ref",
                f"--count={MAX_REFS + 1}",
                "--format=%(refname) %(objecttype) %(*objecttype)",
            ],
            what="for-each-ref",
        ).decode("utf-8", "surrogateescape")
        lines = [x for x in listing.split("\n") if x]
        if len(lines) > MAX_REFS:
            reasons.append("refs")
        resolves = 0
        for line in lines[:MAX_REFS]:
            ref, _, types = line.partition(" ")
            kind, _, peeled = types.partition(" ")
            if ref.startswith(("refs/remotes/", "refs/tags/", "refs/heads/")):
                if peeled == "tag":  # a tag of a tag: %(*objecttype) peels one level only
                    resolves += 1
                    if resolves > MAX_TAG_RESOLVES or not self.names_commit(sub, ref):
                        reasons.append("refs")
                elif (peeled or kind) != "commit":
                    reasons.append("refs")
            else:
                reasons.append("stash" if ref == "refs/stash" else "refs")
        return reasons

    def names_commit(self, sub: Inputs, ref: str) -> bool:
        """True when ``ref`` peels, through any number of tags, to a commit."""
        r = sub.git.run(
            ["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
            git_dir=sub.gitdir,
            work_tree=sub.path,
        )
        return not r.timed_out and r.returncode == 0

    def reflog_commits(self, sub: Inputs) -> list[str] | None:
        """The distinct commits the reflogs of ``HEAD`` and of the local branches reach (those of
        remote-tracking refs are left out: git rewrites them on every fetch); None when they
        cannot be listed in full."""
        r = sub.git.run(
            ["rev-list", "-g", f"--max-count={MAX_REFLOG}", "--branches", "HEAD"],
            git_dir=sub.gitdir,
            work_tree=sub.path,
        )
        if r.timed_out or r.returncode != 0:
            return None
        lines = r.stdout.decode().split("\n")
        commits = [x for x in lines if x]
        if len(commits) >= MAX_REFLOG or not all(removal_content.SHA.fullmatch(x) for x in commits):
            return None
        return list(dict.fromkeys(commits))

    # -- git dirs nobody claims ----------------------------------------------------------------

    def orphans(self, modules_path: str, chain: list[str]) -> None:
        self.pending.append((modules_path, chain))

    def find_orphans(self, modules_path: str, chain: list[str]) -> None:
        """Git dirs below a submodule's ``modules`` directory that no checkout claims are refused
        (a ``deinit`` keeps them, with their commits); the walk is bounded like the top-level
        one."""
        anchor = self.anchor
        found: list[str] = []
        complete = True
        try:
            fd = fsafe.open_chain(
                anchor, anchor.fd, [*chain, "modules"], owner_uid=os.geteuid(),
                start_path=anchor.path,
            )  # fmt: skip
        except fsafe.FsafeError as exc:
            if exc.kind != "missing":
                self.out.dirty.append({"detail": "incomplete", "path": None})
            return
        try:
            complete = removal_content._walk_git_dirs(anchor, fd, modules_path, 0, self.left, found)
        finally:
            os.close(fd)
        for p in found:
            if p not in self.out.gitdirs:
                self.out.dirty.append({"detail": "submodule", "path": removal_content._text(p)})
        if not complete:
            self.out.dirty.append({"detail": "incomplete", "path": None})


def _kind(anchor: fsafe.Anchor, fd: int, name: str) -> str | None:
    try:
        return fsafe.kind_of_mode(anchor.fs.lstat_at(fd, name).mode)
    except FileNotFoundError:
        return None
