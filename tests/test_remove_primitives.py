"""The deletion primitives ``remove`` adds: a root entry deleted last, and the admin-entry child
operations."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wtctl import fsafe, fsafe_child
from wtctl.repobaseline import inproc


def ident(p: Path) -> list[int]:
    st = os.stat(p)
    return [st.st_dev, st.st_ino]


@pytest.fixture
def base(tmp_path: Path) -> Path:
    b = tmp_path / "base"
    b.mkdir()
    return b


def test_last_entry_is_deleted_after_every_other(base: Path) -> None:
    wt = base / "wt"
    (wt / "d").mkdir(parents=True)
    for n in ("a", "b", ".git", "d/x"):
        (wt / n).write_text("x")
    order: list[str] = []

    def hook(stage: str, path: tuple[str, ...]) -> None:
        if stage == "classified":
            order.append("/".join(path))

    with fsafe.open_anchor(str(base)) as a:
        res = fsafe.delete_tree(a, a.fd, "wt", last=".git", hook=hook)
    assert res.complete and not wt.exists()
    assert order[-1] == ".git" and sorted(order[:-1]) == ["a", "b", "d", "d/x"]


def test_last_entry_is_kept_when_another_entry_cannot_go(base: Path) -> None:
    wt = base / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: x")
    (wt / "stay").write_text("x")
    real_unlink = os.unlink

    def unlink(name: str, *, dir_fd: int | None = None) -> None:
        if name == "stay":
            raise PermissionError(1, "denied")
        real_unlink(name, dir_fd=dir_fd)

    with fsafe.open_anchor(str(base)) as a:
        mp = pytest.MonkeyPatch()
        mp.setattr(os, "unlink", unlink)
        mp.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, unlink})
        try:
            res = fsafe.delete_tree(a, a.fd, "wt", last=".git")
        finally:
            mp.undo()
    assert not res.complete
    assert (wt / ".git").exists() and (wt / "stay").exists()
    assert {r.path for r in res.remaining} == {("stay",), (".git",)}


def test_last_entry_alone_is_deleted(base: Path) -> None:
    wt = base / "wt"
    wt.mkdir()
    (wt / ".git").write_text("x")
    with fsafe.open_anchor(str(base)) as a:
        assert fsafe.delete_tree(a, a.fd, "wt", last=".git").complete
    assert not wt.exists()


@pytest.fixture
def common(tmp_path: Path) -> Path:
    c = tmp_path / "common"
    (c / "worktrees" / "w1").mkdir(parents=True)
    (c / "worktrees" / "w1" / "gitdir").write_text("/some/wt/.git\n")
    (c / "worktrees" / "w1" / "HEAD").write_text("ref: refs/heads/x\n")
    (c / "worktrees" / "w1" / "logs").mkdir()
    (c / "worktrees" / "w1" / "logs" / "HEAD").write_text("x")
    return c


def admin_args(common: Path, **extra: object) -> dict[str, object]:
    return {
        "anchor": {"path": str(common), "identity": ident(common)},
        "chain": ["worktrees"],
        "name": "w1",
        **extra,
    }


def test_admin_check_reports_the_entry(common: Path) -> None:
    r = inproc("admin_check", admin_args(common))
    assert r.ok and r.value is not None
    assert r.value == {
        "state": "present",
        "identity": ident(common / "worktrees" / "w1"),
        "locked": False,
        "gitdir": "/some/wt/.git",
    }


def test_admin_check_sees_a_lock_of_any_type(common: Path) -> None:
    os.mkfifo(common / "worktrees" / "w1" / "locked")
    r = inproc("admin_check", admin_args(common))
    assert r.value is not None and r.value["locked"] is True


def test_admin_check_absent_entry_and_absent_worktrees_dir(common: Path) -> None:
    r = inproc("admin_check", {**admin_args(common), "name": "nope"})
    assert r.value == {"state": "absent"}
    (common / "w").mkdir()
    r = inproc("admin_check", {**admin_args(common), "chain": ["missing"]})
    assert r.value == {"state": "absent"}


def test_admin_check_refuses_a_symlinked_entry(common: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "gitdir").write_text("/some/wt/.git\n")
    os.rename(common / "worktrees" / "w1", tmp_path / "moved")
    os.symlink(outside, common / "worktrees" / "w1")
    r = inproc("admin_check", admin_args(common))
    assert not r.ok and r.error is not None and r.error.kind in ("symlink", "not_dir")


def test_admin_check_refuses_a_changed_common_identity(common: Path) -> None:
    a = admin_args(common)
    a["anchor"] = {"path": str(common), "identity": [1, 1]}  # type: ignore[assignment]
    r = inproc("admin_check", a)
    assert not r.ok and r.error is not None and r.error.kind == "identity"


def test_delete_admin_deletes_the_entry_it_verified(common: Path) -> None:
    r = inproc("delete_admin", admin_args(common, expect_gitdir="/some/wt/.git"))
    assert r.ok and r.value is not None and r.value["state"] == "deleted"
    assert not (common / "worktrees" / "w1").exists() and (common / "worktrees").is_dir()


def test_delete_admin_leaves_an_entry_naming_another_path(common: Path) -> None:
    r = inproc("delete_admin", admin_args(common, expect_gitdir="/other/wt/.git"))
    assert r.value is not None and r.value["state"] == "foreign"
    assert (common / "worktrees" / "w1" / "gitdir").exists()


def test_delete_admin_of_an_absent_entry(common: Path) -> None:
    r = inproc("delete_admin", {**admin_args(common, expect_gitdir="/x"), "name": "zz"})
    assert r.value is not None and r.value["state"] == "absent"


def test_delete_admin_never_follows_a_link_inside_the_entry(common: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("p")
    os.symlink(outside, common / "worktrees" / "w1" / "link")
    r = inproc("delete_admin", admin_args(common, expect_gitdir="/some/wt/.git"))
    assert r.value is not None and r.value["state"] == "deleted"
    assert (outside / "precious").read_text() == "p"


def test_walk_lists_entries_up_to_the_limit(base: Path) -> None:
    (base / "d").mkdir()
    for i in range(5):
        (base / "d" / f"f{i}").write_text("x")
    a = {"anchor": {"path": str(base), "identity": ident(base)}, "chain": []}
    r = fsafe_child.run_op("walk", {**a, "list_limit": 3}, timeout=30)
    assert r.ok and r.value is not None
    assert r.value["count"] == 6 and len(r.value["entries"]) == 3
