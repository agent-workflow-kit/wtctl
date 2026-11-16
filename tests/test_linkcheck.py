"""Integrity: a record still names the worktree and admin entry it was made for."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tests.createlib import CW
from tests.test_create import ok
from wtctl import linkcheck
from wtctl.errors import WtctlError
from wtctl.gateway import Gateway
from wtctl.state import Record, State


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def made(w: CW, capsys) -> str:  # noqa: ANN001
    return ok(w.create(capsys))["record"]["id"]


def rec(w: CW, rid: str) -> tuple[Record, State]:
    st = State(w.db, Gateway())
    got = st.get_record(rid)
    assert got is not None
    return got, st


def verify(w: CW, rid: str, **kw):  # noqa: ANN003, ANN201
    r, st = rec(w, rid)
    try:
        return linkcheck.verify_links(r, st, **kw)
    finally:
        st.close()


def unlinked(w: CW, rid: str, **kw) -> WtctlError:  # noqa: ANN003
    with pytest.raises(WtctlError) as ei:
        verify(w, rid, **kw)
    assert ei.value.fields["reason"] == "unlinked" and ei.value.code == "refused"
    return ei.value


def set_col(w: CW, rid: str, **cols: object) -> None:
    conn = sqlite3.connect(w.db)
    for k, v in cols.items():
        conn.execute(f"UPDATE records SET {k} = ? WHERE id = ?", (v, rid))
    conn.commit()
    conn.close()


def test_a_pristine_record_passes_and_reports_current_identities(w: CW, capsys) -> None:  # noqa: ANN001
    rid = made(w, capsys)
    links = verify(w, rid)
    r, st = rec(w, rid)
    st.close()
    assert links.common_id == r["common_id"] and links.dir_id == r["dir_id"]
    assert links.root is not None and links.root[1] == links.root[2]
    assert links.common_dir == r["common_dir"]


def test_dotgit_file_naming_another_admin_dir_is_unlinked(w: CW, capsys) -> None:  # noqa: ANN001
    rid = made(w, capsys)
    r, st = rec(w, rid)
    st.close()
    Path(r["path"], ".git").write_text(f"gitdir: {w.clone}/.git/worktrees/other\n")
    unlinked(w, rid)


def test_dotgit_that_is_a_directory_or_symlink_is_unlinked(w: CW, capsys) -> None:  # noqa: ANN001
    rid = made(w, capsys)
    r, st = rec(w, rid)
    st.close()
    dotgit = Path(r["path"], ".git")
    saved = dotgit.read_text()
    dotgit.unlink()
    dotgit.mkdir()
    unlinked(w, rid)
    dotgit.rmdir()
    (w.tmp / "elsewhere").write_text(saved)
    os.symlink(w.tmp / "elsewhere", dotgit)
    unlinked(w, rid)


def test_huge_dotgit_file_is_refused_not_read(w: CW, capsys) -> None:  # noqa: ANN001
    rid = made(w, capsys)
    r, st = rec(w, rid)
    st.close()
    Path(r["path"], ".git").write_text("gitdir: " + "x" * 2_000_000)
    unlinked(w, rid)


def test_admin_gitdir_and_commondir_must_match(w: CW, capsys) -> None:  # noqa: ANN001
    rid = made(w, capsys)
    r, st = rec(w, rid)
    st.close()
    admin = Path(r["admin_dir"])
    good = (admin / "gitdir").read_text()
    (admin / "gitdir").write_text("/somewhere/else/.git\n")
    unlinked(w, rid)
    (admin / "gitdir").write_text(good)
    verify(w, rid)
    cd = (admin / "commondir").read_text()
    (admin / "commondir").write_text("../../../other\n")
    unlinked(w, rid)
    (admin / "commondir").write_text(cd)
    verify(w, rid)


def test_worktree_directory_replaced_or_missing_is_unlinked(w: CW, capsys) -> None:  # noqa: ANN001
    rid = made(w, capsys)
    r, st = rec(w, rid)
    st.close()
    p = Path(r["path"])
    saved = p.with_name("saved-wt")
    p.rename(saved)
    unlinked(w, rid)  # missing
    p.mkdir()
    (p / ".git").write_text((saved / ".git").read_text())
    unlinked(w, rid)  # a different directory (identity) with the right pointer


def test_symlink_component_below_the_root_is_unlinked(w: CW, capsys) -> None:  # noqa: ANN001
    rid = made(w, capsys)
    r, st = rec(w, rid)
    st.close()
    p = Path(r["path"])
    moved = w.tmp / "moved"
    p.rename(moved)
    os.symlink(moved, p)
    unlinked(w, rid)


def test_changed_recorded_identities_refuse_unless_rebinding(w: CW, capsys) -> None:  # noqa: ANN001
    rid = made(w, capsys)
    set_col(w, rid, dir_id="1:2")
    unlinked(w, rid)
    links = verify(w, rid, rebind=True)
    assert links.dir_id != "1:2"
    r, st = rec(w, rid)
    st.close()
    set_col(w, rid, dir_id=links.dir_id, common_id="1:2")
    unlinked(w, rid)
    assert verify(w, rid, rebind=True).common_id != "1:2"
    set_col(w, rid, common_id=links.common_id)
    conn = sqlite3.connect(w.db)
    conn.execute("UPDATE anchors SET dev_ino = '1:2' WHERE path = ?", (r["base_dir"],))
    conn.commit()
    conn.close()
    unlinked(w, rid)
    links = verify(w, rid, rebind=True)
    assert links.root is not None and links.root[1] == "1:2" and links.root[2] != "1:2"


def test_rebinding_still_requires_the_pointers_to_agree(w: CW, capsys) -> None:  # noqa: ANN001
    rid = made(w, capsys)
    r, st = rec(w, rid)
    st.close()
    (Path(r["admin_dir"]) / "gitdir").write_text("/x/.git\n")
    unlinked(w, rid, rebind=True)


def test_adopted_main_worktree(w: CW) -> None:
    st = State(w.db, Gateway())
    rid = st.allocate_id()
    cst = os.stat(w.clone / ".git")
    st.insert_record(
        {"id": rid, "object_type": "worktree", "state": "active", "repo": "r", "owned": False,
         "path": str(w.clone), "name": "main", "clone_path": str(w.clone),
         "common_dir": str(w.clone / ".git"), "common_id": f"{cst.st_dev}:{cst.st_ino}",
         "dir_id": f"{os.stat(w.clone).st_dev}:{os.stat(w.clone).st_ino}"}
    )  # fmt: skip
    r = st.get_record(rid)
    assert r is not None
    links = linkcheck.verify_links(r, st)
    assert links.root is None
    st.close()
    subprocess.run(["mv", str(w.clone / ".git"), str(w.tmp / "g")], check=True)
    os.symlink(w.tmp / "g", w.clone / ".git")
    st = State(w.db, Gateway())
    with pytest.raises(WtctlError):
        linkcheck.verify_links(st.get_record(rid) or {}, st)
    st.close()


def test_a_worktree_directory_named_with_leading_dots_is_not_outside_its_root(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    rid = made(w, capsys)
    r, st = rec(w, rid)
    st.close()
    p = Path(r["path"])
    moved = Path(r["base_dir"]) / "..odd"
    p.rename(moved)
    (Path(r["admin_dir"]) / "gitdir").write_text(f"{moved}/.git\n")
    set_col(w, rid, path=str(moved))
    assert verify(w, rid).dir_id == r["dir_id"]
