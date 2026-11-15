"""``wtctl forget <id|path> [--holder H]``: drop a worktree record, or one of its holders
(docs/interface.md, forget). Nothing on disk is touched.

The CLI's actor gate has already run (plain ``forget`` is human-only, see the registry). The
command needs no git. TODO(#5): log the ``forget`` event.
"""

from __future__ import annotations

from typing import Any

from ..context import Context, Result
from ..errors import UsageError, WtctlError
from ..state import LIVE_STATES, Record, State, check_holder, record_lease
from .create import record_object


def _view(state: State, record: Record) -> dict[str, Any]:
    return record_object(record, state.bootstrap_summary(record["id"]))


def _live(state: State, record_id: str) -> Record:
    """The record again, under its lease; a tombstone is exit 10 with its state."""
    record = state.get_record(record_id)
    if record is None:
        raise WtctlError("not_found", f"no record {record_id}")
    if record["state"] not in LIVE_STATES:
        raise WtctlError(
            "not_found",
            f"record {record_id} was already {record['state']}",
            details={"state": record["state"], "removed_at": record["removed_at"]},
        )
    return record


def not_a_holder(holder: str, record_id: str) -> str:
    return f"not_a_holder: {holder} is not a holder of {record_id}; nothing changed"


def _project(state: State, record: Record, holder: str | None) -> tuple[Record, list[str]]:
    """What the command would leave behind, for ``--dry-run``."""
    now = state.timestamp()
    if holder is None:
        return {**record, "state": "forgotten", "removed_at": now, "updated": now}, []
    if holder not in record["holders"]:
        return record, [not_a_holder(holder, record["id"])]
    left = [h for h in record["holders"] if h != holder]
    out = {**record, "holders": left, "updated": now}
    if not left and not record["owned"]:
        out.update(state="forgotten", removed_at=now)
    return out, []


def run(ctx: Context) -> Result:
    a, config, state = ctx.args, ctx.config, ctx.state
    holder: str | None = a.holder
    if holder is not None:
        check_holder(holder)  # exit 2
    record = state.resolve(a.target)
    if record["object_type"] != "worktree":
        raise UsageError(f"{record['id']} is a throwaway; use `wtctl remove`")
    warnings: list[str] = []
    if ctx.dry_run:
        shown, warnings = _project(state, record, holder)
        return _result(state, shown, warnings, dry=True)
    with state.lease(record_lease(record["id"]), wait=config.lease_wait_s):
        record = _live(state, record["id"])
        if holder is None:
            done = state.transition(record["id"], LIVE_STATES, "forgotten")
        else:
            done, removed, _forgotten = state.remove_holder(record["id"], holder)
            if not removed:
                warnings.append(not_a_holder(holder, record["id"]))
    return _result(state, done, warnings, dry=False)


def _result(state: State, record: Record, warnings: list[str], *, dry: bool) -> Result:
    out = record_object(record, state.bootstrap_summary(record["id"]))
    data = {"record": out, "warnings": warnings, "dry_run": dry}
    if record["state"] == "forgotten":
        human = f"{'would forget' if dry else 'forgot'} {record['id']}"
    else:
        human = f"{record['id']} stays {record['state']}"
    return Result(data, human=record["id"], stderr="\n".join([human, *warnings]))
