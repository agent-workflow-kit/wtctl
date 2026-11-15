"""``wtctl create``: a worktree (docs/interface.md, Create order).

TODO(#21): ``create --throwaway`` (the throwaway create order) lives in its own issue.
TODO(#16): bootstrap (create order step 14) is a seam: the record keeps ``bootstrap.state: none``.
TODO(#17): the baseline (create order step 15) is a seam: ``baseline_digest`` stays null.
TODO(#5): log ``create``, ``fetch``, ``hook`` and ``refused`` events (create is logged, not
audited).
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import stat
from typing import Any

from .. import fsafe, hooks, naming, worktrees
from .. import labels as labelmod
from ..context import Context, Result
from ..errors import UsageError, WtctlError
from ..gitsetup import open_git
from ..hardgit import Git, load_filter_policy
from ..locate import Resolver, describe_mount, identity_str
from ..mirror import Mirror, Tips
from ..packs import clone_has_commit, transfer_base
from ..state import Lease, Record, State, check_holder, clone_lease, state_path
from ..worktrees import SHA, AddSpec

NAME_RE = naming.NAME_RE
EMPTY_BOOTSTRAP = {"state": "none", "steps": [], "created_paths_count": 0}

# Fields of the record object, in the order docs/interface.md lists them.
# TODO(#7): replace with wtctl.record_view.record_object once list/show land.
FIELDS = (
    "id object_type owned state repo clone_path common_dir common_id admin_dir dir_id base_dir "
    "root_id path name branch created_branch base base_sha head_at_create ref source depth "
    "fetch_url remote labels holders baseline_digest bootstrap created updated missing_since "
    "removed_at last_error"
).split()


def record_object(record: Record) -> dict[str, Any]:
    full = dict(record)
    full["bootstrap"] = {
        "state": record.get("bootstrap_state", "none"),
        "steps": [],  # TODO(#16): the steps of the last run
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
    return Options(
        repo=a.repo,
        branch=a.branch,
        detach=a.detach,
        base=a.base,
        ref=a.ref,
        name=a.name,
        holder=a.holder,
        labels=list(a.label),
        hook_flags=hooks.parse_hook_flags(a.hook),
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
        try:
            with self.state.lease(
                clone_lease(res.clone_path), wait=self.config.lease_wait_s
            ) as lease:
                return self._locked(resolver, lease)
        except Reused as r:
            return self._result_reused(r.record)

    def _git_with_filters(self) -> Git:
        allow = self.settings.allow_filters
        if not allow:
            return self.git
        from ..gitsetup import private_dir

        with private_dir() as p:
            policy = load_filter_policy(
                self.ctx.gateway,
                self.git.git_bin,
                home=os.environ.get("HOME") or os.path.expanduser("~"),
                xdg_config_home=os.environ.get("XDG_CONFIG_HOME"),
                allow=allow,
                path_env=os.environ.get("PATH", ""),
                private_dir=p,
            )
        self.warnings += policy.warnings
        return dataclasses.replace(self.git, filters=policy)

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
        record, rlease = self.state.insert_record_leased(
            record_fields, labels=o.labels, holders=[o.holder] if o.holder else []
        )
        try:
            self._add(plan, record, lease)
        except BaseException:
            rlease.release()
            raise
        lease.release()
        with rlease:
            return self._finish(record, plan, decision_ids)

    # -- names (step 7) ----------------------------------------------------------------------

    def _by_name(self, live: list[Record], name: str) -> Record | None:
        folded = name.casefold()
        for r in live:
            if r["name"] is not None and r["name"].casefold() == folded:
                return r
        return None

    def _by_path(self, live: list[Record], path: str) -> Record | None:
        for r in live:
            if r["path"] == path:
                return r
        return None

    def _maybe_reuse(self, rec: Record | None) -> None:
        """The retry rule: an active record that lists this holder is returned unchanged."""
        if rec is not None and self.o.holder and rec["state"] == "active":
            if self.o.holder in rec["holders"]:
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
                if ref in heads:
                    if explicit:
                        raise WtctlError(
                            "refused",
                            f"branch {branch} already exists",
                            reason="branch_exists",
                            details={"tip": heads[ref]},
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

    def _known_dirs(self, live: list[Record]) -> list[str]:
        dirs = [self.res.clone_path]
        dirs += [c.clone for c in map(self.config.repo, self.config.repos) if c.clone]
        with contextlib.suppress(Exception):
            dirs += [row["path"] for row in self._clone_rows()]
        dirs += [e.path for e in worktrees.worktree_entries(self.git, self.res.common_dir)]
        dirs += [r["path"] for r in live]
        return [os.path.realpath(d) for d in dirs if d]

    def _clone_rows(self) -> list[dict[str, Any]]:
        rows = self.state._conn.execute("SELECT path FROM clones").fetchall()  # noqa: SLF001
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
            if not stat.S_ISDIR(st.st_mode) or os.listdir(path):
                raise WtctlError("refused", f"{path} already exists", reason="path_exists")
        for known in self._known_dirs(live):
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
        self, root: str, recorded: str | None, exc: fsafe.FsafeError | None
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
            "changed": True,
            **info,
        }
        if recorded is None:
            entry["recorded"] = "0:0" if current != "0:0" else "0:1"
        return WtctlError(
            "create_failed",
            f"the worktree root {root} is not the directory wtctl recorded for it",
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
        created_sha: str | None = None
        wrote_upstream = False
        added = False
        try:
            start = plan.start
            assert start is not None
            if plan.action != "existing" and not clone_has_commit(self.git, common, start):
                self._transfer(lease, start)
            self._driver_config_check(plan, early=False)
            if plan.action == "track":
                assert plan.branch is not None
                worktrees.write_upstream(self.git, common, plan.branch, self.settings.remote)
                wrote_upstream = True
            if plan.created_branch:
                created_sha = start
            spec = AddSpec(
                self.path,
                plan.branch,
                start,
                new_branch=plan.created_branch,
            )
            try:
                worktrees.worktree_add(git, common, spec)
            finally:
                added = self._added_on_disk()
            if not added:
                raise WtctlError("create_failed", "git worktree add did not create the worktree")
            info = worktrees.inspect_added(self.git, common, self.path)
            fd = fsafe_open_dir(self.path)
            try:
                dir_id = identity_str(os.fstat(fd))
            finally:
                os.close(fd)
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
        except BaseException:
            if added:
                # The worktree exists: the record stays `creating` for reconcile to settle
                # (a record that says `active` or vanishes would lie about the directory).
                raise
            self._unwind(record, plan, created_sha, wrote_upstream)
            raise

    def _added_on_disk(self) -> bool:
        """The worktree's `.git` pointer exists (git created the directory)."""
        try:
            st = os.lstat(os.path.join(self.path, ".git"))
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode)

    def _transfer(self, lease: Lease, start: str) -> None:
        cid = self.res.recorded_id
        assert cid is not None
        dev, _, ino = cid.partition(":")
        transfer_base(self.mirror, lease, common_id=fsafe.Identity(int(dev), int(ino)), start=start)

    def _unwind(
        self, record: Record, plan: Plan, created_sha: str | None, wrote_upstream: bool
    ) -> None:
        """A failed create leaves no branch this call created, no upstream section it wrote and
        no record (nothing was added to disk)."""
        common = self.res.common_dir
        with contextlib.suppress(Exception):
            if plan.created_branch and plan.branch and created_sha:
                worktrees.delete_created_branch(self.git, common, plan.branch, created_sha)
            if wrote_upstream and plan.branch:
                worktrees.remove_upstream(self.git, common, plan.branch)
        self.state.delete_record(record["id"], expected="creating")

    # -- activation and output (step 14 to 16) ----------------------------------------------

    def _finish(self, record: Record, plan: Plan, decision_ids: list[str]) -> Result:
        o = self.o
        if self.settings.bootstrap.steps and self.settings.bootstrap.auto and not o.no_bootstrap:
            # TODO(#16): run the steps; until then the record says bootstrap never ran.
            self.warnings.append(
                "bootstrap steps are configured but bootstrap is not implemented yet"
            )
        # TODO(#17): record the baseline here (after bootstrap).
        active = self.state.transition(record["id"], "creating", "active")
        out = record_object(active)
        payload = hooks.build_payload(
            "post-create",
            object_type="worktree",
            object_id=active["id"],
            repo=o.repo,
            labels=active["labels"],
            record=out,
            bootstrap={**EMPTY_BOOTSTRAP, "tracked_changes": []},
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
        return self._result(
            out,
            plan.action,
            {**EMPTY_BOOTSTRAP, "tracked_changes": []},
            reused=False,
            human=f"created {active['name']} on {active['branch'] or 'a detached HEAD'}",
        )

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
            record_object(record), None, None, reused=True,
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


def fsafe_open_dir(path: str) -> int:
    from ..locate import open_dir_nofollow

    return open_dir_nofollow(path)


def run(ctx: Context) -> Result:
    if ctx.args.throwaway:
        raise WtctlError("internal", "`wtctl create --throwaway` is not implemented yet (see #21)")
    opts = parse_options(ctx)
    return Creator(ctx, opts).run()


__all__ = ["Reused", "record_object", "run"]
