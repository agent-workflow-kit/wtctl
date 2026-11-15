"""``remove`` of a worktree: the happy path, confirmation and the refusals."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.removelib import RR


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RR:  # noqa: ANN001
    return RR(tmp_path, private_home)


def test_removes_a_clean_pushed_worktree_and_its_branch(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    assert "work" in w.branches()
    d = w.removed()
    assert d["record"]["state"] == "removed" and d["record"]["removed_at"]
    assert d["branch_deleted"] is True and d["branch_kept_reason"] is None
    assert d["overridden"] == []
    assert not w.wt.exists() and not w.admin.exists()
    assert "work" not in w.branches()
    assert w.state_of() == "removed"
