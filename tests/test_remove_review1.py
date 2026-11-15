"""Findings of the first review of ``remove`` (R1 to R11): each test pins one of them.

The suite avoids asserting git's stderr wording (CI runs a newer git than a laptop does).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import Spy
from tests.removelib import RR
from tests.test_remove_delete import adopt_in_db, stuck
from wtctl import audit, fsafe, fsafe_child, removal_delete, removalfs, remover
from wtctl.gateway import ProcessInfo


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RR:  # noqa: ANN001
    return RR(tmp_path, private_home)


def make_partial(w: RR, *, drop_dotgit: bool = True) -> None:
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    if drop_dotgit:
        (w.wt / ".git").unlink()


# -- R1: a mount root at the worktree directory refuses with nothing deleted ----------------------


def delete_tree_result(remaining: list[dict[str, Any]], removed: int = 0) -> fsafe_child.OpResult:
    return fsafe_child.OpResult(
        True, value={"removed": removed, "remaining_total": len(remaining), "remaining": remaining}
    )


def test_r1_a_mount_root_at_the_worktree_directory_is_a_refusal_not_a_partial(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """``delete_tree`` reports a root it cannot open as one remaining entry with an empty path
    (nothing was deleted); that is not a partial removal."""
    w.start(capsys)
    real = removalfs.RUNNER

    def runner(op: str, args: dict[str, Any], **kw: Any) -> Any:
        if op == "delete_tree":
            return delete_tree_result([{"path": [], "reason": "mount_point", "errno": None}])
        return real(op, args, **kw)  # type: ignore[misc]

    monkeypatch.setattr(removalfs, "RUNNER", runner)
    err = w.refused()
    assert err["reason"] == "unlinked" and "unmount" in err["message"]
    assert w.state_of() == "active" and w.wt.is_dir() and w.admin.is_dir()
    assert w.audit_records() == []


def test_r1_an_empty_path_rmdir_failure_is_still_a_partial(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """Only a ``mount_point`` at the root means nothing was deleted; a root that could not be
    removed after its entries went is a partial removal."""
    w.start(capsys)
    real = removalfs.RUNNER

    def runner(op: str, args: dict[str, Any], **kw: Any) -> Any:
        if op == "delete_tree":
            return delete_tree_result([{"path": [], "reason": "not_empty", "errno": 39}], 3)
        return real(op, args, **kw)  # type: ignore[misc]

    monkeypatch.setattr(removalfs, "RUNNER", runner)
    assert w.refused(code=1)["code"] == "partial"
    assert w.state_of() == "partial"


def userns_available(tmp_path: Path) -> tuple[str, str]:
    unshare, mount = shutil.which("unshare"), shutil.which("mount")
    if not (unshare and mount):
        pytest.skip("real mount tests need unshare and mount")
    probe = tmp_path / "probe"
    probe.mkdir()
    ok = subprocess.run([unshare, "-Urm", mount, "-t", "tmpfs", "none", str(probe)], check=False)
    if ok.returncode != 0:
        pytest.skip("unprivileged user+mount namespaces are unavailable here")
    return unshare, mount


def test_r1_real_mount_at_the_worktree_root_inside_a_user_namespace(tmp_path: Path) -> None:
    """The owned race (a mount appears after step 2) and an adopted worktree that is its own mount
    root: nothing is deleted and the record stays ``active`` (``remove_mount_inner.py``)."""
    unshare, _ = userns_available(tmp_path)
    inner = Path(__file__).with_name("remove_mount_inner.py")
    root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "VIRTUAL_ENV")}
    r = subprocess.run(
        [unshare, "-Urm", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-x", str(inner)],
        capture_output=True, text=True, timeout=300, env=env, check=False, cwd=root,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]


# -- R2: a locked admin entry is never deleted when finishing a partial record ---------------------


def test_r2_finishing_a_partial_record_refuses_a_locked_admin_entry(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    make_partial(w)
    (w.admin / "locked").write_text("keep me\n")
    err = w.refused(human=True)
    assert err["reason"] == "locked" and err["details"]["lock_reason"] == "keep me\n"
    assert w.admin.is_dir() and w.wt.is_dir() and w.state_of() == "partial"
    st = w.st(capsys, human=True)
    assert (st["would_remove"], st["reason"]) == (False, "locked")
    assert st["checks"]["locked"]["status"] == "fail"


def test_r2_a_lock_that_appears_after_the_checks_refuses_before_the_record_moves(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    make_partial(w)
    real = removal_delete.admin_check

    def late_lock(*a: Any, **k: Any) -> Any:
        (w.admin / "locked").write_text("late\n")
        return real(*a, **k)

    monkeypatch.setattr(removal_delete, "admin_check", late_lock)
    err = w.refused(human=True)
    assert err["reason"] == "locked" and err["details"]["lock_reason"] == "late\n"
    assert w.admin.is_dir() and w.state_of() == "partial"


def test_r2_a_partial_record_whose_admin_entry_is_gone_has_no_lock(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    make_partial(w)
    shutil.rmtree(w.admin)
    st = w.st(capsys, human=True)
    assert st["would_remove"] is True
    assert w.removed(human=True)["record"]["state"] == "removed"


# -- R3: a missing anchor or intermediate component is not "gone"


def test_r3_a_partial_record_whose_base_dir_was_renamed_is_unlinked(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    make_partial(w)
    base = Path(w.rows("SELECT base_dir FROM records WHERE id = ?", w.rid)[0]["base_dir"])
    moved = Path(str(base) + ".moved")
    base.rename(moved)
    assert w.refused(human=True)["reason"] == "unlinked"
    assert w.admin.is_dir() and w.state_of() == "partial"
    assert (moved / w.wt.relative_to(base)).is_dir()
    st = w.st(capsys, human=True)
    assert (st["would_remove"], st["reason"]) == (False, "unlinked")


def test_r3_a_missing_intermediate_component_is_unlinked(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    make_partial(w)
    base = Path(w.rows("SELECT base_dir FROM records WHERE id = ?", w.rid)[0]["base_dir"])
    deep = base / "x" / "y" / "wt"  # the recorded path, below components that do not exist
    w.db_exec("UPDATE records SET path = ? WHERE id = ?", str(deep), w.rid)
    assert w.refused(human=True)["reason"] == "unlinked"
    assert w.admin.is_dir() and w.state_of() == "partial"


def test_r3_delete_worktree_does_not_treat_a_missing_anchor_as_gone(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    ident = os.stat(base)

    class S:
        def anchor_identity(self, path: str) -> str:
            return f"{ident.st_dev}:{ident.st_ino}"

    rec = {
        "path": str(base / "wt"), "base_dir": str(base), "owned": True,
        "dir_id": f"{ident.st_dev}:{ident.st_ino}",
    }  # fmt: skip
    out = removal_delete.delete_worktree(S(), rec, fsafe_child.Budget())
    assert out.status == "deleted"  # the final component is absent under a matching anchor
    base.rename(tmp_path / "elsewhere")
    out = removal_delete.delete_worktree(S(), rec, fsafe_child.Budget())
    assert out.status == "refused" and out.error is not None and out.error.kind == "missing"


# -- R4: the state check comes before the human gate


@pytest.mark.parametrize("state", ["creating", "unlinked", "missing", "clone_missing"])
def test_r4_a_non_human_remove_of_an_adopted_record_in_another_state_is_unlinked(
    w: RR,
    capsys,
    state: str,  # noqa: ANN001
) -> None:
    w.start(capsys)
    adopt_in_db(w)
    w.db_exec("UPDATE records SET state = ? WHERE id = ?", state, w.rid)
    assert w.refused()["reason"] == "unlinked"
    assert w.refused(human=True)["reason"] == "unlinked"
    assert w.wt.is_dir()


def test_r4_an_active_adopted_record_and_a_partial_one_still_need_a_human(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    adopt_in_db(w)
    assert w.refused(code=13)["code"] == "confirmation_required"
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    assert w.refused(code=13)["code"] == "confirmation_required"


# -- R5: deletion has its own deadline budget


def test_r5_a_spent_budget_does_not_starve_the_deletion(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    monkeypatch.setattr(removalfs, "RUNNER", None)  # the real child
    real = remover.Remover._pre_hooks

    def spend(self: remover.Remover) -> None:
        real(self)
        self.budget.charge(10_000)

    monkeypatch.setattr(remover.Remover, "_pre_hooks", spend)
    d = w.removed()
    assert d["record"]["state"] == "removed" and not w.wt.exists() and not w.admin.exists()


def test_r5_a_child_that_never_started_is_a_refusal_with_nothing_deleted(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    real = removalfs.RUNNER

    def runner(op: str, args: dict[str, Any], **kw: Any) -> Any:
        if op == "delete_tree":
            err = fsafe.FsafeError("deadline", "no time left")
            return fsafe_child.OpResult(False, error=err, timed_out=True, pid=None)
        return real(op, args, **kw)  # type: ignore[misc]

    monkeypatch.setattr(removalfs, "RUNNER", runner)
    assert w.refused()["reason"] == "unlinked"
    assert w.state_of() == "active" and w.wt.is_dir() and w.admin.is_dir()


# -- R6: an owned record without a root has no weaker anchor


class _Ident:
    def anchor_identity(self, path: str) -> str:
        return "1:2"


def test_r6_an_owned_record_without_a_base_dir_is_refused() -> None:
    rec = {"path": "/x/wt", "base_dir": None, "owned": True, "dir_id": "1:3"}
    with pytest.raises(fsafe.FsafeError) as exc:
        removal_delete.delete_args(_Ident(), rec)
    assert exc.value.kind == "identity"
    out = removal_delete.delete_worktree(_Ident(), rec, fsafe_child.Budget())
    assert out.status == "refused"


def test_r6_an_unowned_record_is_anchored_at_its_parent_whatever_its_base_dir() -> None:
    rec = {"path": "/x/wt", "base_dir": "/x", "owned": False, "dir_id": "1:3"}
    args = removal_delete.delete_args(_Ident(), rec)
    assert args["anchor"] == {"path": "/x", "identity": None, "precheck": False}
    assert (args["chain"], args["name"]) == ([], "wt")


def test_r6_an_owned_record_is_anchored_at_its_recorded_root() -> None:
    rec = {"path": "/x/a/wt", "base_dir": "/x", "owned": True, "dir_id": "1:3"}
    args = removal_delete.delete_args(_Ident(), rec)
    assert args["anchor"]["identity"] == [1, 2] and args["chain"] == ["a"]


# -- R7: the in-use check is repeated before a partial record is finished


def test_r7_a_process_that_appears_during_the_hooks_refuses_even_forced(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    make_partial(w)
    real = remover.Remover._pre_hooks

    def appear(self: remover.Remover) -> None:
        real(self)
        w.scanner.procs = [ProcessInfo(77, "vim", str(w.wt), ())]

    monkeypatch.setattr(remover.Remover, "_pre_hooks", appear)
    err = w.refused("--force", human=True)
    assert err["reason"] == "in_use" and err["processes"][0]["pid"] == 77
    assert w.wt.is_dir() and w.admin.is_dir() and w.state_of() == "partial"


# -- R8: a lost record lease stops the removal


def test_r8_a_lease_lost_before_the_deletion_changes_nothing(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    real = remover.Remover._pre_hooks

    def lose(self: remover.Remover) -> None:
        real(self)
        self.rlease.lost = True

    monkeypatch.setattr(remover.Remover, "_pre_hooks", lose)
    assert w.refused(code=12)["code"] == "busy"
    assert w.intact() and "work" in w.branches()


def test_r8_a_lease_lost_during_the_deletion_leaves_a_truthful_partial_record(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    real = removal_delete.delete_worktree
    holder: dict[str, Any] = {}
    real_pre = remover.Remover._pre_hooks

    def keep(self: remover.Remover) -> None:
        holder["r"] = self
        real_pre(self)

    def deleting(*a: Any, **k: Any) -> Any:
        out = real(*a, **k)
        holder["r"].rlease.lost = True
        return out

    monkeypatch.setattr(remover.Remover, "_pre_hooks", keep)
    monkeypatch.setattr(removal_delete, "delete_worktree", deleting)
    assert w.refused(code=12)["code"] == "busy"
    assert w.state_of() == "partial" and not w.wt.exists() and w.admin.is_dir()
    assert "work" in w.branches()


def test_r8_a_lease_lost_during_the_admin_deletion_does_not_mark_the_record_removed(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    real = removal_delete.delete_admin
    holder: dict[str, Any] = {}
    real_pre = remover.Remover._pre_hooks

    def keep(self: remover.Remover) -> None:
        holder["r"] = self
        real_pre(self)

    def deleting(*a: Any, **k: Any) -> Any:
        out = real(*a, **k)
        holder["r"].rlease.lost = True
        return out

    monkeypatch.setattr(remover.Remover, "_pre_hooks", keep)
    monkeypatch.setattr(removal_delete, "delete_admin", deleting)
    assert w.refused(code=12)["code"] == "busy"
    assert w.state_of() == "partial" and not w.admin.exists()


# -- R9: the remove audit is committed before the branch is deleted


def test_r9_the_remove_audit_is_committed_before_the_branch_is_deleted(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    seen: list[tuple[list[str], str]] = []

    def before(sub: str, argv: list[str], kw: dict[str, Any]) -> None:
        if sub == "update-ref" and "-d" in argv:
            seen.append(([f"{a['action']}:{a['result']}" for a in w.audit_records()], w.state_of()))

    w.git_runner = Spy(before=before)
    d = w.removed()
    assert d["branch_deleted"] is True
    assert seen == [(["remove:ok"], "partial")]
    assert w.state_of() == "removed"
    assert [a["action"] for a in w.audit_records()] == ["remove", "branch_delete"]


def test_r9_a_failure_in_the_branch_step_leaves_partial_with_the_remove_audit(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)

    def boom(*a: Any, **k: Any) -> bool:
        raise RuntimeError("branch step failed")

    monkeypatch.setattr(remover.worktrees, "delete_created_branch", boom)
    assert w.rm().code == 1
    assert w.state_of() == "partial"
    assert [f"{a['action']}:{a['result']}" for a in w.audit_records()] == ["remove:ok"]


# -- round 2 F1: audit append failures after the deletion are warnings, not lies ------------------


def fail_audit_append(mp: pytest.MonkeyPatch, nth: int) -> None:
    real = audit.Journal.commit_audit
    calls = {"n": 0}

    def commit(self: Any, prepared: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == nth:
            raise audit.JournalIOError("disk full")
        return real(self, prepared)

    mp.setattr(audit.Journal, "commit_audit", commit)


def last_error(w: RR) -> str | None:
    return w.rows("SELECT last_error FROM records WHERE id = ?", w.rid)[0]["last_error"]


def test_f1_a_failed_remove_audit_append_is_a_warning_and_the_removal_finishes(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    fail_audit_append(monkeypatch, 1)
    d = w.removed()
    assert d["branch_deleted"] is True
    assert any("audit record not written" in x for x in d["warnings"])
    assert w.state_of() == "removed" and last_error(w) is None
    assert not w.wt.exists() and not w.admin.exists()
    assert [a["action"] for a in w.audit_records()] == ["branch_delete"]


def test_f1_a_failed_branch_audit_append_is_a_warning_and_the_removal_finishes(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    fail_audit_append(monkeypatch, 2)
    d = w.removed()
    assert d["branch_deleted"] is True and d["branch_kept_reason"] is None
    assert any("audit record not written" in x for x in d["warnings"])
    assert w.state_of() == "removed"
    assert [a["action"] for a in w.audit_records()] == ["remove"]


def test_f1_a_failed_final_flip_leaves_partial_with_a_truthful_last_error(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    from wtctl import state as state_mod
    from wtctl.errors import WtctlError

    real = state_mod.State.transition

    def transition(self: Any, rid: str, expected: Any, new: str, **kw: Any) -> Any:
        if new == "removed":
            raise WtctlError("busy", "lost the race")
        return real(self, rid, expected, new, **kw)

    monkeypatch.setattr(state_mod.State, "transition", transition)
    r = w.rm()
    assert r.code == 12
    assert w.state_of() == "partial"
    le = last_error(w) or ""
    assert "in progress" not in le and "deleted" in le
    assert not w.wt.exists() and not w.admin.exists()
    assert [a["action"] for a in w.audit_records()] == ["remove", "branch_delete"]


# -- R10: an audit failure while recording a partial removal keeps the partial error


def test_r10_a_failed_audit_write_does_not_hide_the_partial_error(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    p = w.wt / "node_modules" / "stuck.txt"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    stuck(monkeypatch, "stuck.txt")

    def boom(self: Any, prepared: Any) -> Any:
        raise audit.JournalIOError("disk full")

    monkeypatch.setattr(audit.Journal, "commit_audit", boom)
    r = w.rm()
    assert r.code == 1
    err = r.json()["error"]
    assert err["code"] == "partial"
    assert err["details"]["remaining_path"] == str(w.wt)
    assert f"{w.wt}/node_modules/stuck.txt" in err["details"]["remaining"]
    assert "audit" in err["message"] and "disk full" in err["message"]
    assert w.state_of() == "partial"


# -- R11: nothing reads the dead flag --------------------------------------------------------------


def test_r11_tree_outcome_has_no_gone_flag() -> None:
    assert "gone" not in removal_delete.TreeOutcome.__dataclass_fields__
