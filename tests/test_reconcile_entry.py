"""reconcile: the conditions for deleting an admin entry, adopted records, vanished entries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.createlib import REPO
from tests.reclib import RC


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> RC:
    return RC(tmp_path, private_home).build()


def gone_run(w: RC, rec: dict[str, Any]) -> dict[str, Any]:
    w.vanish(rec)
    w.reconciled()
    w.clock.advance(2 * 3600)
    return w.reconciled()


def pushed_wt(w: RC, name: str = "one") -> dict[str, Any]:
    rec = w.wt(name)
    w.push_branch(Path(rec["path"]), rec["branch"])
    return rec


def test_a_locked_entry_is_kept(w: RC) -> None:
    rec = pushed_wt(w)
    (Path(rec["admin_dir"]) / "locked").write_text("on a usb stick")
    data = gone_run(w, rec)
    assert data["changes"] == [] and Path(rec["admin_dir"]).is_dir()
    assert w.row(rec["id"])["state"] == "missing"


def test_an_entry_naming_another_path_is_kept(w: RC) -> None:
    rec = pushed_wt(w)
    (Path(rec["admin_dir"]) / "gitdir").write_text("/elsewhere/.git\n")
    data = gone_run(w, rec)
    assert data["changes"] == [] and Path(rec["admin_dir"]).is_dir()


def test_submodule_git_dirs_keep_the_entry(w: RC) -> None:
    rec = pushed_wt(w)
    (Path(rec["admin_dir"]) / "modules").mkdir()
    data = gone_run(w, rec)
    assert w.kinds(data) == ["kept_unpushed"] and data["changes"] == []
    assert "submodule" in data["reports"][0]["message"]
    assert Path(rec["admin_dir"]).is_dir()


def test_per_worktree_refs_keep_the_entry(w: RC) -> None:
    rec = pushed_wt(w)
    d = Path(rec["admin_dir"]) / "refs" / "bisect"
    d.mkdir(parents=True)
    (d / "bad").write_text(w.shas["c3"] + "\n")
    data = gone_run(w, rec)
    assert w.kinds(data) == ["kept_unpushed"] and Path(rec["admin_dir"]).is_dir()


def test_an_unpushed_reflog_commit_keeps_the_entry(w: RC) -> None:
    rec = w.wt()
    wt = Path(rec["path"])
    w.commit("local", wt)  # unpushed, on the branch and in the HEAD reflog
    data = gone_run(w, rec)
    assert w.kinds(data) == ["kept_unpushed"] and data["changes"] == []
    assert len(data["reports"][0]["tip"]) == 40
    assert Path(rec["admin_dir"]).is_dir()


def test_a_failed_mirror_refresh_keeps_an_entry_that_needs_a_pushed_commit(w: RC) -> None:
    rec = w.wt()
    wt = Path(rec["path"])
    w.commit("local", wt)
    import shutil

    shutil.rmtree(w.remote)
    data = gone_run(w, rec)
    assert "fetch_failed" in w.kinds(data)
    assert Path(rec["admin_dir"]).is_dir() and data["changes"] == []


def test_a_branch_with_unpushed_commits_is_kept_and_reported(w: RC) -> None:
    rec = pushed_wt(w)
    wt = Path(rec["path"])
    w.vanish(rec)
    new = w.commit("elsewhere", w.clone)  # on main, unpushed
    w.g(["update-ref", f"refs/heads/{rec['branch']}", new])
    w.reconciled()
    w.clock.advance(2 * 3600)
    data = w.reconciled()
    assert w.kinds(data) == ["gone_with_unpushed"]
    assert data["reports"][0]["branch"] == rec["branch"] and data["reports"][0]["tip"] == new
    assert w.refs()[f"refs/heads/{rec['branch']}"] == new
    assert not wt.exists() and not Path(rec["admin_dir"]).exists()


def test_an_entry_that_is_already_gone(w: RC) -> None:
    rec = w.wt()
    w.g(["worktree", "remove", "--force", rec["path"]])
    w.reconciled()
    w.clock.advance(2 * 3600)
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "missing", "gone", "mark_gone")]
    rep = data["reports"][0]
    assert rep["kind"] == "gone" and rep["admin_entry"] == "absent" and rep["branch"] == rec["branch"]
    assert f"refs/heads/{rec['branch']}" in w.refs()


def test_adopted_records_never_touch_the_clone(w: RC) -> None:
    other = w.tmp / "adopted"
    w.g(["worktree", "add", "-q", "-b", "adb", str(other)])
    r = w.run("adopt", str(other), "--repo", REPO)
    assert r.code == 0, r.err
    rid = r.json()["record"]["id"]
    admin = sorted((w.clone / ".git" / "worktrees").iterdir())
    import shutil

    shutil.rmtree(other)
    w.reconciled()
    w.clock.advance(2 * 3600)
    data = w.reconciled()
    assert w.kinds(data) == ["adopted_gone"] and data["reports"][0]["id"] == rid
    assert w.row(rid)["state"] == "gone"
    assert sorted((w.clone / ".git" / "worktrees").iterdir()) == admin
    assert "refs/heads/adb" in w.refs()


def test_partial_records_are_left_for_a_human(w: RC) -> None:
    rec = w.wt()
    w.update("UPDATE records SET state='partial', last_error='x' WHERE id=?", rec["id"])
    w.vanish(rec)
    for _ in range(3):
        data = w.reconciled()
        w.clock.advance(3 * 3600)
    assert data["changes"] == [] and Path(rec["admin_dir"]).is_dir()
    assert w.row(rec["id"])["state"] == "partial"


def test_user_files_are_never_deleted(w: RC) -> None:
    rec = pushed_wt(w)
    keep = Path(rec["path"]).parent / "notes.txt"
    keep.write_text("mine")
    gone_run(w, rec)
    assert keep.read_text() == "mine"
