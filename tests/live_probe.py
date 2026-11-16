"""A pytest plugin for the unit tests of the live suite: it records what the live tests declare.

Loaded with ``-p tests.live_probe`` in a collect-only subprocess. It never runs a test: it writes
each collected item's markers and ``needs`` to the file named by ``WTCTL_PROBE_OUT``. Before any
test module is imported (``pytest_configure``) it replaces every way out to the outside world
with a function that fails, so a collection (or an import) that reached for a process, a shell
or a socket crashes: the two live seams, ``subprocess.Popen`` and ``run``, ``os.system`` and
``os.popen``, and ``socket.socket.connect``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess

import pytest


def _boom(*args: object, **kwargs: object) -> None:
    raise RuntimeError("the live suite touched the outside world during collection")


def pytest_configure(config: pytest.Config) -> None:
    from tests.live import harness

    harness.spawn = _boom  # type: ignore[assignment]
    harness.make_gateway = _boom  # type: ignore[assignment]
    subprocess.Popen = _boom  # type: ignore[misc,assignment]
    subprocess.run = _boom  # type: ignore[assignment]
    os.system = _boom  # type: ignore[assignment]
    os.popen = _boom  # type: ignore[assignment]
    socket.socket.connect = _boom  # type: ignore[assignment]
    socket.socket.connect_ex = _boom  # type: ignore[assignment]


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
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
