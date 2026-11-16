"""The conditions under which reconcile may delete an owned record's admin entry
(docs/interface.md, Reconcile, Missing directories).

Everything here only reads. ``decide`` returns a :class:`Verdict`; the caller audits and deletes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from . import fsafe, fsafe_child, removal_content, removalfs
from .hardgit import Git
from .mirror import Mirror, Tips, find_path, parse_cat_file_batch, verify_path

SHA = re.compile(r"[0-9a-f]{40}")
MAX_TIPS = 8


@dataclass
class Verdict:
    """``action``: ``delete`` (every condition holds), ``keep`` (kept silently: locked or the
    entry is another worktree's), ``kept_unpushed`` (kept and reported), ``unlinked`` (the common
    dir or the chain to the entry is not the recorded one) or ``absent``."""

    action: str
    tip: str | None = None
    message: str | None = None
    head: str | None = None


def entry_args(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "anchor": {
            "path": rec["common_dir"],
            "identity": removalfs.identity_list(rec["common_id"]),
        },
        "chain": ["worktrees"],
        "name": os.path.basename(rec["admin_dir"]),
    }


def inspect(rec: dict[str, Any], budget: fsafe_child.Budget) -> dict[str, Any] | fsafe.FsafeError:
    res = removalfs.run("admin_inspect", entry_args(rec), budget)
    if res.ok and res.value is not None:
        return res.value
    return res.error or fsafe.FsafeError("deadline", "admin_inspect did not finish")


def another_entry_names(rec: dict[str, Any], budget: fsafe_child.Budget) -> bool | fsafe.FsafeError:
    """Whether some entry of the clone's ``worktrees`` directory names the record's path."""
    args = entry_args(rec)
    args.pop("name")
    res = removalfs.run("admin_scan", args, budget)
    if not res.ok or res.value is None:
        return res.error or fsafe.FsafeError("deadline", "admin_scan did not finish")
    want = os.path.join(rec["path"], ".git")
    if not res.value.get("complete", False):
        return True  # cannot tell: treat as still registered
    return any(e[1] == want for e in res.value["entries"])


def entry_head(git: Git, rec: dict[str, Any]) -> str | None:
    admin = rec["admin_dir"]
    r = git.run(["rev-parse", "--verify", "HEAD^{commit}"], git_dir=admin, cwd=admin, timeout=60)
    out = r.out.strip()
    return out if r.returncode == 0 and not r.timed_out and SHA.fullmatch(out) else None


def reachable_locally(git: Git, common: str, commit: str) -> bool:
    """``commit`` is an ancestor of a local branch, with the ancestry re-hashed (content-verified
    as in Reachability) so a substituted object cannot fake it."""
    r = git.run(
        ["for-each-ref", "--contains", commit, "--format=%(objectname)", "refs/heads/"],
        git_dir=common,
        cwd=common,
        timeout=60,
    )
    if r.returncode != 0 or r.timed_out:
        return False
    for tip in sorted(set(r.out.split()))[:MAX_TIPS]:
        if SHA.fullmatch(tip) and _verified(git, common, commit, tip):
            return True
    return False


def _verified(git: Git, common: str, commit: str, tip: str) -> bool:
    if tip == commit:
        path = [tip]
    else:
        r = git.run(
            ["rev-list", "--parents", "--ancestry-path", f"{commit}..{tip}"],
            git_dir=common,
            cwd=common,
            timeout=60,
        )
        if r.returncode != 0 or r.timed_out:
            return False
        edges: dict[str, list[str]] = {}
        for line in r.out.split("\n"):
            if not line:
                continue
            shas = line.split(" ")
            if not all(SHA.fullmatch(s) for s in shas):
                return False
            edges[shas[0]] = shas[1:]
        found = find_path(edges, tip, commit)
        if found is None:
            return False
        path = found
    got = git.run(
        ["cat-file", "--batch"],
        git_dir=common,
        cwd=common,
        stdin=("\n".join(path) + "\n").encode(),
        timeout=60,
    )
    if got.returncode != 0 or got.timed_out:
        return False
    try:
        return verify_path(path, parse_cat_file_batch(got.stdout))
    except ValueError:
        return False


def decide(
    rec: dict[str, Any],
    info: dict[str, Any],
    *,
    git: Git,
    mirror_tips: Any,
    config: Any,
    state: Any,
) -> Verdict:
    """Apply the deletion conditions to an inspected, present entry. ``mirror_tips()`` returns
    ``(Mirror | None, Tips | None)`` and is called only when a pushed commit is needed."""
    admin, common = rec["admin_dir"], rec["common_dir"]
    if info["locked"]:
        return Verdict("keep", message="the admin entry is locked")
    if info["gitdir"] != os.path.join(rec["path"], ".git"):
        return Verdict("keep", message="the admin entry names another path")
    cd = info["commondir"]
    if cd is None or os.path.normpath(os.path.join(admin, cd)) != os.path.normpath(common):
        return Verdict("keep", message="the admin entry belongs to another repository")
    head = entry_head(git, rec)
    if head is None:
        return Verdict("kept_unpushed", None, "the admin entry's HEAD cannot be read")
    if info["has_modules"]:
        return Verdict("kept_unpushed", head, "the admin entry holds submodule git directories")
    if info["refs_files"]:
        return Verdict("kept_unpushed", head, "the admin entry holds per-worktree refs")
    pushed_cache: dict[str, bool] = {}
    mirror_box: list[tuple[Mirror | None, Tips | None]] = []

    def pushed(sha: str) -> bool:
        if not mirror_box:
            mirror_box.append(mirror_tips())
        mirror, tips = mirror_box[0]
        if mirror is None or tips is None:
            return False
        if sha not in pushed_cache:
            pushed_cache[sha] = mirror.is_pushed(sha, tips).pushed
        return pushed_cache[sha]

    if not reachable_locally(git, common, head) and not pushed(head):
        return Verdict(
            "kept_unpushed",
            head,
            "the HEAD of the admin entry is neither on a local branch nor pushed",
        )
    inp = removal_content.Inputs(git=git, state=state, record=rec, config=config, disposable=[])
    entries = removal_content.read_reflog(inp)
    start = removal_content._epoch(rec["reflog_start_ts"] or "")
    seen = (
        None
        if entries is None or start is None
        else removal_content.reflog_tips(entries, start, rec["reflog_start_sha"])
    )
    if seen is None:
        return Verdict("kept_unpushed", head, "the HEAD reflog is missing or unreadable")
    for sha in sorted(seen):
        if not pushed(sha):
            return Verdict("kept_unpushed", sha, "a commit in the HEAD reflog is not pushed")
    return Verdict("delete", head, head=head)
