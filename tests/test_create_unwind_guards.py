"""The guards of the create unwind (issue #77, review round 1): each test pins one guard, so
removing the guard makes the test fail (docs/interface.md, Create order step 15)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import CW
from tests.schemacheck import validate
from tests.test_create_baseline_unwind import bad_hook, err
from wtctl import baselinesvc, fsafe, inuse, removal_delete, repobaseline, state, unwind

pytestmark = pytest.mark.skipif(not os.path.isdir("/dev/shm"), reason="needs /dev/shm")


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    world = CW(tmp_path, private_home).build()
    if os.stat("/dev/shm").st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("/dev/shm shares the device")
    return world


@pytest.fixture
def shm() -> Iterator[Path]:
    d = Path("/dev/shm") / f"wtctl-ung-{os.getpid()}-{os.urandom(4).hex()}"
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def after_baseline(monkeypatch: pytest.MonkeyPatch, change: Any) -> None:
    """Run ``change(record_row_dict)`` right after the baseline was computed, i.e. between the
    computation and the unwind (the window in which the worktree could have changed)."""
    real = baselinesvc.record_current

    def wrapper(config, record, **kw):  # noqa: ANN001, ANN202
        result = real(config, record, **kw)
        change(record)
        return result

    monkeypatch.setattr(baselinesvc, "record_current", wrapper)


def leases(w: CW) -> list[str]:
    return [r["name"] for r in w.rows("SELECT name FROM leases")]


def force_partial(w: CW, capsys, monkeypatch: pytest.MonkeyPatch) -> dict:  # noqa: ANN001
    """A create whose unwind is refused because a process is in the worktree."""

    class Busy:
        status = "fail"
        processes = [{"pid": 1}]
        warnings: list[str] = []

    with monkeypatch.context() as m:
        m.setattr(inuse, "check_in_use", lambda *a, **k: Busy())
        return err(w.create(capsys, "--name", "one"))


# --- R15: the identities are re-verified before anything is deleted -----------------------


@pytest.mark.parametrize("what", ["dir_id", "pointer", "admin_gitdir"])
def test_a_changed_identity_before_the_unwind_deletes_nothing(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
    what: str,
) -> None:
    bad_hook(w, shm)
    before = w.refs()

    def change(rec: dict) -> None:
        if what == "dir_id":
            conn = sqlite3.connect(w.db)
            conn.execute("UPDATE records SET dir_id = '1:1' WHERE id = ?", (rec["id"],))
            conn.commit()
            conn.close()
        elif what == "pointer":
            Path(rec["path"], ".git").write_text("gitdir: /elsewhere/worktrees/x\n")
        else:
            Path(rec["admin_dir"], "gitdir").write_text("/elsewhere/.git\n")

    after_baseline(monkeypatch, change)
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["unwound"] is False
    assert "not the recorded worktree" in e["message"]
    rec = w.records()[0]
    assert rec["state"] == "partial"
    assert Path(rec["path"]).is_dir() and Path(rec["path"], ".git").exists()
    assert w.admin_entries() != []
    assert w.refs().keys() >= before.keys()  # the branch is kept too
    assert "refs/heads/wt/one" in w.refs()


# --- R16: a locked or unopenable admin entry is refused ------------------------------------


def test_a_locked_admin_entry_keeps_the_worktree(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)
    after_baseline(monkeypatch, lambda rec: Path(rec["admin_dir"], "locked").write_text("hold\n"))
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["unwound"] is False and "locked" in e["message"]
    rec = w.records()[0]
    assert rec["state"] == "partial" and Path(rec["path"], ".git").exists()
    assert "refs/heads/wt/one" in w.refs()


@pytest.mark.parametrize("shape", ["absent", "error"])
def test_an_admin_entry_that_cannot_be_opened_keeps_the_worktree(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
    shape: str,
) -> None:
    bad_hook(w, shm)
    value: Any = (
        types.SimpleNamespace(present=False, gitdir=None, locked=False)
        if shape == "absent"
        else fsafe.FsafeError("io", "forced")
    )
    monkeypatch.setattr(removal_delete, "admin_check", lambda *a, **k: value)
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["unwound"] is False and "admin entry cannot be opened" in e["message"]
    rec = w.records()[0]
    assert rec["state"] == "partial" and Path(rec["path"], ".git").exists()


# --- R17: the branch guards ----------------------------------------------------------------


def test_a_branch_whose_tip_moved_is_kept_with_a_note(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)
    c1 = w.shas["c1"]
    after_baseline(monkeypatch, lambda rec: w.g(["update-ref", "refs/heads/wt/one", c1]))
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["unwound"] is True  # the worktree is gone; only the branch stays
    assert w.refs()["refs/heads/wt/one"] == c1
    assert "branch wt/one was left" in e["message"]
    assert w.admin_entries() == []


def test_a_preexisting_upstream_key_survives_the_unwind(
    w: CW,
    capsys,
    shm: Path,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)
    b = "feature/remote-only"
    w.g(["config", "--local", f"branch.{b}.pushRemote", "keep"])
    w.g(["config", "--local", f"branch.{b}.remote", "mine"])
    e = err(w.create(capsys, "--branch", b))
    assert e["details"]["unwound"] is True
    assert f"refs/heads/{b}" not in w.refs()
    cfg = w.g(["config", "--local", "--get-regexp", f"^branch\\.{b}\\."])
    assert "pushremote keep" in cfg.lower()
    assert f"branch.{b}.remote mine" in cfg
    assert f"branch.{b}.merge" not in cfg


def test_the_created_branch_flag_guards_the_branch_on_its_own(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """A pre-existing branch survives even when a sha matching its tip is offered."""
    bad_hook(w, shm)
    tip = w.refs()["refs/heads/local-b"]
    real = unwind.unwind_worktree

    def offered(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return real(*a, **{**k, "branch_sha": tip})

    monkeypatch.setattr(unwind, "unwind_worktree", offered)
    e = err(w.create(capsys, "--branch", "local-b"))
    assert e["details"]["unwound"] is True
    assert w.refs()["refs/heads/local-b"] == tip


def test_a_missing_creation_sha_guards_the_branch_on_its_own(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """A branch this create made is still kept when its creation sha is not known."""
    bad_hook(w, shm)
    real = unwind.unwind_worktree

    def forgotten(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return real(*a, **{**k, "branch_sha": None})

    monkeypatch.setattr(unwind, "unwind_worktree", forgotten)
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["unwound"] is True
    assert "refs/heads/wt/one" in w.refs()


# --- R19: the process scanner ----------------------------------------------------------------


def test_an_unavailable_process_scanner_refuses_the_unwind(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)

    class Unavailable:
        status = "unavailable"
        processes: list = []
        warnings: list[str] = []

    monkeypatch.setattr(inuse, "check_in_use", lambda *a, **k: Unavailable())
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["unwound"] is False and "scanner is unavailable" in e["message"]
    rec = w.records()[0]
    assert rec["state"] == "partial" and Path(rec["path"], ".git").exists()


# --- R8: a busy clone lease and unexpected failures fall back to `partial` ----------------


def busy_clone_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    real = state.State.lease

    def lease(self, name, **kw):  # noqa: ANN001, ANN202
        if name.startswith("clone:"):
            raise state.WtctlError("busy", f"lease {name} is held by someone")
        return real(self, name, **kw)

    monkeypatch.setattr(state.State, "lease", lease)


def test_a_busy_clone_lease_falls_back_to_partial(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)
    after_baseline(monkeypatch, lambda rec: busy_clone_lease(monkeypatch))
    e = err(w.create(capsys, "--name", "one"))
    assert e["code"] == "create_failed" and e["details"]["unwound"] is False
    assert "clone is busy" in e["message"]
    rec = w.records()[0]
    assert rec["state"] == "partial" and Path(rec["path"], ".git").exists()
    assert leases(w) == []  # R20: nothing stays leased after a busy clone


def test_an_unexpected_failure_in_the_unwind_falls_back_to_partial(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("boom")

    monkeypatch.setattr(removal_delete, "delete_worktree", boom)
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["unwound"] is False
    assert w.records()[0]["state"] == "partial"
    assert leases(w) == []


def test_a_failure_after_the_tree_is_deleted_does_not_name_the_gone_path(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("boom")

    monkeypatch.setattr(removal_delete, "delete_admin", boom)
    e = err(w.create(capsys, "--name", "one"))
    rec = w.records()[0]
    assert e["details"]["unwound"] is False and rec["state"] == "partial"
    assert not Path(rec["path"]).exists()
    assert e["details"]["remaining"] == [rec["admin_dir"]]
    assert "the directory was removed but its admin entry was left" in e["message"]
    assert f"could not remove {rec['path']}" not in e["message"]


def test_a_failing_partial_transition_is_reported_not_raised(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)

    class Busy:
        status = "fail"
        processes = [{"pid": 1}]
        warnings: list[str] = []

    monkeypatch.setattr(inuse, "check_in_use", lambda *a, **k: Busy())
    real = state.State.transition

    def fails(self, rid, expected, new, **kw):  # noqa: ANN001, ANN202
        if new == "partial":
            raise RuntimeError("disk gone")
        return real(self, rid, expected, new, **kw)

    monkeypatch.setattr(state.State, "transition", fails)
    e = err(w.create(capsys, "--name", "one"))
    assert e["code"] == "create_failed" and "disk gone" in e["message"]
    assert w.records()[0]["state"] == "creating"  # reported, so a human sees why


# --- R9: a lost record lease -------------------------------------------------------------------


def test_a_lost_record_lease_does_not_unwind(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)
    real_init = state.Lease.__init__

    def init(self, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        real_init(self, *a, **k)
        self.lost = True

    monkeypatch.setattr(state.Lease, "__init__", init)
    called: list[int] = []
    real = unwind.unwind_worktree
    monkeypatch.setattr(
        unwind, "unwind_worktree", lambda *a, **k: called.append(1) or real(*a, **k)
    )
    r = w.create(capsys, "--name", "one")
    assert r.code != 0 and r.json()["error"]["code"] == "busy"
    assert called == []
    rec = w.records()[0]
    assert rec["state"] == "creating" and Path(rec["path"], ".git").exists()
    assert w.admin_entries() != []


# --- R20: leases ---------------------------------------------------------------------------------


def test_leases_are_released_after_success_and_after_a_refusal(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    link = bad_hook(w, shm)
    e = force_partial(w, capsys, monkeypatch)
    assert e["details"]["unwound"] is False
    assert leases(w) == []  # after the refusal fallback
    w.cli(capsys, "remove", w.records()[0]["id"], "--yes", human=True)
    e = err(w.create(capsys, "--name", "two"))
    assert e["details"]["unwound"] is True
    assert leases(w) == []  # after a clean unwind
    link.unlink()
    assert w.create(capsys, "--name", "three").code == 0
    assert leases(w) == []


# --- R1: a baseline-less partial record validates ---------------------------------------------


def test_a_baselineless_partial_record_validates_in_show_status_and_list(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)
    force_partial(w, capsys, monkeypatch)
    rec = w.records()[0]
    assert rec["state"] == "partial" and rec["baseline_digest"] is None
    shown = w.cli(capsys, "show", rec["id"])
    assert shown.code == 0, (shown.out, shown.err)
    validate("wtctl/cli/show.json", shown.json())
    status = w.cli(capsys, "status", rec["id"])
    validate("wtctl/cli/status.json", status.json())
    listed = w.cli(capsys, "list", "--state", "partial")
    assert listed.code == 0, (listed.out, listed.err)
    validate("wtctl/cli/list.json", listed.json())
    assert os.path.isdir(rec["path"])


# --- R11: the fallback message says what is true ------------------------------------------------


def test_the_message_does_not_claim_a_kept_worktree_when_the_directory_is_gone(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)
    monkeypatch.setattr(removal_delete, "delete_admin", lambda *a, **k: ("failed", ["/x/admin"]))
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["unwound"] is False
    assert "directory was removed" in e["message"] and "worktree was kept" not in e["message"]
    assert "--force" in e["message"]
