"""``wtctl clone <repo-id>``: clone a repo regardless of ``auto_clone`` (docs/interface.md, clone).

An already resolving repo is returned as is. When its clone's common dir has a different identity
than the recorded one, a human re-accepts it here (the only way besides ``baseline accept``).
"""

from __future__ import annotations

import sys
from typing import Any

from ..context import Context, Result
from ..errors import WtctlError
from ..locate import Resolution, Resolver, is_fuse, resolver_for
from ..output import escape_control
from ..state import repo_lease


def confirm(prompt: str, *, tty: bool) -> bool:
    """Ask on the terminal; False without one (the caller then exits 13)."""
    if not tty:
        return False
    sys.stderr.write(prompt + " [y/N] ")
    sys.stderr.flush()
    try:
        return sys.stdin.readline().strip().lower() in ("y", "yes")
    except (OSError, ValueError):
        return False


def _reaccept(ctx: Context, resolver: Resolver, res: Resolution) -> list[dict[str, Any]]:
    ctx.require_human()
    anchors = res.anchors()
    if any(is_fuse(a["fs_type"]) for a in anchors):
        raise WtctlError(
            "refused",
            f"{res.common_dir} is on a FUSE file system; it cannot be re-accepted",
            reason="unlinked",
            details={"cause": "fuse_anchor", "anchors": anchors},
        )
    if ctx.dry_run:
        return anchors
    if not ctx.yes:
        # paths and mount facts are untrusted text and this prompt is the human gate: escape it
        shown = "; ".join(
            escape_control(
                f"{a['path']}: recorded {a['recorded']}, now {a['current']} "
                f"({a['fs_type']} {a['mount_source']} on {a['mount_point']})"
            )
            for a in anchors
        )
        question = (
            f"{escape_control(res.repo)}: the clone's directory changed ({shown}). Accept it?"
        )
        if not confirm(question, tty=ctx.runtime.stdin_isatty):
            raise WtctlError(
                "confirmation_required", "re-accepting a changed clone needs confirmation (--yes)"
            )
    with resolver.state.lease(repo_lease(res.repo), wait=resolver.config.lease_wait_s):
        resolver.state.put_clone(
            res.repo, path=res.clone_path, common_dir=res.common_dir, common_id=res.common_id
        )
        # TODO(#5): audit this as `clone_accept`.
    res.recorded_id = res.common_id
    return anchors


def run(ctx: Context) -> Result:
    repo = ctx.args.repo
    resolver = resolver_for(ctx, readonly=ctx.dry_run)
    res, _missing = resolver.find(repo)
    cloned = False
    anchors: list[dict[str, Any]] = []
    if res is None:
        res = resolver.clone(repo)
        res.warnings.extend(resolver.notes)
        cloned = res.cloned
    elif res.anchor_changed:
        anchors = _reaccept(ctx, resolver, res)
    data = {**res.to_json(), "cloned": cloned, "anchors": anchors, "dry_run": ctx.dry_run}
    warnings = [w for w in res.stderr_warnings() if not (anchors and "differs" in w)]
    return Result(data, human=res.clone_path, stderr="\n".join(warnings))
