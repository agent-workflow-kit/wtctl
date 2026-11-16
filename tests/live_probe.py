"""A pytest plugin for the unit tests of the live suite: it records what the live tests declare.

Loaded with ``-p tests.live_probe`` in a collect-only subprocess. It never runs a test: it writes
each collected item's markers and ``needs`` to the file named by ``WTCTL_PROBE_OUT``, and replaces
both outside-world seams with functions that fail, so a collection that reached for the network
would crash.
"""

from __future__ import annotations

import json
import os

import pytest


def _boom(*args: object, **kwargs: object) -> None:
    raise RuntimeError("the live suite touched the outside world during collection")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    from tests.live import harness

    harness.spawn = _boom  # type: ignore[assignment]
    harness.make_gateway = _boom  # type: ignore[assignment]
    rows = [
        {
            "id": item.nodeid,
            "markers": sorted({m.name for m in item.iter_markers()}),
            "needs": [a for m in item.iter_markers("needs") for a in m.args],
        }
        for item in items
    ]
    with open(os.environ["WTCTL_PROBE_OUT"], "w") as f:
        json.dump(rows, f)
