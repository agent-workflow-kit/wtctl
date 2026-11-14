"""Ids, records, compare-and-set transitions, addressing and labels."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.statehelpers import FakeClock, make_state, rec
from wtctl.errors import UsageError, WtctlError
from wtctl.state import Conflict, State

ID_RE = re.compile(r"^wt-[a-z2-7]{8}$")


@pytest.fixture
def st(tmp_path: Path) -> State:
    return make_state(tmp_path)


def put(st: State, id_: str, **kw: object) -> dict:
    st.reserve_id(id_)  # test hook: pre-burn a chosen id
    return st.insert_record(rec(id_, **kw))


# --- ids ----------------------------------------------------------------------------------


def test_ids_match_grammar_and_are_unique(st: State) -> None:
    ids = [st.allocate_id() for _ in range(300)]
    assert all(ID_RE.fullmatch(i) for i in ids)
    assert len(set(ids)) == 300


def test_allocation_retries_on_collision(st: State, monkeypatch: pytest.MonkeyPatch) -> None:
    first = st.allocate_id()
    seq = iter([first, first, "wt-aaaaaaaa"])
    monkeypatch.setattr("wtctl.state._random_id", lambda: next(seq))
    assert st.allocate_id() == "wt-aaaaaaaa"


def test_a_burned_id_is_never_reused_even_after_its_tombstone_is_gone(
    st: State, monkeypatch: pytest.MonkeyPatch
) -> None:
    burned = st.allocate_id()  # e.g. a vetoed create: never became a record
    seq = iter([burned, "wt-bbbbbbbb"])
    monkeypatch.setattr("wtctl.state._random_id", lambda: next(seq))
    assert st.allocate_id() == "wt-bbbbbbbb"


def test_tombstoned_record_id_is_never_reused(st: State, monkeypatch: pytest.MonkeyPatch) -> None:
    i = st.allocate_id()
    st.insert_record(rec(i))
    st.transition(i, "active", "removed")
    seq = iter([i, "wt-cccccccc"])
    monkeypatch.setattr("wtctl.state._random_id", lambda: next(seq))
    assert st.allocate_id() == "wt-cccccccc"


def test_insert_requires_an_allocated_id(st: State) -> None:
    with pytest.raises(ValueError, match="allocate"):
        st.insert_record(rec("wt-aaaaaaaa"))


def test_insert_rejects_bad_id_unknown_column_and_bad_enum(st: State) -> None:
    st.reserve_id("wt-aaaaaaaa")
    for bad in (
        rec("wt-AAAAAAAA"),
        rec("wt-aaaaaaa1"),  # '1' is not base32
        rec("wt-aaaaaaaa", nonsense=1),
        rec("wt-aaaaaaaa", state="exploded"),
        rec("wt-aaaaaaaa", object_type="thing"),
        rec("wt-aaaaaaaa", **{"state; drop table records; --": 1}),
    ):
        with pytest.raises((ValueError, WtctlError, Conflict)):
            st.insert_record(bad)
    assert st.get_record("wt-aaaaaaaa") is None


def test_record_roundtrip_with_types(st: State) -> None:
    i = st.allocate_id()
    r = st.insert_record(
        rec(i, owned=False, created_branch=None, depth=0, common_id="1:2"),
        labels=["b=2", "a=1"],
        holders=["agent#1"],
    )
    got = st.get_record(i)
    assert got == r
    assert got is not None
    assert got["owned"] is False and got["created_branch"] is None and got["depth"] == 0
    assert got["labels"] == ["a=1", "b=2"] and got["holders"] == ["agent#1"]
    assert got["created"] == got["updated"] == "2026-01-01T00:00:00Z"
    assert got["removed_at"] is None


def test_live_path_and_name_are_unique_but_tombstones_free_them(st: State) -> None:
    a = put(st, "wt-aaaaaaaa", path="/w/x", name="x")
    st.reserve_id("wt-bbbbbbbb")
    with pytest.raises(Conflict) as e:
        st.insert_record(rec("wt-bbbbbbbb", path="/w/x", name="y"))
    assert e.value.column == "path"
    with pytest.raises(Conflict) as e2:
        st.insert_record(rec("wt-bbbbbbbb", path="/w/y", name="x"))
    assert e2.value.column == "name"
    st.transition(a["id"], "active", "removed")
    st.insert_record(rec("wt-bbbbbbbb", path="/w/x", name="x"))


def test_same_name_in_another_repo_and_throwaways_are_fine(st: State) -> None:
    put(st, "wt-aaaaaaaa", name="x")
    put(st, "wt-bbbbbbbb", name="x", repo="example.com/o/other")
    put(st, "wt-cccccccc", object_type="throwaway", name=None, owned=None)
    put(st, "wt-dddddddd", object_type="throwaway", name=None, owned=None)


def test_failed_insert_leaves_no_partial_rows(st: State) -> None:
    put(st, "wt-aaaaaaaa", path="/w/x")
    st.reserve_id("wt-bbbbbbbb")
    with pytest.raises(Conflict):
        st.insert_record(rec("wt-bbbbbbbb", path="/w/x"), labels=["a=1"], holders=["h"])
    assert st.get_record("wt-bbbbbbbb") is None
    assert st.label_rows("wt-bbbbbbbb") == []


def test_insert_validates_labels_and_holders(st: State) -> None:
    st.reserve_id("wt-aaaaaaaa")
    with pytest.raises(UsageError):
        st.insert_record(rec("wt-aaaaaaaa"), labels=["BAD"])
    with pytest.raises(UsageError):
        st.insert_record(rec("wt-aaaaaaaa"), labels=[f"k{i}=v" for i in range(33)])
    with pytest.raises(UsageError):
        st.insert_record(rec("wt-aaaaaaaa"), holders=["bad holder"])
    assert st.get_record("wt-aaaaaaaa") is None


# --- compare-and-set ------------------------------------------------------------------------


def test_transition_updates_state_fields_and_timestamp(st: State) -> None:
    clock: FakeClock = st.gateway.clock  # type: ignore[assignment]
    put(st, "wt-aaaaaaaa", state="creating")
    clock.advance(5)
    r = st.transition("wt-aaaaaaaa", "creating", "active", head_at_create="a" * 40)
    assert r["state"] == "active" and r["head_at_create"] == "a" * 40
    assert r["updated"] == "2026-01-01T00:00:05Z" and r["created"] == "2026-01-01T00:00:00Z"


def test_transition_from_any_of_several_expected_states(st: State) -> None:
    put(st, "wt-aaaaaaaa", state="unlinked")
    assert st.transition("wt-aaaaaaaa", {"active", "unlinked"}, "active")["state"] == "active"


def test_losing_the_race_is_busy_and_changes_nothing(st: State) -> None:
    put(st, "wt-aaaaaaaa", state="active")
    st.transition("wt-aaaaaaaa", "active", "partial", last_error="x")
    with pytest.raises(WtctlError) as e:
        st.transition("wt-aaaaaaaa", "active", "removed")
    assert e.value.code == "busy" and e.value.exit_code == 12
    got = st.get_record("wt-aaaaaaaa")
    assert got is not None and got["state"] == "partial" and got["last_error"] == "x"


def test_losing_to_a_tombstone_is_not_found_with_details(st: State) -> None:
    put(st, "wt-aaaaaaaa")
    st.transition("wt-aaaaaaaa", "active", "removed")
    with pytest.raises(WtctlError) as e:
        st.transition("wt-aaaaaaaa", "active", "partial")
    assert e.value.code == "not_found" and e.value.exit_code == 10
    d = e.value.fields["details"]
    assert d["state"] == "removed" and d["removed_at"] == "2026-01-01T00:00:00Z"


def test_unknown_record_is_not_found(st: State) -> None:
    with pytest.raises(WtctlError) as e:
        st.transition("wt-zzzzzzzz", "active", "removed")
    assert e.value.code == "not_found"


def test_tombstone_transition_stamps_removed_at_and_is_final(st: State) -> None:
    clock: FakeClock = st.gateway.clock  # type: ignore[assignment]
    put(st, "wt-aaaaaaaa")
    clock.advance(60)
    r = st.transition("wt-aaaaaaaa", "active", "gone")
    assert r["removed_at"] == "2026-01-01T00:01:00Z"
    with pytest.raises(ValueError):
        st.transition("wt-aaaaaaaa", "removed", "active")  # expecting a tombstone is a bug
    with pytest.raises(WtctlError):
        st.transition("wt-aaaaaaaa", "active", "active")


def test_transition_rejects_unknown_column_and_state(st: State) -> None:
    put(st, "wt-aaaaaaaa")
    with pytest.raises(ValueError):
        st.transition("wt-aaaaaaaa", "active", "partial", **{"id": "wt-bbbbbbbb"})
    with pytest.raises(ValueError):
        st.transition("wt-aaaaaaaa", "active", "partial", **{"x = 1, state": "y"})
    with pytest.raises(ValueError):
        st.transition("wt-aaaaaaaa", "active", "sideways")
    with pytest.raises(ValueError):
        st.transition("wt-aaaaaaaa", "active", "partial", removed_at="2026-01-01T00:00:00Z")
    got = st.get_record("wt-aaaaaaaa")
    assert got is not None and got["state"] == "active"


# --- addressing -------------------------------------------------------------------------------


@pytest.fixture
def three(st: State) -> State:
    put(st, "wt-abcdef67", path="/w/1", name="one")
    put(st, "wt-abcd2222", path="/w/2", name="two")
    put(st, "wt-zzzz3333", path="/w/3", name="three")
    return st


def test_unique_prefix_with_or_without_wt(three: State) -> None:
    for sel in ("wt-zzzz", "zzzz", "wt-zzzz33", "zzzz3333", "wt-zzzz3333"):
        assert three.resolve(sel)["id"] == "wt-zzzz3333"


def test_short_prefix_is_usage_error(three: State) -> None:
    for sel in ("wt-abc", "abc", "wt-", "", "wt-a"):
        with pytest.raises(UsageError):
            three.resolve(sel)


def test_ambiguous_prefix_lists_candidates_with_paths(three: State) -> None:
    with pytest.raises(WtctlError) as e:
        three.resolve("wt-abcd")
    assert e.value.code == "ambiguous" and e.value.exit_code == 21
    cands = e.value.fields["candidates"]
    assert cands == [
        {"id": "wt-abcd2222", "path": "/w/2"},
        {"id": "wt-abcdef67", "path": "/w/1"},
    ]
    assert len(e.value.fields["candidates"]) == 2


def test_no_match_is_not_found(three: State) -> None:
    for sel in ("wt-qqqq", "wt-qqqqqqqq"):
        with pytest.raises(WtctlError) as e:
            three.resolve(sel)
        assert e.value.code == "not_found"


def test_prefix_never_matches_tombstones_but_full_id_does(three: State) -> None:
    three.transition("wt-zzzz3333", "active", "forgotten")
    with pytest.raises(WtctlError) as e:
        three.resolve("wt-zzzz")
    assert e.value.code == "not_found" and "details" not in e.value.fields
    with pytest.raises(WtctlError) as e2:
        three.resolve("wt-zzzz3333")
    assert e2.value.code == "not_found"
    assert e2.value.fields["details"]["state"] == "forgotten"
    assert three.resolve("wt-zzzz3333", allow_tombstone=True)["state"] == "forgotten"
    assert three.resolve("zzzz3333", allow_tombstone=True)["state"] == "forgotten"


def test_prefix_ambiguity_ignores_tombstones(three: State) -> None:
    three.transition("wt-abcd2222", "active", "removed")
    assert three.resolve("wt-abcd")["id"].startswith("wt-abcd")


def test_absolute_path_matches_live_record_through_symlinks(tmp_path: Path, st: State) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    put(st, "wt-aaaaaaaa", path=str(real))
    assert st.resolve(str(real))["id"] == "wt-aaaaaaaa"
    assert st.resolve(str(tmp_path / "link"))["id"] == "wt-aaaaaaaa"
    assert st.resolve(str(tmp_path / "link") + "/")["id"] == "wt-aaaaaaaa"


def test_path_never_matches_a_tombstone(tmp_path: Path, st: State) -> None:
    put(st, "wt-aaaaaaaa", path=str(tmp_path))
    st.transition("wt-aaaaaaaa", "active", "removed")
    with pytest.raises(WtctlError) as e:
        st.resolve(str(tmp_path), allow_tombstone=True)
    assert e.value.code == "not_found"


def test_relative_paths_and_junk_are_usage_errors(three: State) -> None:
    for sel in ("./x", "../x", "a/b", "x.y", "WT-ABCD", "wt-abcd ", "wt-abcd\n", "wt-abc1", "-x"):
        with pytest.raises(UsageError):
            three.resolve(sel)


def test_sql_wildcards_in_selector_do_not_match(three: State) -> None:
    for sel in ("wt-%%%%", "wt-____", "%%%%"):
        with pytest.raises(UsageError):
            three.resolve(sel)


# --- labels -----------------------------------------------------------------------------------


def test_add_and_remove_labels_multiset(st: State) -> None:
    put(st, "wt-aaaaaaaa")
    assert st.add_labels("wt-aaaaaaaa", ["b=1", "a=2", "a=1", "a=1"]) == ["a=1", "a=2", "b=1"]
    assert st.add_labels("wt-aaaaaaaa", ["a=1"]) == ["a=1", "a=2", "b=1"]
    assert st.remove_labels("wt-aaaaaaaa", ["a=1", "zz=9"]) == ["a=2", "b=1"]
    got = st.get_record("wt-aaaaaaaa")
    assert got is not None and got["labels"] == ["a=2", "b=1"]


def test_label_limit_is_atomic(st: State) -> None:
    put(st, "wt-aaaaaaaa")
    st.add_labels("wt-aaaaaaaa", [f"k{i}=v" for i in range(30)])
    with pytest.raises(UsageError):
        st.add_labels("wt-aaaaaaaa", ["x=1", "x=2", "x=3"])
    assert len(st.label_rows("wt-aaaaaaaa")) == 30  # nothing from the failed call
    assert len(st.add_labels("wt-aaaaaaaa", ["x=1", "x=2"])) == 32


def test_invalid_label_changes_nothing(st: State) -> None:
    put(st, "wt-aaaaaaaa")
    with pytest.raises(UsageError):
        st.add_labels("wt-aaaaaaaa", ["ok=1", "bad key=2"])
    assert st.label_rows("wt-aaaaaaaa") == []


def test_labels_on_missing_or_tombstoned_record(st: State) -> None:
    with pytest.raises(WtctlError) as e:
        st.add_labels("wt-aaaaaaaa", ["a=1"])
    assert e.value.code == "not_found"
    put(st, "wt-aaaaaaaa")
    st.transition("wt-aaaaaaaa", "active", "removed")
    with pytest.raises(WtctlError) as e2:
        st.remove_labels("wt-aaaaaaaa", ["a=1"])
    assert e2.value.code == "not_found" and e2.value.fields["details"]["state"] == "removed"


def test_labels_survive_state_transitions_and_are_stored_exactly(st: State) -> None:
    put(st, "wt-aaaaaaaa")
    st.add_labels("wt-aaaaaaaa", ["pr=github.com/o/r#42", "k="])
    st.transition("wt-aaaaaaaa", "active", "partial")
    got = st.get_record("wt-aaaaaaaa")
    assert got is not None and got["labels"] == ["k=", "pr=github.com/o/r#42"]


def test_records_listing_orders_and_filters(st: State) -> None:
    put(st, "wt-bbbbbbbb", path="/w/b", name="b")
    put(st, "wt-aaaaaaaa", path="/w/a", name="a")
    st.transition("wt-aaaaaaaa", "active", "removed")
    assert [r["id"] for r in st.records()] == ["wt-bbbbbbbb"]
    assert [r["id"] for r in st.records(states=["removed"])] == ["wt-aaaaaaaa"]
    assert [r["id"] for r in st.records(include_tombstones=True)] == ["wt-aaaaaaaa", "wt-bbbbbbbb"]
