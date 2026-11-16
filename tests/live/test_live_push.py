"""A fast-forward push from a shallow throwaway to a real forge branch.

Needs a disposable repository (``WTCTL_LIVE_<FORGE>_PUSH_REPO``) with a commit on its default
branch and a token that may push and delete branches. The test creates a branch whose name is
``WTCTL_LIVE_PUSH_BRANCH_PREFIX`` plus random hex, pushes to it twice (a create, then a
fast-forward), and deletes it in a ``finally``. It never forces and never touches the default
branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live import liveenv, worlds
from tests.live import scenarios as sc
from tests.live.liveenv import FORGES

pytestmark = pytest.mark.live
needs = pytest.mark.needs

PUSH = [pytest.param(k, id=k, marks=needs(f"{k}:push")) for k in sorted(FORGES)]


@pytest.mark.parametrize("key", PUSH)
@pytest.mark.parametrize("depth", [50, 1])
def test_fast_forward_push_from_a_shallow_throwaway(key: str, depth: int, tmp_path: Path) -> None:
    """F1: ``push --porcelain`` of a commit on a depth-50 and a depth-1 throwaway is accepted by
    the forge (no ``shallow update not allowed``), twice, over HTTPS with the URL-scoped
    token."""
    w = worlds.push_world(tmp_path, FORGES[key])
    sc.push_fast_forward(w, depth, liveenv.push_prefix())
