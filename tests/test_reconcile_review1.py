"""reconcile: the findings of the first review (dry runs, delete rechecks, scan completeness,
parent chains, fail-safe counters, kept entries) and the surviving mutations."""

from __future__ import annotations

import hashlib
import shutil
import zlib
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import REPO
from tests.reclib import RC
from tests.test_reconcile_creating import crash_after_add
from tests.test_reconcile_entry import gone_run, pushed_wt
from tests.throwlib import commit_of, write_object
from tests.vectorlib import GIT
from wtctl import fsafe_child, reconcile_admin, reconcile_probe, removal_delete, removalfs
from wtctl.errors import WtctlError
from wtctl.gateway import Gateway
from wtctl.hardgit import Git
from wtctl.reconciler import Reconciler
from wtctl.state import State

LONG_AGO = "2000-01-01T00:00:00.000Z"


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> RC:
    return RC(tmp_path, private_home).build()


def counters(w: RC, rid: str) -> tuple[Any, ...]:
    r = w.row(rid)
    return (r["state"], r["missing_since"], r["missing_seen"])


# -- R1: a dry run writes no state --


def test_dry_run_does_not_count_an_observation(w: RC) -> None:
    rec = w.wt()
    w.vanish(rec)
    w.reconciled()
    before = counters(w, rec["id"])
    w.clock.advance(2 * 3600)
    w.reconciled("--dry-run")
    assert counters(w, rec["id"]) == before


def test_dry_run_does_not_reactivate_or_clear_a_reappeared_directory(w: RC) -> None:
    rec = w.wt()
    path = Path(rec["path"])
    saved = path.with_name("saved")
    path.rename(saved)
    w.reconciled()
    saved.rename(path)
    before = counters(w, rec["id"])
    data = w.reconciled("--dry-run")
    assert w.actions(data) == [(rec["id"], "missing", "active", "reactivate")]
    assert counters(w, rec["id"]) == before


def test_stale_counters_of_an_active_record_are_cleared_by_a_real_run_only(w: RC) -> None:
    rec = w.wt()
    w.update("UPDATE records SET missing_since=?, missing_seen='1' WHERE id=?", LONG_AGO, rec["id"])
    before = counters(w, rec["id"])
    w.reconciled("--dry-run")
    assert counters(w, rec["id"]) == before
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "active", "active", "clear_missing")]
    assert counters(w, rec["id"]) == ("active", None, None)


# -- R13: an unreadable observation count is one observation --


@pytest.mark.parametrize("seen", [None, "", "x", "-3", "٣"])
def test_a_garbage_observation_count_never_deletes_in_that_run(w: RC, seen: str | None) -> None:
    rec = pushed_wt(w)
    w.vanish(rec)
    w.reconciled()
    w.update(
        "UPDATE records SET missing_since=?, missing_seen=? WHERE id=?", LONG_AGO, seen, rec["id"]
    )
    data = w.reconciled()
    assert data["changes"] == [] and Path(rec["admin_dir"]).is_dir()
    assert w.row(rec["id"])["missing_seen"] == "1"
    assert w.actions(w.reconciled()) == [(rec["id"], "missing", "gone", "admin_entry_delete")]


# -- R14: a missing parent directory is a missing directory --


def test_a_removed_parent_directory_makes_the_record_missing_then_gone(w: RC) -> None:
    rec = pushed_wt(w)
    parent = Path(rec["path"]).parent
    shutil.rmtree(parent)
    assert Path(rec["base_dir"]).is_dir()
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "active", "missing", "mark_missing")]
    w.clock.advance(2 * 3600)
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "missing", "gone", "admin_entry_delete")]
    assert not Path(rec["admin_dir"]).exists() and f"refs/heads/{rec['branch']}" in w.refs()


def test_a_parent_directory_replaced_by_a_link_stays_unlinked(w: RC) -> None:
    rec = w.wt()
    parent = Path(rec["path"]).parent
    elsewhere = w.tmp / "elsewhere"
    shutil.move(str(parent), str(elsewhere))
    parent.symlink_to(elsewhere)
    data = w.reconciled()
    assert w.kinds(data) == ["unlinked"] and w.row(rec["id"])["state"] == "unlinked"
    assert (elsewhere / Path(rec["path"]).name).is_dir()


def test_a_missing_root_is_unlinked_not_missing(w: RC) -> None:
    rec = w.wt()
    shutil.move(rec["base_dir"], str(w.tmp / "moved-root"))
    data = w.reconciled()
    assert w.kinds(data) == ["unlinked"] and w.row(rec["id"])["state"] == "unlinked"


# -- R15: an adopted record whose parent is a mount root is unlinked (an unmounted mount point
# looks like a plain directory and is not detected) --


def adopt(w: RC) -> tuple[str, Path]:
    other = w.tmp / "adopted"
    w.g(["worktree", "add", "-q", "-b", "adb", str(other)])
    r = w.run("adopt", str(other), "--repo", REPO)
    assert r.code == 0, r.err
    return r.json()["record"]["id"], other


@pytest.mark.usefixtures("inproc")
def test_an_adopted_record_whose_parent_is_a_mount_root_is_unlinked_not_missing(
    w: RC, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid, other = adopt(w)
    shutil.rmtree(other)
    monkeypatch.setattr(fsafe_child, "_parent_dev", lambda fd: -1)  # fakes a mount root
    data = w.reconciled()
    assert w.kinds(data) == ["unlinked"] and w.row(rid)["state"] == "unlinked"
    assert data["changes"][0]["action"] == "mark_unlinked"


def test_an_adopted_record_on_the_parents_device_is_missing(w: RC) -> None:
    rid, other = adopt(w)
    shutil.rmtree(other)
    assert w.actions(w.reconciled()) == [(rid, "active", "missing", "mark_missing")]


# -- R4: the HEAD guards --


def test_a_detached_unpushed_head_off_every_branch_keeps_the_entry(w: RC) -> None:
    rec = w.wt()
    wt = Path(rec["path"])
    w.g(["checkout", "-q", "--detach"], wt)
    start = w.g(["rev-parse", "HEAD"], wt)
    w.commit("detached", wt)
    head = w.g(["rev-parse", "HEAD"], wt)
    w.update("UPDATE records SET reflog_start_sha=? WHERE id=?", head, rec["id"])
    assert head != start
    data = gone_run(w, rec)
    assert w.kinds(data) == ["kept_unpushed"] and data["changes"] == []
    assert data["reports"][0]["tip"] == head and "neither" in data["reports"][0]["message"]
    assert Path(rec["admin_dir"]).is_dir()


def test_a_missing_head_reflog_keeps_the_entry(w: RC) -> None:
    rec = pushed_wt(w)
    (Path(rec["admin_dir"]) / "logs" / "HEAD").unlink()
    data = gone_run(w, rec)
    assert w.kinds(data) == ["kept_unpushed"] and "reflog" in data["reports"][0]["message"]
    assert Path(rec["admin_dir"]).is_dir()


def test_an_unreadable_head_reflog_keeps_the_entry(w: RC) -> None:
    rec = pushed_wt(w)
    log = Path(rec["admin_dir"]) / "logs" / "HEAD"
    log.unlink()
    log.mkdir()
    data = gone_run(w, rec)
    assert w.kinds(data) == ["kept_unpushed"] and Path(rec["admin_dir"]).is_dir()


# -- R5/R6: reports --


def test_an_unpushed_branch_is_reported_even_when_the_record_did_not_create_it(w: RC) -> None:
    rec = pushed_wt(w)
    w.update("UPDATE records SET created_branch=0 WHERE id=?", rec["id"])
    w.vanish(rec)
    new = w.commit("elsewhere", w.clone)
    w.g(["update-ref", f"refs/heads/{rec['branch']}", new])
    w.reconciled()
    w.clock.advance(2 * 3600)
    data = w.reconciled()
    assert w.kinds(data) == ["gone_with_unpushed"] and data["reports"][0]["tip"] == new


def test_an_unreadable_entry_head_reports_a_null_tip(w: RC) -> None:
    rec = pushed_wt(w)
    (Path(rec["admin_dir"]) / "HEAD").unlink()
    data = gone_run(w, rec)
    assert w.kinds(data) == ["kept_unpushed"] and data["reports"][0]["tip"] is None
    assert Path(rec["admin_dir"]).is_dir()


def test_a_record_without_a_clone_path_reports_a_null_clone_path(w: RC) -> None:
    rec = w.wt()
    w.clone.rename(w.tmp / "clone-moved")
    w.update("UPDATE records SET clone_path=NULL WHERE id=?", rec["id"])
    data = w.reconciled()
    assert data["reports"] == [{"kind": "clone_missing", "id": rec["id"], "clone_path": None}]


# -- R7: baseline accept of a moved clone --


def moved(w: RC) -> tuple[dict[str, Any], Path]:
    rec = w.wt()
    new = w.tmp / "clone-moved"
    w.clone.rename(new)
    w.repo_cfg["clone"] = str(new)
    w.reconciled()
    return rec, new


def dotgit_text(rec: dict[str, Any]) -> str:
    return (Path(rec["path"]) / ".git").read_text()


def test_reattach_without_yes_or_a_tty_needs_confirmation_and_changes_nothing(w: RC) -> None:
    rec, _ = moved(w)
    r = w.run("baseline", "accept", rec["id"], human=True)
    assert r.code == 13 and w.row(rec["id"])["state"] == "clone_missing"
    assert dotgit_text(rec) == f"gitdir: {rec['admin_dir']}\n"


def test_reattach_refuses_an_incomplete_baseline_before_writing_pointers(w: RC) -> None:
    rec, _ = moved(w)
    w.top["remove"] = {"nested_scan_limit": 5}
    r = w.run("baseline", "accept", rec["id"], "--yes", human=True)
    assert r.code == 20 and r.json()["error"]["reason"] == "repo_config_changed"
    assert w.row(rec["id"])["state"] == "clone_missing"
    assert dotgit_text(rec) == f"gitdir: {rec['admin_dir']}\n"


def test_reattach_with_a_failed_pointer_rewrite_is_refused_and_changes_no_state(
    w: RC, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec, _ = moved(w)
    monkeypatch.setattr(reconcile_probe, "repair_pointers", lambda *a, **k: "no write today")
    before = dict(w.row(rec["id"]))
    r = w.run("baseline", "accept", rec["id"], "--yes", human=True)
    assert r.code == 20
    assert dict(w.row(rec["id"])) == before and dotgit_text(rec) == f"gitdir: {rec['admin_dir']}\n"


def test_reattach_refuses_when_the_integrity_check_fails_after_the_rewrite(
    w: RC, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec, _ = moved(w)
    monkeypatch.setattr(reconcile_probe, "repair_pointers", lambda *a, **k: None)  # wrote nothing
    before = dict(w.row(rec["id"]))
    r = w.run("baseline", "accept", rec["id"], "--yes", human=True)
    assert r.code == 20 and dict(w.row(rec["id"])) == before


# -- R9: the deletion looks again on the entry itself --


def delete_request(rec: dict[str, Any]) -> dict[str, Any]:
    admin = Path(rec["admin_dir"])
    return {
        "anchor": {
            "path": rec["common_dir"],
            "identity": removalfs.identity_list(rec["common_id"]),
        },
        "chain": ["worktrees"],
        "name": admin.name,
        "expect_gitdir": f"{rec['path']}/.git",
        "recheck": {
            "admin_dir": rec["admin_dir"],
            "common_dir": rec["common_dir"],
            "head_file": (admin / "HEAD").read_text().rstrip("\n"),
        },
    }


def test_the_recheck_passes_an_unchanged_entry(w: RC) -> None:
    rec = w.wt()
    assert fsafe_child.op_delete_admin(delete_request(rec))["state"] == "deleted"
    assert not Path(rec["admin_dir"]).exists()


def lock(admin: Path) -> None:
    (admin / "locked").write_text("x")


def modules(admin: Path) -> None:
    (admin / "modules").mkdir()


def refs_file(admin: Path) -> None:
    (admin / "refs" / "bisect").mkdir(parents=True)
    (admin / "refs" / "bisect" / "bad").write_text("0" * 40 + "\n")


def commondir(admin: Path) -> None:
    (admin / "commondir").write_text("../../other\n")


def head(admin: Path) -> None:
    (admin / "HEAD").write_text("0" * 40 + "\n")


@pytest.mark.parametrize("mutate", [lock, modules, refs_file, commondir, head])
def test_the_recheck_refuses_an_entry_that_changed(w: RC, mutate: Any) -> None:
    rec = w.wt()
    req = delete_request(rec)
    mutate(Path(rec["admin_dir"]))
    out = fsafe_child.op_delete_admin(req)
    assert out["state"] == "changed" and out["why"]
    assert Path(rec["admin_dir"]).is_dir()


def test_a_directory_that_came_back_before_the_delete_is_not_deleted(
    w: RC, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = pushed_wt(w)
    w.vanish(rec)
    w.reconciled()
    w.clock.advance(2 * 3600)
    real, calls = reconcile_probe.probe_entry, []

    def probe(state: Any, r: Any, budget: Any) -> Any:
        calls.append(1)
        return real(state, r, budget) if len(calls) == 1 else reconcile_probe.DirProbe("present")

    monkeypatch.setattr(reconcile_probe, "probe_entry", probe)
    r = w.rc()
    assert r.code == 0 and r.json()["changes"] == []
    assert Path(rec["admin_dir"]).is_dir() and w.row(rec["id"])["state"] == "missing"
    assert [a["result"] for a in w.audit_records() if a["action"] == "admin_entry_delete"] == [
        "error"
    ]


@pytest.mark.parametrize("outcome", ["foreign", "partial", "changed", "failed"])
def test_an_admin_delete_that_did_not_delete_leaves_the_record_missing(
    w: RC, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    rec = pushed_wt(w)
    w.vanish(rec)
    w.reconciled()
    w.clock.advance(2 * 3600)
    monkeypatch.setattr(removal_delete, "delete_admin", lambda *a, **k: (outcome, []))
    r = w.rc()
    assert r.code == 0 and r.json()["changes"] == [] and w.row(rec["id"])["state"] == "missing"
    results = [a["result"] for a in w.audit_records() if a["action"] == "admin_entry_delete"]
    assert results == ["error"]


# -- R10: a scan that cannot read an entry is incomplete --


def gone_entry(w: RC) -> dict[str, Any]:
    rec = w.wt()
    w.g(["worktree", "remove", "--force", rec["path"]])
    return rec


def test_a_symlinked_entry_keeps_an_absent_entry_record_from_going(w: RC) -> None:
    rec = gone_entry(w)
    (w.clone / ".git" / "worktrees").mkdir(exist_ok=True)
    decoy = w.tmp / "decoy"
    decoy.mkdir()
    (decoy / "gitdir").write_text(f"{rec['path']}/.git\n")
    (w.clone / ".git" / "worktrees" / "zz").symlink_to(decoy)
    data = gone_run_plain(w)
    assert w.kinds(data) == ["unlinked"] and w.row(rec["id"])["state"] == "missing"


def test_a_symlinked_gitdir_keeps_an_absent_entry_record_from_going(w: RC) -> None:
    rec = gone_entry(w)
    entry = w.clone / ".git" / "worktrees" / "zz"
    entry.mkdir(parents=True)
    real = w.tmp / "real-gitdir"
    real.write_text(f"{rec['path']}/.git\n")
    (entry / "gitdir").symlink_to(real)
    data = gone_run_plain(w)
    assert w.kinds(data) == ["unlinked"] and w.row(rec["id"])["state"] == "missing"


def test_another_entry_naming_the_path_keeps_the_record_from_going(w: RC) -> None:
    rec = gone_entry(w)
    entry = w.clone / ".git" / "worktrees" / "dup"
    entry.mkdir(parents=True)
    (entry / "gitdir").write_text(f"{rec['path']}/.git\n")
    data = gone_run_plain(w)
    assert w.kinds(data) == ["unlinked"] and "another admin entry" in data["reports"][0]["message"]
    assert w.row(rec["id"])["state"] == "missing"


def gone_run_plain(w: RC) -> dict[str, Any]:
    w.reconciled()
    w.clock.advance(2 * 3600)
    return w.reconciled()


def test_the_scan_is_complete_only_when_every_entry_was_read(w: RC) -> None:
    one, two = w.wt("one"), w.wt("two")
    args = {
        "anchor": {
            "path": one["common_dir"],
            "identity": removalfs.identity_list(one["common_id"]),
        },
        "chain": ["worktrees"],
    }
    full = fsafe_child.op_admin_scan(args)
    entries = full["entries"]
    assert full["complete"] is True and isinstance(entries, list) and len(entries) == 2
    (w.clone / ".git" / "worktrees" / "lnk").symlink_to(two["admin_dir"])
    assert fsafe_child.op_admin_scan(args)["complete"] is False


# -- R11 and R12: reclaim --


def test_a_reclaim_refused_for_its_baseline_settles_the_record_as_partial(w: RC) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    w.top["remove"] = {"nested_scan_limit": 5}
    data = w.reconciled()
    assert w.actions(data) == [(rec["id"], "creating", "partial", "abandon_uncomputable_baseline")]
    assert w.kinds(data) == ["abandoned"]
    row = w.row(rec["id"])
    assert row["state"] == "partial" and row["admin_dir"] and row["dir_id"]
    assert row["baseline_digest"] is None and "nested_scan_limit" in row["last_error"]
    assert w.reconciled()["changes"] == []


@pytest.mark.usefixtures("inproc")
def test_a_lost_reclaim_race_resets_every_value_this_call_wrote(
    w: RC, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    monkeypatch.setattr(Reconciler, "move", lambda *a, **k: False)
    w.reconciled()
    row = w.row(rec["id"])
    assert row["state"] == "creating" and row["admin_dir"] is None and row["dir_id"] is None
    assert row["head_at_create"] is None


@pytest.mark.usefixtures("inproc")
def test_a_lost_reclaim_race_never_wipes_a_concurrent_adopters_values(
    w: RC, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = w.wt()
    crash_after_add(w, rec)

    def adopter_wins(self: Reconciler, *a: Any, **k: Any) -> bool:
        w.update(
            "UPDATE records SET admin_dir='/other/admin', dir_id='9:9', head_at_create='abc'"
            " WHERE id=?",
            rec["id"],
        )
        return False

    monkeypatch.setattr(Reconciler, "move", adopter_wins)
    w.reconciled()
    row = w.row(rec["id"])
    assert (row["admin_dir"], row["dir_id"], row["head_at_create"]) == (
        "/other/admin", "9:9", "abc",
    )  # fmt: skip


def test_a_reclaim_empties_the_bootstrap_sets(w: RC) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    w.update(
        "INSERT INTO bootstrap_paths(record_id, path, kind) VALUES (?, 'a', 'file')", rec["id"]
    )
    w.update("INSERT INTO bootstrap_nested(record_id, path) VALUES (?, 'b')", rec["id"])
    w.reconciled()
    assert w.row(rec["id"])["state"] == "active"
    assert w.rows("SELECT * FROM bootstrap_paths WHERE record_id=?", rec["id"]) == []
    assert w.rows("SELECT * FROM bootstrap_nested WHERE record_id=?", rec["id"]) == []


def test_clearing_the_bootstrap_sets_is_busy_when_the_state_differs(w: RC) -> None:
    rec = w.wt()
    w.update(
        "INSERT INTO bootstrap_paths(record_id, path, kind) VALUES (?, 'a', 'file')", rec["id"]
    )
    st = State(w.db, Gateway())
    with pytest.raises(WtctlError) as exc:
        st.clear_bootstrap_sets(rec["id"], expected="creating")
    st.close()
    assert exc.value.code == "busy"
    assert len(w.rows("SELECT * FROM bootstrap_paths WHERE record_id=?", rec["id"])) == 1


# -- R12: surviving mutations --


def test_the_reachability_test_rehashes_the_objects(w: RC, private_home: Path) -> None:
    gd = w.clone / ".git"
    tree = write_object(gd, "tree", b"")
    a, b = commit_of(gd, tree, msg="a"), commit_of(gd, tree, msg="b")
    (gd / "refs" / "heads" / "tampered").write_text(a + "\n")
    git = Git(Gateway(), GIT, str(private_home))
    assert reconcile_admin.reachable_locally(git, str(gd), a) is True
    # an object whose content does not hash to its name
    fa, fb = gd / "objects" / a[:2] / a[2:], gd / "objects" / b[:2] / b[2:]
    fa.chmod(0o644)
    fa.write_bytes(fb.read_bytes())
    assert hashlib.sha1(zlib.decompress(fa.read_bytes())).hexdigest() == b  # noqa: S324
    assert reconcile_admin.reachable_locally(git, str(gd), a) is False


def test_an_entry_of_another_repository_is_kept(w: RC) -> None:
    rec = pushed_wt(w)
    (Path(rec["admin_dir"]) / "commondir").write_text("../../../other\n")
    data = gone_run(w, rec)
    assert data["changes"] == [] and Path(rec["admin_dir"]).is_dir()
    assert w.row(rec["id"])["state"] == "missing"


def test_a_replaced_common_dir_is_unlinked(w: RC) -> None:
    rec = w.wt()
    shutil.move(str(w.clone / ".git"), str(w.tmp / "old-git"))
    (w.clone / ".git").mkdir()
    data = w.reconciled()
    assert w.kinds(data) == ["unlinked"] and w.row(rec["id"])["state"] == "unlinked"


def test_a_repair_that_does_not_take_is_reported_unlinked(
    w: RC, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = w.wt()
    (Path(rec["path"]) / ".git").unlink()
    monkeypatch.setattr(reconcile_probe, "repair_pointers", lambda *a, **k: None)
    data = w.reconciled()
    assert w.kinds(data) == ["unlinked"] and "still mismatched" in data["reports"][0]["message"]
    assert w.row(rec["id"])["state"] == "unlinked"


@pytest.mark.parametrize(("age_days", "kept"), [(29, True), (31, False)])
def test_tombstones_are_kept_for_tombstone_days(w: RC, age_days: int, kept: bool) -> None:
    import datetime as dt

    rec = w.made("--source", "local")
    assert w.rm(rec["id"]).code == 0
    then = w.clock.now() - dt.timedelta(days=age_days)
    stamp = then.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    w.update("UPDATE records SET removed_at=? WHERE id=?", stamp, rec["id"])
    data = w.reconciled()
    assert (data["changes"] == []) is kept
    assert bool(w.rows("SELECT id FROM records WHERE id=?", rec["id"])) is kept
