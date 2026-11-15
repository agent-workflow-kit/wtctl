"""Pushed-state checks (stash, unpushed head and side branches) and the offline mirror."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.removallib import RW
from tests.vectorlib import plain_git
from wtctl import removal_status
from wtctl.state import State, clone_lease

ZERO = "0" * 40


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RW:  # noqa: ANN001
    return RW(tmp_path, private_home)


def checks(d: dict[str, Any], *names: str) -> dict[str, Any]:
    return {n: d["checks"][n] for n in names}


def test_unpushed_commit_makes_head_unreachable(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    sha = w.push_work("a")
    d = w.st(capsys)
    assert d["checks"]["unpushed_head"] == {
        "status": "fail",
        "details": [{"sha": sha, "detail": "not_reachable"}],
    }
    assert (d["would_remove"], d["reason"]) == (False, "unpushed")
    assert "unpushed_head" in d["force_overrides"]


def test_pushed_commit_passes(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    w.push()
    d = w.st(capsys)
    assert d["checks"]["unpushed_head"]["status"] == "pass"
    assert d["would_remove"] is True


def test_detached_head_reports_no_branch_and_still_fails(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["checkout", "-q", "--detach"], w.wt)
    sha = w.push_work("a")
    d = w.st(capsys)
    assert d["current_branch"] is None and d["head"] == sha
    assert d["checks"]["unpushed_head"]["status"] == "fail"


def test_commit_dropped_by_a_force_push_is_not_pushed(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    w.push()
    assert w.st(capsys)["checks"]["unpushed_head"]["status"] == "pass"
    w.g(["push", "-q", "--force", str(w.remote), "main:work"], w.clone)  # rewinds the branch
    d = w.st(capsys)  # the object stays in the mirror; existence alone never counts
    assert d["checks"]["unpushed_head"]["details"][0]["detail"] == "not_reachable"


# -- --merged-head ---------------------------------------------------------------------------


def test_merged_head_accepts_a_head_the_mirror_has_seen(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    sha = w.push_work("a")
    w.push()
    w.st(capsys)  # the mirror fetches it
    w.g(["push", "-q", "--delete", str(w.remote), "work"], w.clone)  # squash-merged, branch gone
    assert w.st(capsys)["checks"]["unpushed_head"]["status"] == "fail"
    d = w.st(capsys, "--merged-head", sha)
    assert d["checks"]["unpushed_head"]["status"] == "pass"
    assert d["would_remove"] is True


def test_merged_head_for_a_local_only_commit_is_not_accepted(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    sha = w.push_work("never-pushed")
    d = w.st(capsys, "--merged-head", sha)
    assert d["checks"]["unpushed_head"]["status"] == "fail"
    assert (d["would_remove"], d["reason"]) == (False, "unpushed")


def test_merged_head_other_than_head_is_not_accepted(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    other = w.shas["c1"]
    d = w.st(capsys, "--merged-head", other)
    assert d["checks"]["unpushed_head"]["status"] == "fail"


@pytest.mark.parametrize("bad", ["abc", "G" * 40, "A" * 40, "0" * 39, ""])
def test_merged_head_must_be_a_full_lowercase_sha(w: RW, capsys, bad: str) -> None:  # noqa: ANN001
    w.start(capsys)
    r = w.status(capsys, "--merged-head", bad)
    assert r.code == 2, (r.out, r.err)


# -- side branches ---------------------------------------------------------------------------


def side_branch(w: RW, name: str = "side") -> str:
    w.g(["checkout", "-q", "-b", name], w.wt)
    sha = w.push_work(name)
    w.g(["checkout", "-q", "work"], w.wt)
    return sha


def test_branch_committed_in_this_worktree_and_not_pushed(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    sha = side_branch(w)
    d = w.st(capsys)
    assert d["checks"]["unpushed_head"]["status"] == "pass"
    assert d["checks"]["unpushed_branches"] == {
        "status": "fail",
        "details": [{"branch": "side", "sha": sha, "detail": "not_reachable"}],
    }
    assert d["reason"] == "unpushed"


def test_side_branch_pushed_passes(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    side_branch(w)
    w.g(["push", "-q", str(w.remote), "side:side"], w.wt)
    assert w.st(capsys)["checks"]["unpushed_branches"]["status"] == "pass"


def test_branch_made_elsewhere_is_not_attributed_to_this_worktree(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["checkout", "-q", "-b", "elsewhere"], w.clone)
    w.commit("elsewhere", w.clone)
    w.g(["checkout", "-q", "main"], w.clone)
    assert w.st(capsys)["checks"]["unpushed_branches"]["status"] == "pass"


def test_backdated_reflog_entry_is_still_attributed(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["checkout", "-q", "-b", "sneaky"], w.wt)
    (w.wt / "s.txt").write_text("s")
    w.g(["add", "."], w.wt)
    env = {"GIT_COMMITTER_DATE": "2001-01-01T00:00:00Z", "GIT_AUTHOR_DATE": "2001-01-01T00:00:00Z"}
    r = plain_git(["commit", "-q", "-m", "old"], cwd=w.wt, home=w.home, env=env)
    assert r.returncode == 0, r.stderr
    w.g(["checkout", "-q", "work"], w.wt)
    d = w.st(capsys)
    assert [x["branch"] for x in d["checks"]["unpushed_branches"]["details"]] == ["sneaky"]


@pytest.mark.parametrize("how", ["emptied", "missing", "garbage", "unrelated"])
def test_reflog_that_cannot_anchor_is_unavailable(w: RW, capsys, how: str) -> None:  # noqa: ANN001
    w.start(capsys)
    side_branch(w)
    log = w.admin / "logs" / "HEAD"
    if how == "emptied":
        log.write_text("")
    elif how == "missing":
        log.unlink()
    elif how == "garbage":
        log.write_text("not a reflog\n")
    else:  # only entries after the start, with a different sha than the start entry
        line = f"{ZERO} {w.g(['rev-parse', 'HEAD'], w.wt)} A <a@b> 4102444800 +0000\tx\n"
        log.write_text(line)
    d = w.st(capsys)
    assert d["checks"]["unpushed_branches"] == {
        "status": "fail",
        "details": [{"branch": None, "sha": None, "detail": "reflog_unavailable"}],
    }
    assert d["reason"] == "unpushed"


def test_reflog_unavailable_does_not_need_the_network(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.admin / "logs" / "HEAD").write_text("")
    shutil.move(w.remote, str(w.remote) + ".gone")
    d = w.st(capsys)
    assert d["checks"]["unpushed_branches"]["status"] == "fail"


# -- stash -----------------------------------------------------------------------------------


def stash_in(w: RW, cwd: Path) -> None:
    (cwd / "c1.txt").write_text("stashed edit")
    w.g(["stash", "push", "-q"], cwd)


def test_stash_made_in_this_worktree_on_unpushed_work(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    stash_in(w, w.wt)
    sha = w.g(["rev-parse", "refs/stash"], w.clone)
    d = w.st(capsys)
    assert d["checks"]["stash"] == {
        "status": "fail",
        "details": [{"ref": "stash@{0}", "sha": sha}],
    }
    assert d["reason"] == "dirty"


def test_stash_on_pushed_work_passes(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    stash_in(w, w.wt)  # first parent is HEAD = a pushed commit
    assert w.st(capsys)["checks"]["stash"]["status"] == "pass"


def test_stash_recorded_at_create_is_not_counted(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    stash_in(w, w.wt)
    sha = w.g(["rev-parse", "refs/stash"], w.clone)
    assert w.st(capsys)["checks"]["stash"]["status"] == "fail"
    w.db_exec("INSERT INTO stash_ids(record_id, oid) VALUES (?, ?)", w.rid, sha)
    assert w.st(capsys)["checks"]["stash"]["status"] == "pass"


def test_stash_of_unrelated_history_is_not_counted(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.commit("clone-only", w.clone)  # on clone main, not an ancestor of the worktree's HEAD
    stash_in(w, w.clone)
    assert w.st(capsys)["checks"]["stash"]["status"] == "pass"


# -- the mirror is offline ---------------------------------------------------------------------


def go_offline(w: RW) -> None:
    shutil.move(w.remote, str(w.remote) + ".gone")


def test_offline_fetch_failure_skips_the_tip_checks_and_is_unknown(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    go_offline(w)
    d = w.st(capsys)
    assert d["checks"]["fetch"]["status"] == "fail"
    assert d["checks"]["fetch"]["details"][0]["code"] == "fetch_failed"
    for n in ("unpushed_head", "unpushed_branches"):
        assert d["checks"][n] == {"status": "skipped", "details": []}
    assert (d["would_remove"], d["reason"]) == (None, None)
    assert "fetch" in d["force_overrides"]


def test_offline_with_a_dirty_tree_is_false_dirty(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "x.txt").write_text("x")
    go_offline(w)
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (False, "dirty")


def test_offline_is_never_true_even_for_a_clean_tree(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    go_offline(w)
    assert w.st(capsys)["would_remove"] is None


def test_offline_stash_candidate_is_skipped_not_failed(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    stash_in(w, w.wt)
    go_offline(w)
    assert w.st(capsys)["checks"]["stash"] == {"status": "skipped", "details": []}


def test_a_held_clone_lease_skips_the_fetch_without_busy(
    w: RW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    monkeypatch.setattr(removal_status, "FETCH_LEASE_WAIT", 0.2)
    holder = State(w.db, __import__("wtctl.gateway", fromlist=["Gateway"]).Gateway())
    lease = holder.lease(clone_lease(str(w.clone)), wait=1)
    try:
        d = w.st(capsys)
    finally:
        lease.release()
        holder.close()
    assert d["checks"]["fetch"] == {"status": "skipped", "details": []}
    assert d["would_remove"] is None
    assert any("lease" in x for x in d["warnings"])


def test_status_does_not_touch_the_worktree_index(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["update-index", "--refresh"], w.wt)
    idx = w.admin / "index"
    before = (idx.read_bytes(), idx.stat().st_mtime_ns)
    w.st(capsys)
    assert (idx.read_bytes(), idx.stat().st_mtime_ns) == before
    assert os.listdir(w.wt) and not (w.admin / "index.lock").exists()
