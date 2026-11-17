"""reconcile of a `creating` record whose baseline cannot be recorded (issue #77): it settles it
as `partial` (reconcile never deletes a file or a branch); `remove` then finishes it."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.reclib import RC
from tests.schemacheck import validate
from tests.test_reconcile_creating import crash_after_add
from wtctl import baselinesvc, repobaseline

pytestmark = pytest.mark.skipif(not os.path.isdir("/dev/shm"), reason="needs /dev/shm")


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> RC:
    world = RC(tmp_path, private_home).build()
    if os.stat("/dev/shm").st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("/dev/shm shares the device")
    return world


@pytest.fixture
def shm() -> Iterator[Path]:
    d = Path("/dev/shm") / f"wtctl-rcu-{os.getpid()}-{os.urandom(4).hex()}"
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def bad_hook(w: RC, shm: Path) -> Path:
    (shm / "h").write_text("#!/bin/sh\n")
    link = w.clone / ".git" / "hooks" / "post-commit"
    os.symlink(shm / "h", link)
    return link


def old_stuck(w: RC) -> dict:
    """What 0.27 left: `creating`, the worktree intact, dir_id and head recorded, no baseline."""
    rec = w.wt()
    w.update("UPDATE records SET state='creating', baseline_digest=NULL WHERE id=?", rec["id"])
    w.update("DELETE FROM baseline_items WHERE record_id=?", rec["id"])
    return rec


def test_the_old_stuck_shape_becomes_partial_and_stays_settled(w: RC, shm: Path) -> None:
    rec = old_stuck(w)
    bad_hook(w, shm)
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "creating", "partial", "abandon_uncomputable_baseline")]
    rep = data["reports"][0]
    assert rep["kind"] == "abandoned" and rep["id"] == rec["id"]
    assert "post-commit" in rep["message"]
    row = w.row(rec["id"])
    assert row["state"] == "partial" and "post-commit" in row["last_error"]
    assert Path(rec["path"], ".git").exists()  # nothing deleted
    assert w.reconciled()["changes"] == []  # converges: no loop in `creating`


def test_a_partial_leftover_is_finished_by_a_human_remove(w: RC, shm: Path, capsys) -> None:  # noqa: ANN001
    rec = old_stuck(w)
    link = bad_hook(w, shm)
    w.reconciled()
    # the baseline is still refused at that position, but the record was never baselined, so
    # `remove` has nothing to compare and finishes it
    r = w.run("remove", rec["id"], "--yes", human=True)
    assert r.code == 0, (r.out, r.err)
    assert not Path(rec["path"]).exists() and w.row(rec["id"])["state"] == "removed"
    assert link.is_symlink()  # the user's link is untouched
    assert w.admin_entries() == []


def test_a_fixed_hook_lets_reconcile_adopt_it_again(w: RC, shm: Path) -> None:
    rec = old_stuck(w)
    link = bad_hook(w, shm)
    link.unlink()
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "creating", "active", "adopt_after_crash")]


def test_a_crash_between_add_and_baseline_still_reclaims_normally(w: RC) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    assert w.actions(w.reconciled()) == [(rec["id"], "creating", "active", "adopt_after_crash")]


def test_a_crash_shape_with_an_uncomputable_baseline_is_partial_with_identity(
    w: RC, shm: Path
) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    bad_hook(w, shm)
    data = w.reconciled()
    assert w.actions(data)[0][2] == "partial"
    row = w.row(rec["id"])
    assert row["admin_dir"] == rec["admin_dir"] and row["dir_id"] == rec["dir_id"]
    validate("wtctl/cli/reconcile.json", data)
