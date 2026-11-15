"""``remove`` of a worktree (docs/interface.md, Remove order steps 1 to 11, Partial records).

The flow is: lease, integrity and lock, baseline, in use and holders, mirror refresh, content
checks, confirmation, ``pre-remove`` hooks, the step-9 re-check and fd-based deletion under the
per-clone lease, the branch, the record. Every refusal is the first by the documented precedence;
``--force`` (a human) overrides only ``held``, the dirty and unpushed checks and a fetch failure,
and never anything that is new at the step-9 re-check.

The record is moved to ``partial`` before the first entry is deleted and to ``removed`` after the
last, so a crash at any point in between leaves a truthful record that only a human can finish.

TODO(#21): throwaway removal. TODO(#23): ``reconcile`` settles records this command left
``partial``. TODO(#7): the record object comes from ``wtctl.record_view`` once list/show land.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from typing import Any

from . import (
    audit,
    baselinesvc,
    fsafe,
    fsafe_child,
    hooks,
    linkcheck,
    removal_content,
    removal_delete,
    removal_integrity,
    removal_status,
    removalfs,
    worktrees,
)
from .commands import status as status_cmd
from .commands.create import record_object
from .context import Context, Result
from .errors import UsageError, WtctlError
from .gitsetup import open_git, with_filters
from .hardgit import Git
from .mirror import Mirror, Tips
from .removal_status import DIRTY, ORDER, UNPUSHED, Status
from .state import LIVE_STATES, TOMBSTONE_STATES, Record, State, clone_lease, record_lease

REMOVE_HOOKS = ("pre-remove", "post-remove")
SHA_LEN = 40
IN_PROGRESS = "removal in progress: if this record is still partial, the removal did not finish"
RECHECKED = ("tracked", "untracked", "ignored", "stash")  # the content checks of step 9


@dataclasses.dataclass
class Options:
    merged_head: str | None
    keep_branch: bool
    holder: str | None
    force: bool
    override_escalation: bool
    hook_flags: dict[str, list[str]]


def _escape(text: str) -> str:
    return fsafe.escape_for_details(text)


def _details_key(details: list[dict[str, Any]]) -> set[str]:
    return {json.dumps(d, sort_keys=True) for d in details}


@dataclasses.dataclass
class Plan:
    """What step 10 will do with the branch, decided (and audited) before anything is deleted."""

    branch: str | None = None
    tip: str | None = None
    delete: bool = False
    kept_reason: str | None = None


class Remover:
    def __init__(self, ctx: Context, opts: Options, record_id: str) -> None:
        self.ctx = ctx
        self.o = opts
        self.rid = record_id
        self.config = ctx.config
        self.state: State = ctx.state
        self.journal = audit.journal_for(ctx)
        self.warnings: list[str] = []
        self.checks: dict[str, dict[str, Any]] = {}
        self.overridden: list[str] = []
        self.budget = fsafe_child.Budget()
        self.decision_ids: list[str] = list(ctx.decision_ids)
        self.head: str | None = None
        self.mirror: Mirror | None = None
        self.tips: Tips | None = None
        self.merged_verified = False
        self.step6: dict[str, set[str]] = {}
        self.flipped = False  # this command moved the record to `partial`
        self.deleting = False  # the first deletion has started

    # -- entry --------------------------------------------------------------------------------

    def run(self) -> Result:
        self.git: Git = open_git(self.config, self.ctx.gateway)
        try:
            with self.state.lease(record_lease(self.rid), wait=self.config.lease_wait_s) as lease:
                self.rlease = lease
                return self._locked(self._fresh())
        except WtctlError as exc:
            self._log_failure(exc)
            raise

    def _fresh(self) -> Record:
        rec = self.state.get_record(self.rid)
        if rec is None or rec["state"] in TOMBSTONE_STATES:
            self.state.resolve(self.rid)  # raises not_found (with the tombstone's details)
            raise WtctlError("not_found", f"no record {self.rid}")
        return rec

    def _locked(self, rec: Record) -> Result:
        self.rec = rec
        if rec["admin_dir"] is None and rec["state"] != "creating":
            raise self._refuse("main_worktree", f"{rec['path']} is the clone's main worktree")
        if not rec["owned"] or rec["state"] == "partial":
            self.ctx.require_human()
        if rec["state"] not in ("active", "partial"):
            raise self._refuse("unlinked", f"{rec['id']} is {rec['state']}, not active")
        self.st = Status(self.ctx, rec)
        self.st.git = self.git
        self.st.budget = self.budget
        self.st.warnings = self.warnings
        self.st.checks = self.checks
        self.settings = self.config.repo(rec["repo"])
        return self._normal(rec)

    # -- refusals -----------------------------------------------------------------------------

    def ordered(self) -> dict[str, dict[str, Any]]:
        return {k: self.checks[k] for k in ORDER if k in self.checks}

    def _refuse(self, reason: str, message: str, **fields: Any) -> WtctlError:
        details = fields.pop("details", None)
        extra: dict[str, Any] = {} if details is None else {"details": details}
        return WtctlError(
            "refused", message, reason=reason, checks=self.ordered(), **fields, **extra
        )

    def _unlinked(self, message: str) -> WtctlError:
        return self._refuse("unlinked", message)

    # -- steps 2 to 4 -------------------------------------------------------------------------

    def _links(self, st: Status) -> None:
        """Integrity, then the lock (step 2): ``unlinked`` or ``locked``."""
        rec = st.record
        if rec["dir_id"] is None:
            st.checks["integrity"] = {
                "status": "fail",
                "details": [{"step": "path", "expected": rec["path"], "actual": None}],
            }
            raise self._unlinked(f"{rec['id']} has no recorded directory identity")
        check, links = removal_integrity.integrity_check(self.state, rec, self.git)
        st.checks["integrity"] = check
        if links is None:
            raise self._unlinked(f"{_escape(rec['path'])} is not the recorded worktree")
        lock = removal_integrity.lock_check(rec)
        st.checks["locked"] = lock
        if lock["status"] == "fail":
            reason = lock["details"][0]["lock_reason"]
            raise self._refuse("locked", f"{rec['id']} is locked", details={"lock_reason": reason})
        if lock["status"] != "pass":
            raise self._unlinked(f"the admin entry of {rec['id']} cannot be opened")

    def _baseline(self, st: Status) -> None:
        """Step 3: a git-class change refuses, a claude-class change is a warning."""
        st.baseline()
        check = st.checks["baseline"]
        if check["status"] != "fail":
            return
        current = st.baseline_current
        assert current is not None
        err = baselinesvc.refusal(st.record, current, check["details"])
        raise WtctlError("refused", err.message, **{**err.fields, "checks": self.ordered()})

    def _in_use(self, st: Status) -> None:
        st.in_use()
        check = st.checks["in_use"]
        if check["status"] == "fail":
            raise self._refuse(
                "in_use",
                f"{st.record['id']} is in use by {len(check['details'])} process(es)",
                processes=check["details"],
            )
        if check["status"] != "pass":
            raise self._refuse(
                "in_use",
                f"cannot tell whether {st.record['id']} is in use: no process scanner works",
                processes=[],
            )

    def _held(self, st: Status, *, recheck: bool) -> None:
        st.held(self.o.holder)
        check = st.checks["held"]
        if check["status"] == "pass":
            return
        holders = list(st.record["holders"])
        if recheck:
            known = self.step6.get("held", set())
            if not self.o.force or _details_key(check["details"]) - known:
                raise self._refuse(
                    "held", f"{st.record['id']} has a new holder", details={"holders": holders}
                )
            return
        if not self.o.force:
            raise self._refuse("held", f"{st.record['id']} is held", details={"holders": holders})
        self.overridden.append("held")

    def _gates(self, st: Status, *, recheck: bool) -> None:
        self._links(st)
        self._baseline(st)
        self._in_use(st)
        self._held(st, recheck=recheck)

    # -- the normal flow ----------------------------------------------------------------------

    def _normal(self, rec: Record) -> Result:
        st = self.st
        if rec["state"] == "partial" and rec["dir_id"] is not None:
            check, links = removal_integrity.integrity_check(self.state, rec, self.git)
            if (
                links is None
                and rec["state"] == "partial"
                and check["details"][0]["step"] in removal_status.PARTIAL_FALLBACK_STEPS
            ):
                st.checks["integrity"] = check
                return self._finish_partial(rec)
        self._gates(st, recheck=False)
        if rec["state"] == "partial":
            self.warnings.append(
                f"{_escape(rec['path'])} is partial: a previous removal left it behind"
                + (f" ({_escape(rec['last_error'])})" if rec["last_error"] else "")
            )
        self._fetch()
        self._content()
        self._confirm_normal()
        self._pre_hooks()
        return self._delete_normal()

    # -- step 5 -------------------------------------------------------------------------------

    def _mirror(self) -> Mirror:
        return self.st.mirror_for()

    def _fetch(self) -> None:
        try:
            mirror = self._mirror()
            with self.state.lease(mirror.lease_name, wait=self.config.lease_wait_s) as lease:
                self.mirror = mirror
                self.tips = mirror.refresh(lease).tips
        except WtctlError as exc:
            if exc.code not in ("fetch_failed", "auth_required") or not self.o.force:
                raise
            details = exc.fields.get("details") or {}
            cause = (
                details.get("cause")
                if details.get("cause")
                in (
                    "timeout",
                    "prune_incomplete",
                )
                else None
            )
            self.checks["fetch"] = {
                "status": "fail",
                "details": [{"code": exc.code, "cause": cause, "message": _escape(exc.message)}],
            }
            self.overridden.append("fetch")
            self.tips = Tips({})  # nothing is pushed
            self.warnings.append(
                "the mirror could not be refreshed (overridden by --force); nothing counts as "
                "pushed, so the branch is kept"
            )
            return
        self.checks["fetch"] = {"status": "pass", "details": []}

    # -- step 6 -------------------------------------------------------------------------------

    def _inputs(self, st: Status) -> removal_content.Inputs:
        rec = st.record
        git = with_filters(
            self.git, self.settings.allow_filters, self.ctx.gateway, self.warnings.append
        )
        return removal_content.Inputs(
            git=git,
            state=self.state,
            record=rec,
            config=self.config,
            disposable=self.settings.disposable,
            mirror=self.mirror,
            tips=self.tips,
            merged_head=self.o.merged_head,
            budget=self.budget,
            warnings=self.warnings,
        )

    def _content(self) -> None:
        inp = self._inputs(self.st)
        try:
            self.checks.update(removal_content.run_all(inp))
            self.head = removal_content.head_sha(inp)
        except fsafe.FsafeError as exc:
            self.checks["integrity"] = {
                "status": "fail",
                "details": [
                    {
                        "step": linkcheck.exc_step(exc, self.rec),
                        "expected": self.rec["path"],
                        "actual": None,
                    }
                ],
            }
            raise self._unlinked(
                f"{_escape(self.rec['path'])} changed while it was being checked ({exc.kind})"
            ) from exc
        self.merged_verified = removal_content.merged_head_ok(inp, self.head)
        for name in (*DIRTY, *UNPUSHED):
            if self.checks[name]["status"] not in ("pass", "fail"):
                raise WtctlError("internal", f"the check {name} could not run")
        failed = [n for n in (*DIRTY, *UNPUSHED) if self.checks[n]["status"] == "fail"]
        if failed and not self.o.force:
            dirty = any(n in DIRTY for n in failed)
            raise self._refuse(
                "dirty" if dirty else "unpushed",
                f"{self.rid} has "
                + ("changes that would be lost" if dirty else "commits that are not pushed"),
            )
        self.overridden += failed
        self.overridden = [n for n in removal_status.FORCEABLE if n in self.overridden]
        for name in (*RECHECKED, "held"):
            self.step6[name] = _details_key(self.checks[name]["details"])

    # -- step 7 -------------------------------------------------------------------------------

    def _summary(self) -> str:
        lines = [f"remove {self.rec['name']} ({_escape(self.rec['path'])})"]
        for name, check in self.ordered().items():
            if check["status"] not in ("pass",):
                lines.append(f"  {name}: {check['status']} ({len(check['details'])})")
        if self.overridden:
            lines.append(f"  --force overrides: {', '.join(self.overridden)}")
        lines += [f"  warning: {w}" for w in self.warnings]
        return "\n".join(lines)

    def _confirm(self, summary: str) -> None:
        if self.ctx.yes:
            return
        if not self.ctx.runtime.stdin_isatty:
            raise WtctlError(
                "confirmation_required",
                f"removing {self.rid} needs confirmation: pass --yes, or run it from a terminal",
            )
        sys.stderr.write(summary + "\nRemove? [y/N] ")
        sys.stderr.flush()
        answer = sys.stdin.readline().strip().lower() if sys.stdin is not None else ""
        if answer not in ("y", "yes"):
            raise WtctlError("confirmation_required", f"removing {self.rid} was not confirmed")

    def _confirm_normal(self) -> None:
        self._confirm(self._summary())

    # -- step 8 -------------------------------------------------------------------------------

    def _pre_hooks(self) -> None:
        rec = self.rec
        payload = hooks.build_payload(
            "pre-remove",
            object_type="worktree",
            object_id=rec["id"],
            repo=rec["repo"],
            labels=rec["labels"],
            record=record_object(rec, self.state.bootstrap_summary(rec["id"])),
            checks=self.ordered(),
            merged_head=self.o.merged_head,
            overridden=list(self.overridden),
            actor=self.ctx.actor.value,
            decision_ids=list(self.ctx.decision_ids),
        )
        env = hooks.hook_env(
            self.config, os.environ, hook="pre-remove", object_id=rec["id"], object_type="worktree"
        )
        specs = hooks.configured_hooks(
            self.config, "pre-remove", self.o.hook_flags.get("pre-remove", [])
        )
        try:
            out = hooks.run_pre_hooks(
                "pre-remove",
                specs,
                payload,
                env=env,
                actor=self.ctx.actor,
                override_escalation=self.o.override_escalation,
                decision_ids=self.ctx.decision_ids,
                on_tick=self.rlease.renew,
            )
        except WtctlError as exc:
            self._log_hooks(getattr(exc, "hook_runs", ()))
            raise
        self._log_hooks(out.runs)
        self.warnings += out.warnings
        self.decision_ids = list(out.decision_ids)

    def _log_hooks(self, runs: Any) -> None:
        for r in runs:
            self._log(
                "hook",
                "warn" if (r.exit_code or r.timed_out or r.start_error) else "info",
                {
                    "hook": r.hook,
                    "cmd": r.cmd,
                    "exit": r.exit_code,
                    "timed_out": r.timed_out,
                    "duration": r.duration,
                    "stderr": r.stderr,
                },
            )

    # -- step 9 to 11 -------------------------------------------------------------------------

    def _recheck(self) -> Status:
        """Step 9 before any deletion: the checks again, under the per-clone lease. A failure
        that is new since step 6 refuses even under ``--force``."""
        st = Status(self.ctx, self._fresh_record())
        st.git = self.git
        st.budget = self.budget
        st.checks = self.checks  # a refusal shows the re-check's results
        self._gates(st, recheck=True)
        for w in st.warnings:
            if w not in self.warnings:
                self.warnings.append(w)
        inp = self._inputs(st)
        try:
            head = removal_content.head_sha(inp)
            results = removal_content.tracked_untracked_ignored(inp)
            results["stash"] = removal_content.stash(inp, head)
        except fsafe.FsafeError as exc:
            raise self._unlinked(
                f"{_escape(self.rec['path'])} changed while it was re-checked ({exc.kind})"
            ) from exc
        new = False
        shown = dict(self.checks)
        for name in RECHECKED:
            res = results[name]
            if res["status"] not in ("pass", "fail"):
                raise WtctlError("internal", f"the check {name} could not run at the re-check")
            added = [
                d for d in res["details"] if json.dumps(d, sort_keys=True) not in self.step6[name]
            ]
            if added:
                new = True
                shown[name] = {"status": "fail", "details": added}
        if new:
            self.checks.update(shown)
            raise self._refuse("dirty", f"{self.rid} changed after the checks: new changes found")
        if head != self.head:
            self.checks["unpushed_head"] = {
                "status": "fail",
                "details": [{"sha": head, "detail": "head_moved"}],
            }
            raise self._refuse("unpushed", f"HEAD of {self.rid} moved after the checks")
        return st

    def _fresh_record(self) -> Record:
        rec = self.state.get_record(self.rid)
        if rec is None or rec["state"] not in LIVE_STATES:
            raise WtctlError("busy", f"record {self.rid} changed while it was being removed")
        return rec

    def _delete_normal(self) -> Result:
        rec = self.rec
        with self.state.lease(clone_lease(rec["clone_path"]), wait=self.config.lease_wait_s):
            st9 = self._recheck()
            rec = st9.record
            plan = self._plan_branch(rec)
            prepared = self._prepare_audits(rec, plan)
            admin = removal_delete.admin_check(self.state, rec, self.budget)
            if isinstance(admin, fsafe.FsafeError) or not admin.present:
                raise self._unlinked(f"the admin entry of {rec['id']} cannot be opened")
            if admin.gitdir != os.path.join(rec["path"], ".git"):
                raise self._unlinked(f"the admin entry of {rec['id']} names another path")
            if admin.locked or removal_integrity.lock_check(rec)["status"] != "pass":
                raise self._refuse("locked", f"{rec['id']} is locked", details={"lock_reason": ""})
            self._begin(rec)
            outcome = removal_delete.delete_worktree(self.state, rec, self.budget)
            self._after_tree(outcome, rec, prepared)
            self._finish_admin(rec, prepared)
            branch_deleted, kept = self._branch(rec, plan, prepared)
        return self._finish(rec, prepared, branch_deleted, kept)

    # -- the partial flow ---------------------------------------------------------------------

    def _finish_partial(self, rec: Record) -> Result:
        """A ``partial`` record whose pointers are gone: no git, no content checks."""
        st = self.st
        st.partial_worktree_unlinked()
        if st.checks.get("integrity", {}).get("status") != "pass":
            raise self._unlinked(f"{_escape(rec['path'])} is not the recorded remaining path")
        st.skip_git_checks()
        self._in_use(st)
        st.checks["held"] = {"status": "skipped", "details": []}
        entries = self._remaining_listing(rec)
        self._confirm("\n".join([f"finish removing {rec['name']}", *entries, *self.warnings]))
        self._pre_hooks()
        with self.state.lease(clone_lease(rec["clone_path"]), wait=self.config.lease_wait_s):
            plan = Plan(kept_reason=self._partial_kept_reason(rec))
            prepared = self._prepare_audits(rec, plan)
            admin = removal_delete.admin_check(self.state, rec, self.budget)
            if isinstance(admin, fsafe.FsafeError):
                raise self._unlinked(f"the admin entry of {rec['id']} cannot be opened")
            self._begin(rec)
            outcome = removal_delete.delete_worktree(
                self.state, rec, self.budget, allow_missing=True
            )
            self._after_tree(outcome, rec, prepared)
            self._finish_admin(rec, prepared)
        return self._finish(rec, prepared, False, plan.kept_reason)

    def _partial_kept_reason(self, rec: Record) -> str:
        if not rec["created_branch"]:
            return "not_created"
        if not self.config.remove.delete_branch:
            return "disabled"
        if self.o.keep_branch:
            return "keep_branch"
        self.warnings.append(
            "the branch was kept: the pushed state is not checked when finishing a partial removal"
        )
        return "unpushed"

    def _remaining_listing(self, rec: Record) -> list[str]:
        try:
            args = {**removalfs.walk_args(self.state, rec), "list_limit": 100}
            res = removalfs.run("walk", args, self.budget)
        except fsafe.FsafeError:
            return []
        if not res.ok or res.value is None:
            return []
        root = rec["path"].rstrip("/")
        lines = [f"  {_escape(rec['path'])}: {res.value['count']} entries"]
        lines += ["  " + _escape("/".join([root, *e])) for e in res.value.get("entries", [])]
        return lines

    # -- deletion bookkeeping -----------------------------------------------------------------

    def _begin(self, rec: Record) -> None:
        """Mark the record `partial` before anything is deleted (a crash then leaves a truthful
        record)."""
        if rec["state"] == "active":
            self.state.transition(rec["id"], "active", "partial", last_error=IN_PROGRESS)
            self.flipped = True
        self.deleting = True

    def _revert(self, rec: Record) -> None:
        """Nothing was deleted: the record is as it was."""
        if self.flipped:
            self.state.transition(rec["id"], "partial", "active", last_error=None)
            self.flipped = False
        self.deleting = False

    def _after_tree(
        self, outcome: removal_delete.TreeOutcome, rec: Record, prepared: Prepared
    ) -> None:
        if outcome.status == "refused":
            self._revert(rec)
            why = outcome.error.kind if outcome.error else "unknown"
            raise self._unlinked(f"{_escape(rec['path'])} is not the recorded worktree ({why})")
        if outcome.status == "partial":
            self._partial(rec, rec["path"], outcome.remaining, outcome.remaining_total, prepared)

    def _finish_admin(self, rec: Record, prepared: Prepared) -> None:
        state, remaining = removal_delete.delete_admin(self.state, rec, self.budget)
        if state == "foreign":
            self.warnings.append(
                f"the admin entry {_escape(rec['admin_dir'] or '')} names another path and was left"
            )
        elif state in ("partial", "failed"):
            self._partial(rec, rec["admin_dir"], remaining, max(len(remaining), 1), prepared)

    def _partial(
        self, rec: Record, path: str, remaining: list[str], total: int, prepared: Prepared
    ) -> None:
        shown = remaining or [_escape(path)]
        error = f"could not delete {shown[0]}" + (f" and {total - 1} more" if total > 1 else "")
        self.state.transition(rec["id"], ("active", "partial"), "partial", last_error=error)
        self._audit_failed(prepared)
        raise WtctlError(
            "partial",
            f"removing {rec['id']} left {total} entr{'y' if total == 1 else 'ies'} behind",
            details={"remaining_path": _escape(path), "remaining": shown[:100]},
        )

    # -- step 10 ------------------------------------------------------------------------------

    def _plan_branch(self, rec: Record) -> Plan:
        plan = Plan(branch=rec["branch"])
        if not rec["created_branch"] or not rec["branch"]:
            plan.kept_reason = "not_created"
        elif not self.config.remove.delete_branch:
            plan.kept_reason = "disabled"
        elif self.o.keep_branch:
            plan.kept_reason = "keep_branch"
        else:
            self._plan_created_branch(rec, plan)
        return plan

    def _plan_created_branch(self, rec: Record, plan: Plan) -> None:
        common = rec["common_dir"]
        branch = str(rec["branch"])
        ref = f"refs/heads/{branch}"
        r = self.git.run(
            ["rev-parse", "--verify", "-q", ref], git_dir=common, cwd=common, timeout=60
        )
        tip = r.out.strip()
        if r.returncode != 0 or r.timed_out or len(tip) != SHA_LEN:
            plan.kept_reason = "not_created"
            self.warnings.append(f"the branch {_escape(branch)} no longer exists")
            return
        plan.tip = tip
        try:
            entries = worktrees.worktree_entries(self.git, common)
        except WtctlError:
            plan.kept_reason = "checked_out_elsewhere"
            self.warnings.append("the worktrees of the clone could not be listed; branch kept")
            return
        mine = os.path.realpath(rec["path"])
        if any(e.branch == ref and os.path.realpath(e.path) != mine for e in entries):
            plan.kept_reason = "checked_out_elsewhere"
            return
        pushed = self.mirror is not None and self.mirror.is_pushed(tip, self.tips).pushed
        merged = self.merged_verified and tip == self.o.merged_head
        if pushed or merged:
            plan.delete = True
        else:
            plan.kept_reason = "unpushed"

    def _branch(self, rec: Record, plan: Plan, prepared: Prepared) -> tuple[bool, str | None]:
        if not plan.delete or plan.branch is None or plan.tip is None:
            return False, plan.kept_reason
        common = rec["common_dir"]
        if not worktrees.delete_created_branch(self.git, common, plan.branch, plan.tip):
            self.warnings.append(
                f"the branch {_escape(plan.branch)} changed after it was checked and was kept"
            )
            return False, "unpushed"
        self.git.run(
            ["config", "--local", "--remove-section", f"branch.{plan.branch}"],
            git_dir=common,
            cwd=common,
            timeout=60,
        )
        prepared.branch_deleted = True
        return True, None

    # -- audit --------------------------------------------------------------------------------

    def _prepare_audits(self, rec: Record, plan: Plan) -> Prepared:
        """Validate and encode the audit records before anything changes (a failure is exit 1)."""
        common: dict[str, Any] = {
            "actor": self.ctx.actor.value,
            "decision_ids": self.decision_ids,
            "target_id": rec["id"],
            "target_path": rec["path"],
        }
        inputs = {
            "head": self.head,
            "merged_head": self.o.merged_head,
            "baseline_digest": rec["baseline_digest"],
            "overridden": list(self.overridden),
        }
        self.journal.check_writable()
        ok = self.journal.prepare_audit("remove", inputs=inputs, result="ok", **common)
        bad = self.journal.prepare_audit("remove", inputs=inputs, result="error", **common)
        branch = None
        if plan.delete and plan.branch is not None and plan.tip is not None:
            branch = self.journal.prepare_audit(
                "branch_delete",
                inputs={"branch": plan.branch, "sha": plan.tip},
                result="ok",
                **common,
            )
        return Prepared(ok, bad, branch)

    def _audit_failed(self, prepared: Prepared) -> None:
        self.journal.commit_audit(prepared.failed)

    # -- step 11 ------------------------------------------------------------------------------

    def _finish(
        self, rec: Record, prepared: Prepared, branch_deleted: bool, kept: str | None
    ) -> Result:
        removed = self.state.transition(
            rec["id"], ("active", "partial"), "removed", last_error=None
        )
        self.journal.commit_audit(prepared.ok)
        if prepared.branch is not None and prepared.branch_deleted:
            self.journal.commit_audit(prepared.branch)
        out = record_object(removed, self.state.bootstrap_summary(removed["id"]))
        payload = hooks.build_payload(
            "post-remove",
            object_type="worktree",
            object_id=removed["id"],
            repo=removed["repo"],
            labels=removed["labels"],
            record=out,
            branch_deleted=branch_deleted,
            branch_kept_reason=kept,
            overridden=list(self.overridden),
        )
        env = hooks.hook_env(
            self.config, os.environ, hook="post-remove", object_id=removed["id"],
            object_type="worktree",
        )  # fmt: skip
        specs = hooks.configured_hooks(
            self.config, "post-remove", self.o.hook_flags.get("post-remove", [])
        )
        post = hooks.run_post_hooks(
            "post-remove", specs, payload, env=env, decision_ids=self.decision_ids,
            on_tick=self.rlease.renew,
        )  # fmt: skip
        self.warnings += post.warnings
        self._log_hooks(post.runs)
        self._log(
            "remove",
            "warn" if post.warnings else "info",
            {
                "path": removed["path"],
                "branch_deleted": branch_deleted,
                "branch_kept_reason": kept,
                "overridden": list(self.overridden),
            },
            rec=removed,
        )
        data = {
            "record": out,
            "checks": self.ordered(),
            "overridden": list(self.overridden),
            "branch_deleted": branch_deleted,
            "branch_kept_reason": kept,
            "warnings": self.warnings,
        }
        human = f"removed {removed['name']} ({_escape(removed['path'])})"
        return Result(data, human=human)

    # -- log ----------------------------------------------------------------------------------

    def _log(
        self, event: str, level: str, details: dict[str, Any], rec: Record | None = None
    ) -> None:
        r = rec or getattr(self, "rec", None)
        warn = self.journal.log_best_effort(
            event,
            actor=self.ctx.actor.value,
            level=level,
            id=r["id"] if r else self.rid,
            repo=r["repo"] if r else None,
            decision_ids=self.decision_ids,
            details=details,
        )
        if warn:
            self.warnings.append(warn)

    def _log_failure(self, exc: WtctlError, rec: Record | None = None) -> None:
        level = audit.level_for(
            exc.exit_code,
            escalated=exc.code == "escalated",
        )
        self._log(
            "refused" if exc.code == "refused" else "remove",
            level,
            {"code": exc.code, "reason": exc.fields.get("reason"), "message": exc.message},
            rec=rec,
        )


@dataclasses.dataclass
class Prepared:
    ok: audit.PreparedAudit
    failed: audit.PreparedAudit
    branch: audit.PreparedAudit | None
    branch_deleted: bool = False


def parse_options(ctx: Context) -> Options:
    a = ctx.args
    merged = status_cmd.merged_head_arg(a.merged_head)
    hook_flags = hooks.parse_hook_flags(a.hook)
    for name in hook_flags:
        if name not in REMOVE_HOOKS:
            raise UsageError(f"--hook {name}: remove fires only {', '.join(REMOVE_HOOKS)}")
    return Options(
        merged_head=merged,
        keep_branch=a.keep_branch,
        holder=a.holder,
        force=a.force,
        override_escalation=a.override_escalation,
        hook_flags=hook_flags,
    )
