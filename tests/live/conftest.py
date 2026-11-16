"""Every live test declares what it needs with ``@needs(...)``; a missing prerequisite skips the
test with a message naming the variable (never a value) before anything touches a network."""

from __future__ import annotations

import pytest

from tests.live import liveenv


def needs_of(node: pytest.Item) -> list[str]:
    return [arg for mark in node.iter_markers("needs") for arg in mark.args]


@pytest.fixture(autouse=True)
def _prerequisites(request: pytest.FixtureRequest) -> None:
    why = liveenv.all_unmet(needs_of(request.node))
    if why:
        pytest.skip("; ".join(why))
