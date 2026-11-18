"""Baseline orchestration shared by ``create``, ``bootstrap``, ``baseline check`` and ``accept``.

The collection and comparison are in ``wtctl.repobaseline``; the integrity check is in
``wtctl.linkcheck``. This module turns a record into a computation and a change into the
``repo_config_changed`` refusal (docs/interface.md, Baseline).
"""

from __future__ import annotations

import os
import pwd
from collections.abc import Callable
from typing import Any

from . import linkcheck, repobaseline, worktrees
from .errors import UsageError, WtctlError
from .hardgit import Git
from .locate import describe_mount
from .repobaseline import Baseline, Item, Spec
from .state import BASELINE_FORMAT, LINKED_BASELINE_FORMAT, LINKS_BASELINE_FORMAT

# The collector runner: ``fsafe_child.run_op`` (a child process under the walk deadline) unless
# a test substitutes ``repobaseline.inproc``.
RUNNER: repobaseline.Runner | None = None

ZERO_DIGEST = "0" * 64


def describe(path: str) -> dict[str, str]:
    """File-system type, mount source and mount point of ``path`` (untrusted text).

    TODO(#26): derive the mount from the opened fd (mount id of ``/proc/self/fdinfo``, macOS
    ``fstatfs``) like fsafe does for its anchors; this looks the path up again by ``stat`` and
    the mountinfo table, deferred with the macOS and FUSE work of #26 (non-blocking: the
    identity compared is the opened fd's, only the text shown to the human is by path).
    """
    try:
        return describe_mount(path, os.stat(path))
    except OSError:
        return {"fs_type": "unknown", "mount_source": "", "mount_point": "/"}


def home_dir() -> str:
    home = os.environ.get("HOME")
    if home and os.path.isabs(home):
        return home
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return "/"


def recorded_items(state: Any, record_id: str) -> list[Item]:
    return [
        Item(r["cls"], r["path"], r["key"], r["type"], r["sha256"], None, r["anchor_id"])
        for r in state.baseline_items(record_id)
    ]


def hooks_identity(items: list[Item]) -> str | None:
    """The recorded identity of an external ``core.hooksPath`` directory, if there is one."""
    return next((i.anchor_id for i in items if i.cls == "git" and i.anchor_id), None)


def hooks_recorded_path(items: list[Item]) -> str | None:
    """The path that identity was recorded for."""
    return next((i.path for i in items if i.cls == "git" and i.anchor_id), None)


class LazyGit:
    """A hardened git that is opened on first use. ``baseline check`` and ``baseline accept`` need
    git only for the linked-worktree listing, so a record that does not use it never pays for
    (or fails on) ``open_git``; a failing open is the same as a failed listing."""

    def __init__(self, opener: Callable[[], Git]) -> None:
        self._opener = opener
        self._git: Git | None = None
        self._error: WtctlError | None = None

    def get(self) -> Git:
        if self._error is not None:
            raise self._error
        if self._git is None:
            try:
                self._git = self._opener()
            except WtctlError as exc:
                self._error = exc
                raise
        return self._git


def spec_for(
    config: Any,
    record: dict[str, Any],
    *,
    common_id: str,
    dir_id: str,
    hooks_id: str | None = None,
    hooks_path: str | None = None,
    rebind_hooks: bool = False,
    links: bool = True,
    linked: bool = True,
    sparse: bool = True,
    git: Git | LazyGit | None = None,
) -> Spec:
    return Spec(
        common_dir=record["common_dir"],
        common_id=common_id,
        worktree=record["path"],
        dir_id=dir_id,
        admin_dir=record["admin_dir"],
        home=home_dir(),
        ignore=list(config.baseline.ignore_config_keys),
        limit=config.remove.nested_scan_limit,
        hooks_identity=hooks_id,
        hooks_identity_path=hooks_path,
        rebind_hooks=rebind_hooks,
        links=links,
        linked=linked,
        sparse=sparse,
        listed=listed_worktrees(git, record["common_dir"]) if linked and git is not None else [],
    )


def listed_worktrees(git: Git | LazyGit, common_dir: str) -> list[str]:
    """The paths of the clone's linked worktrees that git lists and that are neither locked nor
    prunable (docs/interface.md, Baseline, Linked worktrees). One ``git worktree list`` per
    computation, cross-checked against the admin entries (``worktrees.worktree_entries``). A
    listing that fails, or that is forged (a path with a newline), hands in nothing: every
    directory is then walked, which can only report a change, never hide one."""
    try:
        real = git.get() if isinstance(git, LazyGit) else git
        entries = worktrees.worktree_entries(real, common_dir)
    except (WtctlError, OSError):
        return []  # includes an unreadable or non-directory ``<common>/worktrees``
    rest = entries if entries and entries[0].bare else entries[1:]
    return [e.path for e in rest if not e.bare and e.locked is None and e.prunable is None]


def follows_links(state: Any, record_id: str) -> bool:
    """Whether the record's baseline is compared with the symlink-following collector (format 2).
    A baseline recorded before that (format 1) is compared by the collector that recorded it, so
    the new coverage never makes a routine command fail by itself (docs/interface.md,
    Baseline, Format)."""
    return bool(state.baseline_format(record_id) >= LINKS_BASELINE_FORMAT)


def skips_linked(state: Any, record_id: str) -> bool:
    """Whether the record's baseline is compared with the collector that skips verified linked
    worktrees (format 3). A format 1 or 2 baseline was recorded with their content in it and is
    compared by the collector that recorded it (docs/interface.md, Baseline, Format)."""
    return bool(state.baseline_format(record_id) >= LINKED_BASELINE_FORMAT)


def covers_sparse(state: Any, record_id: str) -> bool:
    """Whether the record's baseline includes the admin entry's ``info/sparse-checkout`` (format
    4). An older baseline is compared by the collector that recorded it, so a worktree made
    sparse by git itself, before wtctl covered the file, does not fail by itself."""
    return bool(state.baseline_format(record_id) >= BASELINE_FORMAT)


def compute(spec: Spec) -> Baseline:
    return repobaseline.compute(spec, runner=RUNNER)


def item_rows(b: Baseline) -> list[dict[str, Any]]:
    """The rows to store: items that carry a ``detail`` are never recorded."""
    return [
        {"cls": i.cls, "path": i.path, "key": i.key, "type": i.type, "sha256": i.sha256,
         "anchor_id": i.anchor_id}
        for i in b.items
        if not i.detail
    ]  # fmt: skip


def recorded_digest(record: dict[str, Any]) -> str:
    return record["baseline_digest"] or ZERO_DIGEST


def refusal(
    record: dict[str, Any],
    current: Baseline,
    rows: list[dict[str, Any]],
    *,
    anchors: list[dict[str, Any]] | None = None,
    confirming: bool = False,
) -> WtctlError:
    """``repo_config_changed`` with the changed items. Never an empty ``items`` or
    ``changed_classes`` (the schema forbids them): with no rows, moved ``anchors`` stand alone in
    ``details``; with neither, the recorded digest alone disagrees with the rows and the refusal
    carries just the two digests. Only a caller in ``baseline accept``'s confirm window
    (``confirming``) reads that as the state moving under it (``busy``)."""
    if not rows and not anchors and confirming:
        return WtctlError(
            "busy",
            f"the baseline of {record['id']} moved while it was being confirmed; run it again",
        )
    details: dict[str, Any] = {
        "digest": current.digest,
        "recorded_digest": recorded_digest(record),
    }
    fields: dict[str, Any] = {}
    what = "an anchor identity" if anchors else "the recorded digest"
    if rows:
        classes = repobaseline.changed_classes(rows)
        what = " and ".join(classes)
        fields["items"] = rows
        details["changed_classes"] = classes
    if anchors:
        details["anchors"] = anchors
    return WtctlError(
        "refused",
        f"the repo-config baseline of {record['id']} changed ({what})",
        reason="repo_config_changed",
        details=details,
        **fields,
    )


def is_changed(record: dict[str, Any], current: Baseline) -> bool:
    return current.has_detail() or current.digest != record["baseline_digest"]


def require_worktree(record: dict[str, Any], what: str) -> None:
    """Throwaways have no baseline (a usage error); records not ``active`` are ``unlinked``."""
    if record["object_type"] != "worktree":
        raise UsageError(f"{record['id']} is a throwaway; {what} is for worktrees")
    if record["state"] != "active":
        raise WtctlError(
            "refused", f"{record['id']} is {record['state']}, not active", reason="unlinked"
        )


def verify_unchanged(
    config: Any, state: Any, record: dict[str, Any], git: Git | LazyGit | None = None
) -> tuple[linkcheck.Links, Baseline]:
    """Integrity, then the baseline compared with the recorded one; a change in either class
    raises ``repo_config_changed`` (the shared part of ``baseline check`` and a bootstrap
    re-run)."""
    links = linkcheck.verify_links(record, state)
    items = recorded_items(state, record["id"])
    spec = spec_for(
        config,
        record,
        common_id=record["common_id"],
        dir_id=record["dir_id"],
        hooks_id=hooks_identity(items),
        hooks_path=hooks_recorded_path(items),
        links=follows_links(state, record["id"]),
        linked=skips_linked(state, record["id"]),
        sparse=covers_sparse(state, record["id"]),
        git=git,
    )
    current = compute(spec)
    if is_changed(record, current):
        raise refusal(record, current, repobaseline.compare(items, current.items))
    return links, current


def new_coverage(
    config: Any,
    state: Any,
    record: dict[str, Any],
    current: Baseline,
    git: Git | LazyGit | None = None,
) -> list[dict[str, Any]]:
    """For a format 1 to 3 record whose baseline matched: what a ``baseline accept`` would change
    (the current collector's items that the recording one did not emit, or emitted differently),
    as ``baseline_item`` rows. Empty for a format 4 record."""
    if covers_sparse(state, record["id"]):
        return []
    items = recorded_items(state, record["id"])
    full = compute(
        spec_for(
            config,
            record,
            common_id=record["common_id"],
            dir_id=record["dir_id"],
            hooks_id=hooks_identity(items),
            hooks_path=hooks_recorded_path(items),
            git=git,
        )
    )
    return repobaseline.compare(current.items, full.items)


def record_current(
    config: Any,
    record: dict[str, Any],
    *,
    links: bool = True,
    linked: bool = True,
    sparse: bool = True,
    git: Git | LazyGit | None = None,
) -> Baseline:
    """The baseline of a worktree that was just made or just bootstrapped, from the record's own
    identities (``create`` and a bootstrap re-run record it)."""
    return compute(
        spec_for(
            config,
            record,
            common_id=record["common_id"],
            dir_id=record["dir_id"],
            links=links,
            linked=linked,
            sparse=sparse,
            git=git,
        )
    )
