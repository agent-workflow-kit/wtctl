"""Anchored walks in a child process under a hard deadline, killed by exact PID."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from wtctl import fsafe, fsafe_child


def ident(p: Path) -> list[int]:
    st = os.stat(p)
    return [st.st_dev, st.st_ino]


BLOCK = [sys.executable, "-c", "import sys, time; sys.stdin.read(); time.sleep(600)"]


def anchor_args(p: Path, chain: list[str] | None = None) -> dict[str, object]:
    return {"anchor": {"path": str(p), "identity": ident(p)}, "chain": chain or []}


def test_walk_op_reports_counts_git_special_and_mounts(tmp_path: Path) -> None:
    r = tmp_path / "wt"
    (r / "src").mkdir(parents=True)
    (r / "src" / "a.py").write_text("x")
    (r / "sub" / ".git").mkdir(parents=True)
    (r / "sub" / ".git" / "HEAD").write_text("x")
    (r / "GIT~1").write_text("x")
    os.mkfifo(r / "fifo")
    (r / "ln").symlink_to("src")
    res = fsafe_child.run_op("walk", {**anchor_args(r), "limit": 1000, "stop_at_git": True})
    assert res.ok and not res.timed_out
    v = res.value
    assert v is not None
    assert sorted(v["dotgit"]) == [["GIT~1"], ["sub", ".git"]]
    assert sorted(v["special"]) == [[["fifo"], "fifo"], [["ln"], "symlink"]]
    assert v["mounts"] == []
    assert v["count"] == 7  # .git/HEAD is not descended into


def test_walk_op_limit_is_an_error_kind(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}").write_text("x")
    res = fsafe_child.run_op("walk", {**anchor_args(tmp_path), "limit": 3})
    assert not res.ok and res.error is not None and res.error.kind == "limit"


def test_op_with_wrong_anchor_identity_is_refused(tmp_path: Path) -> None:
    args = anchor_args(tmp_path)
    args["anchor"] = {"path": str(tmp_path), "identity": [1, 2]}
    res = fsafe_child.run_op("walk", args)
    assert not res.ok and res.error is not None and res.error.kind == "identity"


def test_delete_op_deletes_and_reports(tmp_path: Path) -> None:
    (tmp_path / "wt" / "d").mkdir(parents=True)
    (tmp_path / "wt" / "d" / "f").write_text("x")
    res = fsafe_child.run_op(
        "delete_tree",
        {**anchor_args(tmp_path), "name": "wt", "root_identity": ident(tmp_path / "wt")},
    )
    assert res.ok and res.value is not None
    assert res.value["remaining"] == [] and res.value["removed"] == 3
    assert res.value["remaining_total"] == 0
    assert not (tmp_path / "wt").exists()


def test_delete_op_wrong_dir_id_deletes_nothing(tmp_path: Path) -> None:
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / "f").write_text("x")
    res = fsafe_child.run_op(
        "delete_tree", {**anchor_args(tmp_path), "name": "wt", "root_identity": [1, 2]}
    )
    assert not res.ok and res.error is not None and res.error.kind == "identity"
    assert (tmp_path / "wt" / "f").exists()


def test_unknown_op_and_bad_request_are_errors(tmp_path: Path) -> None:
    res = fsafe_child.run_op("rm_rf_slash", {})
    assert not res.ok and res.error is not None and res.error.kind == "bad_request"
    res2 = fsafe_child.run_op("walk", {"anchor": {"path": "relative", "identity": [1, 2]}})
    assert not res2.ok and res2.error is not None and res2.error.kind == "name"


def test_walk_past_its_deadline_is_killed_by_exact_pid_and_reported(tmp_path: Path) -> None:
    pids: list[int] = []
    res = fsafe_child.run_op(
        "walk", anchor_args(tmp_path), timeout=0.5, argv=BLOCK, on_start=pids.append
    )
    assert res.timed_out and not res.ok
    assert res.pid is not None and pids == [res.pid]
    with pytest.raises(ProcessLookupError):
        os.kill(res.pid, 0)


def test_budget_runs_out_and_then_nothing_is_started(tmp_path: Path) -> None:
    budget = fsafe_child.Budget(total=300.0)
    assert budget.remaining() == 300.0
    budget.charge(290.0)
    assert budget.remaining() == 10.0
    budget.charge(20.0)
    assert budget.remaining() == 0.0
    spawned: list[int] = []
    res = fsafe_child.run_op(
        "walk", anchor_args(tmp_path), budget=budget, argv=BLOCK, on_start=spawned.append
    )
    assert res.timed_out and spawned == []


def test_each_walk_is_charged_its_elapsed_time(tmp_path: Path) -> None:
    ticks = [0.0]

    def clock() -> float:
        ticks[0] += 7.0  # every read advances time; run_op reads it once before and once after
        return ticks[0]

    budget = fsafe_child.Budget(total=300.0, clock=clock)
    res = fsafe_child.run_op("walk", anchor_args(tmp_path), budget=budget)
    assert res.ok
    assert budget.remaining() == 293.0


def test_budget_limits_the_per_walk_deadline(tmp_path: Path) -> None:
    budget = fsafe_child.Budget(total=300.0)
    budget.charge(299.6)
    res = fsafe_child.run_op("walk", anchor_args(tmp_path), budget=budget, argv=BLOCK)
    assert res.timed_out  # killed after ~0.4 s, not the 60 s per-walk deadline


def test_default_deadlines_are_the_specified_constants() -> None:
    assert fsafe_child.WALK_DEADLINE == 60.0
    assert fsafe_child.REMOVE_DEADLINE == 300.0
    assert fsafe.DEFAULT_SCAN_LIMIT == 2_000_000


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permissions")
def test_walk_op_reports_unreadable_entries_apart_from_special(tmp_path: Path) -> None:
    (tmp_path / "locked").mkdir()
    os.chmod(tmp_path / "locked", 0o000)
    os.mkfifo(tmp_path / "fifo")
    try:
        res = fsafe_child.run_op("walk", anchor_args(tmp_path))
    finally:
        os.chmod(tmp_path / "locked", 0o755)
    assert res.ok and res.value is not None
    assert res.value["unreadable"] == [[["locked"], "unreadable", 13]]
    assert res.value["special"] == [[["fifo"], "fifo"]]


def test_walk_op_dirs_only(tmp_path: Path) -> None:
    for i in range(10):
        (tmp_path / f"f{i}").write_text("x")
    (tmp_path / "d").mkdir()
    res = fsafe_child.run_op("walk", {**anchor_args(tmp_path), "limit": 2, "dirs_only": True})
    assert (
        res.ok and res.value is not None and res.value["count"] == 2
    )  # d, and the conftest home dir
