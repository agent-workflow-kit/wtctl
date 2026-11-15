"""What ``remove`` would do: Remove order steps 1 to 6, changing nothing (docs/interface.md,
``status``). ``build`` is shared by ``status`` and ``remove --dry-run``.

Unlike ``remove``, it does not stop at the first refusal: every check that can run does, and the
``reason`` is the first by precedence. ``would_remove`` is ``true`` only when every check ran and
passed; a check that could not run (the mirror refresh failed or timed out, the process scanner is
unavailable) leaves it ``null`` or ``false``, never ``true``.

TODO(#5): log the check outcomes when the log lands.
``wtctl.remover`` reuses these checks for steps 2 to 6 and again for the step-9 re-check.
"""

from __future__ import annotations

import os
import re
from typing import Any

from . import (
    baselinesvc,
    fsafe,
    fsafe_child,
    inuse,
    linkcheck,
    removal_content,
    removal_integrity,
    repobaseline,
)
from .context import Context
from .errors import WtctlError
from .gitsetup import open_git, with_filters
from .mirror import Mirror, Tips

FETCH_LEASE_WAIT = 5  # seconds; status never waits for lease_wait

REASONS = (
    "main_worktree",
    "unlinked",
    "locked",
    "repo_config_changed",
    "in_use",
    "held",
    "dirty",
    "unpushed",
)
FORCEABLE = (
    "held",
    "tracked",
    "untracked",
    "ignored",
    "nested_repos",
    "stash",
    "unpushed_head",
    "unpushed_branches",
    "fetch",
)
DIRTY = ("tracked", "untracked", "ignored", "nested_repos", "stash")
UNPUSHED = ("unpushed_head", "unpushed_branches")
ORDER = (
    "integrity",
    "locked",
    "baseline",
    "in_use",
    "held",
    "fetch",
    "tracked",
    "untracked",
    "ignored",
    "nested_repos",
    "stash",
    "unpushed_head",
    "unpushed_branches",
)
SHA = re.compile(r"[0-9a-f]{40}")
# The integrity steps whose failure is what a half-deleted worktree looks like (its pointers or
# its admin entry are gone). A failure of the git step (``git_common_dir``) is a git answer that
# disagrees or did not come: the pointers are intact, so the normal checks must decide.
PARTIAL_FALLBACK_STEPS = ("path", "root", "dotgit", "admin_gitdir", "commondir")
NOT_ACTIVE = ("creating", "unlinked", "missing", "clone_missing")


def _escape(text: str) -> str:
    return inuse.escape_path(text)


class Status:
    def __init__(self, ctx: Context, record: dict[str, Any]) -> None:
        self.ctx = ctx
        self.record = record
        self.state = ctx.state
        self.config = ctx.config
        self.checks: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self.budget = fsafe_child.Budget()
        self.head: str | None = None
        self.current_branch: str | None = None
        self.by_design_skipped = False  # content checks not run on purpose (a partial record)
        self.directory_gone = False  # a partial record whose remaining path no longer exists
        self.baseline_current: baselinesvc.Baseline | None = None

    # -- the checks ---------------------------------------------------------------------------

    def in_use(self) -> None:
        r = self.record
        limit = self.config.remove.nested_scan_limit
        ids, warning = inuse.collect_dir_ids(self.state, r, limit=limit, budget=self.budget)
        if warning:
            self.warnings.append(warning)
        result = inuse.check_in_use(self.ctx.gateway, r["path"], ids)
        self.warnings += result.warnings
        self.checks["in_use"] = result.check()

    def held(self, holder: str | None) -> None:
        others = [h for h in self.record["holders"] if h != holder]
        self.checks["held"] = {
            "status": "fail" if others else "pass",
            "details": [{"holder": h} for h in others],
        }

    def baseline(self) -> None:
        r = self.record
        items = baselinesvc.recorded_items(self.state, r["id"])
        spec = baselinesvc.spec_for(
            self.config,
            r,
            common_id=r["common_id"],
            dir_id=r["dir_id"],
            hooks_id=baselinesvc.hooks_identity(items),
            hooks_path=baselinesvc.hooks_recorded_path(items),
        )
        current = baselinesvc.compute(spec)
        self.baseline_current = current
        rows = repobaseline.compare(items, current.items)
        git_rows = [x for x in rows if x["class"] == "git"]
        if git_rows or (not rows and baselinesvc.is_changed(r, current)):
            if not rows:  # only the recorded digest disagrees: nothing to point at but the clone
                rows = [
                    {
                        "class": "git",
                        "path": r["common_dir"],
                        "key": None,
                        "before": "absent",
                        "after": "absent",
                    }
                ]
            self.checks["baseline"] = {"status": "fail", "details": rows}
        elif rows:
            self.warnings.append("claude-class repo-config items changed since the baseline")
            self.checks["baseline"] = {"status": "warn", "details": rows}
        else:
            self.checks["baseline"] = {"status": "pass", "details": []}

    def mirror_for(self) -> Mirror:
        r = self.record
        url = r.get("fetch_url")
        if not url:
            clone = self.state.get_clone(r["repo"])
            url = clone["fetch_url"] if clone else None
        if not url:
            raise WtctlError("fetch_failed", f"no fetch URL is recorded for {r['repo']}")
        return Mirror(
            git=self.git,
            cfg=self.config,
            state=self.state,
            gateway=self.ctx.gateway,
            repo=r["repo"],
            clone_path=r["clone_path"],
            common_dir=r["common_dir"],
            fetch_url=url,
            environ=os.environ,
            warn=self.warnings.append,
        )

    def fetch(self) -> tuple[Mirror | None, Tips | None]:
        """Refresh the mirror, waiting at most 5 s for the per-clone lease (never ``busy``)."""
        try:
            mirror = self.mirror_for()
            lease = self.state.lease(mirror.lease_name, wait=FETCH_LEASE_WAIT)
        except WtctlError as exc:
            if exc.code == "busy":
                self.warnings.append(
                    "the mirror refresh was skipped: the per-clone lease is held elsewhere"
                )
                self.checks["fetch"] = {"status": "skipped", "details": []}
                return None, None
            return None, self._fetch_failed(exc)
        try:
            with lease:
                return mirror, mirror.refresh(lease).tips
        except WtctlError as exc:
            return mirror, self._fetch_failed(exc)

    def _fetch_failed(self, exc: WtctlError) -> Tips | None:
        code = "auth_required" if exc.code == "auth_required" else "fetch_failed"
        details = exc.fields.get("details") or {}
        cause = (
            details.get("cause")
            if details.get("cause") in ("timeout", "prune_incomplete")
            else None
        )
        self.checks["fetch"] = {
            "status": "fail",
            "details": [{"code": code, "cause": cause, "message": _escape(exc.message)}],
        }
        self.warnings.append(
            "the mirror could not be refreshed; nothing counts as pushed and the checks that "
            "need the remote's tips were skipped"
        )
        return None

    # -- the flow -----------------------------------------------------------------------------

    def worktree(self, merged_head: str | None, holder: str | None) -> None:
        r, ctx = self.record, self.ctx
        self.git = open_git(self.config, ctx.gateway)
        if r["state"] in NOT_ACTIVE or r["dir_id"] is None:
            self.checks["integrity"] = {
                "status": "fail",
                "details": [{"step": "path", "expected": r["path"], "actual": None}],
            }
            self.skip_git_checks()
            self.in_use()
            self.held(holder)
            return
        check, links = removal_integrity.integrity_check(self.state, r, self.git)
        self.checks["integrity"] = check
        if links is None:
            self.skip_git_checks()
            if r["state"] == "partial" and check["details"][0]["step"] in PARTIAL_FALLBACK_STEPS:
                self.partial_worktree_unlinked()
            self.in_use()
            self.held(holder)
            return
        self.checks["locked"] = removal_integrity.lock_check(r)
        self.baseline()
        self.in_use()
        self.held(holder)
        self.read_head()
        if r["state"] == "partial":
            self.warnings.append(
                f"{_escape(r['path'])} is partial: a previous removal left it behind"
                + (f" ({_escape(r['last_error'])})" if r["last_error"] else "")
            )
        settings = self.config.repo(r["repo"])
        git = with_filters(self.git, settings.allow_filters, ctx.gateway, self.warnings.append)
        mirror, tips = self.fetch()
        if "fetch" not in self.checks:
            self.checks["fetch"] = {"status": "pass", "details": []}
        inp = removal_content.Inputs(
            git=git,
            state=self.state,
            record=r,
            config=self.config,
            disposable=settings.disposable,
            mirror=mirror,
            tips=tips,
            merged_head=merged_head,
            budget=self.budget,
            warnings=self.warnings,
        )
        try:
            self.checks.update(removal_content.run_all(inp))
        except fsafe.FsafeError as exc:
            # the directory changed identity under the scan: integrity no longer holds
            self.checks["integrity"] = {
                "status": "fail",
                "details": [
                    {
                        "step": linkcheck.exc_step(exc, r),
                        "expected": r["path"],
                        "actual": None,
                    }
                ],
            }
            self.checks.update(removal_content.not_run())
            self.warnings.append(
                f"{_escape(r['path'])} changed while it was being checked ({exc.kind})"
            )

    def skip_git_checks(self) -> None:
        self.checks.setdefault("locked", removal_content.skipped())
        self.checks["baseline"] = removal_content.skipped()
        self.checks["fetch"] = removal_content.skipped()
        self.checks.update(removal_content.not_run())

    def read_head(self) -> None:
        """HEAD and the current branch of a worktree whose integrity passed (untrusted text)."""
        r = self.record
        try:
            self.head = removal_content.head_sha(
                removal_content.Inputs(
                    git=self.git, state=self.state, record=r, config=self.config, disposable=[]
                )
            )
        except WtctlError:
            self.head = None
        self.current_branch = self._branch_name()

    def _branch_name(self) -> str | None:
        r = self.record
        common = r["common_dir"]
        admin = r["admin_dir"] or common
        rel = os.path.relpath(admin, common)
        try:
            with fsafe.open_anchor(common, expected=removal_content._ident(r["common_id"])) as a:
                fd = fsafe.open_chain(
                    a, a.fd, [] if rel == "." else rel.split(os.sep), start_path=a.path
                )
                try:
                    ffd = fsafe.open_file(a, fd, "HEAD")
                finally:
                    os.close(fd)
                try:
                    text = os.read(ffd, 4096).decode("utf-8", "replace")
                finally:
                    os.close(ffd)
        except (fsafe.FsafeError, OSError):
            return None
        m = re.fullmatch(r"ref: refs/heads/([^\n]+)\n?", text)
        return _escape(m.group(1)) if m else None

    def throwaway(self) -> None:
        r = self.record
        if r["state"] in NOT_ACTIVE or r["dir_id"] is None:
            self.in_use()
            return
        self.in_use()

    def partial_worktree_unlinked(self) -> None:
        """A ``partial`` worktree whose pointers are gone: only the recorded path is verified,
        content checks do not run (Partial records). The lock is still reported: nothing
        overrides it."""
        r = self.record
        try:
            self.directory_gone = self._open_recorded_directory()
        except fsafe.FsafeError:
            return  # a missing anchor or chain component, or any mismatch: not the recorded path
        self.checks["integrity"] = {"status": "pass", "details": []}
        self.checks["locked"] = removal_integrity.lock_check(r)
        self.by_design_skipped = True
        self.warnings.append(
            f"{_escape(r['path'])} is partial and its git pointers are gone; content checks "
            "were skipped and remove only checks that nothing uses it"
        )
        if self.directory_gone:
            self.warnings.append(f"{_escape(r['path'])} no longer exists")

    def _open_recorded_directory(self) -> bool:
        """Open the remaining path as Partial records require: an owned record component by
        component below its recorded ``base_dir`` (whose identity is the recorded one, every
        component on its device and mount), an adopted one below its parent; the opened directory
        must have the recorded ``dir_id``. Returns True when the final component does not exist
        under an anchor and chain that opened (the tree was deleted); a missing anchor or chain
        component raises, as every other mismatch does."""
        r = self.record
        expected = removal_content._ident(r["dir_id"])
        base = r["base_dir"]
        if r["owned"]:
            if not base:
                raise fsafe.FsafeError("identity", f"no root is recorded for {r['path']}")
            rel = os.path.relpath(r["path"], base)
            if rel in (".", "..") or rel.startswith("../") or os.path.isabs(rel):
                raise fsafe.FsafeError("identity", f"{r['path']} is not below its root {base}")
            recorded = removal_content._ident(self.state.anchor_identity(base))
            if recorded is None:
                raise fsafe.FsafeError("identity", f"no identity is recorded for the root {base}")
            anchor_path, parts = base, rel.split(os.sep)
            anchor = fsafe.open_anchor(anchor_path, expected=recorded)
        else:
            anchor_path, name = os.path.split(r["path"])
            parts = [name]
            anchor = fsafe.open_anchor(anchor_path, precheck=False)
        with anchor:
            parent = fsafe.open_chain(anchor, anchor.fd, parts[:-1], start_path=anchor.path)
            try:
                try:
                    fd = fsafe.open_chain(
                        anchor,
                        parent,
                        parts[-1:],
                        expected=expected,
                        start_path=os.path.join(anchor.path, *parts[:-1]),
                    )
                except fsafe.FsafeError as exc:
                    if exc.kind == "missing":
                        return True
                    raise
                os.close(fd)
                return False
            finally:
                os.close(parent)

    # -- the verdict --------------------------------------------------------------------------

    def verdict(self) -> tuple[bool | None, str | None]:
        c = self.checks
        r = self.record

        def failed(name: str) -> bool:
            return c.get(name, {}).get("status") == "fail"

        found: set[str] = set()
        if r["object_type"] == "worktree" and r["admin_dir"] is None and r["state"] != "creating":
            found.add("main_worktree")
        if r["state"] in NOT_ACTIVE or failed("integrity"):
            found.add("unlinked")
        if failed("locked"):
            found.add("locked")
        if failed("baseline"):
            found.add("repo_config_changed")
        if failed("in_use"):
            found.add("in_use")
        if failed("held"):
            found.add("held")
        if any(failed(n) for n in DIRTY):
            found.add("dirty")
        if any(failed(n) for n in UNPUSHED):
            found.add("unpushed")
        for reason in REASONS:
            if reason in found:
                return False, reason
        # A partial record whose pointers are gone runs no git on purpose, so the checks that
        # need git are excused; the in-use check is not: no answer is never "nothing uses it".
        unknown = any(
            v["status"] in ("skipped", "unavailable")
            for name, v in c.items()
            if not (self.by_design_skipped and name != "in_use")
        )
        if unknown or failed("fetch"):
            return None, None
        return True, None

    def result(self) -> dict[str, Any]:
        r = self.record
        would, reason = self.verdict()
        ordered = {k: self.checks[k] for k in ORDER if k in self.checks}
        return {
            "id": r["id"],
            "object_type": r["object_type"],
            "would_remove": would,
            "reason": reason,
            "human_required": r["object_type"] == "worktree"
            and (r["owned"] is False or r["state"] == "partial"),
            "checks": ordered,
            "force_overrides": [
                n for n in FORCEABLE if self.checks.get(n, {}).get("status") == "fail"
            ],
            "head": self.head,
            "current_branch": self.current_branch,
            "warnings": self.warnings,
        }


def build(
    ctx: Context, record: dict[str, Any], *, merged_head: str | None, holder: str | None
) -> dict[str, Any]:
    """The ``status`` object of ``record`` (docs/interface.md, ``status``)."""
    st = Status(ctx, record)
    if record["object_type"] == "throwaway":
        st.throwaway()
    else:
        st.worktree(merged_head, holder)
    return st.result()


__all__ = ["Status", "build"]
