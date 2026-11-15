"""The ``clones`` table: first recording of a repo's clone identity (docs/interface.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.statehelpers import make_state

REPO = "example.test/org/repo"


def test_the_first_recording_wins_and_the_loser_changes_nothing(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    winner = st.record_first_clone(
        REPO, path="/a", common_dir="/a/.git", common_id="1:10", source="discovered"
    )
    assert winner["common_id"] == "1:10" and winner["path"] == "/a"
    # The loser read "nothing recorded" before the winner wrote; its call must not overwrite.
    loser = st.record_first_clone(
        REPO, path="/b", common_dir="/b/.git", common_id="1:20", source="discovered"
    )
    assert loser["common_id"] == "1:10" and loser["path"] == "/a"
    stored = st.get_clone(REPO)
    assert stored is not None and (stored["common_id"], stored["path"]) == ("1:10", "/a")


def test_a_row_without_an_identity_is_filled_in_once(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    st.put_clone(REPO, path="/old")
    row = st.record_first_clone(REPO, path="/a", common_dir="/a/.git", common_id="1:10")
    assert (row["common_id"], row["path"]) == ("1:10", "/a")
    again = st.record_first_clone(REPO, path="/c", common_id="1:30")
    assert (again["common_id"], again["path"]) == ("1:10", "/a")


def test_the_first_recording_needs_a_path_and_an_identity(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    with pytest.raises(ValueError):
        st.record_first_clone(REPO, path="/a")
    with pytest.raises(ValueError):
        st.record_first_clone(REPO, common_id="1:1")
    with pytest.raises(ValueError):
        st.record_first_clone(REPO, path="/a", common_id="1:1", mirror_path="/m")
