"""Real git-lfs with ``create.allow_filters: [lfs]`` (see docs/live-testing.md).

Needs the ``git-lfs`` binary and a public repository on github.com or gitlab.com
(``WTCTL_LIVE_LFS_REPO``; the download is anonymous, no token is ever used) whose commit
``WTCTL_LIVE_LFS_REF`` tracks ``WTCTL_LIVE_LFS_PATH`` with git-lfs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live import liveenv, worlds
from tests.live import scenarios as sc

pytestmark = [pytest.mark.live, pytest.mark.needs("lfs")]


def _world(tmp_path: Path):  # noqa: ANN202
    w = worlds.lfs_world(tmp_path)
    sc.install_lfs_driver(w)
    return w


def test_lfs_is_smudged_when_allowed_and_a_pointer_otherwise(tmp_path: Path) -> None:
    """D27: with ``allow_filters: [lfs]`` the worktree holds the real file; with no honored
    driver it holds the pointer text."""
    w = _world(tmp_path)
    sha = liveenv.getenv("WTCTL_LIVE_LFS_REF") or ""
    path = liveenv.getenv("WTCTL_LIVE_LFS_PATH") or ""
    sc.ok(w.wtctl("clone", w.repo_id), "clone")
    smudged, pointer, notes = sc.lfs_content(w, sha, path)
    assert not smudged.startswith(sc.LFS_POINTER), f"the honored driver left a pointer: {notes}"
    assert pointer.startswith(sc.LFS_POINTER), f"a blanked driver should leave a pointer: {notes}"


@pytest.mark.parametrize(("key", "value"), sc.LFS_PLANTED_KEYS, ids=["lfs.url", "customtransfer"])
def test_planted_lfs_transfer_keys_are_refused(key: str, value: str, tmp_path: Path) -> None:
    """D27/spike E2: a planted ``lfs.url`` or ``lfs.customtransfer.*`` in the clone makes
    ``create`` exit 12 ``unsafe_repo_config`` while git-lfs is honored."""
    w = _world(tmp_path)
    sha = liveenv.getenv("WTCTL_LIVE_LFS_REF") or ""
    sc.ok(w.wtctl("clone", w.repo_id), "clone")
    sc.lfs_planted_key_refused(w, sha, key, value)
