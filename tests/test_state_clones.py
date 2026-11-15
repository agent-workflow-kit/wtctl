"""The ``clones`` table: first recording of a repo's clone identity (docs/interface.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.statehelpers import make_state
from wtctl.errors import WtctlError
from wtctl.state import Conflict

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


def test_put_clone_does_not_take_mirror_columns(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    for col in ("mirror_path", "default_branch"):
        with pytest.raises(ValueError):
            st.put_clone(REPO, path="/a", **{col: "x"})


def test_set_mirror_writes_only_its_columns_and_leaves_verified_at(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    st.put_clone(REPO, path="/a")
    before = st.get_clone(REPO)
    assert before is not None
    st.set_mirror(REPO, mirror_path="/m", default_branch="main")
    after = st.get_clone(REPO)
    assert after is not None
    assert (after["mirror_path"], after["default_branch"]) == ("/m", "main")
    assert after["verified_at"] == before["verified_at"] and after["path"] == "/a"
    st.set_mirror(REPO, default_branch=None)
    assert st.get_clone(REPO)["default_branch"] is None  # type: ignore[index]


@pytest.mark.parametrize("fields", [{"path": "/x"}, {"mirror_path": "/m", "verified_at": "x"}, {}])
def test_set_mirror_refuses_unknown_or_no_fields(tmp_path: Path, fields: dict) -> None:
    st = make_state(tmp_path)
    st.put_clone(REPO, path="/a")
    with pytest.raises(ValueError):
        st.set_mirror(REPO, **fields)


def test_set_mirror_needs_a_row(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    with pytest.raises(WtctlError) as exc:
        st.set_mirror(REPO, mirror_path="/m")
    assert exc.value.code == "internal"


def test_set_mirror_refuses_a_path_another_repo_stores(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    st.put_clone(REPO, path="/a")
    st.put_clone("example.test/org/other", path="/b")
    st.set_mirror(REPO, mirror_path="/m")
    with pytest.raises(Conflict):
        st.set_mirror("example.test/org/other", mirror_path="/m")
    other = st.get_clone("example.test/org/other")
    assert other is not None and other["mirror_path"] in (None, "")
    st.set_mirror(REPO, mirror_path="/m")  # its own path again is fine
