"""Run inside ``unshare -Urm`` by ``test_remove_review1.py`` (not collected by itself): a real
file system mounted on the worktree directory of a record being removed. Nothing is deleted and
the record stays ``active``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.removelib import RR
from tests.test_remove_delete import adopt_in_db
from wtctl import removal_delete


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RR:  # noqa: ANN001
    return RR(tmp_path, private_home)


def mount(path: Path) -> None:
    subprocess.run(["mount", "-t", "tmpfs", "none", str(path)], check=True)


def umount(path: Path) -> None:
    subprocess.run(["umount", str(path)], check=True)


def mount_after_the_checks(mp: pytest.MonkeyPatch, w: RR) -> None:
    """The mount appears after the step-2 integrity check, inside the step-9 window."""
    real = removal_delete.admin_check

    def late(*a: Any, **k: Any) -> Any:
        out = real(*a, **k)
        mount(w.wt)
        return out

    mp.setattr(removal_delete, "admin_check", late)


def assert_nothing_deleted(w: RR) -> None:
    assert w.state_of() == "active" and w.admin.is_dir()
    umount(w.wt)
    assert (w.wt / "c1.txt").exists() and (w.wt / ".git").exists()
    assert w.audit_records() == []


def test_owned_mount_root_appearing_after_the_checks(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    mount_after_the_checks(monkeypatch, w)
    err = w.refused()
    assert err["reason"] == "unlinked" and "unmount" in err["message"]
    assert_nothing_deleted(w)


def test_adopted_mount_root_appearing_after_the_checks(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    adopt_in_db(w)
    mount_after_the_checks(monkeypatch, w)
    err = w.refused(human=True)
    assert err["reason"] == "unlinked" and "unmount" in err["message"]
    assert_nothing_deleted(w)


def test_adopted_worktree_that_is_its_own_mount_is_unlinked(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    adopt_in_db(w)
    mount(w.wt)
    assert w.refused(human=True)["reason"] == "unlinked"
    assert w.state_of() == "active" and w.admin.is_dir()
    umount(w.wt)
    assert (w.wt / "c1.txt").exists()


def test_adopted_worktree_whose_parent_is_a_mount_point_is_removed(w: RR, capsys) -> None:  # noqa: ANN001
    """An adopted record has no recorded root: its parent is opened without the mount pre-check
    (it may legitimately be a mount), and the directory itself is verified by ``dir_id``."""
    w.start(capsys)
    adopt_in_db(w)
    parent = w.wt.parent
    subprocess.run(["mount", "--bind", str(parent), str(parent)], check=True)
    d = w.removed(human=True)
    assert d["record"]["state"] == "removed" and not w.wt.exists()
