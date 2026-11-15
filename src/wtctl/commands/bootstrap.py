"""``wtctl bootstrap <id|path>``: re-run Bootstrap on an owned, active worktree (human-only; the
CLI's actor gate has already run).

TODO(#17): refuse ``repo_config_changed`` on a change in either baseline class (as ``baseline
check`` does), and re-record the baseline after a successful run.
TODO(#19): refuse ``in_use`` when a process runs in the worktree.
TODO(#5): log the ``bootstrap`` event.
"""

from __future__ import annotations

from typing import Any

from .. import bootstrapper, fsafe
from ..context import Context, Result
from ..errors import UsageError, WtctlError
from ..gitsetup import open_git, with_filters
from ..state import Record, record_lease
from .create import record_object

BOOTSTRAPPABLE = "active"


def _check_type(record: Record) -> None:
    if record["object_type"] != "worktree":
        raise UsageError(f"{record['id']} is a throwaway; bootstrap is for worktrees")


def _check(record: Record) -> None:
    """The record-state rules (docs/interface.md, Commands by record state)."""
    _check_type(record)
    if record["state"] != BOOTSTRAPPABLE:
        raise WtctlError(
            "refused",
            f"{record['id']} is {record['state']}, not active",
            reason="unlinked",
        )
    if not record["owned"]:
        raise WtctlError(
            "refused", f"{record['id']} is adopted; only owned worktrees are bootstrapped",
            reason="not_owned",
        )  # fmt: skip


def run(ctx: Context) -> Result:
    a, config, state = ctx.args, ctx.config, ctx.state
    git = open_git(config, ctx.gateway)  # git_too_old (exit 12)
    record = state.resolve(a.target)
    if record["state"] == "creating" and not ctx.dry_run:
        # a live creator holds the record lease: wait for it (then busy) before refusing; the
        # state is checked again under the lease
        _check_type(record)
    else:
        _check(record)
    if ctx.dry_run:
        return _dry(ctx, record)
    with state.lease(record_lease(record["id"]), wait=config.lease_wait_s) as lease:
        record = state.get_record(record["id"])
        if record is None:
            raise WtctlError("not_found", f"no record for {a.target}")
        _check(record)
        # TODO(#17): baseline refusal (either class) goes here.
        # TODO(#19): in_use refusal goes here.
        settings = config.repo(record["repo"])
        warnings: list[str] = []
        git = with_filters(git, settings.allow_filters, ctx.gateway, warnings.append)
        if not a.copy_env and not settings.bootstrap.steps:
            warnings.append("no bootstrap steps are configured; only the snapshots were taken")
        try:
            outcome = bootstrapper.run_bootstrap(
                git=git,
                config=config,
                settings=settings,
                record=record,
                copy_env=a.copy_env,
                run_the_steps=True,
                rerun=True,
                warn=warnings.append,
                on_tick=bootstrapper.lease_ticker(lease),
            )
            if lease.lost:
                raise WtctlError(
                    "busy", f"the lease on {record['id']} was lost during bootstrap; nothing stored"
                )
            record = state.record_bootstrap(
                record["id"],
                expected=BOOTSTRAPPABLE,
                state=outcome.state,
                steps=outcome.steps,
                paths=outcome.paths,
                nested=outcome.nested,
            )
        except fsafe.FsafeError as exc:
            raise WtctlError(
                "refused",
                f"{record['path']} is not the recorded worktree ({exc.kind})",
                reason="unlinked",
            ) from exc
        summary = state.bootstrap_summary(record["id"])
        out = record_object(record, summary)
        if outcome.failed_step is not None:
            raise WtctlError(
                "bootstrap_failed",
                f"bootstrap step {len(outcome.steps)} failed in {record['path']}",
                details={"record": out, "step": outcome.failed_step},
            )
        # TODO(#17): re-record the baseline here (after a successful run).
        boot = {**summary, "tracked_changes": outcome.tracked_changes}
    return _result(out, boot, warnings, dry=False, human=f"bootstrapped {record['name']}")


def _dry(ctx: Context, record: Record) -> Result:
    summary = ctx.state.bootstrap_summary(record["id"])
    boot = {**summary, "tracked_changes": []}
    return _result(
        record_object(record, summary),
        boot,
        [],
        dry=True,
        human=f"would bootstrap {record['name']}",
    )


def _result(
    record: dict[str, Any], boot: dict[str, Any], warnings: list[str], *, dry: bool, human: str
) -> Result:
    data = {"record": record, "bootstrap": boot, "warnings": warnings, "dry_run": dry}
    return Result(data, human=record["path"], stderr="\n".join([human, *warnings]))
