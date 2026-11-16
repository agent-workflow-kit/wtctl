"""``reconcile`` (docs/interface.md, Reconcile): keep records and the clones' worktree metadata
honest after crashes, moves and manual changes, without ever deleting a user file or a branch.

The only things this module deletes are an owned record's own admin entry (under strict
conditions, audited first) and expired tombstones. Pointers are rewritten by wtctl itself, never
by ``git worktree repair``. Every record is handled under its own per-record lease taken without
waiting (a live creator, remover or bootstrap is skipped), and every state change is a
compare-and-set. A record the run cannot understand is reported and left as it is.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
from collections.abc import Collection, Iterator
from typing import Any

from . import (
    audit,
    baselinesvc,
    fsafe,
    fsafe_child,
    hooks,
    inuse,
    linkcheck,
    reconcile_admin,
    reconcile_probe,
    reconcile_strays,
    removal_delete,
    removal_integrity,
    worktrees,
)
from .context import Context, Result
from .duration import parse_duration
from .errors import WtctlError
from .gitsetup import open_git
from .mirror import Mirror, Tips
from .output import escape_control
from .removal_status import Status
from .state import GLOBAL_LEASE, Record, clone_lease, record_lease

WORKTREE_STATES = ("active", "unlinked", "missing", "clone_missing")
NOT_MISSING = ("active", "unlinked", "clone_missing")
EPOCH_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _epoch(ts: str | None) -> float | None:
    try:
        return dt.datetime.strptime(ts or "", EPOCH_FMT).replace(tzinfo=dt.UTC).timestamp()
    except ValueError:
        return None


class Reconciler:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.state = ctx.state
        self.dry = ctx.dry_run
        self.journal = audit.journal_for(ctx)
        self.changes: list[dict[str, Any]] = []
        self.reports: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.budget = fsafe_child.Budget()
        self._tips: dict[str, tuple[Mirror | None, Tips | None]] = {}
        self.grace = parse_duration(self.config.reconcile.missing_grace, flag="reconcile.missing_grace")

    # -- output ---------------------------------------------------------------------------------

    def report(self, kind: str, **fields: Any) -> None:
        self.reports.append({"kind": kind, **fields})

    def move(
        self, rec: Record, expected: str | Collection[str], new: str, action: str, **updates: Any
    ) -> bool:
        """A compare-and-set state change; recorded as a change only when it happened."""
        if not self.dry:
            try:
                self.state.transition(rec["id"], expected, new, **updates)
            except WtctlError as exc:
                if exc.code not in ("busy", "not_found"):
                    raise
                self.warnings.append(f"{rec['id']}: changed by another command; left alone")
                return False
        self.changes.append({"id": rec["id"], "from": rec["state"], "to": new, "action": action})
        return True

    @contextlib.contextmanager
    def leased(self, rid: str) -> Iterator[bool]:
        """The record's lease without waiting; False when a live process holds it."""
        try:
            lease = self.state.try_lease(record_lease(rid))
        except WtctlError as exc:  # held by this very process
            if exc.code != "busy":
                raise
            lease = None
        if lease is None:
            yield False
            return
        try:
            yield True
        finally:
            lease.release()

    def each(self, records: list[Record], states: Collection[str], handler: Any) -> None:
        for rec in records:
            with self.leased(rec["id"]) as got:
                if not got:
                    continue
                cur = self.state.get_record(rec["id"])
                if cur is None or cur["state"] not in states:
                    continue
                try:
                    handler(cur)
                except audit.JournalIOError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one record must not stop the run
                    self.warnings.append(f"{rec['id']}: {type(exc).__name__}: {exc}")

    # -- the run --------------------------------------------------------------------------------

    def run(self) -> Result:
        if self.dry:
            self.work()
            return self.result(already_running=False)
        try:
            lease = self.state.lease(GLOBAL_LEASE, wait=self.config.lease_wait_s)
        except WtctlError as exc:
            if exc.code != "busy":
                raise
            return self.result(already_running=True)
        with lease:
            self.work()
            self.finish()
        return self.result(already_running=False)

    def work(self) -> None:
        self.git = open_git(self.config, self.ctx.gateway)
        live = self.state.records(states=("creating",))
        self.each(live, ("creating",), self.creating)
        found = self.state.records(object_type="worktree", states=WORKTREE_STATES)
        self.each(found, WORKTREE_STATES, self.worktree)
        vanished = self.state.records(object_type="throwaway", states=("active", "partial"))
        self.each(vanished, ("active", "partial"), self.throwaway)
        self.tombstones()
        self.strays()

    def result(self, *, already_running: bool) -> Result:
        data = {
            "changes": self.changes,
            "reports": self.reports,
            "already_running": already_running,
            "dry_run": self.dry,
        }
        lines = []
        if already_running:
            lines.append("another reconcile or prune is running; nothing was done")
        verb = "would " if self.dry else ""
        lines += [f"{verb}{c['action']} {c['id']} ({c['from']} -> {c['to']})" for c in self.changes]
        for r in self.reports:
            where = r.get("id") or r.get("path") or r.get("clone_path") or ""
            lines.append(f"{r['kind']} {where}")
        text = "\n".join(escape_control(x) for x in lines)
        stderr = "\n".join(escape_control(f"warning: {w}") for w in self.warnings)
        return Result(data, human=text, stderr=stderr)

    def finish(self) -> None:
        """The log record and the ``post-reconcile`` hook (not for a dry run)."""
        warn = self.journal.log_best_effort(
            "reconcile",
            actor=self.ctx.actor.value,
            level=audit.level_for(0, refused=bool(self.reports)),
            decision_ids=self.ctx.decision_ids,
            details={
                "changes": [f"{c['id']}:{c['action']}" for c in self.changes][:200],
                "reports": [r["kind"] for r in self.reports][:200],
            },
        )
        if warn:
            self.warnings.append(warn)
        if not self.changes:
            return
        payload = hooks.build_payload(
            "post-reconcile", object_type="reconcile", object_id=None, repo=None, labels=[],
            changes=self.changes, reports=self.reports,
        )  # fmt: skip
        env = hooks.hook_env(
            self.config, os.environ, hook="post-reconcile", object_id=None, object_type="reconcile"
        )
        specs = hooks.configured_hooks(self.config, "post-reconcile")
        out = hooks.run_post_hooks(
            "post-reconcile", specs, payload, env=env, decision_ids=list(self.ctx.decision_ids)
        )
        self.warnings += out.warnings

    # -- creating records -----------------------------------------------------------------------

    def creating(self, rec: Record) -> None:
        ids, _ = inuse.collect_dir_ids(
            self.state, rec, limit=self.config.remove.nested_scan_limit, budget=self.budget
        )
        use = inuse.check_in_use(self.ctx.gateway, rec["path"], ids)
        if use.status != "pass":
            why = (
                f"{len(use.processes)} process(es) are in the planned path"
                if use.status == "fail"
                else "the process scanner is unavailable"
            )
            self.report("abandoned", id=rec["id"], path=rec["path"], message=why)
            return
        if rec["object_type"] == "throwaway":
            self.creating_throwaway(rec)
        else:
            self.creating_worktree(rec)

    def creating_throwaway(self, rec: Record) -> None:
        """Delete the directory fd-based only when its identity and its root's are recorded and
        still hold; otherwise leave it. Either way the record is ``gone`` (a directory left is
        reported as a stray). No hooks fire."""
        note = "the creator died; the directory was left"
        if rec["dir_id"] and rec["root_id"] and rec["base_dir"]:
            if self.dry:
                note = "the creator died; the directory is deleted"
            else:
                out = removal_delete.delete_throwaway(dict(rec), self.budget)
                note = (
                    "the creator died; the directory was deleted"
                    if out.status == "deleted"
                    else "the creator died; the directory could not be deleted and was left"
                )
        self.move(rec, "creating", "gone", "abandon_throwaway", last_error=note)

    def branch_kept(self, rec: Record) -> str | None:
        """The branch a create made, when it still exists (it is kept)."""
        if not rec["created_branch"] or not rec["branch"] or not rec["common_dir"]:
            return None
        r = self.git.run(
            ["rev-parse", "--verify", "-q", f"refs/heads/{rec['branch']}"],
            git_dir=rec["common_dir"], cwd=rec["common_dir"], timeout=60,
        )  # fmt: skip
        return rec["branch"] if r.returncode == 0 and not r.timed_out else None

    def planted(self, rec: Record) -> tuple[str, str | None]:
        """What is at the planned path: ``absent``, ``unknown`` (cannot tell), ``other`` or
        ``worktree`` with the admin directory its ``.git`` file names. Read with no-follow
        descriptors from the recorded worktree root."""
        base, path = rec["base_dir"], rec["path"]
        ident = self.state.anchor_identity(base) if base else None
        rel = os.path.relpath(path, base) if base else ".."
        if not base or ident is None or rel in (".", "..") or rel.startswith(("../", "/")):
            return "unknown", None
        parts = rel.split(os.sep)
        try:
            with fsafe.open_anchor(base, expected=reconcile_probe._ident(ident)) as anchor:
                try:
                    pfd = fsafe.open_chain(anchor, anchor.fd, parts[:-1], start_path=anchor.path)
                except fsafe.FsafeError as exc:
                    return ("absent", None) if exc.kind in ("missing", "not_dir") else ("unknown", None)
                try:
                    try:
                        anchor.fs.lstat_at(pfd, parts[-1])
                    except FileNotFoundError:
                        return "absent", None
                    try:
                        wfd = fsafe.open_dir(anchor, pfd, parts[-1])
                    except fsafe.FsafeError:
                        return "other", None
                    try:
                        text = linkcheck._read_small(anchor, wfd, ".git")
                    except fsafe.FsafeError:
                        return "other", None
                    finally:
                        os.close(wfd)
                finally:
                    os.close(pfd)
        except fsafe.FsafeError:
            return "unknown", None
        named = linkcheck._pointer_value(text, "gitdir: ")
        return ("worktree", named) if named else ("other", None)

    def creating_worktree(self, rec: Record) -> None:
        kind, admin = self.planted(rec)
        if kind == "absent":
            branch = self.branch_kept(rec)
            if self.move(rec, "creating", "gone", "abandon", last_error="the creator died"):
                extra = {"branch": branch} if branch else {}
                self.report("abandoned", id=rec["id"], path=rec["path"], **extra)
            return
        if kind == "worktree" and admin is not None and self.reclaim(rec, admin):
            return
        self.report("abandoned", id=rec["id"], path=rec["path"])

    def reclaim(self, rec: Record, admin: str) -> bool:
        """Adopt a worktree whose creator died after ``git worktree add``: its ``.git`` file names
        an admin entry directly under the recorded common dir whose ``gitdir`` names the planned
        path. Returns False when it is not that worktree or cannot be recorded in full."""
        if os.path.dirname(admin) != os.path.join(rec["common_dir"], "worktrees"):
            return False
        probe = {**rec, "admin_dir": admin, "dir_id": None}
        try:
            links = linkcheck.verify_links(probe, self.state)
        except WtctlError:
            return False
        if self.dry:
            self.changes.append(
                {"id": rec["id"], "from": "creating", "to": "active", "action": "adopt_after_crash"}
            )
            self.report("adopted_after_crash", id=rec["id"], path=rec["path"])
            return True
        info = worktrees.inspect_existing(self.git, rec["common_dir"], admin)
        self.state.transition(
            rec["id"], "creating", "creating",
            admin_dir=admin, dir_id=links.dir_id, head_at_create=info.head,
            reflog_start_ts=self.state.timestamp(), reflog_start_sha=info.reflog_start_sha,
        )  # fmt: skip
        self.state.set_stash_ids(rec["id"], info.stash_ids)
        self.state.clear_bootstrap_sets(rec["id"], expected="creating")
        current = self.state.get_record(rec["id"])
        assert current is not None
        baseline = baselinesvc.record_current(self.config, current)
        if baseline.has_detail():
            self.warnings.append(f"{rec['id']}: the repo-config baseline could not be recorded")
            return False
        done = self.move(
            current, "creating", "active", "adopt_after_crash",
            baseline_digest=baseline.digest, baseline_items=baselinesvc.item_rows(baseline),
            bootstrap_state="interrupted",
        )  # fmt: skip
        if done:
            self.report("adopted_after_crash", id=rec["id"], path=rec["path"])
        return done

    # -- worktree records -----------------------------------------------------------------------

    def unlinked(self, rec: Record, message: str) -> None:
        if rec["state"] != "unlinked":
            self.move(
                rec, WORKTREE_STATES, "unlinked", "mark_unlinked",
                missing_since=None, missing_seen=None,
            )  # fmt: skip
        self.report("unlinked", id=rec["id"], path=rec["path"], message=message)

    def worktree(self, rec: Record) -> None:
        clone = reconcile_probe.probe_clone(rec)
        if clone == "gone":
            self.clone_missing(rec)
            return
        if clone == "unlinked":
            self.unlinked(rec, "the clone's directory is not the one that was recorded")
            return
        probe = reconcile_probe.probe_entry(self.state, rec, self.budget)
        if probe.status == "unknown":
            self.unlinked(rec, probe.detail or "the worktree directory cannot be examined")
        elif probe.status == "absent":
            self.absent(rec)
        else:
            self.present(rec)

    def clone_missing(self, rec: Record) -> None:
        if rec["state"] != "clone_missing":
            self.move(
                rec, WORKTREE_STATES, "clone_missing", "mark_clone_missing",
                missing_since=None, missing_seen=None,
            )  # fmt: skip
        candidate = reconcile_probe.moved_candidate(
            self.config, self.state, self.git, rec, self.budget
        )
        if candidate is not None:
            self.report("moved_clone", id=rec["id"], clone_path=candidate)
        else:
            self.report("clone_missing", id=rec["id"], clone_path=rec["clone_path"] or "/")

    def present(self, rec: Record) -> None:
        check, _ = removal_integrity.integrity_check(self.state, rec, self.git)
        if check["status"] == "pass":
            if rec["state"] != "active":
                self.move(
                    rec, WORKTREE_STATES, "active", "reactivate",
                    missing_since=None, missing_seen=None,
                )  # fmt: skip
            elif rec["missing_since"] or rec["missing_seen"]:
                self.state.transition(
                    rec["id"], "active", "active", missing_since=None, missing_seen=None
                )
            return
        step = check["details"][0]["step"]
        if not (rec["owned"] and rec["admin_dir"] and step in ("dotgit", "admin_gitdir")):
            self.unlinked(rec, f"the integrity check failed at {step}")
            return
        self.repair(rec, step)

    def repair(self, rec: Record, step: str) -> None:
        try:
            lease = self.state.lease(clone_lease(rec["clone_path"]), wait=5)
        except WtctlError as exc:
            if exc.code != "busy":
                raise
            self.unlinked(rec, "the clone is busy; the pointers were not rewritten")
            return
        with lease:
            why = reconcile_probe.repair_pointers(
                self.state, rec, self.budget, write=not self.dry
            )
            if why is not None:
                self.unlinked(rec, why)
                return
            if not self.dry:
                after, _ = removal_integrity.integrity_check(self.state, rec, self.git)
                if after["status"] != "pass":
                    self.unlinked(rec, "still mismatched after the pointers were rewritten")
                    return
        self.move(
            rec, WORKTREE_STATES, "active", "repair", missing_since=None, missing_seen=None
        )

    # -- missing --------------------------------------------------------------------------------

    def absent(self, rec: Record) -> None:
        now = self.ctx.gateway.clock.now()
        if rec["state"] != "missing" or not rec["missing_since"]:
            stamp = now.strftime(EPOCH_FMT)[:-4] + "Z"
            self.move(
                rec, WORKTREE_STATES, "missing", "mark_missing",
                missing_since=stamp, missing_seen="1",
            )  # fmt: skip
            return
        seen = int(rec["missing_seen"]) + 1 if (rec["missing_seen"] or "").isdigit() else 2
        if not self.dry:
            self.state.transition(rec["id"], "missing", "missing", missing_seen=str(seen))
        since = _epoch(rec["missing_since"])
        if seen < 2 or since is None or now.timestamp() - since < self.grace:
            return
        if not rec["owned"]:
            if self.move(rec, "missing", "gone", "mark_gone"):
                self.report("adopted_gone", id=rec["id"], path=rec["path"])
            return
        self.settle_owned(rec)

    def tips_for(self, rec: Record) -> tuple[Mirror | None, Tips | None]:
        """The clone's mirror and its refreshed tips (once per clone and run); a failed refresh
        is reported ``fetch_failed`` and nothing counts as pushed."""
        key = rec["clone_path"]
        if key not in self._tips:
            st = Status(self.ctx, dict(rec))
            st.git = self.git
            st.warnings = self.warnings
            mirror, tips = st.fetch()
            if tips is None:
                bad = st.checks.get("fetch", {}).get("details") or [{}]
                msg = bad[0].get("message") or "the mirror could not be refreshed"
                self.report("fetch_failed", clone_path=key, message=msg)
            self._tips[key] = (mirror, tips)
        return self._tips[key]

    def branch_tip(self, rec: Record) -> str | None:
        if not rec["branch"]:
            return None
        r = self.git.run(
            ["rev-parse", "--verify", "-q", f"refs/heads/{rec['branch']}"],
            git_dir=rec["common_dir"], cwd=rec["common_dir"], timeout=60,
        )  # fmt: skip
        out = r.out.strip()
        return out if r.returncode == 0 and len(out) == 40 else None

    def settle_owned(self, rec: Record) -> None:
        info = reconcile_admin.inspect(dict(rec), self.budget)
        if isinstance(info, fsafe.FsafeError):
            self.report(
                "unlinked", id=rec["id"], path=rec["path"], message=f"{info.kind}: {info.message}"
            )
            return
        if info["state"] == "absent":
            self.entry_absent(rec)
            return
        verdict = reconcile_admin.decide(
            dict(rec), info, git=self.git, mirror_tips=lambda: self.tips_for(rec),
            config=self.config, state=self.state,
        )  # fmt: skip
        if verdict.action == "keep":
            self.warnings.append(f"{rec['id']}: the admin entry was kept: {verdict.message}")
        elif verdict.action == "kept_unpushed":
            self.report(
                "kept_unpushed", id=rec["id"], tip=verdict.tip or "0" * 40, message=verdict.message
            )
        else:
            self.delete_entry(rec, verdict)

    def entry_absent(self, rec: Record) -> None:
        other = reconcile_admin.another_entry_names(dict(rec), self.budget)
        if other is not False:
            self.report(
                "unlinked", id=rec["id"], path=rec["path"],
                message="another admin entry of the clone names this path",
            )  # fmt: skip
            return
        branch = self.branch_kept(rec)
        if self.move(rec, "missing", "gone", "mark_gone"):
            extra = {"branch": branch} if branch else {}
            self.report("gone", id=rec["id"], path=rec["path"], admin_entry="absent", **extra)

    def delete_entry(self, rec: Record, verdict: reconcile_admin.Verdict) -> None:
        tip = self.branch_tip(rec)
        if self.dry:
            self.changes.append(
                {"id": rec["id"], "from": "missing", "to": "gone", "action": "admin_entry_delete"}
            )
            return
        inputs = {
            "admin_entry": rec["admin_dir"], "head": verdict.head,
            "branch": rec["branch"], "tip": tip,
        }  # fmt: skip
        common: dict[str, Any] = {
            "actor": self.ctx.actor.value,
            "decision_ids": self.ctx.decision_ids,
            "target_id": rec["id"],
            "target_path": rec["path"],
        }
        self.journal.check_writable()
        ok = self.journal.prepare_audit("admin_entry_delete", inputs=inputs, result="ok", **common)
        bad = self.journal.prepare_audit(
            "admin_entry_delete", inputs=inputs, result="error", **common
        )
        with self.state.lease(clone_lease(rec["clone_path"]), wait=5):
            outcome, remaining = removal_delete.delete_admin(self.state, dict(rec), self.budget)
        if outcome not in ("deleted", "absent"):
            with contextlib.suppress(audit.JournalIOError):
                self.journal.commit_audit(bad)
            self.warnings.append(f"{rec['id']}: the admin entry was not deleted ({outcome})")
            return
        try:
            self.journal.commit_audit(ok)
        except audit.JournalIOError as exc:
            self.warnings.append(f"{rec['id']}: deleted; audit record not written: {exc.message}")
        if self.move(rec, "missing", "gone", "admin_entry_delete"):
            self.unpushed_branch(rec, tip)

    def unpushed_branch(self, rec: Record, tip: str | None) -> None:
        """The record is gone; a branch it created is kept, and reported when its tip is not
        pushed."""
        if not rec["created_branch"] or tip is None:
            return
        mirror, tips = self.tips_for(rec)
        if mirror is not None and tips is not None and mirror.is_pushed(tip, tips).pushed:
            return
        self.report("gone_with_unpushed", id=rec["id"], branch=rec["branch"], tip=tip)

    # -- throwaways -----------------------------------------------------------------------------

    def throwaway(self, rec: Record) -> None:
        probe = reconcile_probe.probe_entry(self.state, rec, self.budget)
        if probe.status == "absent":
            self.move(
                rec, ("active", "partial"), "gone", "mark_gone",
                last_error="the directory is gone",
            )  # fmt: skip

    # -- tombstones and strays ------------------------------------------------------------------

    def tombstones(self) -> None:
        days = int(self.config.tombstone_days)
        cutoff = self.state.timestamp_before(days * 86400)
        for rec in self.state.expired_tombstones(cutoff):
            if self.dry or self.state.delete_tombstone(rec["id"], cutoff=cutoff):
                self.changes.append(
                    {"id": rec["id"], "from": rec["state"], "to": None, "action": "tombstone_delete"}
                )

    def strays(self) -> None:
        live = self.state.records(object_type="throwaway", states=("creating", "active", "partial"))
        self.reports += reconcile_strays.throwaway_strays(
            self.config.effective_throwaway_root, {r["id"] for r in live}
        )
        clones = [
            c for c in (self.config.repo(r).clone for r in self.config.repos) if c is not None
        ]
        self.reports += reconcile_strays.clone_tmp_strays(
            self.config.effective_clone_root, clones
        )
        self.reports += reconcile_strays.mirror_strays(self.state)

