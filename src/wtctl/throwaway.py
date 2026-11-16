"""``wtctl create --throwaway`` (docs/interface.md, Throwaway create order).

A throwaway is a separate disposable clone at one commit, holding third-party-controlled content
that is never trusted. Nothing here runs anything the content names: the repository is created
with an empty template, its config is only what wtctl writes, ``info/attributes`` blanks every
filter, nothing writes a reflog, the global git config is never read, and the fetch (or the pack
from the clone) is checked with ``fsckObjects`` / ``--strict`` so a tree that names ``.git`` is
refused. The directory is created through the throwaway root's no-follow fd, its identity is
recorded before anything is written into it, and every step that writes by path re-checks it.

A failure at any step deletes the directory (fd-based, as ``remove`` does) and the record; a
directory that cannot be deleted leaves the record ``partial`` so ``prune`` retries it. A creator
that is killed leaves a ``creating`` record for ``reconcile`` (TODO(#23)).
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
from typing import Any

from . import audit, fsafe, fsafe_child, hooks, removal_delete
from . import labels as labelmod
from .context import Context, Result
from .credentials import (
    NetworkKind,
    prepare_network_auth,
    rejected_token_error,
    run_network_git,
)
from .errors import UsageError, WtctlError
from .gitsetup import open_git, private_dir
from .hardgit import Git, classify_fetch, outcome_error
from .locate import Resolution, Resolver, describe_mount, identity_str
from .mirror import mirror_url
from .packs import clone_has_commit, fill_local_throwaway, is_shallow
from .record_view import FIELDS as RECORD_FIELDS
from .record_view import record_object
from .state import Conflict, Lease, Record, State
from .worktrees import SHA

CREATE_HOOKS = ("pre-create", "post-create")
SOURCES = ("auto", "local", "remote")

# The keys of a throwaway's config, in ``git config --list`` spelling: what ``init --template=``
# writes (``core.symlinks`` and friends only where the file system needs them) plus the two keys
# wtctl sets. A token goes only to a repository whose config holds nothing else.
OWNED_KEYS = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.symlinks",
        "core.ignorecase",
        "core.precomposeunicode",
        "fetch.fsckobjects",
    }
)
ATTRIBUTES = b"* -filter\n"
NOT_OUR_REF = ("not our ref", "couldn't find remote ref", "unadvertised object")


@dataclasses.dataclass
class Options:
    repo: str
    ref: str
    source: str
    depth: int | None
    labels: list[str]
    hook_flags: dict[str, list[str]]
    override_escalation: bool


def parse_options(ctx: Context) -> Options:
    """Step 1: the usage checks (exit 2)."""
    a = ctx.args
    for value, flag in (
        (a.branch, "--branch"),
        (a.detach, "--detach"),
        (a.base, "--base"),
        (a.name, "--name"),
        (a.holder, "--holder"),
        (a.copy_env, "--copy-env"),
        (a.no_bootstrap, "--no-bootstrap"),
    ):
        if value not in (None, False):
            raise UsageError(f"{flag} is not accepted with --throwaway")
    if a.ref is None:
        raise UsageError("--throwaway needs --ref <40-hex commit id>")
    if not SHA.fullmatch(a.ref):
        raise UsageError("--ref must be a full 40-digit lowercase hex commit id")
    if a.depth is not None and a.depth < 0:
        raise UsageError("--depth must be an integer of 0 or more (0 = full history)")
    source = a.source or "auto"
    if source == "local" and a.depth not in (None, 0, 1):
        raise UsageError("--source local copies the full history (0) or depth 1; use --depth 0|1")
    labelmod.merge_labels((), a.label)
    hook_flags = hooks.parse_hook_flags(a.hook)
    for name in hook_flags:
        if name not in CREATE_HOOKS:
            raise UsageError(f"--hook {name}: create fires only {', '.join(CREATE_HOOKS)}")
    return Options(
        repo=a.repo,
        ref=a.ref,
        source=source,
        depth=a.depth,
        labels=list(a.label),
        hook_flags=hook_flags,
        override_escalation=a.override_escalation,
    )


@dataclasses.dataclass
class Plan:
    source: str  # local | remote
    depth: int  # the depth used: 0 = full history
    url: str  # the URL fetched from, or the clone's path for local


def remote_url_choice(
    *, repo_clone_url: str | None, clone_fetch_url: str | None, host_url: str
) -> str:
    """The URL of a ``remote`` source, in order: ``repos.<id>.clone_url``, the resolved clone's
    fetch URL (only the URL, never its objects), then ``hosts.<host>.clone_url``."""
    if repo_clone_url is not None:
        return repo_clone_url
    if clone_fetch_url is not None:
        return clone_fetch_url
    return host_url


def choose_source(
    requested: str,
    *,
    explicit_depth: int | None,
    default_depth: int,
    clone_has_ref: bool,
    clone_shallow: bool,
) -> tuple[str, int]:
    """``(source, depth)`` for step 8. ``clone_has_ref`` says the clone holds the commit.

    ``local`` copies the full history unless ``--depth 1`` (``throwaway.depth`` does not apply).
    ``auto`` is ``local`` when the clone has the commit, unless an explicit depth other than 0 or
    1 asks for something a pack cannot give, or the full history is wanted and the clone is
    shallow (a pack of it would be cut short); else ``remote``, at the explicit depth or
    ``throwaway.depth``.
    """
    local_depth = 1 if explicit_depth == 1 else 0
    remote_depth = default_depth if explicit_depth is None else explicit_depth
    if requested == "local":
        return "local", local_depth
    if requested == "remote":
        return "remote", remote_depth
    usable = (
        clone_has_ref
        and explicit_depth in (None, 0, 1)
        and not (local_depth == 0 and clone_shallow)
    )
    return ("local", local_depth) if usable else ("remote", remote_depth)


class ThrowawayCreator:
    def __init__(self, ctx: Context, opts: Options) -> None:
        self.ctx = ctx
        self.o = opts
        self.config = ctx.config
        self.state: State = ctx.state
        self.dry = ctx.dry_run
        self.warnings: list[str] = []
        self.journal = audit.journal_for(ctx)
        self.dfd: int | None = None  # the throwaway directory, open from creation to the end
        self.record: Record | None = None

    # -- entry --------------------------------------------------------------------------------

    def run(self) -> Result:
        try:
            return self._run()
        except WtctlError as exc:
            self._log("throwaway_create", audit.level_for(exc.exit_code), {"code": exc.code})
            raise

    def _run(self) -> Result:
        self.git: Git = open_git(self.config, self.ctx.gateway)  # git_too_old (exit 12)
        self.rid = self.state.preview_id() if self.dry else self.state.allocate_id()
        self.res = self._resolve_clone()
        self.root = os.path.realpath(self.config.effective_throwaway_root)
        self.path = os.path.join(self.root, self.rid)
        if self.dry:
            self._check_root(write=False)
            return self._result_dry()
        decision_ids = self._pre_create()
        root_id = self._check_root(write=True)
        record, rlease = self._insert(root_id)
        self.record = record
        with rlease:
            try:
                active = self._build(record, rlease)
            except BaseException as exc:
                self._discard(record, exc)
                raise
            finally:
                self._close_dir()
            return self._finish(active, decision_ids)

    # -- step 4: the clone --------------------------------------------------------------------

    def _resolve_clone(self) -> Resolution | None:
        """The clone, never auto-cloned. A resolution failure ends the command only for a
        ``local`` source; otherwise it means "no clone" (a warning, except "not found")."""
        o = self.o
        self.resolver = Resolver(self.config, self.state, self.git)
        failure: WtctlError | None = None
        res: Resolution | None = None
        try:
            res = self.resolver.locate(o.repo, auto_clone=False)
            res.require_sha1()
            if res.anchor_changed:
                res.require_anchor()
        except WtctlError as exc:
            if (
                exc.code == "create_failed"
                and (exc.fields.get("details") or {}).get("cause") == "object_format"
            ):
                raise
            if exc.exit_code not in (10, 12, 21):
                raise
            failure, res = exc, None
        if failure is not None:
            if o.source == "local":
                raise failure
            if failure.code != "not_found":
                self.warnings.append(f"no clone of {o.repo} will be used: {failure.message}")
        elif res is not None:
            self.warnings += res.warnings
        return res

    # -- step 5: hooks ------------------------------------------------------------------------

    def _pre_create(self) -> list[str]:
        o = self.o
        specs = hooks.configured_hooks(
            self.config, "pre-create", o.hook_flags.get("pre-create", [])
        )
        payload = hooks.build_payload(
            "pre-create",
            object_type="throwaway",
            object_id=self.rid,
            repo=o.repo,
            labels=labelmod.render(labelmod.merge_labels((), o.labels)),
            clone_path=self.res.clone_path if self.res else None,
            path=self.path,
            branch=None,
            branch_action=None,
            base=None,
            ref=o.ref,
            source=o.source,
            actor=self.ctx.actor.value,
            decision_ids=list(self.ctx.decision_ids),
        )
        env = hooks.hook_env(
            self.config,
            os.environ,
            hook="pre-create",
            object_id=self.rid,
            object_type="throwaway",
        )
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
        self.decision_ids = list(out.decision_ids)
        return self.decision_ids

    # -- step 6: the root and the record ------------------------------------------------------

    def _root_error(self, recorded: str | None, exc: fsafe.FsafeError | None = None) -> WtctlError:
        if exc is not None and exc.kind not in ("identity", "mount_point"):
            return WtctlError(
                "create_failed", f"cannot open the throwaway root {self.root}: {exc.message}"
            )
        current, info = "0:0", {"fs_type": "unknown", "mount_source": "", "mount_point": "/"}
        with contextlib.suppress(OSError):
            st = os.stat(self.root)
            current = identity_str(st)
            info = describe_mount(self.root, st)
        entry = {
            "anchor": "throwaway_root",
            "path": fsafe.escape_for_details(self.root),
            "recorded": recorded or ("0:0" if current != "0:0" else "0:1"),
            "current": current,
            "changed": True,
            **info,
        }
        return WtctlError(
            "create_failed",
            f"the throwaway root {self.root} is not the directory wtctl recorded for it",
            details={"cause": "anchor_changed", "anchors": [entry]},
        )

    def _make_root(self) -> None:
        """A missing root is created (mode 0700) through its parent's no-follow fd and checked to
        be on the parent's device and mount; an existing one is left as it is."""
        if os.path.lexists(self.root):
            return
        parent, name = os.path.split(self.root)
        try:
            os.makedirs(parent, exist_ok=True)
            with fsafe.open_anchor(parent, precheck=False) as pa:
                fd, _ = fsafe.ensure_dir(pa, pa.fd, name, forbid_git=False)
                os.close(fd)
        except (OSError, fsafe.FsafeError) as exc:
            what = exc.message if isinstance(exc, fsafe.FsafeError) else exc.strerror
            raise WtctlError(
                "create_failed", f"cannot create the throwaway root {self.root}: {what}"
            ) from None

    def _open_root(self, expected: str | None) -> fsafe.Anchor:
        exp = None
        if expected is not None:
            dev, _, ino = expected.partition(":")
            exp = fsafe.Identity(int(dev), int(ino))
        try:
            anchor = fsafe.open_anchor(self.root, expected=exp)
        except fsafe.FsafeError as exc:
            raise self._root_error(expected, exc) from None
        st = anchor.fs.fstat(anchor.fd)
        if st.uid != os.getuid() or st.mode & 0o077:
            anchor.close()
            raise WtctlError(
                "create_failed",
                f"the throwaway root {self.root} must be owned by you and have mode 0700",
            )
        return anchor

    def _check_root(self, *, write: bool) -> str | None:
        """The throwaway root as an anchor (step 6): created and recorded on first use when
        ``write``; otherwise only checked against its recorded identity. Returns the identity."""
        if write:
            self._make_root()
        elif not os.path.lexists(self.root):
            return None
        recorded = self.state.anchor_identity(self.root)
        with self._open_root(recorded) as anchor:
            current = f"{anchor.identity.dev}:{anchor.identity.ino}"
            if recorded is None and write:
                recorded = self.state.record_anchor(self.root, current)
            if recorded is not None and recorded != current:
                raise self._root_error(recorded)
        return recorded

    def _requested_depth(self) -> int:
        d = self.o.depth
        return self.config.throwaway.depth if d is None else d

    def _fields(self, root_id: str | None) -> dict[str, Any]:
        return {
            "id": self.rid,
            "object_type": "throwaway",
            "state": "creating",
            "repo": self.o.repo,
            "clone_path": self.res.clone_path if self.res else None,
            "base_dir": self.root,
            "root_id": root_id,
            "path": self.path,
            "ref": self.o.ref,
            "source": self.o.source,
            "depth": self._requested_depth(),
        }

    def _insert(self, root_id: str | None) -> tuple[Record, Lease]:
        assert root_id is not None
        try:
            return self.state.insert_record_leased(self._fields(root_id), labels=self.o.labels)
        except Conflict as exc:
            raise WtctlError(
                "refused", f"{self.path} was taken meanwhile ({exc.column})", reason="path_exists"
            ) from None

    # -- steps 7 to 9: the directory and its contents -----------------------------------------

    def _build(self, record: Record, rlease: Lease) -> Record:
        self._make_dir(record)
        self._init_repository()
        plan = self._choose()
        record = self.state.transition(record["id"], "creating", "creating", fetch_url=plan.url)
        self._check_lease(rlease)
        if plan.source == "local":
            self._fill_local(plan)
        else:
            self._fill_remote(plan)
        self._verify()
        self._check_lease(rlease)
        try:
            return self.state.transition(
                record["id"],
                "creating",
                "active",
                source=plan.source,
                depth=plan.depth,
                fetch_url=plan.url,
            )
        except WtctlError as exc:
            if exc.code != "busy":
                raise
            raise WtctlError(
                "internal", f"record {record['id']} changed while create held its lease"
            ) from None

    @staticmethod
    def _check_lease(lease: Lease) -> None:
        if lease.lost:
            raise WtctlError("busy", "the record lease was lost while the throwaway was created")

    def _make_dir(self, record: Record) -> None:
        """Step 7: ``mkdirat`` mode 0700 relative to the root fd, opened no-follow and checked
        to be on the root's device and mount; its identity is recorded before anything is written
        into it."""
        recorded = self.state.anchor_identity(self.root)
        with self._open_root(recorded) as anchor:
            try:
                os.mkdir(self.rid, 0o700, dir_fd=anchor.fd)
            except FileExistsError:
                raise WtctlError(
                    "create_failed", f"{self.path} already exists", details=None
                ) from None
            except OSError as exc:
                raise WtctlError(
                    "create_failed", f"cannot create {self.path}: {exc.strerror}"
                ) from None
            try:
                fd = fsafe.open_dir(anchor, anchor.fd, self.rid, owner_uid=os.getuid())
            except fsafe.FsafeError as exc:
                # we just made it: an empty directory of ours, removed by name (no recursion)
                with contextlib.suppress(OSError):
                    os.rmdir(self.rid, dir_fd=anchor.fd)
                raise WtctlError(
                    "create_failed", f"cannot open the new directory {self.path}: {exc.message}"
                ) from None
            self.dfd = fd
            st = os.fstat(fd)
            self.dir_id = f"{st.st_dev}:{st.st_ino}"
            if st.st_mode & 0o077:
                raise WtctlError("create_failed", f"{self.path} is not private (mode)")
            self.state.transition(record["id"], "creating", "creating", dir_id=self.dir_id)

    def _anchor(self) -> fsafe.Anchor:
        return self._open_root(self.state.anchor_identity(self.root))

    def _verify(self) -> None:
        """The directory at the path is still the one created: reached from the root's fd without
        following a link, with the recorded identity, and its ``.git`` is a real directory."""
        dev, _, ino = self.dir_id.partition(":")
        try:
            with self._anchor() as anchor:
                fd = fsafe.open_chain(
                    anchor,
                    anchor.fd,
                    [self.rid],
                    owner_uid=os.getuid(),
                    expected=fsafe.Identity(int(dev), int(ino)),
                )
                os.close(fd)
        except fsafe.FsafeError as exc:
            raise WtctlError(
                "create_failed",
                f"{self.path} is no longer the directory that was created ({exc.kind})",
            ) from None

    def _git_fd(self, anchor: fsafe.Anchor) -> int:
        assert self.dfd is not None
        try:
            return fsafe.open_dir(anchor, self.dfd, ".git", owner_uid=os.getuid())
        except fsafe.FsafeError as exc:
            raise WtctlError(
                "create_failed", f"{self.path}/.git is not a plain directory ({exc.kind})"
            ) from None

    def _init_repository(self) -> None:
        """``init --template=`` with isolated config, then ``info/attributes`` (``* -filter``),
        ``core.logAllRefUpdates=false`` and ``fetch.fsckObjects=true``."""
        with private_dir() as p:
            r = self.git.run(
                ["init", "-q", "--template=", "--", self.path], private_dir=p, timeout=60
            )
        self._checked(r, "git init of the throwaway")
        self._verify()
        with self._anchor() as anchor:
            gfd = self._git_fd(anchor)
            try:
                try:
                    ifd, _ = fsafe.ensure_dir(anchor, gfd, "info", forbid_git=False)
                    try:
                        fd = fsafe.create_new_file(ifd, "attributes", mode=0o644, forbid_git=False)
                        try:
                            os.write(fd, ATTRIBUTES)
                        finally:
                            os.close(fd)
                    finally:
                        os.close(ifd)
                except fsafe.FsafeError as exc:
                    raise WtctlError(
                        "create_failed", f"cannot write info/attributes: {exc.message}"
                    ) from None
            finally:
                os.close(gfd)
        for key, value in (("core.logAllRefUpdates", "false"), ("fetch.fsckObjects", "true")):
            r = self._git(["config", "--local", key, value])
            self._checked(r, f"git config {key}")

    # -- git in the throwaway -----------------------------------------------------------------

    @property
    def tgit(self) -> str:
        return os.path.join(self.path, ".git")

    def _git(self, args: list[str], *, timeout: float = 300, work_tree: bool = False) -> Any:
        """Hardened git in the throwaway. Only a command that writes the work tree gets one (and
        then runs in it): without ``--work-tree`` git would write into its cwd."""
        return self.git.run(
            args,
            git_dir=self.tgit,
            work_tree=self.path if work_tree else None,
            cwd=self.path if work_tree else self.tgit,
            allow_shallow=True,
            timeout=timeout,
        )

    @staticmethod
    def _checked(r: Any, what: str) -> Any:
        if r.timed_out:
            raise WtctlError("create_failed", f"{what} timed out")
        if r.returncode != 0:
            lines = r.err.strip().splitlines()
            tail = lines[-1][:200] if lines else "no output"
            raise WtctlError("create_failed", f"{what} failed (exit {r.returncode}): {tail}")
        return r

    # -- step 8: the source -------------------------------------------------------------------

    def _choose(self) -> Plan:
        o, res = self.o, self.res
        has = res is not None and clone_has_commit(self.git, res.common_dir, o.ref)
        if o.source == "local":
            if res is None or not has:
                raise WtctlError(
                    "ref_not_found", f"the clone of {o.repo} does not hold commit {o.ref}"
                )
        shallow = res is not None and is_shallow(res.common_dir)
        source, depth = choose_source(
            o.source,
            explicit_depth=o.depth,
            default_depth=self.config.throwaway.depth,
            clone_has_ref=has,
            clone_shallow=shallow,
        )
        if source == "local":
            assert res is not None
            return Plan("local", depth, res.clone_path)
        return Plan("remote", depth, mirror_url(self._remote_url()))

    def _remote_url(self) -> str:
        o = self.o
        configured = self.config.repo(o.repo).clone_url is not None
        fetch_url = self.res.fetch_url if self.res else None
        needs_template = not configured and fetch_url is None
        return remote_url_choice(
            repo_clone_url=self.resolver.clone_url(o.repo) if configured else None,
            clone_fetch_url=fetch_url,
            host_url=self.resolver.clone_url(o.repo) if needs_template else "",
        )

    # -- step 9: fill -------------------------------------------------------------------------

    def _fill_local(self, plan: Plan) -> None:
        assert self.res is not None
        fill_local_throwaway(
            self.git,
            self.config,
            clone_dir=self.res.common_dir,
            throwaway=self.path,
            sha=self.o.ref,
            depth=plan.depth,
            verify=self._verify,
        )

    def _fill_remote(self, plan: Plan) -> None:
        ref = self.o.ref
        auth = prepare_network_auth(
            self.config,
            plan.url,
            environ=os.environ,
            gateway=self.ctx.gateway,
            warn=self.warnings.append,
        )
        args = ["fetch", "--no-tags", "--no-recurse-submodules"]
        if plan.depth > 0:
            args += ["--depth", str(plan.depth)]
        args += ["--", plan.url, ref]
        kind = NetworkKind.NETWORK if plan.depth > 0 else NetworkKind.CLONE
        self._verify()
        result = run_network_git(
            self.git, args, cfg=self.config, auth=auth, kind=kind, tool_owned=True,
            owned_keys=OWNED_KEYS, git_dir=self.tgit, cwd=self.tgit, allow_shallow=True,
        )  # fmt: skip
        outcome = classify_fetch(result)
        err = rejected_token_error(auth, outcome_error(outcome, result))
        if err is not None:
            if err.code == "fetch_failed" and any(m in result.err for m in NOT_OUR_REF):
                raise WtctlError("ref_not_found", f"the remote does not have commit {ref}")
            raise err
        self._delete_fetch_head()
        if not clone_has_commit(self.git, self.tgit, ref):
            raise WtctlError("ref_not_found", f"the fetch did not supply the commit {ref}")
        self._verify()
        update = self._git(["update-ref", "--no-deref", "HEAD", ref], timeout=60)
        self._checked(update, "git update-ref")
        self._verify()
        limit = float(min(self.config.clone_timeout_s, 24 * 86400))
        self._checked(
            self._git(["read-tree", "-u", "--reset", "HEAD"], timeout=limit, work_tree=True),
            "git read-tree",
        )
        self._verify()
        self._head_check()

    def _delete_fetch_head(self) -> None:
        with self._anchor() as anchor:
            gfd = self._git_fd(anchor)
            try:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink("FETCH_HEAD", dir_fd=gfd)
            except OSError as exc:
                raise WtctlError(
                    "create_failed", f"cannot delete FETCH_HEAD: {exc.strerror}"
                ) from None
            finally:
                os.close(gfd)

    def _head_check(self) -> None:
        head = self._checked(self._git(["rev-parse", "HEAD"], timeout=60), "git rev-parse HEAD")
        if head.out.strip() != self.o.ref:
            raise WtctlError("create_failed", "the throwaway's HEAD is not the requested commit")

    # -- failure: delete the directory and the record -----------------------------------------

    def _close_dir(self) -> None:
        if self.dfd is not None:
            with contextlib.suppress(OSError):
                os.close(self.dfd)
            self.dfd = None

    def _discard(self, record: Record, exc: BaseException) -> None:
        """Delete what was created and the record. The directory goes through the same fd-based
        deletion as ``remove``; one that cannot be deleted leaves the record ``partial`` (``prune``
        retries it), and one that is no longer the recorded directory is left, with the record
        ``gone`` (``reconcile`` reports it as a stray)."""
        self._close_dir()
        notes: list[str] = []
        try:
            current = self.state.get_record(record["id"]) or record
            if current["dir_id"] is None:
                self.state.delete_record(record["id"], expected="creating")
                return
            outcome = removal_delete.delete_throwaway(current, fsafe_child.Budget())
            if outcome.status == "deleted":
                self.state.delete_record(record["id"], expected="creating")
            elif outcome.status == "partial":
                self.state.transition(
                    record["id"], "creating", "partial",
                    last_error="creation failed and the directory could not be fully deleted",
                )  # fmt: skip
                notes.append(f"{self.path} could not be fully deleted; the record is partial")
            else:
                self.state.transition(
                    record["id"], "creating", "gone",
                    last_error="creation failed and the directory is not the recorded one",
                )  # fmt: skip
                notes.append(f"{self.path} was left: it is not the directory that was created")
        except Exception as cleanup:  # noqa: BLE001 - cleanup must not mask the original error
            notes.append(f"cleanup incomplete: {cleanup}")
        if notes and isinstance(exc, WtctlError):
            exc.message += "; " + "; ".join(notes)
            exc.args = (exc.message,)

    # -- step 10: activation and output -------------------------------------------------------

    def _finish(self, active: Record, decision_ids: list[str]) -> Result:
        out = record_object(self.state, active)
        payload = hooks.build_payload(
            "post-create",
            object_type="throwaway",
            object_id=active["id"],
            repo=active["repo"],
            labels=out["labels"],
            record=out,
            bootstrap=None,
        )
        env = hooks.hook_env(
            self.config, os.environ, hook="post-create", object_id=active["id"],
            object_type="throwaway",
        )  # fmt: skip
        specs = hooks.configured_hooks(
            self.config, "post-create", self.o.hook_flags.get("post-create", [])
        )
        post = hooks.run_post_hooks(
            "post-create", specs, payload, env=env, decision_ids=decision_ids
        )
        self.warnings += post.warnings
        self._log(
            "throwaway_create",
            "warn" if post.warnings else "info",
            {"path": active["path"], "source": active["source"], "depth": active["depth"]},
            rec=active,
        )
        data = {
            "record": out,
            "branch_action": None,
            "bootstrap": None,
            "reused": False,
            "warnings": list(self.warnings),
            "dry_run": False,
        }
        human = f"created throwaway {active['id']} at {active['source']} (depth {active['depth']})"
        return Result(data, human=active["path"], stderr="\n".join([human, *self.warnings]))

    def _result_dry(self) -> Result:
        now = self.state.timestamp()
        rec: Record = {
            **{k: None for k in RECORD_FIELDS},
            **self._fields(None),
            "root_id": self.state.anchor_identity(self.root) or "0:1",
            "labels": labelmod.render(labelmod.merge_labels((), self.o.labels)),
            "holders": [],
            "created": now,
            "updated": now,
        }
        obj = {k: rec[k] for k in RECORD_FIELDS}
        data = {
            "record": obj,
            "branch_action": None,
            "bootstrap": None,
            "reused": False,
            "warnings": list(self.warnings),
            "dry_run": True,
        }
        return Result(
            data,
            human=self.path,
            stderr="\n".join([f"would create throwaway {self.rid}", *self.warnings]),
        )

    # -- log ----------------------------------------------------------------------------------

    def _log(
        self, event: str, level: str, details: dict[str, Any], rec: Record | None = None
    ) -> None:
        warn = self.journal.log_best_effort(
            event,
            actor=self.ctx.actor.value,
            level=level,
            id=(rec or {}).get("id") or getattr(self, "rid", None),
            repo=self.o.repo,
            decision_ids=getattr(self, "decision_ids", list(self.ctx.decision_ids)),
            details=details,
        )
        if warn:
            self.warnings.append(warn)


def run(ctx: Context) -> Result:
    return ThrowawayCreator(ctx, parse_options(ctx)).run()


__all__ = [
    "OWNED_KEYS",
    "Options",
    "Plan",
    "ThrowawayCreator",
    "choose_source",
    "parse_options",
    "remote_url_choice",
    "run",
]
