"""The bootstrap-created sets, the step runs and the bootstrap state in state.db (issue #16)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.statehelpers import make_state, rec
from wtctl.errors import WtctlError
from wtctl.state import State

RID = "wt-aaaaaaaa"


@pytest.fixture
def st(tmp_path: Path) -> State:
    s = make_state(tmp_path)
    s.reserve_id(RID)
    s.insert_record(rec(RID, state="creating"))
    return s


def step(argv: list[str], exit_: int | None = 0, tail: str = "") -> dict:
    return {"argv": argv, "exit": exit_, "duration_ms": 5, "output_tail": tail}


def test_a_new_record_has_no_bootstrap(st: State) -> None:
    assert st.bootstrap_summary(RID) == {"state": "none", "steps": [], "created_paths_count": 0}
    assert st.bootstrap_paths(RID) == []
    assert st.bootstrap_nested(RID) == []


def test_record_bootstrap_stores_state_steps_paths_and_nested_together(st: State) -> None:
    got = st.record_bootstrap(
        RID,
        expected="creating",
        state="ok",
        steps=[step(["make"], 0, "built\n")],
        paths=[("a.txt", "untracked", "ab" * 32), ("node_modules", "ignored_dir", None)],
        nested=["node_modules/dep/.git"],
    )
    assert got["bootstrap_state"] == "ok"
    assert st.bootstrap_summary(RID) == {
        "state": "ok",
        "steps": [step(["make"], 0, "built\n")],
        "created_paths_count": 2,
    }
    assert st.bootstrap_paths(RID) == [
        ("a.txt", "untracked", "ab" * 32),
        ("node_modules", "ignored_dir", None),
    ]
    assert st.bootstrap_nested(RID) == ["node_modules/dep/.git"]


def test_a_second_run_replaces_the_steps_and_adds_to_the_sets(st: State) -> None:
    st.record_bootstrap(
        RID, expected="creating", state="failed",
        steps=[step(["a"], 1, "x"), ], paths=[("p1", "untracked", "d1")], nested=["n1/.git"],
    )  # fmt: skip
    st.record_bootstrap(
        RID, expected="creating", state="ok",
        steps=[step(["b"], 0), step(["c"], 0)], paths=[("p2", "ignored_file", None)],
        nested=["n2/.git"],
    )  # fmt: skip
    summary = st.bootstrap_summary(RID)
    assert [s["argv"] for s in summary["steps"]] == [["b"], ["c"]]
    assert summary["created_paths_count"] == 2
    assert st.bootstrap_nested(RID) == ["n1/.git", "n2/.git"]


def test_a_path_recorded_twice_keeps_the_newest_hash(st: State) -> None:
    st.record_bootstrap(RID, expected="creating", state="ok", steps=[],
                        paths=[("p", "untracked", "old")], nested=[])  # fmt: skip
    st.record_bootstrap(RID, expected="creating", state="ok", steps=[],
                        paths=[("p", "untracked", "new")], nested=[])  # fmt: skip
    assert st.bootstrap_paths(RID) == [("p", "untracked", "new")]


def test_a_lost_state_race_changes_nothing(st: State) -> None:
    with pytest.raises(WtctlError) as exc:
        st.record_bootstrap(RID, expected="active", state="ok", steps=[step(["x"])],
                            paths=[("p", "untracked", "d")], nested=["n/.git"])  # fmt: skip
    assert exc.value.code == "busy"
    assert st.bootstrap_paths(RID) == []
    assert st.bootstrap_nested(RID) == []
    assert st.bootstrap_summary(RID)["steps"] == []
    record = st.get_record(RID)
    assert record is not None and record["bootstrap_state"] == "none"


def test_an_unknown_state_or_path_kind_is_a_programmer_error(st: State) -> None:
    with pytest.raises(ValueError):
        st.record_bootstrap(RID, expected="creating", state="weird", steps=[], paths=[], nested=[])
    with pytest.raises(ValueError):
        st.record_bootstrap(RID, expected="creating", state="ok", steps=[],
                            paths=[("p", "symlink", "d")], nested=[])  # fmt: skip


def test_a_path_that_cannot_be_text_is_refused_not_stored(st: State) -> None:
    with pytest.raises(ValueError):
        st.record_bootstrap(RID, expected="creating", state="ok", steps=[],
                            paths=[("bad\udcff", "untracked", "d")], nested=[])  # fmt: skip


def test_the_sets_go_with_the_record(st: State) -> None:
    st.record_bootstrap(RID, expected="creating", state="ok", steps=[step(["x"])],
                        paths=[("p", "untracked", "d")], nested=["n/.git"])  # fmt: skip
    assert st.delete_record(RID, expected="creating")
    assert st.bootstrap_paths(RID) == []
    assert st.bootstrap_nested(RID) == []
    runs = st._conn.execute("SELECT COUNT(*) FROM bootstrap_runs").fetchone()[0]
    assert runs == 0


def test_set_bootstrap_state_alone(st: State) -> None:
    st.set_bootstrap_state(RID, "interrupted", expected="creating")
    assert st.bootstrap_summary(RID)["state"] == "interrupted"


def test_a_stepless_run_clears_the_steps_of_the_previous_run(st: State) -> None:
    st.record_bootstrap(
        RID, expected="creating", state="failed", steps=[step(["x"], 1)], paths=[], nested=[]
    )
    assert len(st.bootstrap_summary(RID)["steps"]) == 1
    st.record_bootstrap(RID, expected="creating", state="ok", steps=[], paths=[], nested=[])
    assert st.bootstrap_summary(RID)["steps"] == []
    st.record_bootstrap(
        RID, expected="creating", state="ok", steps=[step(["y"])], paths=[], nested=[]
    )
    assert [s["argv"] for s in st.bootstrap_summary(RID)["steps"]] == [["y"]]
