"""``wtctl list``: records, newest first, filtered."""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any

from ..context import Context, Result
from ..duration import parse_duration
from ..errors import UsageError
from ..gitsetup import open_git
from ..output import render_table
from ..record_view import record_object
from ..state import STATES, Record

_REPO_ID = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+"
)

_SHA = re.compile(r"[0-9a-f]{40}")
HEADERS = ("ID", "TYPE", "STATE", "REPO", "NAME/REF", "BRANCH", "AGE")


def _timestamp(moment: dt.datetime) -> str:
    # Not strftime: %Y is not zero-padded for years < 1000 on glibc, which breaks string order.
    return f"{moment.year:04d}" + moment.strftime("-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _age(created: str, now: dt.datetime) -> str:
    try:
        then = dt.datetime.strptime(created, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return "?"
    seconds = max(0, int((now - then).total_seconds()))
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _row(record: Record, now: dt.datetime) -> tuple[str, ...]:
    ref = record["ref"] if record["object_type"] == "throwaway" else record["name"]
    return (
        record["id"],
        record["object_type"],
        record["state"],
        record["repo"],
        (ref or "-")[:12] if record["object_type"] == "throwaway" else (ref or "-"),
        record["branch"] or "-",
        _age(record["created"], now),
    )


def _clone_dirs(ctx: Context) -> list[str]:
    """Clones wtctl has a row, a live record or a configured path for (existing directories)."""
    found: set[str] = set()
    for row in ctx.state.clone_paths():
        found.add(row)
    found.update(r["clone_path"] for r in ctx.state.records() if r["clone_path"])
    for repo in ctx.config.repos:
        configured = ctx.config.repo(repo).clone
        if configured:
            found.add(configured)
    return sorted(p for p in found if os.path.isdir(os.path.join(p, ".git")))


def foreign_worktrees(ctx: Context) -> list[dict[str, Any]]:
    """Worktrees git lists for those clones that have no live record. Everything read from the
    clone is untrusted; only hardened git runs there."""
    git = open_git(ctx.config, ctx.gateway)
    recorded = {r["path"] for r in ctx.state.records()}
    out: list[dict[str, Any]] = []
    for clone in _clone_dirs(ctx):
        common = os.path.join(clone, ".git")
        r = git.run(["worktree", "list", "--porcelain"], git_dir=common, cwd=common, timeout=60)
        if r.returncode != 0 or r.timed_out:
            continue
        block: dict[str, str] = {}
        for line in [*r.out.split("\n"), ""]:
            if line:
                key, _, value = line.partition(" ")
                block[key] = value
                continue
            path = block.get("worktree", "")
            if path.startswith("/") and "bare" not in block and path not in recorded:
                ref = block.get("branch", "")
                head = block.get("HEAD", "")
                out.append(
                    {
                        "foreign": True,
                        "path": path,
                        "branch": ref[len("refs/heads/") :]
                        if ref.startswith("refs/heads/")
                        else None,
                        "head": head if _SHA.fullmatch(head) else None,
                        "clone_path": clone,
                    }
                )
            block = {}
    return out


def run(ctx: Context) -> Result:
    args = ctx.args
    # Usage checks first (check order stage 1), before state.db is opened.
    if args.repo is not None and not _REPO_ID.fullmatch(args.repo):
        raise UsageError(f"invalid repo id {args.repo!r}")
    for s in args.state:
        if s not in STATES:
            raise UsageError(f"unknown state {s!r}: expected one of {', '.join(STATES)}")
    older = parse_duration(args.older_than, flag="--older-than") if args.older_than else None
    now = ctx.gateway.clock.now()
    cutoff: str | None = None
    if older is not None:
        try:
            cutoff = _timestamp(now - dt.timedelta(seconds=older))
        except OverflowError:
            # Older than anything representable: nothing can match.
            return Result(data={"records": [], "foreign": []}, human="")
    object_type = "throwaway" if args.throwaways else "worktree" if args.worktrees else None
    # `--state` names tombstones explicitly; without it only live records are listed.
    found = ctx.state.records(
        states=args.state or None,
        repo=args.repo,
        labels=args.label,
        object_type=object_type,
        created_before=cutoff,
        newest_first=True,
    )
    records: list[dict[str, Any]] = [record_object(ctx.state, r) for r in found]
    foreign = foreign_worktrees(ctx) if args.all else []
    human = render_table(HEADERS, [_row(r, now) for r in found]) if found else ""
    return Result(data={"records": records, "foreign": foreign}, human=human)
