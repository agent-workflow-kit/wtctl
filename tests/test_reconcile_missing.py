"""reconcile: missing directories, grace, two observations, admin-entry deletion."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.reclib import RC


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> RC:
    return RC(tmp_path, private_home).build()


def test_nothing_to_do(w: RC) -> None:
    w.wt()
    data = w.reconciled()
    assert data["changes"] == [] and data["already_running"] is False and data["dry_run"] is False


def test_first_run_marks_missing_and_never_deletes(w: RC) -> None:
    rec = w.wt()
    w.vanish(rec)
    w.clock.advance(10 * 3600)
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "active", "missing", "mark_missing")]
    assert w.row(rec["id"])["missing_since"] is not None
    assert Path(rec["admin_dir"]).is_dir()


def test_admin_entry_deleted_after_grace_and_two_runs(w: RC) -> None:
    rec = w.wt()
    w.push_branch(Path(rec["path"]), rec["branch"])
    w.vanish(rec)
    w.reconciled()
    data = w.reconciled()  # still inside the grace period
    assert data["changes"] == [] and Path(rec["admin_dir"]).is_dir()
    w.clock.advance(2 * 3600)
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "missing", "gone", "admin_entry_delete")]
    assert not Path(rec["admin_dir"]).exists()
    assert w.row(rec["id"])["state"] == "gone"
    assert rec["branch"] in w.refs().get(f"refs/heads/{rec['branch']}", "") or w.refs()
    assert f"refs/heads/{rec['branch']}" in w.refs()  # the branch is kept
    audit = [a for a in w.audit_records() if a["action"] == "admin_entry_delete"]
    assert len(audit) == 1 and audit[0]["target"]["id"] == rec["id"]


def test_grace_elapsed_but_one_observation_is_not_enough(w: RC) -> None:
    rec = w.wt()
    w.push_branch(Path(rec["path"]), rec["branch"])
    w.vanish(rec)
    w.top["reconcile"] = {"missing_grace": "1s"}
    w.clock.advance(5)
    w.reconciled()  # the first observation
    assert Path(rec["admin_dir"]).is_dir()
    w.clock.advance(5)
    w.reconciled()
    assert not Path(rec["admin_dir"]).exists()


def test_a_clock_going_backwards_cannot_skip_the_grace(w: RC) -> None:
    rec = w.wt()
    w.push_branch(Path(rec["path"]), rec["branch"])
    w.vanish(rec)
    w.reconciled()
    w.clock.advance(-7 * 24 * 3600)
    w.reconciled()
    w.reconciled()
    assert Path(rec["admin_dir"]).is_dir() and w.row(rec["id"])["state"] == "missing"


def test_a_directory_that_reappears_returns_to_active(w: RC) -> None:
    rec = w.wt()
    path = Path(rec["path"])
    saved = path.with_name("saved")
    path.rename(saved)
    w.reconciled()
    assert w.row(rec["id"])["state"] == "missing"
    saved.rename(path)
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "missing", "active", "reactivate")]
    assert w.row(rec["id"])["missing_since"] is None


def test_dry_run_changes_nothing(w: RC) -> None:
    rec = w.wt()
    w.vanish(rec)
    data = w.reconciled("--dry-run")
    assert data["dry_run"] is True
    assert w.actions(data) == [(rec["id"], "active", "missing", "mark_missing")]
    assert w.row(rec["id"])["state"] == "active"
