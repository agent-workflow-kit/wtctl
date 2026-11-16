"""state.db: the baseline rows are replaced together with the record's digest."""

from __future__ import annotations

from pathlib import Path

import pytest

from wtctl.errors import WtctlError
from wtctl.gateway import Gateway
from wtctl.state import State

D1, D2 = "a" * 64, "b" * 64


def item(path: str, key: str | None = None, sha: str = D1, cls: str = "git", **kw) -> dict:  # noqa: ANN003
    return {"cls": cls, "path": path, "key": key, "type": "config" if key else "file",
            "sha256": sha, "anchor_id": kw.get("anchor_id")}  # fmt: skip


@pytest.fixture
def st(tmp_path: Path) -> State:
    s = State(tmp_path / "state.db", Gateway())
    rid = s.allocate_id()
    s.insert_record({"id": rid, "object_type": "worktree", "state": "creating", "repo": "r",
                     "path": "/p", "name": "n", "owned": True})  # fmt: skip
    s.rid = rid  # type: ignore[attr-defined]
    return s


def test_activation_stores_digest_and_rows_in_one_step(st: State) -> None:
    rows = [item("/c/config", "core.bare"), item("/c/hooks"), item("x/CLAUDE.md", cls="claude")]
    rec = st.transition(st.rid, "creating", "active", baseline_digest=D1, baseline_items=rows)  # type: ignore[attr-defined]
    assert rec["baseline_digest"] == D1 and rec["state"] == "active"
    got = st.baseline_items(st.rid)  # type: ignore[attr-defined]
    assert sorted((r["cls"], r["path"], r["key"]) for r in got) == sorted(
        (r["cls"], r["path"], r["key"]) for r in rows
    )


def test_replacing_rows_drops_the_old_ones(st: State) -> None:
    st.transition(st.rid, "creating", "active", baseline_digest=D1, baseline_items=[item("/a")])  # type: ignore[attr-defined]
    st.transition(st.rid, "active", "active", baseline_digest=D2, baseline_items=[item("/b")])  # type: ignore[attr-defined]
    assert [r["path"] for r in st.baseline_items(st.rid)] == ["/b"]  # type: ignore[attr-defined]


def test_a_lost_compare_and_set_stores_no_rows(st: State) -> None:
    with pytest.raises(WtctlError):
        st.transition(st.rid, "active", "active", baseline_digest=D1, baseline_items=[item("/a")])  # type: ignore[attr-defined]
    assert st.baseline_items(st.rid) == []  # type: ignore[attr-defined]
    assert st.get_record(st.rid)["baseline_digest"] is None  # type: ignore[attr-defined]


def test_rows_go_with_the_record(st: State) -> None:
    st.transition(st.rid, "creating", "active", baseline_digest=D1, baseline_items=[item("/a")])  # type: ignore[attr-defined]
    st.transition(st.rid, "active", "gone")  # type: ignore[attr-defined]
    st.delete_record(st.rid, expected="gone")  # type: ignore[attr-defined]
    assert st.baseline_items(st.rid) == []  # type: ignore[attr-defined]


def test_rerecord_anchor_replaces_an_identity(st: State) -> None:
    assert st.record_anchor("/root", "1:2") == "1:2"
    assert st.record_anchor("/root", "3:4") == "1:2"  # never replaced implicitly
    st.rerecord_anchor("/root", "3:4")
    assert st.anchor_identity("/root") == "3:4"
