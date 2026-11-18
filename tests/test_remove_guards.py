"""Guards of ``remove`` that the first review found untested (T1 to T15): each test fails if the
guard it names is removed. No test asserts git's stderr wording."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import Spy, script
from tests.removelib import RR
from wtctl import (
    fsafe,
    fsafe_child,
    removal_content,
    removal_delete,
    removalfs,
    remover,
    worktrees,
)
from wtctl.errors import WtctlError
from wtctl.gateway import Gateway, GitResult, ProcessInfo
from wtctl.state import State, clone_lease

GIT_ID = "-c user.name=t -c user.email=t@example.test"


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RR:  # noqa: ANN001
    return RR(tmp_path, private_home)


def mutate(w: RR, body: str) -> None:
    """A ``pre-remove`` hook that changes the world after the checks and the confirmation."""
    w.top["hooks"] = {"pre-remove": [{"cmd": script(w.tmp / "m.sh", body)}]}


def after_recheck(mp: pytest.MonkeyPatch, fn: Callable[[], object]) -> None:
    """Run ``fn`` after the step-9 re-check passed, before the admin entry is checked."""
    real = remover.Remover._plan_branch

    def wrapped(self: remover.Remover, rec: Any) -> Any:
        fn()
        return real(self, rec)

    mp.setattr(remover.Remover, "_plan_branch", wrapped)


def wrap_runner(
    mp: pytest.MonkeyPatch,
    before: Callable[[str, dict[str, Any]], Any] | None = None,
    after: Callable[[str, dict[str, Any], Any], Any] | None = None,
) -> None:
    """Wrap the child-operation runner. ``before`` may return a result that replaces the call;
    ``after`` may return a replacement for the real result."""
    real = removalfs.RUNNER
    assert real is not None

    def runner(op: str, args: dict[str, Any], **kw: Any) -> Any:
        if before is not None:
            replaced = before(op, args)
            if replaced is not None:
                return replaced
        res = real(op, args, **kw)
        if after is not None:
            return after(op, args, res) or res
        return res

    mp.setattr(removalfs, "RUNNER", runner)


def commit_ignore(w: RR) -> None:
    (w.wt / ".gitignore").write_text("*.secret\n")
    w.g(["add", ".gitignore"], w.wt)
    w.g(["commit", "-q", "-m", "ignore"], w.wt)


def details(err: dict[str, Any], check: str) -> str:
    return str(err["checks"][check]["details"])


@pytest.fixture
def restore_modes() -> Iterator[list[Path]]:
    paths: list[Path] = []
    yield paths
    for p in paths:
        p.chmod(0o755)


# -- T1: the step-9 re-check of the ignored and stash classes -------------------------------------


def test_t1_a_new_ignored_file_made_in_the_hook_refuses_even_forced(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    commit_ignore(w)
    (w.wt / "a.secret").write_text("a")
    mutate(w, f"echo b > {w.wt}/b.secret")
    err = w.refused("--force", human=True)
    assert err["reason"] == "dirty" and err["checks"]["ignored"]["status"] == "fail"
    assert "b.secret" in details(err, "ignored") and "a.secret" not in details(err, "ignored")
    assert w.intact()


def test_t1_a_new_stash_made_in_the_hook_refuses_even_forced(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")  # an unpushed HEAD: a stash on it counts
    mutate(w, f"echo e >> {w.wt}/c1.txt && git -C {w.wt} {GIT_ID} stash push -q")
    err = w.refused("--force", human=True)
    assert err["reason"] == "dirty" and err["checks"]["stash"]["status"] == "fail"
    assert w.intact()


def test_t1_ignored_and_stash_findings_already_present_pass_the_recheck_when_forced(
    w: RR,
    capsys,  # noqa: ANN001
) -> None:
    w.start(capsys)
    commit_ignore(w)  # an unpushed commit
    (w.wt / "a.secret").write_text("a")
    (w.wt / "c1.txt").write_text("edit")
    w.g(["stash", "push", "-q"], w.wt)
    d = w.removed("--force", human=True)
    assert {"ignored", "stash"} <= set(d["overridden"])
    assert not w.wt.exists() and not w.admin.exists()


# -- T2: --merged-head and the branch


def test_t2_a_merged_head_other_than_the_tip_keeps_an_unpushed_branch(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    d = w.removed("--force", "--merged-head", w.shas["c1"], human=True)
    assert d["branch_deleted"] is False and d["branch_kept_reason"] == "unpushed"
    assert "work" in w.branches()


def test_t2_a_merged_head_the_mirror_never_saw_keeps_the_branch(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    head = w.push_work("a")  # local only: not an ancestor of anything the remote has
    d = w.removed("--force", "--merged-head", head, human=True)
    assert d["branch_deleted"] is False and d["branch_kept_reason"] == "unpushed"
    assert "work" in w.branches()


# -- T3: what a deletion that failed half-way leaves


@pytest.mark.parametrize("cut", ["io", "timeout"])
def test_t3_a_deletion_cut_off_after_deleting_entries_is_partial_never_active(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    cut: str,
) -> None:
    w.start(capsys)

    def after(op: str, args: dict[str, Any], res: Any) -> Any:
        if op != "delete_tree":
            return None
        if cut == "io":
            return fsafe_child.OpResult(False, error=fsafe.FsafeError("io", "boom"))
        err = fsafe.FsafeError("deadline", "killed")
        return fsafe_child.OpResult(False, error=err, timed_out=True, pid=4242)

    wrap_runner(monkeypatch, after=after)
    err = w.refused(code=1)
    assert err["code"] == "partial" and err["details"]["remaining_path"] == str(w.wt)
    assert w.state_of() == "partial" and w.admin.is_dir()


@pytest.mark.parametrize(
    "kind",
    [
        "identity", "symlink", "not_dir", "mount_point", "owner", "permission", "unavailable",
        "bad_request", "name", "missing",
    ],
)  # fmt: skip
def test_t3_an_error_raised_before_anything_was_deleted_leaves_the_record_active(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    w.start(capsys)

    def before(op: str, args: dict[str, Any]) -> Any:
        if op == "delete_tree":
            return fsafe_child.OpResult(False, error=fsafe.FsafeError(kind, "refused"))
        return None

    wrap_runner(monkeypatch, before=before)
    assert w.refused()["reason"] == "unlinked"
    assert w.intact() and w.audit_records() == []


# -- T4: the anchors of the deletion


class Anchors:
    def __init__(self, identity: str | None) -> None:
        self.identity = identity

    def anchor_identity(self, path: str) -> str | None:
        return self.identity


def ident(path: Path) -> str:
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}"


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "base"
    wt = base / "a" / "wt"
    wt.mkdir(parents=True)
    (wt / "f").write_text("x")
    (wt / ".git").write_text("gitdir: x\n")
    return base, wt


def owned_record(base: Path, wt: Path, **over: Any) -> dict[str, Any]:
    return {"path": str(wt), "base_dir": str(base), "owned": True, "dir_id": ident(wt), **over}


def test_t4_a_base_dir_swapped_for_a_directory_of_another_identity_deletes_nothing(
    tree: tuple[Path, Path],
    tmp_path: Path,
    inproc,  # noqa: ANN001
) -> None:
    base, wt = tree
    other = tmp_path / "other"
    other.mkdir()
    out = removal_delete.delete_worktree(
        Anchors(ident(other)), owned_record(base, wt), fsafe_child.Budget()
    )
    assert out.status == "refused" and out.error is not None and out.error.kind == "identity"
    assert (wt / "f").exists() and (wt / ".git").exists()


def test_t4_an_anchor_without_a_recorded_identity_deletes_nothing(
    tree: tuple[Path, Path],
    inproc,  # noqa: ANN001
) -> None:
    base, wt = tree
    out = removal_delete.delete_worktree(
        Anchors(None), owned_record(base, wt), fsafe_child.Budget()
    )
    assert out.status == "refused" and out.error is not None and out.error.kind == "identity"
    assert (wt / "f").exists()


def test_t4_a_path_outside_the_base_dir_deletes_nothing(
    tree: tuple[Path, Path],
    tmp_path: Path,
    inproc,  # noqa: ANN001
) -> None:
    base, wt = tree
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for path in (str(elsewhere), str(base), str(base / "a" / ".." / ".." / "elsewhere")):
        rec = owned_record(base, wt, path=path)
        out = removal_delete.delete_worktree(Anchors(ident(base)), rec, fsafe_child.Budget())
        assert out.status == "refused" and out.error is not None and out.error.kind == "identity"
    assert elsewhere.is_dir() and (wt / "f").exists()


def test_t4_a_directory_with_another_identity_than_dir_id_deletes_nothing(
    tree: tuple[Path, Path],
    tmp_path: Path,
    inproc,  # noqa: ANN001
) -> None:
    base, wt = tree
    rec = owned_record(base, wt, dir_id=ident(tmp_path))
    out = removal_delete.delete_worktree(Anchors(ident(base)), rec, fsafe_child.Budget())
    assert out.status == "refused" and out.error is not None and out.error.kind == "identity"
    assert (wt / "f").exists()


def test_t4_the_walk_compares_the_root_identity_where_the_root_is_the_walk_root(
    tree: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    base, wt = tree
    args = {"anchor": {"path": str(base), "identity": None}, "chain": ["a", "wt"]}
    ok = fsafe_child.OPS["walk"]({**args, "root_identity": [*map(int, ident(wt).split(":"))]})
    assert int(str(ok["count"])) >= 2
    wrong = [*map(int, ident(tmp_path).split(":"))]
    with pytest.raises(fsafe.FsafeError) as exc:
        fsafe_child.OPS["walk"]({**args, "root_identity": wrong})
    assert exc.value.kind == "identity"


def admin_record(w: RR) -> dict[str, Any]:
    return dict(w.rows("SELECT * FROM records WHERE id = ?", w.rid)[0])


def test_t4_a_tampered_common_id_deletes_no_admin_entry(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    rec = admin_record(w)
    rec["common_id"] = "1:1"
    assert isinstance(removal_delete.admin_check(None, rec, fsafe_child.Budget()), fsafe.FsafeError)
    state, remaining = removal_delete.delete_admin(None, rec, fsafe_child.Budget())
    assert state == "failed" and remaining == [str(w.admin)]
    assert w.admin.is_dir()


def test_t4_an_admin_dir_outside_the_worktrees_directory_is_never_deleted(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    rec = admin_record(w)
    rec["admin_dir"] = str(w.clone / ".git" / "hooks")
    assert isinstance(removal_delete.admin_check(None, rec, fsafe_child.Budget()), fsafe.FsafeError)
    assert removal_delete.delete_admin(None, rec, fsafe_child.Budget())[0] == "failed"
    assert (w.clone / ".git" / "hooks").is_dir()


# -- T5: --force never overrides these


def assert_refused_alike(w: RR, reason: str) -> None:
    assert w.refused()["reason"] == reason
    assert w.refused("--force", human=True)["reason"] == reason


def test_t5_main_worktree(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    st = os.stat(w.clone)
    w.db_exec(
        "UPDATE records SET path = ?, dir_id = ?, admin_dir = NULL, base_dir = NULL, owned = 0 "
        "WHERE id = ?",
        str(w.clone), f"{st.st_dev}:{st.st_ino}", w.rid,
    )  # fmt: skip
    assert_refused_alike(w, "main_worktree")
    assert (w.clone / "c1.txt").exists()


def test_t5_no_recorded_directory_identity(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET dir_id = NULL WHERE id = ?", w.rid)
    assert_refused_alike(w, "unlinked")
    assert w.wt.is_dir() and w.admin.is_dir()


def test_t5_a_record_that_is_not_active(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'missing' WHERE id = ?", w.rid)
    assert_refused_alike(w, "unlinked")
    assert w.wt.is_dir() and w.admin.is_dir()


def test_t5_an_admin_entry_that_cannot_be_opened(w: RR, capsys, restore_modes) -> None:  # noqa: ANN001
    w.start(capsys)
    restore_modes.append(w.admin)
    w.admin.chmod(0)
    assert_refused_alike(w, "unlinked")
    w.admin.chmod(0o755)
    assert w.wt.is_dir() and w.state_of() == "active"


def test_t5_a_tree_that_changes_identity_while_it_is_checked(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)

    def changed(*a: Any, **k: Any) -> Any:
        raise fsafe.FsafeError("identity", "changed under the scan")

    monkeypatch.setattr(removal_content, "tracked_untracked_ignored", changed)
    err = w.refused()
    assert err["reason"] == "unlinked" and err["checks"]["integrity"]["status"] == "fail"
    assert w.refused("--force", human=True)["reason"] == "unlinked"
    assert w.intact()


def test_t5_the_admin_gitdir_changed_after_the_recheck(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    after_recheck(monkeypatch, lambda: (w.admin / "gitdir").write_text("/x/.git\n"))
    assert w.refused("--force", human=True)["reason"] == "unlinked"
    assert w.wt.is_dir() and w.admin.is_dir() and w.state_of() == "active"


def test_t5_a_lock_that_appears_after_the_recheck(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    after_recheck(monkeypatch, lambda: (w.admin / "locked").write_text("late\n"))
    assert w.refused("--force", human=True)["reason"] == "locked"
    assert w.wt.is_dir() and w.admin.is_dir() and w.state_of() == "active"


# -- T6: an admin entry that stays leaves the record partial


@pytest.mark.parametrize("outcome", ["partial", "failed"])
def test_t6_an_admin_entry_that_could_not_be_deleted_is_partial_never_removed(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    w.start(capsys)
    monkeypatch.setattr(
        removal_delete, "delete_admin", lambda *a, **k: (outcome, [f"{w.admin}/gitdir"])
    )
    err = w.refused(code=1)
    assert err["code"] == "partial" and err["details"]["remaining_path"] == str(w.admin)
    assert w.state_of() == "partial" and not w.wt.exists()
    assert [(a["action"], a["result"]) for a in w.audit_records()] == [("remove", "error")]


# -- T7 to T9: the branch


def test_t7_a_branch_that_advanced_after_planning_is_kept_with_a_warning(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    other = w.shas["c1"]

    def before(sub: str, argv: list[str], kw: dict[str, Any]) -> None:
        if sub == "update-ref" and "-d" in argv:
            w.g(["update-ref", "refs/heads/work", other])

    w.git_runner = Spy(before=before)
    d = w.removed()
    assert d["branch_deleted"] is False and d["branch_kept_reason"] == "unpushed"
    assert any("changed after it was checked" in x for x in d["warnings"])
    assert w.g(["rev-parse", "refs/heads/work"]) == other


def test_t8_worktrees_that_cannot_be_listed_keep_the_branch(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)

    def boom(*a: Any, **k: Any) -> Any:
        raise WtctlError("internal", "cannot list")

    monkeypatch.setattr(worktrees, "worktree_entries", boom)
    d = w.removed()
    assert d["branch_deleted"] is False and d["branch_kept_reason"] == "checked_out_elsewhere"
    assert any("could not be listed" in x for x in d["warnings"])
    assert "work" in w.branches() and not w.wt.exists()


def test_t9_a_branch_deleted_in_the_hook_is_reported_not_created(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    mutate(w, f"git -C {w.wt} checkout -q --detach && git -C {w.clone} branch -q -D work")
    d = w.removed()
    assert d["branch_deleted"] is False and d["branch_kept_reason"] == "not_created"
    assert any("no longer exists" in x for x in d["warnings"])
    assert not w.wt.exists()


# -- T10: the admin entry just before the deletion


def test_t10_an_admin_entry_deleted_after_the_recheck_is_unlinked(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    after_recheck(monkeypatch, lambda: shutil.rmtree(w.admin))
    assert w.refused("--force", human=True)["reason"] == "unlinked"
    assert w.wt.is_dir() and w.state_of() == "active"


def test_t10_an_admin_entry_that_became_unopenable_after_the_recheck_is_unlinked(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    restore_modes,  # noqa: ANN001
) -> None:
    w.start(capsys)
    restore_modes.append(w.admin)
    after_recheck(monkeypatch, lambda: w.admin.chmod(0))
    assert w.refused("--force", human=True)["reason"] == "unlinked"
    w.admin.chmod(0o755)
    assert w.wt.is_dir() and w.state_of() == "active"


def test_t10_a_partial_record_whose_admin_entry_became_unopenable_is_unlinked(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    restore_modes,  # noqa: ANN001
) -> None:
    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    (w.wt / ".git").unlink()
    restore_modes.append(w.admin)
    real = remover.Remover._partial_kept_reason

    def chmod_then(self: remover.Remover, rec: Any) -> str:
        w.admin.chmod(0)  # runs under the clone lease, before the admin entry is checked
        return real(self, rec)

    monkeypatch.setattr(remover.Remover, "_partial_kept_reason", chmod_then)
    assert w.refused(human=True)["reason"] == "unlinked"
    w.admin.chmod(0o755)
    assert w.wt.is_dir() and w.state_of() == "partial"


# -- T11: the per-clone lease is held while anything is deleted


def lease_probe(w: RR, seen: list[str]) -> Callable[[], None]:
    def probe() -> None:
        st = State(w.db, Gateway())
        try:
            with st.lease(clone_lease(str(w.clone)), wait=0):
                seen.append("free")
        except WtctlError as exc:
            seen.append(exc.code)
        finally:
            st.close()

    return probe


def spy_on_leases(w: RR, mp: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    probe = lease_probe(w, seen)

    def before(op: str, args: dict[str, Any]) -> None:
        if op in ("delete_tree", "delete_admin"):
            probe()

    wrap_runner(mp, before=before)

    def git_before(sub: str, argv: list[str], kw: dict[str, Any]) -> None:
        if sub == "update-ref" and "-d" in argv:
            probe()

    w.git_runner = Spy(before=git_before)
    return seen


def test_t11_the_clone_lease_is_held_during_the_deletions_and_the_branch(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    seen = spy_on_leases(w, monkeypatch)
    assert w.removed()["branch_deleted"] is True
    assert seen == ["busy", "busy", "busy"]  # the tree, the admin entry, the branch


def test_t11_the_clone_lease_is_held_while_finishing_a_partial_record(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    (w.wt / ".git").unlink()
    seen = spy_on_leases(w, monkeypatch)
    w.removed(human=True)
    assert seen == ["busy", "busy"]  # the tree and the admin entry; no git, no branch


# -- T12: the confirmation


def test_t12_y_on_a_pipe_without_yes_is_not_a_confirmation(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    err = w.refused(code=13, yes=False, tty=False, stdin="y\n", human=True)
    assert err["code"] == "confirmation_required" and w.intact()


def test_t12_a_declined_prompt_never_runs_the_pre_remove_hook(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    marker = w.tmp / "hook-ran"
    w.top["hooks"] = {"pre-remove": [{"cmd": f"touch {marker}"}]}
    w.refused(code=13, yes=False, tty=True, stdin="n\n", human=True)
    assert not marker.exists() and w.intact()
    w.removed(yes=False, tty=True, stdin="y\n", human=True)
    assert marker.exists()


def test_t12_a_dirty_worktree_without_yes_refuses_before_any_prompt(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "dirty.txt").write_text("x")
    r = w.rm(yes=False, tty=True, stdin="y\n", human=True)
    assert r.code == 20 and r.json()["error"]["reason"] == "dirty"
    assert "Remove?" not in r.err and w.intact()


# -- T13: the audit record


def test_t13_a_forced_removal_audit_lists_the_overridden_checks(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "agent-1")
    (w.wt / "dirty.txt").write_text("x")
    d = w.removed("--force", human=True)
    assert d["overridden"] == ["held", "untracked"]
    rec = [a for a in w.audit_records() if a["action"] == "remove"][0]
    assert rec["inputs"]["overridden"] == ["held", "untracked"]


def test_t13_an_unforced_removal_audit_overrides_nothing(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.removed()
    assert w.audit_records()[0]["inputs"]["overridden"] == []


# -- T14: precedence and the remove-level fetch failures


def test_t14_in_use_is_reported_before_held(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "h")
    w.scanner.procs = [ProcessInfo(5, "vim", str(w.wt), ())]
    assert w.refused()["reason"] == "in_use"
    assert w.refused("--force", human=True)["reason"] == "in_use"


def test_t14_repo_config_changed_is_reported_before_in_use(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.scanner.procs = [ProcessInfo(5, "vim", str(w.wt), ())]
    w.g(["config", "alias.zz", "status"])
    assert w.refused()["reason"] == "repo_config_changed"


def failing_fetch(w: RR, result: GitResult) -> None:
    def before(sub: str, argv: list[str], kw: dict[str, Any]) -> GitResult | None:
        return result if sub == "fetch" else None

    w.git_runner = Spy(before=before)


def test_t14_auth_required_exits_12_and_a_forced_removal_overrides_it(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    failing_fetch(w, GitResult(128, b"", b"fatal: could not read Username for 'https://x': no\n"))
    assert w.refused(code=12)["code"] == "auth_required" and w.intact()
    d = w.removed("--force", human=True)
    assert (
        "fetch" in d["overridden"] and d["checks"]["fetch"]["details"][0]["code"] == "auth_required"
    )
    assert d["branch_kept_reason"] == "unpushed"


def test_t14_prune_incomplete_exits_12_and_a_forced_removal_overrides_it(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    failing_fetch(w, GitResult(0, b"", b"error: could not delete references\n"))
    err = w.refused(code=12)
    assert err["code"] == "fetch_failed" and err["details"]["cause"] == "prune_incomplete"
    d = w.removed("--force", human=True)
    assert "fetch" in d["overridden"]
    assert d["checks"]["fetch"]["details"][0]["cause"] == "prune_incomplete"


@pytest.mark.parametrize(
    "stderr,cause",
    [
        (b"fatal: repository 'https://x/r.git/' not found\n", "repository_not_found"),
        (b"fatal: unable to access 'https://x/': Could not resolve host: x\n", "network_unreachable"),
        (b"fatal: unable to access 'https://x/': The requested URL returned error: 429\n", "rate_limited"),
    ],
)
def test_t14_transport_causes_exit_12_and_a_forced_removal_overrides_them(
    w: RR,  # noqa: ANN001
    capsys,  # noqa: ANN001
    stderr: bytes,
    cause: str,
) -> None:
    w.start(capsys)
    failing_fetch(w, GitResult(128, b"", stderr))
    err = w.refused(code=12)
    assert err["code"] == "fetch_failed" and err["details"] == {"cause": cause}
    d = w.removed("--force", human=True)
    assert d["checks"]["fetch"]["details"][0]["cause"] == cause


def test_t14_the_holders_seen_at_step_6_pass_the_recheck_when_forced(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "agent-1")
    mutate(w, "true")  # the hook runs between the step-6 checks and the step-9 re-check
    d = w.removed("--force", human=True)
    assert d["overridden"] == ["held"] and d["record"]["state"] == "removed"


# -- T15: the record is read again under the lease


def sql_hook(w: RR, sql: str) -> None:
    py = w.tmp / "sql.py"
    py.write_text(
        "import sqlite3, sys\n"
        "c = sqlite3.connect(sys.argv[1])\n"
        "c.execute(sys.argv[2], (sys.argv[3],))\n"
        "c.commit()\n"
    )
    mutate(w, f'{sys.executable} {py} {w.db} "{sql}" {w.rid}')


def test_t15_a_record_that_left_the_live_states_during_the_hook_is_busy(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    sql_hook(w, "UPDATE records SET state = 'forgotten' WHERE id = ?")
    assert w.refused("--force", human=True, code=12)["code"] == "busy"
    assert w.wt.is_dir() and w.admin.is_dir()


def test_t15_a_record_whose_path_changed_during_the_hook_is_unlinked(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    sql_hook(w, "UPDATE records SET path = path || '-x' WHERE id = ?")
    assert w.refused("--force", human=True)["reason"] == "unlinked"
    assert w.wt.is_dir() and w.admin.is_dir()


def test_t15_the_partial_finish_warns_that_the_pointers_are_gone(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    (w.wt / ".git").unlink()
    d = w.removed(human=True)
    assert any("git pointers are gone" in x for x in d["warnings"])
