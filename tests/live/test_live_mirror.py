"""Mirror, default-branch detection, pushed check and strict local transfer on real forges.

Needs only the public repository of each forge; it clones it (a full clone: choose a small one).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live import scenarios as sc, worlds
from tests.live.liveenv import FORGES

pytestmark = pytest.mark.live
needs = pytest.mark.needs

FORGE_PARAMS = [pytest.param(k, id=k, marks=needs(f"{k}:public")) for k in sorted(FORGES)]


@pytest.mark.parametrize("key", FORGE_PARAMS)
def test_mirror_refresh_default_branch_and_local_strict_transfer(
    key: str, tmp_path: Path, record_property: pytest.RecordProperty
) -> None:
    """G1-G3: the treeless first refresh and an incremental one succeed with
    ``GIT_NO_LAZY_FETCH=1`` set on the network calls; the default branch from
    ``ls-remote --symref`` matches the forge's; the pushed check at removal passes without a lazy
    fetch; a full-history local throwaway passes ``index-pack --strict``. Record the git
    version: the claims for git 2.45 and newer need it."""
    w = worlds.public_world(tmp_path, FORGES[key])
    version = w.plain([w.git_bin, "--version"]).out.strip()
    record_property("git_version", version)
    got = sc.mirror_flow(w)
    record_property("default_branch", got["default_branch"])
