"""``wtctl prune --throwaways`` (docs/interface.md, prune).

Removes live throwaways older than ``--older-than`` (default ``throwaway.max_age``) through
Throwaway removal, and retries every ``partial`` throwaway whatever its age. It skips records
with a label matching ``prune.protect_labels`` and throwaways that are in use, vetoed or
escalated by a hook, or busy; a removal that fails is reported ``failed`` and ``prune`` still
exits 0. It runs under the global lease. Cron-compatible: no prompt, no TTY needed, every outcome
is data.
"""

from __future__ import annotations

from typing import Any

from .. import audit
from .. import labels as labelmod
from ..context import Context, Result
from ..duration import parse_duration
from ..errors import UsageError, WtctlError
from ..output import escape_control
from ..removal_status import Status
from ..state import GLOBAL_LEASE, Record, record_lease
from ..throwaway_remove import RemoveOptions, ThrowawayRemover

MAX_PATHS = 100


def protected(labels: list[str], protect: list[str]) -> bool:
    """A ``prune.protect_labels`` entry is a key (any value) or a ``key=value`` pair."""
    have = {x.partition("=")[0] for x in labels} | set(labels)
    return any(p in have for p in protect)


def _skip(rid: str, reason: str, processes: Any = (), paths: Any = ()) -> dict[str, Any]:
    return {
        "id": rid,
        "reason": reason,
        "processes": list(processes),
        "paths": list(paths)[:MAX_PATHS],
    }


class Pruner:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.state = ctx.state
        self.removed: list[str] = []
        self.skipped: list[dict[str, Any]] = []
        self.journal = audit.journal_for(ctx)
        self.warnings: list[str] = []
        self.cutoff: str | None = None

    # -- usage --------------------------------------------------------------------------------

    def age(self) -> int | None:
        a = self.ctx.args
        labelmod.merge_labels((), a.label)
        if a.older_than is not None:
            return parse_duration(a.older_than, flag="--older-than")
        configured = self.config.throwaway.max_age
        if configured is not None:
            return parse_duration(configured, flag="throwaway.max_age")
        if not a.if_configured:
            raise UsageError(
                "prune needs --older-than or throwaway.max_age (or --if-configured, which then "
                "only retries partial throwaways)"
            )
        return None

    # -- selection ----------------------------------------------------------------------------

    def candidates(self, age: int | None) -> list[Record]:
        """Throwaways to try, oldest first: every ``partial`` one, and the ``active`` ones older
        than ``age``. ``creating`` ones belong to a live (or dead) creator, which reconcile
        settles (TODO(#23): until it lands, a killed creator's record stays); nothing here
        touches them."""
        cutoff = self.cutoff = None if age is None else self.state.timestamp_before(age)
        found = self.state.records(
            object_type="throwaway", states=("active", "partial"), labels=self.ctx.args.label
        )
        return [
            r
            for r in found
            if r["state"] == "partial" or (cutoff is not None and r["created"] < cutoff)
        ]

    # -- one record ---------------------------------------------------------------------------

    def one(self, rec: Record) -> None:
        rid = rec["id"]
        if protected(rec["labels"], self.config.prune.protect_labels):
            self.skipped.append(_skip(rid, "protected"))
            return
        if self.ctx.dry_run:
            self.dry(rec)
            return
        try:
            lease = self.state.try_lease(record_lease(rid))
        except WtctlError as exc:  # held by this very process (tests, a hook running wtctl)
            if exc.code != "busy":
                raise
            lease = None
        if lease is None:
            self.skipped.append(_skip(rid, "busy"))
            return
        try:
            if not self.still_due(rid):
                return
            opts = RemoveOptions(hook_flags={}, override_escalation=False)
            ThrowawayRemover(self.ctx, rid, opts).remove(lease)
            self.removed.append(rid)
        except WtctlError as exc:
            self.skipped.append(self.map_error(rid, exc))
        except Exception as exc:  # noqa: BLE001 - one record must not stop the run
            self.warnings.append(f"{rid}: {type(exc).__name__}: {exc}")
            self.skipped.append(_skip(rid, "failed"))
        finally:
            lease.release()

    def still_due(self, rid: str) -> bool:
        """The selection was made before the lease: under the lease, re-read the record. One that
        was removed (or went missing) meanwhile is skipped silently, and one that is no longer
        old enough or became protected is left alone."""
        cur = self.state.get_record(rid)
        if cur is None or cur["object_type"] != "throwaway":
            return False
        if cur["state"] not in ("active", "partial"):
            return False
        if protected(cur["labels"], self.config.prune.protect_labels):
            self.skipped.append(_skip(rid, "protected"))
            return False
        return not (
            cur["state"] == "active" and (self.cutoff is None or cur["created"] >= self.cutoff)
        )

    def dry(self, rec: Record) -> None:
        st = Status(self.ctx, rec)
        st.in_use()
        check = st.checks["in_use"]
        if check["status"] == "fail":
            self.skipped.append(_skip(rec["id"], "in_use", check["details"]))
        elif check["status"] != "pass":
            self.skipped.append(_skip(rec["id"], "failed"))  # no scanner: not removable
        else:
            self.removed.append(rec["id"])

    def map_error(self, rid: str, exc: WtctlError) -> dict[str, Any]:
        if exc.code == "refused" and exc.fields.get("reason") == "in_use":
            processes = exc.fields.get("processes") or []
            if processes:
                return _skip(rid, "in_use", processes)
            # no process scanner: nothing to list, and the schema's `in_use` always lists some.
            # The record is untouched and the next run tries again.
            self.warnings.append(f"{rid}: {exc.message}")
            return _skip(rid, "failed")
        if exc.code == "vetoed":
            return _skip(rid, "vetoed")
        if exc.code == "escalated":
            return _skip(rid, "escalated")
        if exc.code == "busy":
            return _skip(rid, "busy")
        paths = (exc.fields.get("details") or {}).get("remaining") or []
        self.warnings.append(f"{rid}: {exc.message}")
        return _skip(rid, "failed", paths=paths)

    # -- run ----------------------------------------------------------------------------------

    def run(self) -> Result:
        age = self.age()
        if self.ctx.dry_run:
            return self.result(self.work(age), already_running=False)
        try:
            lease = self.state.lease(GLOBAL_LEASE, wait=self.config.lease_wait_s)
        except WtctlError as exc:
            if exc.code != "busy":
                raise
            return self.result(None, already_running=True)
        with lease:
            return self.result(self.work(age), already_running=False)

    def work(self, age: int | None) -> int:
        todo = self.candidates(age)
        for rec in todo:
            self.one(rec)
        return len(todo)

    def result(self, tried: int | None, *, already_running: bool) -> Result:
        data = {
            "removed": self.removed,
            "skipped": self.skipped,
            "already_running": already_running,
            "dry_run": self.ctx.dry_run,
        }
        lines = []
        if already_running:
            lines.append("another prune or reconcile is running; nothing was done")
        verb = "would remove" if self.ctx.dry_run else "removed"
        lines += [f"{verb} {i}" for i in self.removed]
        lines += [f"skipped {s['id']} ({s['reason']})" for s in self.skipped]
        if not self.ctx.dry_run and not already_running:
            self.journal_summary(tried or 0)
        lines += [f"warning: {w}" for w in self.warnings]
        return Result(data, human="\n".join(escape_control(x) for x in lines))

    def journal_summary(self, tried: int) -> None:
        warn = self.journal.log_best_effort(
            "prune",
            actor=self.ctx.actor.value,
            level=audit.level_for(0, refused=bool(self.skipped)),
            decision_ids=self.ctx.decision_ids,
            details={
                "tried": tried,
                "removed": len(self.removed),
                "skipped": {s["id"]: s["reason"] for s in self.skipped},
            },
        )
        if warn:
            self.warnings.append(warn)


def run(ctx: Context) -> Result:
    if not ctx.args.throwaways:
        raise UsageError("prune needs --throwaways")
    return Pruner(ctx).run()
