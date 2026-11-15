"""``forget`` (docs/interface.md, forget): holders, plain forget, states, and what stays on disk."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.adoptlib import (  # noqa: F401
    adopt,
    disk_snapshot,
    err_of,
    fast,
    foreign,
    ok_adopt,
    ok_forget,
    w,
)
from tests.createlib import CW, REPO
from wtctl.gateway import Gateway
from wtctl.state import State, record_lease


def adopted(w: CW, capsys, *holders: str, name: str = "ext") -> tuple[Path, dict]:  # noqa: ANN001
    path = foreign(w, name)
    data = ok_adopt(adopt(w, capsys, path))
    for h in holders:
        data = ok_adopt(adopt(w, capsys, path, "--holder", h))
    return path, data["record"]


def forget(w: CW, capsys, target: str, *argv: str, human: bool = False):  # noqa: ANN001, ANN201
    return w.cli(capsys, "forget", target, *argv, human=human)


def state_of(w: CW, rid: str) -> str:
    return w.rows("SELECT state FROM records WHERE id = ?", rid)[0][0]


# -- --holder -----------------------------------------------------------------------------------


def test_holders_are_counted_and_the_last_one_forgets_an_adopted_record(w: CW, capsys) -> None:  # noqa: ANN001
    path, r = adopted(w, capsys, "a", "b")
    snap = disk_snapshot(w)
    one = ok_forget(forget(w, capsys, r["id"], "--holder", "a"))
    assert one["record"]["state"] == "active" and one["record"]["holders"] == ["b"]
    assert one["warnings"] == [] and one["dry_run"] is False
    last = ok_forget(forget(w, capsys, r["id"], "--holder", "b"))
    assert last["record"]["state"] == "forgotten" and last["record"]["holders"] == []
    assert last["record"]["removed_at"] is not None
    assert disk_snapshot(w) == snap  # nothing on disk was touched
    assert path.is_dir() and (path / ".git").is_file()


def test_a_non_holder_is_a_warning_and_changes_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys, "a")
    out = ok_forget(forget(w, capsys, r["id"], "--holder", "zzz"))
    assert out["record"]["state"] == "active" and out["record"]["holders"] == ["a"]
    assert len(out["warnings"]) == 1 and out["warnings"][0].startswith("not_a_holder")


def test_a_non_holder_never_forgets_a_holderless_adopted_record(w: CW, capsys) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys)
    out = ok_forget(forget(w, capsys, r["id"], "--holder", "a"))
    assert out["record"]["state"] == "active"
    assert out["warnings"][0].startswith("not_a_holder")


def test_a_retry_after_the_record_was_forgotten_is_exit_10_with_the_state(w: CW, capsys) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys, "a")
    ok_forget(forget(w, capsys, r["id"], "--holder", "a"))
    err = err_of(forget(w, capsys, r["id"], "--holder", "a"), 10)
    assert err["details"]["state"] == "forgotten"


def test_an_owned_record_is_never_forgotten_through_a_holder(w: CW, capsys) -> None:  # noqa: ANN001
    created = json.loads(w.create(capsys, "--holder", "h").out)["record"]
    out = ok_forget(forget(w, capsys, created["id"], "--holder", "h"))
    assert out["record"]["state"] == "active" and out["record"]["holders"] == []
    assert os.path.isdir(created["path"])


def test_create_no_longer_reuses_a_record_that_lost_its_last_holder(w: CW, capsys) -> None:  # noqa: ANN001
    created = json.loads(w.create(capsys, "--branch", "keep", "--holder", "h").out)["record"]
    again = json.loads(w.create(capsys, "--branch", "keep", "--holder", "h").out)
    assert again["reused"] is True
    ok_forget(forget(w, capsys, created["id"], "--holder", "h"))
    r = w.create(capsys, "--branch", "keep", "--holder", "h")
    err = err_of(r, 20, "branch_in_use")
    assert err["holder_id"] == created["id"]


def test_adopt_holder_forget_holder_round_trip_leaves_the_clone_alone(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    before = disk_snapshot(w)
    rid = ok_adopt(adopt(w, capsys, path, "--holder", "agent"))["record"]["id"]
    ok_forget(forget(w, capsys, rid, "--holder", "agent"))
    assert disk_snapshot(w) == before
    # and the path can be adopted again as a new record
    again = ok_adopt(adopt(w, capsys, path, "--holder", "agent"))
    assert again["record"]["id"] != rid and again["existing"] is False


def test_a_non_human_may_forget_a_holder_in_any_live_state(w: CW, capsys) -> None:  # noqa: ANN001
    for i, state in enumerate(["creating", "unlinked", "missing", "clone_missing", "partial"]):
        _, r = adopted(w, capsys, "h", name=f"n{i}")
        w.set_state(r["id"], state)
        out = ok_forget(forget(w, capsys, r["id"], "--holder", "h"))
        assert out["record"]["state"] == "forgotten", state


def test_dry_run_with_holder_reports_without_changing(w: CW, capsys) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys, "a")
    out = ok_forget(forget(w, capsys, r["id"], "--holder", "a", "--dry-run"))
    assert out["dry_run"] is True and out["record"]["state"] == "forgotten"
    assert state_of(w, r["id"]) == "active"
    assert w.rows("SELECT holder FROM holders")[0][0] == "a"
    owned = json.loads(w.create(capsys, "--holder", "h").out)["record"]
    out = ok_forget(forget(w, capsys, owned["id"], "--holder", "h", "--dry-run"))
    assert out["record"]["state"] == "active" and out["record"]["holders"] == []
    assert w.rows("SELECT count(*) FROM holders WHERE holder = 'h'")[0][0] == 1


def test_dry_run_with_a_non_holder_warns(w: CW, capsys) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys, "a")
    out = ok_forget(forget(w, capsys, r["id"], "--holder", "q", "--dry-run"))
    assert out["warnings"][0].startswith("not_a_holder") and out["record"]["holders"] == ["a"]


def test_a_bad_holder_is_a_usage_error(w: CW, capsys) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys, "a")
    err_of(forget(w, capsys, r["id"], "--holder", "bad holder"), 2)
    err_of(forget(w, capsys, r["id"], "--holder", ""), 2)
    assert state_of(w, r["id"]) == "active"


# -- plain forget ---------------------------------------------------------------------------------


def test_plain_forget_needs_a_human_before_anything_is_resolved(w: CW, capsys) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys, "a")
    assert err_of(forget(w, capsys, r["id"]), 13)["code"] == "confirmation_required"
    assert err_of(forget(w, capsys, "wt-zzzzzzzz"), 13)["code"] == "confirmation_required"
    assert err_of(forget(w, capsys, "x"), 13)["code"] == "confirmation_required"
    assert state_of(w, r["id"]) == "active"


def test_plain_forget_drops_the_record_whatever_its_holders(w: CW, capsys) -> None:  # noqa: ANN001
    path, r = adopted(w, capsys, "a", "b")
    snap = disk_snapshot(w)
    out = ok_forget(forget(w, capsys, r["id"], human=True))
    assert out["record"]["state"] == "forgotten" and out["warnings"] == []
    assert disk_snapshot(w) == snap and path.is_dir()
    assert w.rows("SELECT count(*) FROM records WHERE state = 'active'")[0][0] == 0


def test_plain_forget_of_an_owned_record_leaves_the_worktree_and_branch(w: CW, capsys) -> None:  # noqa: ANN001
    created = json.loads(w.create(capsys, "--holder", "h").out)["record"]
    refs = w.refs()
    out = ok_forget(forget(w, capsys, created["id"], human=True))
    assert out["record"]["state"] == "forgotten"
    assert os.path.isdir(created["path"]) and w.refs() == refs
    assert created["path"] in w.worktree_paths()


def test_plain_forget_by_path_and_by_prefix(w: CW, capsys) -> None:  # noqa: ANN001
    path, r = adopted(w, capsys)
    ok_forget(forget(w, capsys, str(path), human=True))
    _, r2 = adopted(w, capsys, name="two")
    ok_forget(forget(w, capsys, r2["id"][:7], human=True))
    assert w.rows("SELECT count(*) FROM records WHERE state = 'forgotten'")[0][0] == 2


@pytest.mark.parametrize("state", ["creating", "unlinked", "missing", "clone_missing", "partial"])
def test_plain_forget_works_in_every_live_state(w: CW, capsys, state: str) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys)
    w.set_state(r["id"], state)
    out = ok_forget(forget(w, capsys, r["id"], human=True))
    assert out["record"]["state"] == "forgotten"


@pytest.mark.parametrize("state", ["forgotten", "removed", "gone"])
def test_a_tombstone_is_exit_10_with_details(w: CW, capsys, state: str) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys)
    w.set_state(r["id"], state)
    for argv in ([], ["--holder", "a"]):
        err = err_of(forget(w, capsys, r["id"], *argv, human=True), 10)
        assert err["details"]["state"] == state


def test_an_unknown_id_and_a_bad_selector(w: CW, capsys) -> None:  # noqa: ANN001
    err_of(forget(w, capsys, "wt-zzzzzzzz", human=True), 10)
    err_of(forget(w, capsys, "wt-ab", human=True), 2)
    err_of(forget(w, capsys, "relative/path", human=True), 2)
    err_of(forget(w, capsys, str(w.tmp / "nothing"), human=True), 10)


def test_ambiguous_prefix(w: CW, capsys) -> None:  # noqa: ANN001
    st = State(w.db, Gateway())
    ids = []
    for i in range(2):
        rid = "wt-aaaa" + "ab"[i] + "aaa"
        st.reserve_id(rid)
        st.insert_record({"id": rid, "object_type": "worktree", "state": "active", "repo": REPO,
                          "path": f"/x/{i}", "name": f"n{i}", "owned": False})  # fmt: skip
        ids.append(rid)
    st.close()
    err = err_of(forget(w, capsys, "aaaa", human=True), 21)
    assert len(err["candidates"]) == 2


def seed_throwaway(w: CW) -> str:
    st = State(w.db, Gateway())
    rid = st.allocate_id()
    st.insert_record(
        {"id": rid, "object_type": "throwaway", "state": "active", "repo": REPO, "path": "/t/x"}
    )
    st.close()
    return rid


def test_throwaways_are_refused_with_and_without_a_holder(w: CW, capsys) -> None:  # noqa: ANN001
    rid = seed_throwaway(w)
    err_of(forget(w, capsys, rid, human=True), 2)
    err_of(forget(w, capsys, rid, "--holder", "h"), 2)
    assert state_of(w, rid) == "active"


def test_dry_run_of_plain_forget_changes_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys, "a")
    out = ok_forget(forget(w, capsys, r["id"], "--dry-run", human=True))
    assert out["dry_run"] is True and out["record"]["state"] == "forgotten"
    assert state_of(w, r["id"]) == "active"


def test_a_dry_run_still_needs_a_human_for_plain_forget(w: CW, capsys) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys)
    err_of(forget(w, capsys, r["id"], "--dry-run"), 13)


def test_a_live_creator_makes_forget_busy(w: CW, capsys) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys, "a")
    w.set_state(r["id"], "creating")
    st = State(w.db, Gateway())
    lease = st.lease(record_lease(r["id"]), wait=1)
    try:
        err_of(forget(w, capsys, r["id"], human=True), 12)
        err_of(forget(w, capsys, r["id"], "--holder", "a"), 12)
    finally:
        lease.release()
        st.close()
    assert state_of(w, r["id"]) == "creating"


def test_forget_does_not_need_git(w: CW, capsys, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    _, r = adopted(w, capsys, "a")
    monkeypatch.setenv("PATH", "/nonexistent")
    w.top["git_bin"] = "/nonexistent/git"
    out = ok_forget(forget(w, capsys, r["id"], "--holder", "a"))
    assert out["record"]["state"] == "forgotten"


