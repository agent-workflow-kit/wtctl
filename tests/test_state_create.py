"""State additions for create: deleting a creating record, worktree-root anchors, stash ids."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.statehelpers import make_state, rec
from wtctl.errors import WtctlError
from wtctl.state import State


@pytest.fixture
def st(tmp_path: Path) -> State:
    return make_state(tmp_path)


def put(st: State, id_: str, **kw: object) -> None:
    st.reserve_id(id_)
    st.insert_record(rec(id_, **kw), holders=["h1"], labels=["a=b"])


def test_delete_record_removes_a_creating_record_and_its_children(st: State) -> None:
    put(st, "wt-aaaaaaaa", state="creating")
    assert st.delete_record("wt-aaaaaaaa", expected="creating") is True
    assert st.get_record("wt-aaaaaaaa") is None
    assert st.id_allocated("wt-aaaaaaaa")  # the id stays burned
    n = st._conn.execute("SELECT count(*) FROM holders").fetchone()[0]
    assert n == 0


def test_delete_record_is_a_compare_and_set(st: State) -> None:
    put(st, "wt-aaaaaaaa", state="active")
    assert st.delete_record("wt-aaaaaaaa", expected="creating") is False
    assert st.get_record("wt-aaaaaaaa") is not None


def test_delete_record_of_nothing_is_false(st: State) -> None:
    assert st.delete_record("wt-aaaaaaaa", expected="creating") is False


def test_anchor_first_use_wins(st: State) -> None:
    assert st.anchor_identity("/r") is None
    assert st.record_anchor("/r", "1:2") == "1:2"
    assert st.record_anchor("/r", "3:4") == "1:2"  # never re-recorded by itself
    assert st.anchor_identity("/r") == "1:2"


def test_anchor_identity_must_be_dev_ino(st: State) -> None:
    with pytest.raises(ValueError):
        st.record_anchor("/r", "nope")


def test_stash_ids_replace_the_set(st: State) -> None:
    put(st, "wt-aaaaaaaa")
    st.set_stash_ids("wt-aaaaaaaa", ["a" * 40, "b" * 40])
    st.set_stash_ids("wt-aaaaaaaa", ["c" * 40])
    assert st.stash_ids("wt-aaaaaaaa") == ["c" * 40]


def test_preview_id_burns_nothing(st: State) -> None:
    i = st.preview_id()
    assert i.startswith("wt-") and not st.id_allocated(i)


def test_timestamp_matches_the_state_format(st: State) -> None:
    assert st.timestamp().endswith("Z") and len(st.timestamp()) == 24


def test_record_lookup_helpers(st: State) -> None:
    put(st, "wt-aaaaaaaa", branch="b1", path="/w/1")
    assert [r["id"] for r in st.live_records_for_repo("example.com/o/r")] == ["wt-aaaaaaaa"]
    assert st.live_records_for_repo("other/x/y") == []
    with pytest.raises(WtctlError):
        st.transition("wt-aaaaaaaa", "creating", "active")
