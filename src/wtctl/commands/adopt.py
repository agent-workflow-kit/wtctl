"""``wtctl adopt <path>``: record a worktree wtctl did not create (docs/interface.md, adopt).

The directory is untrusted. Everything is read through no-follow descriptors anchored at the
directory itself: its ``.git`` pointer, the admin entry it names and that entry's back-pointers
must agree with the clone that clone resolution found for the repo (a directory that merely looks
like a worktree, or one of another clone, is refused). The baseline is recorded with the record,
in one transaction; git runs only through the hardened runner.

TODO(#5): audit ``adopt`` (path, digest, holder) and log it.
TODO(#23): reconcile settles ``missing`` and ``unlinked`` adopted records (``adopted_gone``).
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from typing import Any

from .. import baselinesvc, fsafe, linkcheck, naming, urls, worktrees
from .. import labels as labelmod
from ..context import Context, Result
from ..errors import UsageError, WtctlError
from ..gitsetup import open_git
from ..hardgit import Git, UnsafePathError, refuse_newline_path
from ..locate import Resolution, Resolver, describe_mount, is_fuse
from ..state import (
    LIVE_STATES,
    Conflict,
    Record,
    State,
    check_holder,
    clone_lease,
    record_lease,
    state_path,
)
from .create import FIELDS, record_object

MAX_POINTER = 4096
GITDIR_PREFIX = "gitdir: "
# git names an admin entry after the worktree's basename, keeping most characters (`!`, a leading
# `-` or `.` occur; create's own names never have those), so any plain component is accepted.
ADMIN_NAME = re.compile(r"[^\x00-\x1f\x7f/]{1,255}\Z")


class _Existing(Exception):
    """The path is recorded: act on that record (outside the per-clone lease)."""

    def __init__(self, record: Record) -> None:
        super().__init__(record["id"])
        self.record = record


@dataclass(frozen=True)
class Layout:
    common: str  # the common dir as the worktree names it (not yet checked against the clone)
    admin: str | None  # None: the main worktree
    dir_id: str


def _within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _unlinked(message: str) -> WtctlError:
    return WtctlError("refused", message, reason="unlinked")


def _shown(path: str) -> str:
    return fsafe.escape_for_details(path)


class Adopter:
    def __init__(self, ctx: Context) -> None:
        a = ctx.args
        self.ctx = ctx
        self.config = ctx.config
        self.state: State = ctx.state
        self.dry = ctx.dry_run
        self.holder: str | None = a.holder
        self.warnings: list[str] = []
        # usage (exit 2)
        if "\0" in a.path or not a.path:
            raise UsageError("the path is empty or contains a NUL byte")
        if a.name is not None and not naming.NAME_RE.fullmatch(a.name):
            raise UsageError(
                f"invalid name {a.name!r}: expected ^[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}$"
            )
        if a.holder is not None:
            check_holder(a.holder)
        if a.repo is not None:
            urls.validate_repo_id(a.repo)
        self.labels = list(a.label)
        labelmod.merge_labels((), self.labels)
        self.name: str | None = a.name
        self.repo: str | None = a.repo
        try:
            self.path = refuse_newline_path(os.path.realpath(a.path))
        except UnsafePathError:
            raise UsageError("a path containing a newline cannot be adopted") from None
        for private in (str(state_path().parent), self.config.effective_throwaway_root):
            if _within(self.path, os.path.realpath(private)):
                raise UsageError(f"{_shown(self.path)} lies inside {_shown(private)}")

    # -- entry ---------------------------------------------------------------------------------

    def run(self) -> Result:
        try:
            st = os.lstat(self.path)
        except OSError:
            raise WtctlError(
                "not_found", f"{_shown(self.path)} does not exist (not a git worktree)"
            ) from None
        if not stat.S_ISDIR(st.st_mode):
            raise WtctlError("not_found", f"{_shown(self.path)} is not a git worktree")
        self.git: Git = open_git(self.config, self.ctx.gateway)  # git_too_old (exit 12)
        rec = self._recorded()
        if rec is not None:
            return self._existing(rec)
        layout = self._layout()
        repo = self.repo or self._derive_repo(layout.common)
        resolver = Resolver(self.config, self.state, self.git, readonly=self.dry)
        res = resolver.locate(repo, auto_clone=False)
        res.require_sha1("adopt")
        res.require_anchor()
        self.warnings += res.warnings
        if os.path.realpath(res.common_dir) != os.path.realpath(layout.common):
            raise WtctlError(
                "remote_mismatch",
                f"{_shown(self.path)} belongs to {_shown(layout.common)}, not to the clone of "
                f"{repo} that wtctl resolved ({_shown(res.common_dir)})",
            )
        if self.dry:
            return self._fresh(resolver, repo, res, layout)
        try:
            with self.state.lease(clone_lease(res.clone_path), wait=self.config.lease_wait_s):
                return self._fresh(resolver, repo, res, layout)
        except _Existing as e:
            return self._existing(e.record)

    # -- an already recorded path ----------------------------------------------------------------

    def _recorded(self) -> Record | None:
        for r in self.state.records():
            if r["path"] == self.path:
                if r["object_type"] != "worktree":
                    raise UsageError(f"{self.path} is a throwaway; adopt is for worktrees")
                return r
        return None

    def _existing(self, rec: Record) -> Result:
        """The per-record lease, then the table in Commands by record state."""
        if self.dry:
            return self._existing_decide(rec)
        with self.state.lease(record_lease(rec["id"]), wait=self.config.lease_wait_s):
            current = self.state.get_record(rec["id"])
            if current is None:
                raise WtctlError("not_found", f"no record {rec['id']}")
            if current["state"] not in LIVE_STATES:
                raise WtctlError(
                    "not_found",
                    f"record {rec['id']} was already {current['state']}",
                    details={"state": current["state"], "removed_at": current["removed_at"]},
                )
            return self._existing_decide(current)

    def _existing_decide(self, rec: Record) -> Result:
        state = rec["state"]
        if state == "creating":
            raise WtctlError(
                "refused",
                f"{_shown(self.path)} is the path of the record {rec['id']}, still being created",
                reason="path_exists",
                holder_id=rec["id"],
            )
        if state == "missing":
            raise WtctlError("not_found", f"{_shown(self.path)} is not a git worktree")
        if state != "active":
            raise _unlinked(f"{rec['id']} is {state}, not active")
        if self.holder is not None:
            if self.dry:
                if self.holder not in rec["holders"]:
                    rec = {**rec, "holders": sorted([*rec["holders"], self.holder])}
            else:
                rec = self.state.add_holder(rec["id"], self.holder, expected="active")
        return self._result(rec, existing=True, human=f"{rec['id']} is already recorded")

    # -- reading the directory -----------------------------------------------------------------

    def _layout(self) -> Layout:
        try:
            with fsafe.open_anchor(self.path) as anchor:
                dir_id = f"{anchor.identity.dev}:{anchor.identity.ino}"
                try:
                    st = os.stat(".git", dir_fd=anchor.fd, follow_symlinks=False)
                except FileNotFoundError:
                    raise self._no_dotgit() from None
                if stat.S_ISDIR(st.st_mode):
                    return Layout(os.path.join(self.path, ".git"), None, dir_id)
                if not stat.S_ISREG(st.st_mode):
                    raise _unlinked(f"{_shown(self.path)}/.git is not a file or a directory")
                fd = fsafe.open_file(anchor, anchor.fd, ".git")
                try:
                    data = os.read(fd, MAX_POINTER + 1)
                finally:
                    os.close(fd)
        except fsafe.FsafeError as exc:
            raise _unlinked(f"{_shown(self.path)} cannot be adopted ({exc.kind})") from exc
        return self._parse_pointer(data, dir_id)

    def _no_dotgit(self) -> WtctlError:
        parent = os.path.dirname(self.path)
        while parent != os.path.dirname(parent):
            if os.path.lexists(os.path.join(parent, ".git")):
                return UsageError(
                    f"{_shown(self.path)} is inside a worktree, not its top level; "
                    f"adopt {_shown(parent)}"
                )
            parent = os.path.dirname(parent)
        return WtctlError("not_found", f"{_shown(self.path)} is not a git worktree")

    def _parse_pointer(self, data: bytes, dir_id: str) -> Layout:
        text = data.decode("utf-8", "surrogateescape")
        if len(data) > MAX_POINTER or not text.startswith(GITDIR_PREFIX):
            raise WtctlError("not_found", f"{_shown(self.path)} is not a git worktree")
        value = text[len(GITDIR_PREFIX) :]
        value = value[:-1] if value.endswith("\n") else value
        if "\n" in value or "\0" in value:
            raise _unlinked(f"{_shown(self.path)}/.git is not a single gitdir line")
        if "/modules/" in value and "/worktrees/" not in value:
            raise UsageError(
                f"{_shown(self.path)} is a submodule checkout; adopt the superproject's "
                "worktree instead"
            )
        admin = value
        if not os.path.isabs(admin) or os.path.normpath(admin) != admin:
            raise _unlinked(f"{_shown(self.path)}/.git does not name an absolute, normalized path")
        parent, name = os.path.split(admin)
        if (
            os.path.basename(parent) != "worktrees"
            or not ADMIN_NAME.fullmatch(name)
            or name in (".", "..")
        ):
            raise _unlinked(f"{_shown(self.path)}/.git does not name an admin entry of a clone")
        return Layout(os.path.dirname(parent), admin, dir_id)

    def _derive_repo(self, common: str) -> str:
        """``--repo`` default: the repo id the URL of the clone's ``origin`` remote normalizes to
        (a configured ``clone_url`` that matches also identifies one)."""
        r = self.git.run(
            ["config", "--no-includes", "-z", "--get-all", "remote.origin.url"],
            git_dir=common,
            timeout=60,
        )
        if r.returncode == 1:
            raise WtctlError(
                "remote_mismatch",
                f"the clone has no remote 'origin'; pass --repo ({_shown(common)})",
                details={"cause": "no_remote"},
            )
        values = [v for v in r.stdout.split(b"\0") if v]
        if r.returncode != 0 or len(values) != 1:
            raise WtctlError(
                "remote_mismatch", "the clone's origin remote is unusable; pass --repo"
            )
        try:
            raw = values[0].decode("utf-8")
        except UnicodeDecodeError:
            raise WtctlError(
                "remote_mismatch", "the origin URL is not UTF-8; pass --repo"
            ) from None
        stripped = urls.strip_userinfo(raw)
        norm = urls.normalize_url(stripped)
        if norm is not None:
            try:
                return urls.validate_repo_id(norm)
            except UsageError:
                pass
        found = [
            rid
            for rid in self.config.repos
            if urls.matches_repo(stripped, rid, clone_url=self.config.repo(rid).clone_url)
        ]
        if len(found) != 1:
            raise WtctlError(
                "remote_mismatch", "cannot tell which repo the origin remote is; pass --repo"
            )
        return str(found[0])

    # -- under the per-clone lease -----------------------------------------------------------------

    def _fresh(self, resolver: Resolver, repo: str, res: Resolution, layout: Layout) -> Result:
        state = self.state
        again, _ = resolver.find(repo)
        if again is None or (again.common_dir, again.fetch_url) != (res.common_dir, res.fetch_url):
            raise WtctlError("remote_mismatch", f"the clone of {repo} changed while adopt ran")
        again.require_anchor()
        live = state.records()
        for r in live:
            if r["path"] == self.path:
                if r["object_type"] != "worktree":
                    raise UsageError(f"{self.path} is a throwaway; adopt is for worktrees")
                raise _Existing(r)
        self._check_nesting(live, res)
        if is_fuse(describe_mount(self.path, os.stat(self.path))["fs_type"]):
            raise _unlinked(f"{_shown(self.path)} is on a FUSE file system")
        common_id = res.recorded_id or res.common_id
        pseudo: Record = {
            "id": "(new)", "path": self.path, "common_dir": res.common_dir,
            "admin_dir": None if layout.admin is None else
            os.path.join(res.common_dir, "worktrees", os.path.basename(layout.admin)),
            "owned": False, "base_dir": None, "common_id": common_id, "dir_id": layout.dir_id,
        }  # fmt: skip
        linkcheck.verify_links(pseudo, state)
        info = worktrees.inspect_existing(self.git, res.common_dir, pseudo["admin_dir"])
        name = self._pick_name(live, repo)
        settings = self.config.repo(repo)
        rid = state.preview_id() if self.dry else state.allocate_id()
        pseudo["id"] = rid
        baseline = baselinesvc.record_current(self.config, pseudo)
        if baseline.has_detail():
            bad = [i.path for i in baseline.items if i.detail][:5]
            raise WtctlError(
                "create_failed",
                "the repo-config baseline could not be computed in full "
                f"(see remove.nested_scan_limit): {', '.join(bad)}",
            )
        fields: dict[str, Any] = {
            "id": rid,
            "object_type": "worktree",
            "owned": False,
            "state": "active",
            "repo": repo,
            "clone_path": res.clone_path,
            "common_dir": res.common_dir,
            "common_id": common_id,
            "admin_dir": pseudo["admin_dir"],
            "dir_id": layout.dir_id,
            "path": self.path,
            "name": name,
            "branch": info.branch,
            "created_branch": False,
            "head_at_create": info.head,
            "fetch_url": res.fetch_url,
            "remote": settings.remote,
            "reflog_start_ts": state.timestamp(),
            "reflog_start_sha": info.reflog_start_sha,
            "baseline_digest": baseline.digest,
        }
        if self.dry:
            return self._result(
                self._projected(fields), existing=False, human=f"would adopt {name}"
            )
        try:
            record = state.insert_record(
                fields,
                labels=self.labels,
                holders=[self.holder] if self.holder else [],
                baseline_items=baselinesvc.item_rows(baseline),
                stash_ids=info.stash_ids,
            )
        except Conflict as exc:  # a name or path taken since the checks above
            raise WtctlError(
                "refused", f"{_shown(self.path)} was taken meanwhile ({exc.column})",
                reason="path_exists",
            ) from None  # fmt: skip
        return self._result(record, existing=False, human=f"adopted {name}")

    def _check_nesting(self, live: list[Record], res: Resolution) -> None:
        """No adopted worktree lies inside, or around, another worktree: the clone's own
        (``git worktree list``, cross-checked against the admin entries) or a recorded one."""
        known: dict[str, bool] = {r["path"]: True for r in live}
        mine: worktrees.WorktreeEntry | None = None
        for e in worktrees.worktree_entries(self.git, res.common_dir):
            real = os.path.realpath(e.path)
            if real == self.path:
                mine = e
            else:
                known[real] = True
        if mine is None:
            raise _unlinked(f"git does not list {_shown(self.path)} as a worktree of the clone")
        if mine.locked is not None:
            raise WtctlError(
                "refused",
                f"{_shown(self.path)} is locked",
                reason="locked",
                details={"lock_reason": fsafe.escape_for_details(mine.locked)},
            )
        if mine.prunable is not None:
            raise _unlinked(f"git considers {_shown(self.path)} prunable")
        for other in known:
            if _within(self.path, other):
                raise UsageError(f"{_shown(self.path)} lies inside the worktree {_shown(other)}")
            if _within(other, self.path):
                raise UsageError(f"{_shown(self.path)} contains the worktree {_shown(other)}")

    def _pick_name(self, live: list[Record], repo: str) -> str:
        taken = {r["name"].casefold(): r for r in live if r["repo"] == repo and r["name"]}
        if self.name is not None:
            holder = taken.get(self.name.casefold())
            if holder is not None:
                raise WtctlError(
                    "refused",
                    f"a worktree named {self.name!r} already exists for {repo}",
                    reason="path_exists",
                    holder_id=holder["id"],
                )
            return self.name
        seed = naming.name_from_branch(os.path.basename(self.path))
        for name in naming.candidates(seed or "adopted"):
            if name.casefold() not in taken:
                return name
        raise WtctlError(
            "create_failed", f"no free name for {repo} among {naming.MAX_SUFFIX} candidates"
        )

    # -- output ------------------------------------------------------------------------------------

    def _projected(self, fields: dict[str, Any]) -> Record:
        now = self.state.timestamp()
        return {
            **{k: None for k in FIELDS},
            **fields,
            "labels": labelmod.render(labelmod.merge_labels((), self.labels)),
            "holders": [self.holder] if self.holder else [],
            "bootstrap_state": "none",
            "created": now,
            "updated": now,
        }

    def _result(self, record: Record, *, existing: bool, human: str) -> Result:
        stored = self.state.get_record(record["id"]) is not None
        out = record_object(record, self.state.bootstrap_summary(record["id"]) if stored else None)
        data = {"record": out, "existing": existing, "dry_run": self.dry}
        return Result(data, human=out["path"], stderr="\n".join([human, *self.warnings]))


def run(ctx: Context) -> Result:
    return Adopter(ctx).run()
