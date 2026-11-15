"""``wtctl bootstrap <id>``: the human-only re-run (docs/interface.md, Bootstrap, Re-run)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from tests.createlib import CW, REPO
from tests.schemacheck import validate
from tests.test_create import ok
from wtctl.state import State, record_lease

PY = sys.executable


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def pystep(code: str, **kw) -> dict:  # noqa: ANN003
    return {"argv": [PY, "-c", code], **kw}


def made(w: CW, capsys) -> dict:  # noqa: ANN001
    """A created worktree (no bootstrap configured yet); returns its record."""
    return ok(w.create(capsys))["record"]


def rerun(w: CW, capsys, rid: str, *argv: str, human: bool = True):  # noqa: ANN001, ANN201
    return w.cli(capsys, "bootstrap", rid, *argv, human=human)


def good(r) -> dict:  # noqa: ANN001
    assert r.code == 0, (r.out, r.err)
    doc = r.json()
    rec = dict(doc["record"])
    rec["baseline_digest"] = rec["baseline_digest"] or "0" * 64  # TODO(#17)
    validate("wtctl/cli/bootstrap.json", {**doc, "record": rec})
    return doc


def err_of(r, code: int) -> dict:  # noqa: ANN001
    assert r.code == code, (r.out, r.err)
    return r.json()["error"]


def set_row(w: CW, rid: str, **cols: object) -> None:
    conn = sqlite3.connect(w.db)
    for k, v in cols.items():
        conn.execute(f"UPDATE records SET {k} = ? WHERE id = ?", (v, rid))
    conn.commit()
    conn.close()


def test_a_rerun_records_new_ignored_entries_and_copied_env_not_other_untracked(
    w: CW, capsys  # noqa: ANN001
) -> None:
    (w.work / ".gitignore").write_text("node_modules/\n.env\n")
    w.g(["add", ".gitignore"], w.work)
    w.g(["commit", "-q", "-m", "ig"], w.work)
    w.g(["push", "-q", str(w.remote), "main"], w.work)
    rec = made(w, capsys)
    wt = Path(rec["path"])
    (wt / "agent.txt").write_text("agent wrote this")
    (w.clone / ".env").write_text("K=v\n")
    w.repo_cfg["copy_env"] = [".env"]
    w.repo_cfg["bootstrap"] = {
        "steps": [pystep("import os; os.makedirs('node_modules/p'); open('node_modules/p/i','w');"
                         "open('new.txt','w')")]
    }  # fmt: skip
    data = good(rerun(w, capsys, rec["id"], "--copy-env"))
    assert data["dry_run"] is False
    assert data["bootstrap"]["state"] == "ok" and data["record"]["bootstrap"]["state"] == "ok"
    rows = w.rows("SELECT path, kind, digest FROM bootstrap_paths WHERE record_id = ?", rec["id"])
    assert {r["path"]: (r["kind"], r["digest"]) for r in rows} == {
        "node_modules": ("ignored_dir", None),
        ".env": ("untracked", hashlib.sha256(b"K=v\n").hexdigest()),
    }
    assert data["bootstrap"]["created_paths_count"] == 2
    assert (wt / "agent.txt").exists() and (wt / "new.txt").exists()


def test_rerunning_twice_does_not_grow_the_sets(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.repo_cfg["bootstrap"] = {"steps": [pystep("print(1)")]}
    (w.clone / ".env").write_text("K=v\n")
    w.repo_cfg["copy_env"] = [".env"]
    first = good(rerun(w, capsys, rec["id"], "--copy-env"))
    second = good(rerun(w, capsys, rec["id"], "--copy-env"))  # the file exists: not overwritten
    assert first["bootstrap"]["created_paths_count"] == second["bootstrap"]["created_paths_count"]
    assert any("exists" in m for m in second["warnings"])
    assert w.count("bootstrap_paths") == 1
    assert len(second["bootstrap"]["steps"]) == 1


def test_an_agent_cannot_rerun(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    err_of(rerun(w, capsys, rec["id"], human=False), 13)


def test_the_actor_gate_comes_before_record_resolution(w: CW, capsys) -> None:  # noqa: ANN001
    err_of(rerun(w, capsys, "wt-zzzzzzzz", human=False), 13)
    err_of(rerun(w, capsys, "wt-zzzzzzzz"), 10)


def test_a_throwaway_is_a_usage_error(w: CW, capsys) -> None:  # noqa: ANN001
    st = State(w.db, __import__("wtctl.gateway", fromlist=["Gateway"]).Gateway())
    rid = st.allocate_id()
    st.insert_record({"id": rid, "object_type": "throwaway", "state": "active", "repo": REPO,
                      "path": str(w.tmp / "t"), "name": "t"})  # fmt: skip
    st.close()
    err_of(rerun(w, capsys, rid), 2)


@pytest.mark.parametrize("state", ["unlinked", "missing", "clone_missing", "partial"])
def test_a_record_that_is_not_active_is_refused_unlinked(w: CW, capsys, state: str) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.set_state(rec["id"], state)
    err = err_of(rerun(w, capsys, rec["id"]), 20)
    assert err["reason"] == "unlinked"


def test_a_creating_record_is_refused_unlinked(w: CW, capsys) -> None:  # noqa: ANN001
    rid = w.plant_creating(branch="b", name="x")
    assert err_of(rerun(w, capsys, rid), 20)["reason"] == "unlinked"


def test_an_adopted_record_is_refused_not_owned(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    set_row(w, rec["id"], owned=0)
    assert err_of(rerun(w, capsys, rec["id"]), 20)["reason"] == "not_owned"


def test_a_replaced_worktree_directory_is_refused_unlinked(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    marker = w.tmp / "ran"
    w.repo_cfg["bootstrap"] = {"steps": [pystep(f"open({str(marker)!r},'w')")]}
    set_row(w, rec["id"], dir_id="1:1")
    assert err_of(rerun(w, capsys, rec["id"]), 20)["reason"] == "unlinked"
    assert not marker.exists()


def test_a_symlink_swapped_in_for_the_worktree_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    marker = w.tmp / "ran"
    w.repo_cfg["bootstrap"] = {"steps": [pystep(f"open({str(marker)!r},'w')")]}
    moved = w.tmp / "moved"
    os.rename(rec["path"], moved)
    os.symlink(moved, rec["path"])
    assert err_of(rerun(w, capsys, rec["id"]), 20)["reason"] == "unlinked"
    assert not marker.exists()


def test_a_failing_rerun_exits_12_keeps_the_record_active_and_the_old_sets(
    w: CW, capsys  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    w.repo_cfg["bootstrap"] = {"steps": [pystep("open('a','w')")]}
    good(rerun(w, capsys, rec["id"]))
    w.repo_cfg["bootstrap"] = {"steps": [pystep("open('b','w'); raise SystemExit(3)")]}
    r = rerun(w, capsys, rec["id"])
    err = err_of(r, 12)
    assert err["code"] == "bootstrap_failed" and err["details"]["step"]["exit"] == 3
    assert err["details"]["record"]["state"] == "active"
    assert err["details"]["record"]["bootstrap"]["state"] == "failed"
    assert w.records()[0]["state"] == "active"
    assert w.count("bootstrap_runs") == 2  # both runs are kept


def test_dry_run_changes_nothing_and_runs_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    marker = w.tmp / "ran"
    w.repo_cfg["bootstrap"] = {"steps": [pystep(f"open({str(marker)!r},'w')")]}
    data = good(rerun(w, capsys, rec["id"], "--dry-run"))
    assert data["dry_run"] is True and not marker.exists()
    assert w.count("bootstrap_runs") == 0
    assert w.records()[0]["bootstrap_state"] == "none"


def test_a_held_record_lease_is_busy(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    st = State(w.db, __import__("wtctl.gateway", fromlist=["Gateway"]).Gateway())
    lease = st.lease(record_lease(rec["id"]), wait=0)
    try:
        # the same process holds it: the CLI's own state handle is another pid-equal holder, so
        # use a record the lease table says is taken by a live holder
        err = err_of(rerun(w, capsys, rec["id"]), 12)
        assert err["code"] == "busy"
    finally:
        lease.release()
        st.close()


def test_bootstrap_auto_false_does_not_block_a_rerun(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.repo_cfg["bootstrap"] = {"auto": False, "steps": [pystep("print('x')")]}
    data = good(rerun(w, capsys, rec["id"]))
    assert data["bootstrap"]["steps"][0]["output_tail"] == "x\n"


def test_a_rerun_without_steps_says_so(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    data = good(rerun(w, capsys, rec["id"]))
    assert data["bootstrap"]["steps"] == [] and any("no bootstrap steps" in m for m in data["warnings"])


def test_tracked_changes_are_reported(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.repo_cfg["bootstrap"] = {"steps": [pystep("open('c1.txt','a').write('more')")]}
    data = good(rerun(w, capsys, rec["id"]))
    assert data["bootstrap"]["tracked_changes"] == ["c1.txt"]


def test_selecting_by_path_works(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    good(rerun(w, capsys, rec["path"]))
