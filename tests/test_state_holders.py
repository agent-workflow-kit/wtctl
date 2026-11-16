"""Holder reference counting in state.db: add, remove, and the forget-on-last rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.statehelpers import make_state, rec
from wtctl.errors import UsageError, WtctlError
from wtctl.state import State


@pytest.fixture
def st(tmp_path: Path) -> State:
    return make_state(tmp_path)


def put(st: State, **kw: object) -> str:
    rid = st.allocate_id()
    st.insert_record(rec(rid, **kw))
    return rid


def test_add_holder_is_idempotent_and_sorted(st: State) -> None:
    rid = put(st)
    assert st.add_holder(rid, "b", expected="active")["holders"] == ["b"]
    assert st.add_holder(rid, "a", expected="active")["holders"] == ["a", "b"]
    assert st.add_holder(rid, "a", expected="active")["holders"] == ["a", "b"]


def test_add_holder_checks_the_grammar(st: State) -> None:
    rid = put(st)
    with pytest.raises(UsageError):
        st.add_holder(rid, "bad holder", expected="active")


def test_add_holder_is_a_compare_and_set_on_the_state(st: State) -> None:
    rid = put(st)
    st.transition(rid, "active", "missing")
    with pytest.raises(WtctlError) as e:
        st.add_holder(rid, "h", expected="active")
    assert e.value.code == "busy"
    assert st.get_record(rid)["holders"] == []  # type: ignore[index]


def test_add_holder_to_a_tombstone_is_not_found(st: State) -> None:
    rid = put(st)
    st.transition(rid, "active", "forgotten")
    with pytest.raises(WtctlError) as e:
        st.add_holder(rid, "h", expected="active")
    assert e.value.code == "not_found"
    assert e.value.fields["details"]["state"] == "forgotten"


def test_remove_holder_reports_whether_it_was_present(st: State) -> None:
    rid = put(st, owned=False)
    st.add_holder(rid, "a", expected="active")
    st.add_holder(rid, "b", expected="active")
    r, removed, forgotten = st.remove_holder(rid, "a")
    assert (r["holders"], removed, forgotten) == (["b"], True, False)
    r, removed, forgotten = st.remove_holder(rid, "a")
    assert (r["holders"], removed, forgotten) == (["b"], False, False)


def test_removing_the_last_holder_of_an_adopted_record_forgets_it(st: State) -> None:
    rid = put(st, owned=False)
    st.add_holder(rid, "a", expected="active")
    r, removed, forgotten = st.remove_holder(rid, "a")
    assert (removed, forgotten) == (True, True)
    assert r["state"] == "forgotten" and r["removed_at"] is not None


def test_removing_the_last_holder_of_an_owned_record_keeps_it(st: State) -> None:
    rid = put(st, owned=True)
    st.add_holder(rid, "a", expected="active")
    r, removed, forgotten = st.remove_holder(rid, "a")
    assert (removed, forgotten, r["state"], r["holders"]) == (True, False, "active", [])


def test_a_non_holder_of_a_holderless_adopted_record_never_forgets_it(st: State) -> None:
    rid = put(st, owned=False)
    r, removed, forgotten = st.remove_holder(rid, "a")
    assert (removed, forgotten, r["state"]) == (False, False, "active")


def test_remove_holder_on_a_tombstone_is_not_found(st: State) -> None:
    rid = put(st, owned=False)
    st.add_holder(rid, "a", expected="active")
    st.remove_holder(rid, "a")
    with pytest.raises(WtctlError) as e:
        st.remove_holder(rid, "a")
    assert e.value.code == "not_found" and e.value.fields["details"]["state"] == "forgotten"


def test_the_count_never_goes_negative_under_repeated_removal(st: State) -> None:
    rid = put(st, owned=True)
    st.add_holder(rid, "a", expected="active")
    for _ in range(5):
        st.remove_holder(rid, "a")
    n = st._conn.execute("SELECT count(*) FROM holders WHERE record_id = ?", (rid,)).fetchone()[0]
    assert n == 0


def test_two_connections_race_for_the_last_holder_exactly_one_forgets(tmp_path: Path) -> None:
    a = make_state(tmp_path, pid=100, start="s100")
    b = make_state(tmp_path, pid=101, start="s101")
    rid = a.allocate_id()
    a.insert_record(rec(rid, owned=False))
    a.add_holder(rid, "h", expected="active")
    r1 = a.remove_holder(rid, "h")
    with pytest.raises(WtctlError):  # the tombstone is final
        b.remove_holder(rid, "h")
    assert r1[2] is True


def test_insert_record_takes_baseline_rows_and_stash_ids_in_one_transaction(st: State) -> None:
    rid = st.allocate_id()
    rows = [
        {
            "cls": "git",
            "path": "/p",
            "key": "k",
            "type": "config",
            "sha256": "ab",
            "anchor_id": None,
        }
    ]
    st.insert_record(rec(rid), baseline_items=rows, stash_ids=["b" * 40, "a" * 40])
    assert st.baseline_items(rid)[0]["path"] == "/p"
    assert st.stash_ids(rid) == ["a" * 40, "b" * 40]


def test_a_failed_insert_leaves_no_baseline_rows(st: State) -> None:
    rid = st.allocate_id()
    st.insert_record(rec(rid, path="/same"))
    rid2 = st.allocate_id()
    rows = [
        {"cls": "git", "path": "/p", "key": None, "type": "x", "sha256": "ab", "anchor_id": None}
    ]
    with pytest.raises(Exception):  # noqa: B017 - the unique live path
        st.insert_record(rec(rid2, path="/same"), baseline_items=rows)
    n = st._conn.execute(
        "SELECT count(*) FROM baseline_items WHERE record_id = ?", (rid2,)
    ).fetchone()[0]
    assert n == 0
