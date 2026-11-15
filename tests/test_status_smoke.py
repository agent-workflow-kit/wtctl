from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.removallib import RW, inproc  # noqa: F401


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RW:  # noqa: ANN001
    return RW(tmp_path, private_home)


def test_smoke(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    d = w.st(capsys)
    print(json.dumps(d, indent=1))
    assert d["would_remove"] is False
