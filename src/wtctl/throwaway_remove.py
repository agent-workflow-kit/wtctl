"""``remove`` of a throwaway (docs/interface.md, Throwaway removal), also of a ``partial`` one.

No confirmation, no baseline, no content checks and no git. Under the per-record lease: the
in-use check, the ``pre-remove`` hooks, the in-use check again (hooks take time), then the
fd-based deletion of ``<base_dir>/<id>/`` by the same child operation that deletes a worktree's
tree (``removal_delete``): the throwaway root is opened no-follow and must have its recorded
identity, the directory its recorded ``dir_id``, no link is followed and no mount point is
crossed. The record is ``partial`` while the deletion runs (a crash leaves a truthful record), and
``removed`` after it; a directory that is already gone marks it ``gone``. Throwaway removals are
logged, not audited.

``prune`` calls :meth:`ThrowawayRemover.remove` with the lease it took itself.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from . import audit, fsafe, fsafe_child, hooks, removal_delete, removalfs
from .context import Context, Result
from .errors import WtctlError
from .record_view import record_object
from .removal_status import Status
from .state import LIVE_STATES, TOMBSTONE_STATES, Lease, Record, State, record_lease

IN_PROGRESS = "removal in progress: if this record is still partial, the removal did not finish"


@dataclasses.dataclass
class RemoveOptions:
    hook_flags: dict[str, list[str]]
    override_escalation: bool


def _escape(text: str) -> str:
    return fsafe.escape_for_details(text)


class ThrowawayRemover:
    def __init__(self, ctx: Context, record_id: str, opts: RemoveOptions) -> None:
        self.ctx = ctx
        self.o = opts
        self.rid = record_id
        self.config = ctx.config
        self.state: State = ctx.state
        self.journal = audit.journal_for(ctx)
        self.warnings: list[str] = []
        self.checks: dict[str, dict[str, Any]] = {}
        self.budget = fsafe_child.Budget()
        # the deletion has a budget of its own, so the earlier steps cannot starve it
        self.del_budget = fsafe_child.Budget()
        self.decision_ids: list[str] = list(ctx.decision_ids)
        self.lease: Lease | None = None
        self.flipped = False

    # -- entry --------------------------------------------------------------------------------

    def run(self) -> Result:
        """Take the per-record lease (waiting up to ``lease_wait``) and remove."""
        try:
            with self.state.lease(record_lease(self.rid), wait=self.config.lease_wait_s) as lease:
                return self.remove(lease)
        except WtctlError as exc:
            self._log_failure(exc)
            raise

    def remove(self, lease: Lease) -> Result:
        """Remove under ``lease`` (held by the caller)."""
        self.lease = lease
        rec = self._fresh()
        self.rec = rec
        if rec["state"] not in ("active", "partial"):
            raise self._refuse("unlinked", f"{self.rid} is {rec['state']}, not active")
        self._in_use(rec)
        self._pre_hooks(rec)
        rec = self._fresh()
        self.rec = rec
        self._in_use(rec)  # the hooks and the wait for them took time
        gone = self._probe(rec)
        if gone:
            self.warnings.append(f"{_escape(rec['path'])} no longer exists; the record is `gone`")
            new = self.state.transition(rec["id"], ("active", "partial"), "gone", last_error=None)
            return self._finish(new, "gone")
        self._begin(rec)
        outcome = removal_delete.delete_throwaway(rec, self.del_budget)
        if outcome.status == "refused":
            self._revert(rec)
            why = outcome.error.kind if outcome.error else "unknown"
            hint = "; unmount it first" if why == "mount_point" else ""
            raise self._refuse(
                "unlinked", f"{_escape(rec['path'])} is not the recorded throwaway ({why}){hint}"
            )
        if outcome.status == "partial":
            self._partial(rec, outcome.remaining, outcome.remaining_total)
        self._check_lease()
        try:
            removed = self.state.transition(
                rec["id"], ("active", "partial"), "removed", last_error=None
            )
        except WtctlError:
            self._set_last_error(rec, "deleted; the record could not be marked removed")
            raise
        return self._finish(removed, "removed")

    # -- state --------------------------------------------------------------------------------

    def _fresh(self) -> Record:
        rec = self.state.get_record(self.rid)
        if rec is None or rec["state"] in TOMBSTONE_STATES:
            self.state.resolve(self.rid)  # raises not_found (with the tombstone's details)
            raise WtctlError("not_found", f"no record {self.rid}")
        if rec["object_type"] != "throwaway" or rec["state"] not in LIVE_STATES:
            raise WtctlError("busy", f"record {self.rid} changed while it was being removed")
        return rec

    def _check_lease(self) -> None:
        if self.lease is not None and self.lease.lost:
            raise WtctlError("busy", f"the lease on {self.rid} was lost during the removal")

    def _refuse(self, reason: str, message: str, **fields: Any) -> WtctlError:
        return WtctlError("refused", message, reason=reason, checks=dict(self.checks), **fields)

    def _begin(self, rec: Record) -> None:
        """Mark the record `partial` before anything is deleted (a crash then leaves a truthful
        record, and a later `remove` or `prune` finishes it)."""
        if rec["state"] == "active":
            self.state.transition(rec["id"], "active", "partial", last_error=IN_PROGRESS)
            self.flipped = True

    def _revert(self, rec: Record) -> None:
        if self.flipped:
            self.state.transition(rec["id"], "partial", "active", last_error=None)
            self.flipped = False

    def _set_last_error(self, rec: Record, message: str) -> None:
        try:
            self.state.transition(rec["id"], ("active", "partial"), "partial", last_error=message)
        except WtctlError:
            pass  # best effort: the caller is already reporting a failure

    def _partial(self, rec: Record, remaining: list[str], total: int) -> None:
        shown = remaining or [_escape(rec["path"])]
        error = f"could not delete {shown[0]}" + (f" and {total - 1} more" if total > 1 else "")
        self.state.transition(rec["id"], ("active", "partial"), "partial", last_error=error)
        raise WtctlError(
            "partial",
            f"removing {rec['id']} left {total} entr{'y' if total == 1 else 'ies'} behind",
            details={"remaining_path": _escape(rec["path"]), "remaining": shown[:100]},
        )

    # -- checks -------------------------------------------------------------------------------

    def _in_use(self, rec: Record) -> None:
        st = Status(self.ctx, rec)
        st.budget = self.budget
        st.in_use()
        for w in st.warnings:
            if w not in self.warnings:
                self.warnings.append(w)
        check = st.checks["in_use"]
        self.checks["in_use"] = check
        if check["status"] == "fail":
            raise self._refuse(
                "in_use",
                f"{rec['id']} is in use by {len(check['details'])} process(es)",
                processes=check["details"],
            )
        if check["status"] != "pass":
            raise self._refuse(
                "in_use",
                f"cannot tell whether {rec['id']} is in use: no process scanner works",
                processes=[],
            )

    def _probe(self, rec: Record) -> bool:
        """Open the throwaway root with its recorded identity and look for the directory's name:
        True when the directory is absent. A root that is missing, replaced or on another mount
        is ``unlinked`` (the data may exist elsewhere), never "gone"."""
        try:
            args = removal_delete.throwaway_args(rec)
        except fsafe.FsafeError as exc:
            raise self._refuse("unlinked", f"{_escape(rec['path'])}: {exc.message}") from None
        res = removalfs.run("entry_state", args, self.budget)
        if not res.ok or res.value is None:
            why = res.error.kind if res.error else "deadline"
            raise self._refuse(
                "unlinked", f"the throwaway root of {rec['id']} cannot be verified ({why})"
            )
        return bool(res.value["state"] == "absent")

    # -- hooks --------------------------------------------------------------------------------

    def _pre_hooks(self, rec: Record) -> None:
        payload = hooks.build_payload(
            "pre-remove",
            object_type="throwaway",
            object_id=rec["id"],
            repo=rec["repo"],
            labels=rec["labels"],
            record=record_object(self.state, rec),
            checks=dict(self.checks),
            merged_head=None,
            overridden=[],
            actor=self.ctx.actor.value,
            decision_ids=list(self.ctx.decision_ids),
        )
        env = hooks.hook_env(
            self.config, os.environ, hook="pre-remove", object_id=rec["id"],
            object_type="throwaway",
        )  # fmt: skip
        specs = hooks.configured_hooks(
            self.config, "pre-remove", self.o.hook_flags.get("pre-remove", [])
        )
        assert self.lease is not None
        try:
            out = hooks.run_pre_hooks(
                "pre-remove",
                specs,
                payload,
                env=env,
                actor=self.ctx.actor,
                override_escalation=self.o.override_escalation,
                decision_ids=self.ctx.decision_ids,
                on_tick=self.lease.renew,
            )
        except WtctlError as exc:
            self._log_hooks(getattr(exc, "hook_runs", ()), rec)
            raise
        self._log_hooks(out.runs, rec)
        self.warnings += out.warnings
        self.decision_ids = list(out.decision_ids)

    def _log_hooks(self, runs: Any, rec: Record) -> None:
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
                rec,
            )

    # -- the end ------------------------------------------------------------------------------

    def _finish(self, rec: Record, outcome: str) -> Result:
        out = record_object(self.state, rec)
        payload = hooks.build_payload(
            "post-remove",
            object_type="throwaway",
            object_id=rec["id"],
            repo=rec["repo"],
            labels=rec["labels"],
            record=out,
            branch_deleted=False,
            branch_kept_reason=None,
            overridden=[],
        )
        env = hooks.hook_env(
            self.config, os.environ, hook="post-remove", object_id=rec["id"],
            object_type="throwaway",
        )  # fmt: skip
        specs = hooks.configured_hooks(
            self.config, "post-remove", self.o.hook_flags.get("post-remove", [])
        )
        assert self.lease is not None
        post = hooks.run_post_hooks(
            "post-remove", specs, payload, env=env, decision_ids=self.decision_ids,
            on_tick=self.lease.renew,
        )  # fmt: skip
        self.warnings += post.warnings
        self._log_hooks(post.runs, rec)
        self._log(
            "throwaway_remove",
            "warn" if post.warnings or outcome == "gone" else "info",
            {"path": rec["path"], "result": outcome},
            rec,
        )
        data = {
            "record": out,
            "checks": dict(self.checks),
            "overridden": [],
            "branch_deleted": False,
            "branch_kept_reason": None,
            "warnings": list(self.warnings),
        }
        word = "removed" if outcome == "removed" else "marked gone"
        return Result(
            data,
            human=f"{word} throwaway {rec['id']} ({_escape(rec['path'])})",
            stderr="\n".join(self.warnings),
        )

    # -- log ----------------------------------------------------------------------------------

    def _log(self, event: str, level: str, details: dict[str, Any], rec: Record | None) -> None:
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

    def _log_failure(self, exc: WtctlError) -> None:
        level = audit.level_for(exc.exit_code, escalated=exc.code == "escalated")
        self._log(
            "refused" if exc.code == "refused" else "throwaway_remove",
            level,
            {"code": exc.code, "reason": exc.fields.get("reason"), "message": exc.message},
            None,
        )
