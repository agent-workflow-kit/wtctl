"""reconcile: pointer rewrite (never `git worktree repair`), planted entries, moved clones."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.createlib import REPO, Spy
from tests.reclib import RC


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> RC:
    return RC(tmp_path, private_home).build()


def test_a_missing_dotgit_file_is_rewritten(w: RC) -> None:
    rec = w.wt()
    spy = Spy()
    w.git_runner = spy
    dotgit = Path(rec["path"]) / ".git"
    dotgit.unlink()
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "active", "active", "repair")]
    assert dotgit.read_text() == f"gitdir: {rec['admin_dir']}\n"
    assert spy.subs() and "worktree" not in spy.subs()  # never `git worktree repair`
    assert w.reconciled()["changes"] == []


def test_a_moved_worktree_gets_its_admin_pointer_rewritten(w: RC) -> None:
    rec = w.wt()
    (Path(rec["admin_dir"]) / "gitdir").write_text("/where/it/used/to/be/.git\n")
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "active", "active", "repair")]
    assert (Path(rec["admin_dir"]) / "gitdir").read_text() == f"{rec['path']}/.git\n"


def test_an_unlinked_record_returns_to_active_once_it_is_consistent(w: RC) -> None:
    rec = w.wt()
    w.update("UPDATE records SET state='unlinked' WHERE id=?", rec["id"])
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "unlinked", "active", "reactivate")]


def test_a_planted_admin_entry_pointing_elsewhere_is_not_followed(w: RC) -> None:
    rec = w.wt()
    outside = w.tmp / "outside"
    outside.mkdir()
    (Path(rec["admin_dir"]) / "gitdir").write_text(f"{outside}/.git\n")
    w.reconciled()
    assert os.listdir(outside) == []
    assert (Path(rec["admin_dir"]) / "gitdir").read_text() == f"{rec['path']}/.git\n"


def test_a_dotgit_naming_another_admin_entry_is_not_rewritten(w: RC) -> None:
    one, two = w.wt("one"), w.wt("two")
    dotgit = Path(one["path"]) / ".git"
    dotgit.write_text(f"gitdir: {two['admin_dir']}\n")
    (Path(one["admin_dir"]) / "gitdir").write_text("/x/.git\n")
    data = w.reconciled()
    assert w.kinds(data) == ["unlinked"] and data["reports"][0]["id"] == one["id"]
    assert dotgit.read_text() == f"gitdir: {two['admin_dir']}\n"
    assert (Path(one["admin_dir"]) / "gitdir").read_text() == "/x/.git\n"
    assert w.row(one["id"])["state"] == "unlinked"


def test_a_replaced_directory_is_not_written_into(w: RC) -> None:
    rec = w.wt()
    path = Path(rec["path"])
    path.rename(path.with_name("moved-away"))
    path.mkdir()
    data = w.reconciled()
    assert w.kinds(data) == ["unlinked"]
    assert os.listdir(path) == []
    assert w.row(rec["id"])["state"] == "unlinked"


def test_a_symlinked_admin_entry_is_not_followed(w: RC) -> None:
    rec = w.wt()
    admin = Path(rec["admin_dir"])
    decoy = w.tmp / "decoy"
    admin.rename(decoy)
    admin.symlink_to(decoy)
    (Path(rec["path"]) / ".git").unlink()
    data = w.reconciled()
    assert w.kinds(data) == ["unlinked"]
    assert not (Path(rec["path"]) / ".git").exists()


def test_a_dotgit_symlink_is_not_replaced(w: RC) -> None:
    rec = w.wt()
    dotgit = Path(rec["path"]) / ".git"
    dotgit.unlink()
    dotgit.symlink_to("/etc")
    data = w.reconciled()
    assert w.kinds(data) == ["unlinked"] and dotgit.is_symlink()


def test_dry_run_writes_nothing(w: RC) -> None:
    rec = w.wt()
    dotgit = Path(rec["path"]) / ".git"
    dotgit.unlink()
    data = w.reconciled("--dry-run")
    assert w.actions(data) == [(rec["id"], "active", "active", "repair")]
    assert not dotgit.exists()


def test_an_adopted_worktree_is_not_rewritten(w: RC) -> None:
    other = w.tmp / "adopted"
    w.g(["worktree", "add", "-q", "-b", "adb", str(other)])
    assert w.run("adopt", str(other), "--repo", REPO).code == 0
    (other / ".git").unlink()
    data = w.reconciled()
    assert w.kinds(data) == ["unlinked"] and not (other / ".git").exists()


def test_a_moved_clone_is_reported_and_nothing_is_changed(w: RC) -> None:
    rec = w.wt()
    new = w.tmp / "clone-moved"
    w.clone.rename(new)
    w.repo_cfg["clone"] = str(new)
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "active", "clone_missing", "mark_clone_missing")]
    assert data["reports"] == [{"kind": "moved_clone", "id": rec["id"], "clone_path": str(new)}]
    row = w.row(rec["id"])
    assert row["clone_path"] == str(w.clone) and row["admin_dir"] == rec["admin_dir"]
    assert (new / ".git" / "worktrees").is_dir()
    again = w.reconciled()
    assert again["changes"] == [] and w.kinds(again) == ["moved_clone"]


def test_a_clone_that_cannot_be_found_is_clone_missing(w: RC) -> None:
    rec = w.wt()
    new = w.tmp / "clone-moved"
    w.clone.rename(new)
    data = w.reconciled()
    assert w.kinds(data) == ["clone_missing"]
    assert data["reports"][0]["clone_path"] == str(w.clone)
    assert w.row(rec["id"])["state"] == "clone_missing"


def test_a_moved_clone_that_does_not_name_the_worktree_is_not_a_candidate(w: RC) -> None:
    rec = w.wt()
    new = w.tmp / "clone-moved"
    w.clone.rename(new)
    (new / ".git" / "worktrees" / Path(rec["admin_dir"]).name / "gitdir").write_text("/o/.git\n")
    w.repo_cfg["clone"] = str(new)
    assert w.kinds(w.reconciled()) == ["clone_missing"]


def test_baseline_accept_reattaches_a_moved_clone(w: RC) -> None:
    rec = w.wt()
    new = w.tmp / "clone-moved"
    w.clone.rename(new)
    w.repo_cfg["clone"] = str(new)
    w.reconciled()
    assert w.run("baseline", "accept", rec["id"], "--yes").code == 13  # human only
    dry = w.run("baseline", "accept", rec["id"], "--yes", "--dry-run", human=True)
    assert dry.code == 0 and w.row(rec["id"])["state"] == "clone_missing"
    r = w.run("baseline", "accept", rec["id"], "--yes", human=True)
    assert r.code == 0, (r.out, r.err)
    row = w.row(rec["id"])
    new_admin = str(new / ".git" / "worktrees" / Path(rec["admin_dir"]).name)
    assert row["state"] == "active" and row["clone_path"] == str(new)
    assert row["admin_dir"] == new_admin and row["common_dir"] == str(new / ".git")
    assert (Path(rec["path"]) / ".git").read_text() == f"gitdir: {new_admin}\n"
    assert w.rows("SELECT path FROM clones")[0]["path"] == str(new)
    assert w.reconciled()["changes"] == []
    assert w.run("baseline", "check", rec["id"]).code == 0


def test_baseline_accept_refuses_a_clone_missing_record_without_a_candidate(w: RC) -> None:
    rec = w.wt()
    w.clone.rename(w.tmp / "clone-moved")
    w.reconciled()
    r = w.run("baseline", "accept", rec["id"], "--yes", human=True)
    assert r.code == 20 and w.row(rec["id"])["state"] == "clone_missing"
