"""Shared body of ``label add`` and ``label remove``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import labels as labelmod
from .context import Context, Result
from .state import record_lease

DEFAULT_LEASE_WAIT = 600.0  # seconds; `lease_wait` defaults to 10m


def lease_wait(ctx: Context) -> float:
    """Seconds to wait for a held lease.

    TODO(#3): read `lease_wait` from the validated configuration (``ctx.config``).
    """
    return DEFAULT_LEASE_WAIT


def _pairs(rendered: list[str]) -> set[labelmod.Pair]:
    return {labelmod.parse_label(x) for x in rendered}


def edit(
    ctx: Context,
    *,
    preview: Callable[[set[labelmod.Pair], list[str]], set[labelmod.Pair]],
    apply: Callable[[Any, str, list[str]], list[str]],
    verb: str,
) -> Result:
    labels: list[str] = list(ctx.args.labels)
    # Check order stage 1: every label is validated before the record is looked up, so a bad
    # label is a usage error (2) whatever the target is.
    for text in labels:
        labelmod.parse_label(text)
    state = ctx.state
    record = state.resolve(ctx.args.target)  # a tombstone is never a target: 10 with details
    record_id = record["id"]
    # The lease is taken in both modes: a dry run must report `busy` like the real call would
    # (only `status` and `remove --dry-run` are exempt). Only the write is skipped for a dry run.
    with state.lease(record_lease(record_id), wait=lease_wait(ctx)):
        # Re-read inside the lease: a concurrent change may have landed since `resolve`.
        current = state.get_record(record_id)
        before = current["labels"] if current is not None else record["labels"]
        if ctx.dry_run:
            after = labelmod.render(preview(_pairs(before), labels))
        else:
            # TODO(#5): write the `label` log record (event `label`, id, repo, actor,
            # decision_ids, details: the labels requested and the result) after this succeeds.
            after = apply(state, record_id, labels)
    changed = _pairs(after) != _pairs(before)
    note = " (dry run)" if ctx.dry_run else ""
    summary = f"{record_id}: {verb}{note}, {len(after)} label(s)" + (
        "" if changed else ", unchanged"
    )
    return Result(
        data={"id": record_id, "labels": after, "dry_run": ctx.dry_run},
        human="\n".join(after),
        stderr=summary,
    )
