"""Integrity and lock checks of a removal (docs/interface.md, Integrity; Remove order step 2).

Integrity reads files only through no-follow descriptors anchored at recorded identities
(``wtctl.linkcheck``) and then asks hardened git, with an explicit ``--git-dir``, for the common
dir (``git rev-parse --git-common-dir``); every disagreement is ``unlinked``.
"""

from __future__ import annotations

import os
from typing import Any

from . import fsafe, linkcheck
from .errors import WtctlError
from .hardgit import Git

LOCK_REASON_MAX = 4096


def integrity_check(
    state: Any, record: dict[str, Any], git: Git
) -> tuple[dict[str, Any], linkcheck.Links | None]:
    """``(check, links)``: ``pass`` with the verified links, or ``fail`` with the failing step."""
    try:
        links = linkcheck.verify_links(record, state)
    except linkcheck.Unlinked as exc:
        detail = {"step": exc.step, "expected": exc.expected, "actual": exc.actual}
        return {"status": "fail", "details": [detail]}, None
    except WtctlError as exc:
        if exc.fields.get("reason") != "unlinked":
            raise
        detail = {"step": "path", "expected": record["path"], "actual": None}
        return {"status": "fail", "details": [detail]}, None
    common = record["common_dir"]
    gitdir = record["admin_dir"] or common
    r = git.run(["rev-parse", "--git-common-dir"], git_dir=gitdir, cwd=gitdir, timeout=60)
    answer = r.out.strip() if r.returncode == 0 and not r.timed_out else None
    if answer is not None and "\n" not in answer:
        resolved = os.path.realpath(os.path.join(gitdir, answer))
        if resolved == os.path.realpath(common):
            return {"status": "pass", "details": []}, links
    detail = {"step": "git_common_dir", "expected": common, "actual": answer}
    return {"status": "fail", "details": [detail]}, None


def _ident(text: str | None) -> fsafe.Identity | None:
    if not text:
        return None
    dev, _, ino = text.partition(":")
    return fsafe.Identity(int(dev), int(ino))


def lock_check(record: dict[str, Any]) -> dict[str, Any]:
    """``fail`` when the admin entry has a ``locked`` file of any type (never opened unless it is a
    regular file); ``skipped`` when the entry cannot be opened; ``pass`` otherwise."""
    admin, common = record["admin_dir"], record["common_dir"]
    if not admin:
        return {"status": "pass", "details": []}  # a main worktree has no admin entry
    rel = os.path.relpath(admin, common)
    try:
        with fsafe.open_anchor(common, expected=_ident(record["common_id"])) as anchor:
            fd = fsafe.open_chain(anchor, anchor.fd, rel.split(os.sep), start_path=anchor.path)
            try:
                try:
                    st = anchor.fs.lstat_at(fd, "locked")
                except FileNotFoundError:
                    return {"status": "pass", "details": []}
                reason = ""
                if fsafe.kind_of_mode(st.mode) == "file":
                    try:
                        lfd = fsafe.open_file(anchor, fd, "locked")
                        try:
                            data = os.read(lfd, LOCK_REASON_MAX)
                        finally:
                            os.close(lfd)
                        reason = data.decode("utf-8", "replace")
                    except (fsafe.FsafeError, OSError):
                        reason = ""
                return {"status": "fail", "details": [{"lock_reason": reason}]}
            finally:
                os.close(fd)
    except (fsafe.FsafeError, OSError):
        return {"status": "skipped", "details": []}
