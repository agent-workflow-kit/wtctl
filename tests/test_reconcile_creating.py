"""reconcile: records a killed create left in state ``creating``."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import REPO
from tests.reclib import RC
from wtctl.gateway import Gateway
from wtctl.state import State, record_lease


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> RC:
    return RC(tmp_path, private_home).build()


def crash_after_add(w: RC, rec: dict[str, Any]) -> None:
    """The record as a create killed after ``git worktree add`` leaves it."""
    w.update(
        "UPDATE records SET state='creating', admin_dir=NULL, dir_id=NULL, baseline_digest=NULL,"
        " reflog_start_ts=NULL, reflog_start_sha=NULL, head_at_create=NULL WHERE id=?",
        rec["id"],
    )
    w.update("DELETE FROM baseline_items WHERE record_id=?", rec["id"])


def test_a_worktree_the_creator_left_intact_is_adopted_after_the_crash(w: RC) -> None:
    rec = w.wt(holder="agent-1")
    crash_after_add(w, rec)
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "creating", "active", "adopt_after_crash")]
    assert data["reports"][0] == {
        "kind": "adopted_after_crash",
        "id": rec["id"],
        "path": rec["path"],
    }
    row = w.row(rec["id"])
    assert row["state"] == "active" and row["admin_dir"] == rec["admin_dir"]
    assert row["dir_id"] == rec["dir_id"] and row["bootstrap_state"] == "interrupted"
    assert row["baseline_digest"] and row["reflog_start_ts"]
    shown = w.run("show", rec["id"]).json()
    assert shown["holders"] == ["agent-1"] and shown["bootstrap"]["state"] == "interrupted"
    assert shown["bootstrap"]["created_paths_count"] == 0
    again = w.run("create", "--repo", REPO, "--branch", rec["branch"], "--name", "one",
                  "--no-bootstrap", "--holder", "agent-1")  # fmt: skip
    assert again.code == 0 and again.json().get("reused") is True


def test_a_live_creator_is_skipped_silently(w: RC) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    held = State(w.db, Gateway())
    lease = held.try_lease(record_lease(rec["id"]))
    assert lease is not None
    data = w.reconciled()
    assert data["changes"] == [] and "abandoned" not in w.kinds(data)
    assert w.row(rec["id"])["state"] == "creating"
    lease.release()
    held.close()


def test_a_lease_of_a_dead_process_is_taken_over(w: RC) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    w.update(
        "INSERT INTO leases(name, pid, start_time, expires) VALUES (?, ?, 'x', 9999999999)",
        record_lease(rec["id"]), dead.pid,
    )  # fmt: skip
    w.reconciled()
    assert w.row(rec["id"])["state"] == "active"


def test_a_planned_path_that_does_not_exist_becomes_gone(w: RC) -> None:
    rid = w.plant_creating(branch="b-x", name="ghost")
    w.update("UPDATE records SET created_branch=1, common_dir=?, base_dir=? WHERE id=?",
             str(w.clone / ".git"), str(w.root.resolve() / "worktrees"), rid)  # fmt: skip
    w.g(["branch", "b-x", "main"])
    w.rows("SELECT 1")
    (w.root.resolve() / "worktrees").mkdir(parents=True, exist_ok=True)
    w.run("list")  # opens state, nothing else
    from wtctl.state import State as S

    st = S(w.db, Gateway())
    st.record_anchor(str(w.root.resolve() / "worktrees"), _ident(w.root.resolve() / "worktrees"))
    st.close()
    data = w.reconciled()
    assert w.actions(data) == [(rid, "creating", "gone", "abandon")]
    assert data["reports"][0]["kind"] == "abandoned" and data["reports"][0]["branch"] == "b-x"
    assert "refs/heads/b-x" in w.refs()


def _ident(p: Path) -> str:
    st = p.stat()
    return f"{st.st_dev}:{st.st_ino}"


def test_a_path_that_is_not_the_worktree_stays_creating(w: RC) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    (Path(rec["path"]) / ".git").unlink()
    (Path(rec["path"]) / ".git").write_text("gitdir: /somewhere/else\n")
    data = w.reconciled()
    assert w.kinds(data) == ["abandoned"] and data["changes"] == []
    assert w.row(rec["id"])["state"] == "creating"


def test_an_entry_naming_another_path_is_not_adopted(w: RC) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    (Path(rec["admin_dir"]) / "gitdir").write_text("/planted/.git\n")
    data = w.reconciled()
    assert w.kinds(data) == ["abandoned"] and w.row(rec["id"])["state"] == "creating"


def test_a_process_in_the_planned_path_skips_the_record(w: RC) -> None:
    from wtctl.gateway import ProcessInfo as P

    rec = w.wt()
    crash_after_add(w, rec)
    w.scanner.procs = [P(4242, "make", rec["path"], ())]
    data = w.reconciled()
    assert w.kinds(data) == ["abandoned"] and data["changes"] == []
    assert w.row(rec["id"])["state"] == "creating"
    w.scanner.procs = []
    assert w.reconciled()["changes"][0]["to"] == "active"


def test_an_unavailable_scanner_skips_the_record(w: RC) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    w.scanner.fail = True
    data = w.reconciled()
    assert w.kinds(data) == ["abandoned"] and w.row(rec["id"])["state"] == "creating"


def test_a_throwaway_directory_is_deleted_fd_based(w: RC) -> None:
    rec = w.made("--source", "local")
    w.update("UPDATE records SET state='creating' WHERE id=?", rec["id"])
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "creating", "gone", "abandon_throwaway")]
    assert not Path(rec["path"]).exists() and data["reports"] == []


def test_a_throwaway_without_dir_id_is_left_and_reported_as_a_stray(w: RC) -> None:
    rec = w.made("--source", "local")
    w.update("UPDATE records SET state='creating', dir_id=NULL WHERE id=?", rec["id"])
    data = w.reconciled()
    assert w.row(rec["id"])["state"] == "gone" and Path(rec["path"]).is_dir()
    assert data["reports"] == [{"kind": "stray", "path": rec["path"], "kind_of": "throwaway"}]


def test_a_throwaway_with_a_replaced_directory_is_left(w: RC) -> None:
    rec = w.made("--source", "local")
    w.update("UPDATE records SET state='creating' WHERE id=?", rec["id"])
    Path(rec["path"]).rename(Path(rec["path"]).with_name("old-dir"))  # keeps its inode busy
    Path(rec["path"]).mkdir()
    (Path(rec["path"]) / "mine.txt").write_text("keep")
    data = w.reconciled()
    assert (Path(rec["path"]) / "mine.txt").read_text() == "keep"
    assert w.row(rec["id"])["state"] == "gone" and w.kinds(data) == ["stray", "stray"]
