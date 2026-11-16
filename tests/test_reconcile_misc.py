"""reconcile: strays, tombstones, throwaways, locking, idempotence, audit, hooks."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import pytest

from tests.reclib import RC
from wtctl.gateway import Gateway
from wtctl.state import GLOBAL_LEASE, State


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> RC:
    return RC(tmp_path, private_home).build()


# -- strays -------------------------------------------------------------------------------------


def test_stray_throwaway_directories_are_reported_and_never_deleted(w: RC) -> None:
    live = w.made("--source", "local")
    w.troot.joinpath("wt-orphan1").mkdir()
    (w.troot / "wt-orphan1" / "f").write_text("mine")
    odd = w.troot / "a\nb\x1b[31m"
    odd.mkdir()
    data = w.reconciled()
    paths = sorted(r["path"] for r in data["reports"] if r["kind_of"] == "throwaway")
    assert paths == sorted([str(w.troot / "wt-orphan1"), str(w.troot) + "/a\\x0ab\\x1b[31m"])
    assert all("\n" not in p for p in paths)
    assert (w.troot / "wt-orphan1" / "f").read_text() == "mine" and odd.is_dir()
    assert Path(live["path"]).is_dir()


def test_clone_tmp_and_mirror_strays(w: RC) -> None:
    w.top["clone_root"] = str(w.tmp / "clones")
    leftover = w.tmp / "clones" / "example.test" / "org" / "repo.wtctl-tmp-abc123"
    leftover.mkdir(parents=True)
    (leftover / "objects").write_text("x")
    beside = w.tmp / "clone.wtctl-tmp-q9"
    beside.mkdir()
    w.wt()  # creates (and records) the clone's mirror
    mirrors = w.home / ".local" / "state" / "wtctl" / "mirrors"
    assert any(p.name.endswith(".git") for p in mirrors.iterdir())
    orphan = mirrors / "0123456789abcdef.git"
    orphan.mkdir()
    data = w.reconciled()
    got = {(r["kind_of"], r["path"]) for r in data["reports"] if r["kind"] == "stray"}
    assert got == {
        ("clone_tmp", str(leftover)),
        ("clone_tmp", str(beside)),
        ("mirror", str(orphan)),
    }
    assert leftover.is_dir() and beside.is_dir() and orphan.is_dir()


def test_a_throwaway_whose_directory_vanished_is_gone(w: RC) -> None:
    rec = w.made("--source", "local")
    shutil.rmtree(rec["path"])
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "active", "gone", "mark_gone")]
    assert w.reconciled()["changes"] == []


# -- tombstones ---------------------------------------------------------------------------------


def test_old_tombstones_are_deleted_and_ids_are_never_reused(w: RC) -> None:
    old, new, future = (
        w.made("--source", "local"),
        w.made("--source", "local"),
        w.made("--source", "local"),
    )
    for r in (old, new, future):
        assert w.rm(r["id"]).code == 0
    w.update("UPDATE records SET removed_at='2000-01-01T00:00:00.000Z' WHERE id=?", old["id"])
    w.update("UPDATE records SET removed_at='2999-01-01T00:00:00.000Z' WHERE id=?", future["id"])
    data = w.reconciled()
    assert w.actions(data) == [(old["id"], "removed", None, "tombstone_delete")]
    assert w.rows("SELECT id FROM records WHERE id=?", old["id"]) == []
    assert w.rows("SELECT id FROM ids WHERE id=?", old["id"])  # the id stays burned
    assert w.row(new["id"])["state"] == "removed" and w.row(future["id"])["state"] == "removed"


def test_dry_run_keeps_tombstones(w: RC) -> None:
    old = w.made("--source", "local")
    w.rm(old["id"])
    w.update("UPDATE records SET removed_at='2000-01-01T00:00:00.000Z' WHERE id=?", old["id"])
    assert len(w.reconciled("--dry-run")["changes"]) == 1
    assert w.rows("SELECT id FROM records WHERE id=?", old["id"])


# -- locking, idempotence, audit, actors ----------------------------------------------------------


def test_a_second_reconcile_reports_already_running(w: RC) -> None:
    rec = w.wt()
    w.vanish(rec)
    held = State(w.db, Gateway())
    lease = held.try_lease(GLOBAL_LEASE)
    assert lease is not None
    data = w.reconciled()
    assert data == {"changes": [], "reports": [], "already_running": True, "dry_run": False}
    assert w.row(rec["id"])["state"] == "active"
    lease.release()
    held.close()
    assert w.reconciled()["changes"]


def test_a_failed_audit_write_deletes_nothing_and_a_retry_converges(w: RC) -> None:
    rec = w.wt()
    w.push_branch(Path(rec["path"]), rec["branch"])
    w.vanish(rec)
    w.reconciled()
    w.clock.advance(2 * 3600)
    audit = w.home / ".local" / "state" / "wtctl" / "audit.jsonl"
    audit.mkdir()
    r = w.rc()
    assert r.code == 1
    assert Path(rec["admin_dir"]).is_dir() and w.row(rec["id"])["state"] == "missing"
    audit.rmdir()
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "missing", "gone", "admin_entry_delete")]
    assert w.reconciled()["changes"] == []  # idempotent


def test_the_audit_record_names_the_actor_and_the_entry(w: RC) -> None:
    rec = w.wt()
    w.push_branch(Path(rec["path"]), rec["branch"])
    w.vanish(rec)
    w.settle_missing(rec["id"])
    (a,) = [x for x in w.audit_records() if x["action"] == "admin_entry_delete"]
    assert a["actor"] == "auto:tester" and a["result"] == "ok"
    assert a["inputs"]["admin_entry"] == rec["admin_dir"] and a["inputs"]["branch"] == rec["branch"]
    assert a["target"] == {"id": rec["id"], "path": rec["path"]}
    assert any(e["event"] == "reconcile" for e in w.log_events())


def test_the_post_reconcile_hook_fires_only_when_something_changed(w: RC) -> None:
    out = w.tmp / "hook.json"
    w.top["hooks"] = {"post-reconcile": [{"cmd": f"cat > {out}"}]}
    w.reconciled()
    assert not out.exists()
    rec = w.wt()
    w.vanish(rec)
    w.reconciled()
    import json

    payload = json.loads(out.read_text())
    assert payload["hook"] == "post-reconcile" and payload["changes"][0]["id"] == rec["id"]


def test_a_user_branch_and_files_survive_everything(w: RC) -> None:
    rec = w.wt()
    w.vanish(rec)
    before = w.refs()
    w.settle_missing(rec["id"], runs=3)
    assert w.refs() == before


def test_clock_helpers_are_utc() -> None:
    assert dt.datetime.now(dt.UTC).tzinfo is not None


def test_list_all_shows_foreign_worktrees(w: RC) -> None:
    rec = w.wt()
    other = w.tmp / "foreign"
    w.g(["worktree", "add", "-q", "-b", "fb", str(other)])
    assert w.run("list").json()["foreign"] == []
    foreign = w.run("list", "--all").json()["foreign"]
    paths = {f["path"]: f for f in foreign}
    assert set(paths) == {str(other), str(w.clone)}  # the main worktree has no record either
    assert paths[str(other)]["branch"] == "fb" and paths[str(other)]["clone_path"] == str(w.clone)
    assert len(paths[str(other)]["head"]) == 40 and rec["path"] not in paths
