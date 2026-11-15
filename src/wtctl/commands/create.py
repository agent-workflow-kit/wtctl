"""``wtctl create``: a worktree (docs/interface.md, Create order).

TODO(#21): ``create --throwaway`` (the throwaway create order) lives in its own issue.
TODO(#5): log ``create``, ``fetch``, ``hook`` and ``refused`` events (create is logged, not
audited).
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import sqlite3
import stat
from typing import Any

from .. import baselinesvc, bootstrapper, fsafe, hooks, naming, repobaseline, worktrees
from .. import labels as labelmod
from ..context import Context, Result
from ..errors import UsageError, WtctlError
from ..gitsetup import open_git, with_filters
from ..hardgit import Git
from ..locate import Resolver, describe_mount, identity_str
from ..mirror import Mirror, Tips
from ..packs import clone_has_commit, transfer_base
from ..state import (
    Conflict,
    Lease,
    Record,
    State,
    check_holder,
    clone_lease,
    record_lease,
    state_path,
)
from ..worktrees import SHA, AddSpec

NAME_RE = naming.NAME_RE
CREATE_HOOKS = ("pre-create", "post-create")  # the hooks that can fire on create
EMPTY_BOOTSTRAP = {"state": "none", "steps": [], "created_paths_count": 0}

# Fields of the record object, in the order docs/interface.md lists them.
# TODO(#7): replace with wtctl.record_view.record_object once list/show land.
FIELDS = (
    "id object_type owned state repo clone_path common_dir common_id admin_dir dir_id base_dir "
    "root_id path name branch created_branch base base_sha head_at_create ref source depth "
    "fetch_url remote labels holders baseline_digest bootstrap created updated missing_since "
    "removed_at last_error"
).split()


def record_object(record: Record, bootstrap: dict[str, Any] | None = None) -> dict[str, Any]:
    """The record object. ``bootstrap`` is ``State.bootstrap_summary``; without it (a record that
    is not stored) the bootstrap is the empty one."""
    full = dict(record)
    full["bootstrap"] = bootstrap or {
        "state": record.get("bootstrap_state", "none"),
        "steps": [],
        "created_paths_count": 0,
    }
    return {k: full.get(k) for k in FIELDS}


# -- usage ------------------------------------------------------------------------------------


@dataclasses.dataclass
class Options:
    repo: str
    branch: str | None
    detach: bool
    base: str | None
    ref: str | None
    name: str | None
    holder: str | None
    labels: list[str]
    hook_flags: dict[str, list[str]]
    no_bootstrap: bool
    copy_env: bool
    override_escalation: bool


def _no_option_like(value: str, what: str) -> None:
    if value.startswith("-"):
        raise UsageError(f"{what} {value!r} starts with '-'")


def parse_options(ctx: Context) -> Options:
    """Stage 1 of the check order that needs no git (exit 2)."""
    a = ctx.args
    if a.source is not None or a.depth is not None:
        raise UsageError("--source and --depth are only for --throwaway")
    if a.name is not None and not NAME_RE.fullmatch(a.name):
        raise UsageError(f"invalid name {a.name!r}: expected ^[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}$")
    if a.holder is not None:
        check_holder(a.holder)
    if a.ref is not None and not SHA.fullmatch(a.ref):
        raise UsageError("--ref must be a full 40-digit lowercase hex commit id")
    if a.base is not None:
        _no_option_like(a.base, "--base")
        if a.base.startswith("refs/"):
            raise UsageError("--base is a branch name, not a ref: drop the refs/ prefix")
    if a.branch is not None:
        _no_option_like(a.branch, "--branch")
    labelmod.merge_labels((), a.label)
    hook_flags = hooks.parse_hook_flags(a.hook)
    for name in hook_flags:
        if name not in CREATE_HOOKS:
            raise UsageError(f"--hook {name}: create fires only {', '.join(CREATE_HOOKS)}")
    return Options(
        repo=a.repo,
        branch=a.branch,
        detach=a.detach,
        base=a.base,
        ref=a.ref,
        name=a.name,
        holder=a.holder,
        labels=list(a.label),
        hook_flags=hook_flags,
        no_bootstrap=a.no_bootstrap,
        copy_env=a.copy_env,
        override_escalation=a.override_escalation,
    )


# -- the plan ---------------------------------------------------------------------------------


@dataclasses.dataclass
class Plan:
    action: str  # existing | track | new | detached
    branch: str | None
    start: str | None  # the commit the worktree starts at (the branch tip for `existing`)
    base: str | None
    base_sha: str | None
    created_branch: bool


class Reused(Exception):
    """The retry rule applied: ``record`` is returned unchanged."""

    def __init__(self, record: Record) -> None:
        super().__init__(record["id"])
        self.record = record


class _WaitForCreator(Exception):
    """A live creator holds the lease of a ``creating`` record this holder's retry collides with:
    unwind (releasing the per-clone lease), wait for the record's lease, then plan again."""

    def __init__(self, record_id: str) -> None:
        super().__init__(record_id)
        self.record_id = record_id


def _within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


class Creator:
    def __init__(self, ctx: Context, opts: Options) -> None:
        self.ctx = ctx
        self.o = opts
        self.config = ctx.config
        self.state: State = ctx.state
        self.dry = ctx.dry_run
        self.warnings: list[str] = []
        self.settings = self.config.repo(opts.repo)
        self._waited: set[str] = set()  # creating records whose creator was already waited for

    # -- entry ---------------------------------------------------------------------------

    def run(self) -> Result:
        o = self.o
        self.git: Git = open_git(self.config, self.ctx.gateway)  # git_too_old (exit 12)
        for value, what in ((o.branch, "--branch"), (o.base, "--base")):
            if value is not None:
                worktrees.check_ref_name(self.git, value, what=what)
        resolver = Resolver(self.config, self.state, self.git)
        res = resolver.locate(o.repo, auto_clone=False if self.dry else None)
        res.require_sha1()
        res.require_anchor()
        self.res = res
        self.warnings += res.warnings
        self.wgit = self._git_with_filters()
        while True:
            try:
                with self.state.lease(
                    clone_lease(res.clone_path), wait=self.config.lease_wait_s
                ) as lease:
                    return self._locked(resolver, lease)
            except Reused as r:
                return self._result_reused(r.record)
            except _WaitForCreator as w:
                self._wait_for_creator(w.record_id)  # without the per-clone lease; then replan

    def _wait_for_creator(self, record_id: str) -> None:
        """Wait for a creator's record lease to be released. A timeout is not an error here: the
        record is looked at again and, if it is not ``active``, the documented refusal follows."""
        self._waited.add(record_id)
        try:
            with self.state.lease(record_lease(record_id), wait=self.config.lease_wait_s):
                pass
        except WtctlError as exc:
            if exc.code != "busy":
                raise

    def _git_with_filters(self) -> Git:
        return with_filters(
            self.git, self.settings.allow_filters, self.ctx.gateway, self.warnings.append
        )

    # -- under the per-clone lease (steps 5 to 13) -------------------------------------------

    def _locked(self, resolver: Resolver, lease: Lease) -> Result:
        res, o = self.res, self.o
        # 6. the recorded URL, verified again under the lease
        again, _ = resolver.find(o.repo)
        if again is None or (again.common_dir, again.fetch_url) != (res.common_dir, res.fetch_url):
            raise WtctlError(
                "remote_mismatch", f"the clone of {o.repo} changed while create was starting"
            )
        again.require_anchor()
        # 4. the record id (a vetoed create burns it)
        self.rid = self.state.preview_id() if self.dry else self.state.allocate_id()
        mirror = Mirror(
            git=self.git,
            cfg=self.config,
            state=self.state,
            gateway=self.ctx.gateway,
            repo=o.repo,
            clone_path=res.clone_path,
            common_dir=res.common_dir,
            fetch_url=res.fetch_url,
            environ=os.environ,
            warn=self.warnings.append,
        )
        self.mirror = mirror
        heads = worktrees.local_heads(self.git, res.common_dir)
        live = self.state.live_records_for_repo(o.repo)
        self.name, self.branch, self.path = self._resolve_names(heads, live)
        # 8. refresh the mirror unless the branch exists locally and no base is needed
        tips: Tips | None = None
        needs_base = o.base is not None or o.ref is not None
        if not (o.branch is not None and f"refs/heads/{o.branch}" in heads and not needs_base):
            tips = mirror.refresh(lease).tips
        # 9. classify the branch
        plan = self._classify(heads, tips, live)
        # 10. the path
        self._check_path(live)
        root = self._prepare_root(write=False)
        self._driver_config_check(plan, early=True)
        record_fields = self._record_fields(plan, root)
        if self.dry:
            return self._result_dry(record_fields, plan)
        # 11. pre-create hooks: nothing outside the mirror has been written yet
        decision_ids = self._pre_create(plan)
        self._prepare_root(write=True)
        # 12. the record and its lease, in one transaction
        try:
            record, rlease = self.state.insert_record_leased(
                record_fields, labels=o.labels, holders=[o.holder] if o.holder else []
            )
        except Conflict as exc:  # another actor took the name or path since step 7 and 10
            raise WtctlError(
                "refused", f"{self.path} was taken meanwhile ({exc.column})", reason="path_exists"
            ) from None
        try:
            self._add(plan, record, lease)
        except BaseException:
            rlease.release()
            raise
        lease.release()
        with rlease:
            return self._finish(record, plan, decision_ids, rlease)

    # -- names (step 7) ----------------------------------------------------------------------

    def _by_name(self, live: list[Record], name: str) -> Record | None:
        folded = name.casefold()
        for r in live:
            if r["name"] is not None and r["name"].casefold() == folded:
                return r
        return None

    @staticmethod
    def _case_variant(heads: dict[str, str], ref: str) -> str | None:
        """The tip of a local branch whose name differs from ``ref`` only by case (a collision on
        a case-insensitive file system, and confusing on any)."""
        folded = ref.casefold()
        for name, sha in heads.items():
            if name != ref and name.casefold() == folded:
                return sha
        return None

    def _by_path(self, live: list[Record], path: str) -> Record | None:
        for r in live:
            if r["path"] == path:
                return r
        return None

    def _maybe_reuse(self, rec: Record | None) -> None:
        """The retry rule: an active record that lists this holder is returned unchanged."""
        if rec is None or not self.o.holder or self.o.holder not in rec["holders"]:
            return
        if rec["state"] == "creating" and not self.dry and rec["id"] not in self._waited:
            # a live creator holds the record's lease for the whole `creating` window (the
            # baseline step makes it long enough for a concurrent retry): leave, without the
            # per-clone lease, wait for it, and plan again. Not in a dry run (it takes no lease).
            raise _WaitForCreator(rec["id"])
        if rec["state"] == "active":
            raise Reused(self.state.get_record(rec["id"]) or rec)

    def _worktree_base_dir(self) -> str:
        return os.path.realpath(self.settings.worktree_root)

    def _render_path(self, name: str) -> str:
        host = self.res.repo.partition("/")[0]
        project = self.res.repo.partition("/")[2]
        try:
            return naming.render_path(
                self.config.worktree_path,
                root=self._worktree_base_dir(),
                host=host,
                path=project,
                name=name,
                record_id=self.rid,
            )
        except naming.PathNewlineError as exc:
            raise WtctlError("create_failed", f"worktree_path cannot be used: {exc}") from None
        except ValueError as exc:
            raise UsageError(f"worktree_path cannot be used: {exc}") from None

    def _render_branch(self, name: str) -> str:
        date = self.ctx.gateway.clock.now().astimezone().strftime("%Y%m%d")
        branch = naming.render_branch(
            self.config.branch_prefix,
            self.settings.branch_template,
            name=name,
            record_id=self.rid,
            date=date,
        )
        worktrees.check_ref_name(self.git, branch, what="the rendered branch")
        return branch

    def _resolve_names(
        self, heads: dict[str, str], live: list[Record]
    ) -> tuple[str, str | None, str]:
        o = self.o
        explicit = o.name is not None
        if explicit:
            bases = [o.name]
        else:
            seed = naming.name_from_branch(o.branch) if o.branch is not None else ""
            bases = naming.candidates(seed or naming.name_from_id(self.rid))
        template_built = o.branch is None and not o.detach
        for name in bases:
            assert name is not None
            holder = self._by_name(live, name)
            if holder is not None:
                if explicit:
                    self._maybe_reuse(holder)
                    raise WtctlError(
                        "refused",
                        f"a worktree named {name!r} already exists for {o.repo}",
                        reason="path_exists",
                        holder_id=holder["id"],
                    )
                continue
            path = self._render_path(name)
            if not explicit and os.path.lexists(path):
                self.warnings.append(f"{path} already exists; the name {name!r} was skipped")
                continue
            if template_built:
                branch = self._render_branch(name)
                ref = f"refs/heads/{branch}"
                tip = heads.get(ref) or self._case_variant(heads, ref)
                if tip is not None:
                    if explicit:
                        raise WtctlError(
                            "refused",
                            f"branch {branch} already exists",
                            reason="branch_exists",
                            details={"tip": tip},
                        )
                    continue
                return name, branch, path
            return name, o.branch, path
        raise WtctlError(
            "create_failed", f"no free name for {o.repo} among {naming.MAX_SUFFIX} candidates"
        )

    # -- classification (step 9) -------------------------------------------------------------

    def _default_base(self) -> str:
        o = self.o
        name = o.base or self.settings.base
        if name is None:
            row = self.state.get_clone(o.repo) or {}
            name = row.get("default_branch")
        if not name:
            raise WtctlError(
                "ref_not_found",
                f"{o.repo} has no known default branch: pass --base or set repos.<id>.base",
            )
        return str(name)

    def _resolve_base(self, heads: dict[str, str], tips: Tips | None) -> tuple[str, str]:
        name = self._default_base()
        ref = f"refs/heads/{name}"
        if tips is not None and ref in tips.refs:
            return ref, tips.refs[ref]
        if ref in heads:
            return ref, heads[ref]
        raise WtctlError("ref_not_found", f"base branch {name!r} was not found for {self.o.repo}")

    def _check_ref_commit(self, sha: str, tips: Tips | None) -> None:
        """A ``--ref`` must be a commit in the clone or in the mirror."""
        if clone_has_commit(self.git, self.res.common_dir, sha):
            return
        if tips is not None and clone_has_commit(self.git, self.mirror.path, sha):
            return
        raise WtctlError("ref_not_found", f"commit {sha} is neither in the clone nor in the mirror")

    def _classify(self, heads: dict[str, str], tips: Tips | None, live: list[Record]) -> Plan:
        o = self.o
        branch = self.branch
        if o.ref is not None:
            self._check_ref_commit(o.ref, tips)
        if branch is None:  # --detach
            base = base_sha = None
            if o.ref is None or o.base is not None:
                base, base_sha = self._resolve_base(heads, tips)
            return Plan("detached", None, o.ref or base_sha, base, base_sha, False)
        self._check_in_use(branch, live)
        local = heads.get(f"refs/heads/{branch}")
        remote = None if tips is None else tips.refs.get(f"refs/heads/{branch}")
        if local is not None or (o.branch is not None and remote is not None):
            tip = local if local is not None else remote
            assert tip is not None
            if o.base is not None or (o.ref is not None and o.ref != tip):
                raise WtctlError(
                    "refused",
                    f"branch {branch} already exists; it cannot be combined with --base or "
                    "another --ref",
                    reason="branch_exists",
                    details={"tip": tip},
                )
            if local is not None:
                return Plan("existing", branch, tip, None, None, False)
            return Plan("track", branch, tip, None, None, True)
        variant = self._case_variant(heads, f"refs/heads/{branch}")
        if variant is not None:
            raise WtctlError(
                "refused",
                f"branch {branch} already exists under a name that differs only by case",
                reason="branch_exists",
                details={"tip": variant},
            )
        base = base_sha = None
        if o.ref is None or o.base is not None:
            base, base_sha = self._resolve_base(heads, tips)
        start = o.ref or base_sha
        return Plan("new", branch, start, base, base_sha, True)

    def _check_in_use(self, branch: str, live: list[Record]) -> None:
        ref = f"refs/heads/{branch}"
        for entry in worktrees.worktree_entries(self.git, self.res.common_dir):
            if entry.branch == ref:
                holder = self._by_path(live, os.path.realpath(entry.path)) or self._by_path(
                    live, entry.path
                )
                self._maybe_reuse(holder)
                raise WtctlError(
                    "refused",
                    f"branch {branch} is checked out in {entry.path}",
                    reason="branch_in_use",
                    holder_path=entry.path,
                    holder_id=None if holder is None else holder["id"],
                )
        for r in live:
            if r["state"] == "creating" and r["branch"] == branch:
                raise WtctlError(
                    "refused",
                    f"branch {branch} is planned by the record {r['id']}",
                    reason="branch_in_use",
                    holder_path=r["path"],
                    holder_id=r["id"],
                )

    # -- the path (step 10) ------------------------------------------------------------------

    def _known_dirs(self) -> list[str]:
        """Every directory a new worktree must not lie in or around: the clones, and the worktrees
        of every repo (live records of all repos, and the worktree list of each known clone)."""
        dirs = [self.res.clone_path]
        dirs += [c.clone for c in map(self.config.repo, self.config.repos) if c.clone]
        try:
            rows = self._clone_rows()
        except sqlite3.Error as exc:
            raise WtctlError("internal", f"cannot read the known clones: {exc}") from exc
        own = os.path.realpath(self.res.common_dir)
        for row in rows:
            dirs.append(row["path"])
            common = row.get("common_dir")
            if common and os.path.realpath(common) != own and os.path.isdir(common):
                dirs += self._other_clone_worktrees(row["path"], common)
        dirs += [e.path for e in worktrees.worktree_entries(self.git, self.res.common_dir)]
        dirs += [r["path"] for r in self.state.records()]
        return [os.path.realpath(d) for d in dirs if d]

    @staticmethod
    def _other_clone_worktrees(clone: str, common: str) -> list[str]:
        """The worktree paths of a clone other than the target's, from its admin entries alone (no
        git call runs against it); fails closed, naming the clone, when they cannot be read."""
        try:
            gitdirs = worktrees.admin_gitdirs(common)
        except OSError as exc:
            raise WtctlError(
                "internal", f"cannot read the worktrees of the known clone {clone}: {exc.strerror}"
            ) from exc
        return [g.removesuffix("/.git") for g in gitdirs]

    def _clone_rows(self) -> list[dict[str, Any]]:
        rows = self.state._conn.execute("SELECT path, common_dir FROM clones").fetchall()  # noqa: SLF001
        return [dict(r) for r in rows]

    def _check_path(self, live: list[Record]) -> None:
        path = self.path
        holder = self._by_path(live, path)
        if holder is not None:
            self._maybe_reuse(holder)
            raise WtctlError(
                "refused",
                f"{path} is the path of the record {holder['id']}",
                reason="path_exists",
                holder_id=holder["id"],
            )
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            pass
        except (NotADirectoryError, OSError) as exc:
            if isinstance(exc, PermissionError):
                raise WtctlError(
                    "create_failed", f"cannot examine {path}: {exc.strerror}"
                ) from None
            raise WtctlError("refused", f"{path} cannot be used", reason="path_exists") from None
        else:
            try:
                occupied = not stat.S_ISDIR(st.st_mode) or bool(os.listdir(path))
            except OSError as exc:
                raise WtctlError(
                    "create_failed", f"cannot examine {path}: {exc.strerror}"
                ) from None
            if occupied:
                raise WtctlError("refused", f"{path} already exists", reason="path_exists")
        for known in self._known_dirs():
            if _within(path, known):
                raise UsageError(f"{path} lies inside the existing worktree {known}")
            if _within(known, path):
                raise UsageError(f"{path} contains the existing worktree {known}")
        private = [str(state_path().parent), self.config.effective_throwaway_root]
        for d in private:
            if _within(path, os.path.realpath(d)):
                raise UsageError(f"{path} lies inside {d}")

    def _prepare_root(self, *, write: bool) -> str:
        """The worktree root as an anchor (create order step 10); returns its realpath.

        Without ``write`` (step 10, and every dry run) an existing root is only checked against its
        recorded identity. With it, right after ``pre-create`` (which must see nothing written
        outside the mirror), a missing root is created, recorded on first use and the directories
        down to the worktree are made through the anchor fd.
        """
        root = self._worktree_base_dir()
        if not write and not os.path.isdir(root):
            return root
        if write:
            try:
                os.makedirs(root, mode=0o755, exist_ok=True)
            except OSError as exc:
                raise WtctlError("create_failed", f"cannot create {root}: {exc.strerror}") from None
        recorded = self.state.anchor_identity(root)
        expected = None
        if recorded is not None:
            dev, _, ino = recorded.partition(":")
            expected = fsafe.Identity(int(dev), int(ino))
        try:
            anchor = fsafe.open_anchor(root, expected=expected)
        except fsafe.FsafeError as exc:
            raise self._root_error(root, recorded, exc) from None
        with anchor:
            current = f"{anchor.identity.dev}:{anchor.identity.ino}"
            if recorded is None and write:
                recorded = self.state.record_anchor(root, current)
            if recorded is not None and recorded != current:
                raise self._root_error(root, recorded, None)
            if write:
                self._make_parents(anchor, root)
        return root

    def _root_error(
        self,
        root: str,
        recorded: str | None,
        exc: fsafe.FsafeError | None,
        *,
        message: str | None = None,
        changed: bool = True,
    ) -> WtctlError:
        if exc is not None and exc.kind not in ("identity", "mount_point"):
            return WtctlError(
                "create_failed", f"cannot open the worktree root {root}: {exc.message}"
            )
        current = "0:0"
        info = {"fs_type": "unknown", "mount_source": "", "mount_point": "/"}
        with contextlib.suppress(OSError):
            st = os.stat(root)
            current = identity_str(st)
            info = describe_mount(root, st)
        entry = {
            "anchor": "worktree_root",
            "path": fsafe.escape_for_details(root),
            "recorded": recorded or current,
            "current": current,
            "changed": changed,
            **info,
        }
        if recorded is None:
            entry["recorded"] = "0:0" if current != "0:0" else "0:1"
        return WtctlError(
            "create_failed",
            message or f"the worktree root {root} is not the directory wtctl recorded for it",
            details={"cause": "anchor_changed", "anchors": [entry]},
        )

    def _make_parents(self, anchor: fsafe.Anchor, root: str) -> None:
        """Create the directories between the root and the worktree through the anchor fd
        (no-follow), so a planted symlink below the root is never walked."""
        rel = os.path.relpath(os.path.dirname(self.path), root)
        parts = [] if rel == "." else rel.split(os.sep)
        fd = os.dup(anchor.fd)
        try:
            for part in parts:
                try:
                    nfd, _ = fsafe.ensure_dir(anchor, fd, part)
                except fsafe.FsafeError as exc:
                    raise WtctlError(
                        "create_failed", f"cannot create {part!r} below {root}: {exc.message}"
                    ) from None
                os.close(fd)
                fd = nfd
        finally:
            os.close(fd)

    def _driver_config_check(self, plan: Plan, *, early: bool) -> None:
        if not self.wgit.filters.drivers:
            return
        start = None if early else plan.start
        keys = worktrees.unsafe_driver_keys(self.wgit, self.res.common_dir, start)
        if keys:
            raise WtctlError(
                "unsafe_repo_config",
                "the clone's config would make an honored filter driver contact or run "
                "something the repository chose; drop the driver from create.allow_filters",
                details={"keys": keys},
            )

    # -- hooks (step 11) ---------------------------------------------------------------------

    def _pre_create(self, plan: Plan) -> list[str]:
        o = self.o
        specs = hooks.configured_hooks(
            self.config, "pre-create", o.hook_flags.get("pre-create", [])
        )
        payload = hooks.build_payload(
            "pre-create",
            object_type="worktree",
            object_id=self.rid,
            repo=o.repo,
            labels=labelmod.render(labelmod.merge_labels((), o.labels)),
            clone_path=self.res.clone_path,
            path=self.path,
            branch=plan.branch,
            branch_action=plan.action,
            base=plan.base,
            ref=o.ref,
            source=None,
            actor=self.ctx.actor.value,
            decision_ids=list(self.ctx.decision_ids),
        )
        env = hooks.hook_env(
            self.config, os.environ, hook="pre-create", object_id=self.rid, object_type="worktree"
        )
        # TODO(#5): log the hook runs (also those carried by a veto: err.hook_runs).
        out = hooks.run_pre_hooks(
            "pre-create",
            specs,
            payload,
            env=env,
            actor=self.ctx.actor,
            override_escalation=o.override_escalation,
            decision_ids=self.ctx.decision_ids,
        )
        self.warnings += out.warnings
        return list(out.decision_ids)

    # -- the record (step 12) ----------------------------------------------------------------

    def _record_fields(self, plan: Plan, root: str) -> dict[str, Any]:
        res = self.res
        fields: dict[str, Any] = {
            "id": self.rid,
            "object_type": "worktree",
            "owned": True,
            "state": "creating",
            "repo": self.o.repo,
            "clone_path": res.clone_path,
            "common_dir": res.common_dir,
            "common_id": res.recorded_id,
            "base_dir": root,
            "path": self.path,
            "name": self.name,
            "branch": plan.branch,
            "created_branch": plan.created_branch,
            "base": plan.base,
            "base_sha": plan.base_sha,
            "ref": self.o.ref,
            "fetch_url": res.fetch_url,
            "remote": self.settings.remote,
        }
        return fields

    # -- adding the worktree (step 13) -------------------------------------------------------

    def _add(self, plan: Plan, record: Record, lease: Lease) -> None:
        git, common = self.wgit, self.res.common_dir
        created: list[str] = []  # [start] once the branch ref is known to be ours
        written: dict[str, worktrees.UpstreamKey] = {}
        added = False
        try:
            start = plan.start
            assert start is not None
            if plan.action != "existing" and not clone_has_commit(self.git, common, start):
                self._transfer(lease, start)
            self._driver_config_check(plan, early=False)
            if plan.created_branch:
                # The ref is created create-only, so a branch this call owns was never anybody
                # else's and the unwind may delete it; one that exists is a refusal.
                assert plan.branch is not None
                worktrees.create_branch_ref(
                    self.git, common, plan.branch, start, lambda: created.append(start)
                )
            if plan.action == "track":
                assert plan.branch is not None
                worktrees.write_upstream(
                    self.git, common, plan.branch, self.settings.remote, written
                )
            spec = AddSpec(self.path, plan.branch, None if plan.branch else start)
            try:
                worktrees.worktree_add(git, common, spec)
            finally:
                # git is dead here (the gateway kills its group on any exception)
                added = self._added_on_disk()
            if not added:
                raise WtctlError("create_failed", "git worktree add did not create the worktree")
            dir_id = self._verify_placement()
            info = worktrees.inspect_added(self.git, common, self.path)
            self.state.transition(
                record["id"],
                "creating",
                "creating",
                admin_dir=info.admin_dir,
                dir_id=dir_id,
                head_at_create=info.head,
                reflog_start_ts=self.state.timestamp(),
                reflog_start_sha=info.reflog_start_sha,
            )
            self.state.set_stash_ids(record["id"], info.stash_ids)
        except BaseException as exc:
            if added:
                # The worktree exists: branch, directory and the `creating` record stay for
                # reconcile to settle (a record that says `active` or vanishes would lie).
                kept = self._kept(exc, record, plan)
                if kept is exc:
                    raise
                raise kept from exc
            notes = self._unwind(record, plan, created[0] if created else None, written)
            self._annotate(exc, notes)
            raise

    def _kept(self, exc: BaseException, record: Record, plan: Plan) -> BaseException:
        """The error for a failure after git created the worktree: ``create_failed`` naming the
        record that was kept (an interrupt is passed through)."""
        if not isinstance(exc, Exception):
            return exc
        if isinstance(exc, WtctlError) and exc.code == "create_failed" and "details" in exc.fields:
            exc.fields["details"].setdefault("record", record["id"])
            exc.fields["details"].setdefault("branch", plan.branch)
            exc.fields["details"].setdefault("path", self.path)
            return exc
        what = worktrees.scrub_urls(
            exc.message if isinstance(exc, WtctlError) else f"{type(exc).__name__}: {exc}"
        )
        return WtctlError(
            "create_failed",
            f"{what} (the worktree was added; record {record['id']} stays `creating`)",
            details={"record": record["id"], "branch": plan.branch, "path": self.path},
        )

    def _added_on_disk(self) -> bool:
        """git created the worktree: its `.git` pointer exists, or an admin entry names the
        path (the entry is written first, so it also covers a pointer that is gone again)."""
        try:
            st = os.lstat(os.path.join(self.path, ".git"))
        except OSError:
            pass
        else:
            if stat.S_ISREG(st.st_mode):
                return True
        mine = os.path.join(self.path, ".git")
        try:
            return any(g == mine for g in worktrees.admin_gitdirs(self.res.common_dir))
        except OSError:
            return True  # cannot tell: keep the record and the branch for reconcile

    def _verify_placement(self) -> str:
        """After the add: walk from the root anchor to the worktree without following a symlink
        and return the worktree directory's identity (``dir_id``). git created the parents by
        path, so a parent swapped between our checks and its ``mkdir`` shows up here as
        ``anchor_changed``; the record stays ``creating`` (the worktree exists)."""
        root = self._worktree_base_dir()
        recorded = self.state.anchor_identity(root)
        expected = None
        if recorded is not None:
            dev, _, ino = recorded.partition(":")
            expected = fsafe.Identity(int(dev), int(ino))
        try:
            anchor = fsafe.open_anchor(root, expected=expected)
        except fsafe.FsafeError as exc:
            raise self._root_error(root, recorded, exc) from None
        with anchor:
            fd = os.dup(anchor.fd)
            try:
                for part in os.path.relpath(self.path, root).split(os.sep):
                    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                    try:
                        nfd = os.open(part, flags, dir_fd=fd)
                    except OSError:
                        raise self._root_error(
                            root,
                            recorded,
                            None,
                            message=f"a parent of {self.path} was replaced while git created it",
                            changed=False,
                        ) from None
                    os.close(fd)
                    fd = nfd
                try:
                    dotgit = os.stat(".git", dir_fd=fd, follow_symlinks=False)
                except OSError:
                    dotgit = None
                if dotgit is None or not stat.S_ISREG(dotgit.st_mode):
                    raise WtctlError("create_failed", f"{self.path} has no .git file after the add")
                return identity_str(os.fstat(fd))
            finally:
                os.close(fd)

    def _transfer(self, lease: Lease, start: str) -> None:
        cid = self.res.recorded_id
        assert cid is not None
        dev, _, ino = cid.partition(":")
        transfer_base(self.mirror, lease, common_id=fsafe.Identity(int(dev), int(ino)), start=start)

    def _unwind(
        self,
        record: Record,
        plan: Plan,
        created_sha: str | None,
        written: dict[str, worktrees.UpstreamKey],
    ) -> list[str]:
        """A failed create (nothing was added to disk) leaves no branch this call created, no
        upstream key it wrote and no record. Returns what could not be undone."""
        common = self.res.common_dir
        notes: list[str] = []
        if plan.created_branch and plan.branch and created_sha:
            try:
                if not worktrees.delete_created_branch(self.git, common, plan.branch, created_sha):
                    notes.append(
                        f"branch {plan.branch} was left (it moved, or could not be deleted)"
                    )
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
                notes.append(f"branch {plan.branch} was left ({exc})")
        if written and plan.branch:
            try:
                notes += worktrees.remove_upstream(self.git, common, plan.branch, written)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"the upstream of {plan.branch} was left ({exc})")
        try:
            self.state.delete_record(record["id"], expected="creating")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"record {record['id']} was left `creating` ({exc})")
        return notes

    def _annotate(self, exc: BaseException, notes: list[str]) -> None:
        """Make an incomplete cleanup visible: in the error's message, and as warnings."""
        if not notes:
            return
        self.warnings += notes
        if isinstance(exc, WtctlError):
            exc.message += "; cleanup incomplete: " + "; ".join(notes)
            exc.args = (exc.message,)

    # -- activation and output (step 14 to 16) ----------------------------------------------

    def _bootstrap(
        self, record: Record, plan: Plan, rlease: Lease, run_the_steps: bool
    ) -> bootstrapper.Outcome:
        """Create order step 14: bootstrap steps 1 to 5 under the record lease, stored in one
        transaction. A failure of wtctl itself (not of a step) keeps the `creating` record and
        the worktree, like any failure after git created the worktree."""
        current = self.state.get_record(record["id"])
        assert current is not None
        try:
            outcome = bootstrapper.run_bootstrap(
                git=self.wgit,
                config=self.config,
                settings=self.settings,
                record=current,
                copy_env=self.o.copy_env,
                run_the_steps=run_the_steps,
                rerun=False,
                warn=self.warnings.append,
                on_tick=bootstrapper.lease_ticker(rlease),
            )
            if rlease.lost:
                raise WtctlError(
                    "busy", f"the lease on {record['id']} was lost during bootstrap; nothing stored"
                )
            self.state.record_bootstrap(
                record["id"],
                expected="creating",
                state=outcome.state,
                steps=outcome.steps,
                paths=outcome.paths,
                nested=outcome.nested,
            )
        except BaseException as exc:
            if not rlease.lost:
                with contextlib.suppress(Exception):
                    self.state.set_bootstrap_state(record["id"], "interrupted", expected="creating")
            kept = self._kept(exc, record, plan)
            if kept is exc:
                raise
            raise kept from exc
        return outcome

    def _baseline(self, record: Record, plan: Plan) -> repobaseline.Baseline:
        """Create order step 15: the repo-config baseline, after bootstrap. One that could not be
        computed in full (a bound or a deadline) is not recorded: the worktree exists, so the
        record stays `creating` for a human, like any failure after git created the worktree."""
        current = self.state.get_record(record["id"])
        assert current is not None
        try:
            baseline = baselinesvc.record_current(self.config, current)
        except BaseException as exc:
            kept = self._kept(exc, record, plan)
            if kept is exc:
                raise
            raise kept from exc
        if baseline.has_detail():
            bad = [i.path for i in baseline.items if i.detail][:5]
            raise self._kept(
                WtctlError(
                    "create_failed",
                    "the repo-config baseline could not be computed in full "
                    f"(see remove.nested_scan_limit): {', '.join(bad)}",
                ),
                record,
                plan,
            )
        return baseline

    def _finish(self, record: Record, plan: Plan, decision_ids: list[str], rlease: Lease) -> Result:
        o = self.o
        run_the_steps = bool(
            self.settings.bootstrap.steps and self.settings.bootstrap.auto and not o.no_bootstrap
        )
        outcome: bootstrapper.Outcome | None = None
        if o.copy_env or run_the_steps:
            outcome = self._bootstrap(record, plan, rlease, run_the_steps)
        baseline = self._baseline(record, plan)
        try:
            active = self.state.transition(
                record["id"],
                "creating",
                "active",
                baseline_digest=baseline.digest,
                baseline_items=baselinesvc.item_rows(baseline),
            )
        except WtctlError as exc:
            if exc.code != "busy":
                raise
            # no other actor changes a record whose lease is held: a lost compare-and-set is a bug
            raise WtctlError(
                "internal", f"record {record['id']} changed while create held its lease"
            ) from None
        summary = self.state.bootstrap_summary(active["id"])
        boot = {**summary, "tracked_changes": outcome.tracked_changes if outcome else []}
        out = record_object(active, summary)
        payload = hooks.build_payload(
            "post-create",
            object_type="worktree",
            object_id=active["id"],
            repo=o.repo,
            labels=active["labels"],
            record=out,
            bootstrap=boot,
        )
        env = hooks.hook_env(
            self.config,
            os.environ,
            hook="post-create",
            object_id=active["id"],
            object_type="worktree",
        )
        specs = hooks.configured_hooks(
            self.config, "post-create", o.hook_flags.get("post-create", [])
        )
        post = hooks.run_post_hooks(
            "post-create", specs, payload, env=env, decision_ids=decision_ids
        )
        self.warnings += post.warnings
        result = self._result(
            out,
            plan.action,
            boot,
            reused=False,
            human=f"created {active['name']} on {active['branch'] or 'a detached HEAD'}",
        )
        if outcome is not None and outcome.failed_step is not None:
            n = len(outcome.steps)
            err = WtctlError(
                "bootstrap_failed",
                f"bootstrap step {n} failed; the worktree {active['path']} was kept",
                details={"record": out, "step": outcome.failed_step},
            )
            err.stdout_text = active["path"]
            raise err
        return result

    def _result(
        self,
        record: dict[str, Any],
        action: str | None,
        bootstrap: Any,
        *,
        reused: bool,
        human: str,
    ) -> Result:
        data = {
            "record": record,
            "branch_action": action,
            "bootstrap": bootstrap,
            "reused": reused,
            "warnings": list(self.warnings),
            "dry_run": self.dry,
        }
        return Result(data, human=record["path"], stderr="\n".join([human, *self.warnings]))

    def _result_reused(self, record: Record) -> Result:
        # schemas/cli/create.json: a reused result reports dry_run false, since the answer is
        # the same with or without --dry-run (nothing is changed either way).
        self.dry = False
        return self._result(
            record_object(record, self.state.bootstrap_summary(record["id"])),
            None, None, reused=True,
            human=f"reused {record['name']} (a retry)",
        )  # fmt: skip

    def _result_dry(self, fields: dict[str, Any], plan: Plan) -> Result:
        now = self.state.timestamp()
        rec: Record = {
            **{k: None for k in FIELDS},
            **fields,
            "labels": labelmod.render(labelmod.merge_labels((), self.o.labels)),
            "holders": [self.o.holder] if self.o.holder else [],
            "bootstrap_state": "none",
            "created": now,
            "updated": now,
        }
        return self._result(
            record_object(rec), plan.action, {**EMPTY_BOOTSTRAP, "tracked_changes": []},
            reused=False, human=f"would create {fields['name']} ({plan.action})",
        )  # fmt: skip


def run(ctx: Context) -> Result:
    if ctx.args.throwaway:
        raise WtctlError("internal", "`wtctl create --throwaway` is not implemented yet (see #21)")
    opts = parse_options(ctx)
    return Creator(ctx, opts).run()


__all__ = ["Reused", "record_object", "run"]
