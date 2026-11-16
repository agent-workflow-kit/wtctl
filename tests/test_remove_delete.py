"""``remove``: the deletion itself (SAFE-1, mounts, read-only trees), partial records, branches,
adopted records and concurrency."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import Spy
from tests.removelib import RR
from tests.schemacheck import validate
from wtctl import fsafe, removal_delete


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RR:  # noqa: ANN001
    return RR(tmp_path, private_home)


def disposable(w: RR, *names: str) -> None:
    for n in names:
        p = w.wt / "node_modules" / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")


# -- what is deleted ---------------------------------------------------------------------------


def test_hostile_names_and_trees_are_deleted_by_exact_name(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    nm = w.wt / "node_modules"
    nm.mkdir()
    names = ["-rf", "a\nb", "--no-preserve-root", "ünï", "x" * 200, ".git", "..foo", " lead"]
    for n in names:
        (nm / n).mkdir(exist_ok=True) if n == ".git" else (nm / n).write_text("x")
    deep = nm / "d"
    deep.mkdir()
    for i in range(60):
        deep = deep / f"l{i}"
        deep.mkdir()
    (nm / "fifo").write_text("")
    os.remove(nm / "fifo")
    os.mkfifo(nm / "fifo")
    (nm / "link").symlink_to("/nonexistent")
    # nested .git is refused by the content checks (nested_repos), so this tree is forced
    d = w.removed("--force", human=True)
    assert "nested_repos" in d["overridden"] and not w.wt.exists() and not w.admin.exists()


def test_read_only_directories_are_deleted(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    ro = w.wt / "node_modules" / "ro"
    (ro / "sub").mkdir(parents=True)
    (ro / "sub" / "f").write_text("x")
    os.chmod(ro / "sub", 0o500)
    os.chmod(ro, 0o500)
    d = w.removed()
    assert d["record"]["state"] == "removed" and not w.wt.exists()


def test_an_unreadable_directory_is_dirty_and_deleted_when_forced(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    ro = w.wt / "node_modules" / "ro"
    (ro / "sub").mkdir(parents=True)
    (ro / "sub" / "f").write_text("x")
    os.chmod(ro, 0o000)
    assert w.refused()["reason"] == "dirty"  # the nested-repo scan could not read it
    assert w.intact()
    d = w.removed("--force", human=True)
    assert "nested_repos" in d["overridden"] and not w.wt.exists()


def test_the_removal_stays_inside_the_worktree(w: RR, capsys, tmp_path: Path) -> None:  # noqa: ANN001
    w.start(capsys)
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep").write_text("k")
    nm = w.wt / "node_modules"
    nm.mkdir()
    (nm / "dirlink").symlink_to(outside)
    (nm / "filelink").symlink_to(outside / "sub" / "keep")
    w.removed()
    assert (outside / "sub" / "keep").read_text() == "k" and not w.wt.exists()
    assert w.wt.parent.is_dir()  # the parent directories stay


def swap_during_delete(mp: pytest.MonkeyPatch, when: str, target: tuple[str, ...], fn: Any) -> None:
    real = fsafe.delete_tree

    def wrapped(anchor: Any, parent_fd: int, name: str, **kw: Any) -> Any:
        def hook(stage: str, path: tuple[str, ...]) -> None:
            if stage == when and path == target:
                fn()

        return real(anchor, parent_fd, name, hook=hook, **kw)

    mp.setattr(fsafe, "delete_tree", wrapped)


def test_directory_swapped_for_a_symlink_mid_delete_deletes_nothing_outside(
    w: RR,
    capsys,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep").write_text("k")
    (w.wt / "node_modules" / "a").mkdir(parents=True)
    (w.wt / "node_modules" / "a" / "f").write_text("x")
    a = w.wt / "node_modules" / "a"

    def swap() -> None:
        os.rename(a, str(a) + ".moved")
        os.symlink(outside, a)

    swap_during_delete(monkeypatch, "classified", ("node_modules", "a"), swap)
    r = w.rm()
    assert (outside / "sub" / "keep").read_text() == "k"
    # the link was unlinked as a link; the moved tree is an entry the listing never saw, so its
    # parent cannot be removed: partial, and a later remove deletes it
    assert r.code == 1 and r.json()["error"]["code"] == "partial"
    assert not a.exists() and (Path(str(a) + ".moved") / "f").exists()
    assert w.state_of() == "partial"


def test_ancestor_swapped_for_a_symlink_mid_delete_deletes_nothing_outside(
    w: RR,
    capsys,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep").write_text("k")
    (outside / "node_modules").mkdir()
    (outside / "node_modules" / "keep2").write_text("k")
    nm = w.wt / "node_modules"
    (nm / "a").mkdir(parents=True)
    (nm / "a" / "f").write_text("x")

    def swap() -> None:
        os.rename(nm, str(nm) + ".moved")
        os.symlink(outside / "node_modules", nm)

    swap_during_delete(monkeypatch, "opened", ("node_modules",), swap)
    r = w.rm()
    assert (outside / "sub" / "keep").read_text() == "k"
    assert (outside / "node_modules" / "keep2").read_text() == "k"
    assert r.code == 1 and r.json()["error"]["code"] == "partial"


def test_worktree_root_swapped_for_a_symlink_mid_delete(
    w: RR,
    capsys,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """The worktree directory itself is swapped after its fd was opened."""
    w.start(capsys)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    (w.wt / "node_modules").mkdir()
    (w.wt / "node_modules" / "f").write_text("x")

    def swap() -> None:
        os.rename(w.wt, str(w.wt) + ".moved")
        os.symlink(outside, w.wt)

    swap_during_delete(monkeypatch, "opened", ("node_modules",), swap)
    r = w.rm()
    assert (outside / "keep").read_text() == "k"
    assert r.code == 1


# -- mounts --------------------------------------------------------------------------------------


def test_a_mount_point_inside_is_skipped_and_reported_by_the_real_child(tmp_path: Path) -> None:
    unshare, mount = shutil.which("unshare"), shutil.which("mount")
    if not (unshare and mount):
        pytest.skip("real mount tests need unshare and mount")
    probe = tmp_path / "probe"
    probe.mkdir()
    ok = subprocess.run([unshare, "-Urm", mount, "-t", "tmpfs", "none", str(probe)], check=False)
    if ok.returncode != 0:
        pytest.skip("unprivileged user+mount namespaces are unavailable here")
    work = tmp_path / "ns"
    work.mkdir()
    scenario = Path(__file__).with_name("remove_mount_scenario.py")
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    r = subprocess.run(
        [unshare, "-Urm", sys.executable, str(scenario), str(work)],
        capture_output=True, text=True, timeout=120, env=env, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "partial"
    assert set(out["remaining"]) == {
        f"{work}/base/wt/node_modules/mnt",
        f"{work}/base/wt/.git",
    }
    assert out["inside_kept"] == "precious" and out["plain_gone"] and out["dotgit_kept"]


# -- partial records -------------------------------------------------------------------------------


def stuck(mp: pytest.MonkeyPatch, name: str) -> dict[str, bool]:
    """Make unlinking ``name`` fail while ``flag['on']``."""
    flag = {"on": True}
    real = os.unlink

    def unlink(path: Any, *, dir_fd: int | None = None) -> None:
        if flag["on"] and path == name:
            raise PermissionError(1, "denied")
        real(path, dir_fd=dir_fd)

    mp.setattr(os, "unlink", unlink)
    mp.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, unlink})
    return flag


def test_an_entry_that_cannot_be_deleted_leaves_a_partial_record(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    disposable(w, "stuck.txt", "ok.txt")
    stuck(monkeypatch, "stuck.txt")
    err = w.refused(code=1)
    assert err["code"] == "partial"
    assert err["details"]["remaining_path"] == str(w.wt)
    assert err["details"]["remaining"] == [f"{w.wt}/node_modules/stuck.txt", f"{w.wt}/.git"]
    assert w.state_of() == "partial"
    row = w.rows("SELECT last_error FROM records WHERE id = ?", w.rid)[0]
    assert "stuck.txt" in row["last_error"]
    assert (w.wt / ".git").exists() and w.admin.is_dir() and "work" in w.branches()
    assert not (w.wt / "node_modules" / "ok.txt").exists()
    recs = w.audit_records()
    assert [(r["action"], r["result"]) for r in recs] == [("remove", "error")]


def test_finishing_a_partial_record_is_human_only_and_runs_the_normal_checks(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    disposable(w, "stuck.txt")
    flag = stuck(monkeypatch, "stuck.txt")
    w.refused(code=1)
    assert w.refused(code=13)["code"] == "confirmation_required"
    flag["on"] = False
    # the tracked files are gone, which the content checks report: a human forces it
    err = w.refused(human=True)
    assert err["reason"] == "dirty" and err["checks"]["tracked"]["status"] == "fail"
    (w.wt / "late.txt").write_text("x")  # and a new file is reported like any other
    err = w.refused(human=True)
    assert err["checks"]["untracked"]["details"] == [{"path": "late.txt"}]
    d = w.removed("--force", human=True)
    assert {"tracked", "untracked"} <= set(d["overridden"])
    assert d["record"]["state"] == "removed" and not w.wt.exists() and not w.admin.exists()
    assert d["branch_deleted"] is True


def test_partial_with_the_pointers_gone_skips_git_and_deletes_the_rest(
    w: RR,
    capsys,  # noqa: ANN001
) -> None:
    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    (w.wt / ".git").unlink()
    (w.wt / "left.txt").write_text("x")  # unchecked: the confirmation is the human's look
    out = w.tmp / "payload.json"
    w.top["hooks"] = {"pre-remove": [{"cmd": f"cat > {out}"}]}
    assert w.refused(code=13)["code"] == "confirmation_required"
    assert w.refused(code=13, human=True, yes=False)["code"] == "confirmation_required"
    d = w.removed(human=True)
    payload = json.loads(out.read_text())
    validate("wtctl/hooks/pre-remove.json", payload)
    assert payload["record"]["state"] == "partial"
    assert payload["checks"]["untracked"]["status"] == "skipped"
    assert d["record"]["state"] == "removed" and not w.wt.exists() and not w.admin.exists()
    assert d["branch_kept_reason"] == "unpushed" and "work" in w.branches()
    assert [a["inputs"]["head"] for a in w.audit_records()] == [None]


def test_partial_in_use_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import ProcessInfo

    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    (w.wt / ".git").unlink()
    w.scanner.procs = [ProcessInfo(9, "sh", str(w.wt), ())]
    assert w.refused(human=True)["reason"] == "in_use"
    assert w.wt.is_dir() and w.admin.is_dir()


def test_partial_whose_directory_is_another_directory_deletes_nothing(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    (w.wt / ".git").unlink()
    old = Path(str(w.wt) + ".old")
    w.wt.rename(old)
    w.wt.mkdir()
    (w.wt / "other").write_text("x")
    assert w.refused(human=True)["reason"] == "unlinked"
    assert (w.wt / "other").exists() and (old / "c1.txt").exists() and w.admin.is_dir()


def test_partial_with_the_directory_gone_finishes_the_admin_entry(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    shutil.rmtree(w.wt)
    status = w.st(capsys)  # the same verdict without removing anything
    assert (status["would_remove"], status["human_required"]) == (True, True)
    d = w.removed(human=True)
    assert d["record"]["state"] == "removed" and not w.admin.exists()


def test_partial_never_deletes_an_admin_entry_naming_another_path(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    shutil.rmtree(w.wt)
    (w.admin / "gitdir").write_text("/somewhere/else/.git\n")
    d = w.removed(human=True)
    assert w.admin.is_dir() and any("names another path" in x for x in d["warnings"])


def test_crash_after_the_first_deletion_leaves_a_partial_record(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    real = removal_delete.delete_worktree
    monkeypatch.setattr(removal_delete, "delete_worktree", boom)
    assert w.refused(code=1)["code"] == "internal"
    assert w.state_of() == "partial"
    assert w.wt.is_dir() and w.admin.is_dir()
    monkeypatch.setattr(removal_delete, "delete_worktree", real)
    d = w.removed(human=True)  # the pointers are intact: a normal removal, human-only
    assert d["record"]["state"] == "removed"


def test_crash_after_the_tree_leaves_a_truthful_record_that_a_human_finishes(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    real = removal_delete.delete_admin
    monkeypatch.setattr(removal_delete, "delete_admin", boom)
    assert w.refused(code=1)["code"] == "internal"
    assert w.state_of() == "partial" and not w.wt.exists() and w.admin.is_dir()
    monkeypatch.setattr(removal_delete, "delete_admin", real)
    assert w.refused(code=13)["code"] == "confirmation_required"
    d = w.removed(human=True)
    assert d["record"]["state"] == "removed" and not w.admin.exists()


def test_a_refusal_at_the_open_step_leaves_the_record_active(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """The worktree directory is replaced between the checks and the deletion's open."""
    w.start(capsys)
    real = removal_delete.delete_worktree
    moved = Path(str(w.wt) + ".moved")

    def swapped(*a: Any, **k: Any) -> Any:
        os.rename(w.wt, moved)
        w.wt.mkdir()
        (w.wt / "other").write_text("x")
        return real(*a, **k)

    monkeypatch.setattr(removal_delete, "delete_worktree", swapped)
    assert w.refused()["reason"] == "unlinked"
    assert w.state_of() == "active"
    assert (w.wt / "other").exists() and (moved / "c1.txt").exists() and w.admin.is_dir()
    assert w.audit_records() == []


def test_admin_entry_retargeted_between_the_check_and_the_delete_is_left(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    real = removal_delete.delete_admin

    def retarget(*a: Any, **k: Any) -> Any:
        (w.admin / "gitdir").write_text("/somewhere/else/.git\n")
        return real(*a, **k)

    monkeypatch.setattr(removal_delete, "delete_admin", retarget)
    d = w.removed()
    assert not w.wt.exists() and w.admin.is_dir()
    assert any("names another path" in x for x in d["warnings"])


# -- branches --------------------------------------------------------------------------------------


def test_branch_moved_before_the_compare_and_set_is_kept(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    other = w.shas["c1"]

    def before(sub: str, argv: list[str], kw: dict[str, Any]) -> None:
        if sub == "update-ref" and "-d" in argv:
            w.g(["update-ref", "refs/heads/work", other])

    w.git_runner = Spy(before=before)
    d = w.removed()
    assert d["branch_deleted"] is False and d["branch_kept_reason"] == "unpushed"
    assert w.g(["rev-parse", "refs/heads/work"]) == other
    assert not w.wt.exists() and [a["action"] for a in w.audit_records()] == ["remove"]


def test_a_branch_that_was_not_created_by_wtctl_is_kept(w: RR, capsys) -> None:  # noqa: ANN001
    from tests.removallib import FakeScanner
    from tests.test_create import ok

    w.build()
    w.scanner = FakeScanner()

    rec = ok(w.create(capsys, "--branch", "local-b"))["record"]
    w.rid, w.wt, w.admin = rec["id"], Path(rec["path"]), Path(rec["admin_dir"])
    d = w.removed()
    assert d["branch_deleted"] is False and d["branch_kept_reason"] == "not_created"
    assert "local-b" in w.branches()


def test_keep_branch_and_delete_branch_disabled(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    d = w.removed("--keep-branch")
    assert d["branch_kept_reason"] == "keep_branch" and "work" in w.branches()


def test_delete_branch_config_false(w: RR, capsys) -> None:  # noqa: ANN001
    w.build()
    w.top["remove"] = {"delete_branch": False}
    w.start_built(capsys)
    d = w.removed()
    assert d["branch_kept_reason"] == "disabled" and "work" in w.branches()


def test_branch_checked_out_in_another_worktree_is_kept(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["checkout", "-q", "--detach"], w.wt)
    w.g(["worktree", "add", "-q", str(w.tmp / "second"), "work"])
    d = w.removed()
    assert d["branch_deleted"] is False and d["branch_kept_reason"] == "checked_out_elsewhere"
    assert "work" in w.branches()


def test_branch_section_in_the_config_is_removed_with_the_branch(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["config", "branch.work.remote", "origin"])
    w.g(["config", "branch.work.merge", "refs/heads/work"])
    d = w.removed()
    assert d["branch_deleted"] is True
    out = subprocess.run(
        ["git", "config", "--local", "--get-regexp", r"^branch\.work\."],
        cwd=w.clone, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert out.returncode == 1 and out.stdout == ""


def test_unpushed_branch_is_kept_when_forced(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    d = w.removed("--force", human=True)
    assert d["branch_kept_reason"] == "unpushed" and "work" in w.branches()
    assert [a["action"] for a in w.audit_records()] == ["remove"]


def test_teardown_flow_with_a_squash_merged_branch(w: RR, capsys) -> None:  # noqa: ANN001
    """agent-spawn's flow: remove --yes --holder <h> --merged-head <sha> after the remote branch
    was deleted by a squash merge."""
    w.start(capsys, "--holder", "agent-7")
    head = w.push_work("feature")
    w.push()
    w.st(capsys)  # a refresh while the remote branch exists: the mirror keeps the commit
    w.g(["push", "-q", str(w.remote), "--delete", "work"], w.wt)
    err = w.refused("--holder", "agent-7")
    assert err["reason"] == "unpushed"
    err = w.refused("--holder", "agent-7", "--merged-head", "b" * 40)
    assert err["reason"] == "unpushed"  # a sha the caller made up
    d = w.removed("--holder", "agent-7", "--merged-head", head)
    assert d["branch_deleted"] is True and d["overridden"] == []
    assert "work" not in w.branches() and not w.wt.exists()
    rec = [a for a in w.audit_records() if a["action"] == "remove"][0]
    assert rec["inputs"]["merged_head"] == head and rec["inputs"]["head"] == head


# -- adopted records -----------------------------------------------------------------------


def adopt_in_db(w: RR) -> None:
    w.db_exec(
        "UPDATE records SET owned = 0, base_dir = NULL, created_branch = 0 WHERE id = ?", w.rid
    )


def test_adopted_worktree_needs_a_human_and_never_touches_the_branch(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    adopt_in_db(w)
    assert w.refused(code=13)["code"] == "confirmation_required"
    assert w.intact()
    d = w.removed(human=True)
    assert d["record"]["state"] == "removed" and not w.wt.exists() and not w.admin.exists()
    assert d["branch_kept_reason"] == "not_created" and "work" in w.branches()
    assert w.clone.is_dir() and (w.clone / "c1.txt").exists()


def test_adopted_worktree_whose_directory_was_replaced_is_unlinked(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    adopt_in_db(w)
    old = Path(str(w.wt) + ".old")
    w.wt.rename(old)
    w.wt.mkdir()
    (w.wt / "other").write_text("x")
    assert w.refused(human=True)["reason"] == "unlinked"
    assert (w.wt / "other").exists() and (old / "c1.txt").exists()


def test_adopted_main_worktree_is_always_refused(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    ident = os.stat(w.clone)
    w.db_exec(
        "UPDATE records SET path = ?, dir_id = ?, admin_dir = NULL, base_dir = NULL, owned = 0 "
        "WHERE id = ?",
        str(w.clone), f"{ident.st_dev}:{ident.st_ino}", w.rid,
    )  # fmt: skip
    err = w.refused(human=True)
    assert err["reason"] == "main_worktree"
    assert w.refused()["reason"] == "main_worktree"
    assert (w.clone / "c1.txt").exists() and (w.clone / ".git").is_dir()


# -- leases ----------------------------------------------------------------------------------------


def test_a_held_record_lease_is_busy_and_changes_nothing(w: RR, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import Gateway
    from wtctl.state import State, record_lease

    w.start(capsys)
    st = State(w.db, Gateway())
    with st.lease(record_lease(w.rid), wait=1):
        assert w.refused(code=12)["code"] == "busy"
    st.close()
    assert w.intact()


def test_a_held_clone_lease_is_busy_at_the_refresh(w: RR, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import Gateway
    from wtctl.state import State, clone_lease

    w.start(capsys)
    st = State(w.db, Gateway())
    with st.lease(clone_lease(str(w.clone)), wait=1):
        assert w.refused(code=12)["code"] == "busy"
    st.close()
    assert w.intact()


def test_a_removal_leaves_no_lease_behind(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.removed()
    assert w.count("leases") == 0


# -- hostile clone config is inert ---------------------------------------------------------


def test_clone_hooks_do_not_run(w: RR, capsys) -> None:  # noqa: ANN001
    """A reference-transaction hook in the clone (part of the recorded baseline) must not run for
    the branch deletion: wtctl's git never runs repository hooks."""
    w.build()
    marker = w.tmp / "hook-ran"
    hook = w.clone / ".git" / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    w.start_built(capsys)
    marker.unlink(missing_ok=True)
    d = w.removed()
    assert d["branch_deleted"] is True
    assert not marker.exists()


def test_an_unusual_branch_name_is_deleted_by_exact_ref(w: RR, capsys) -> None:  # noqa: ANN001
    from tests.removallib import FakeScanner
    from tests.test_create import ok

    w.build()
    w.scanner = FakeScanner()
    rec = ok(w.create(capsys, "--branch", "feat/ünï-x--y.z"))["record"]
    w.rid, w.wt, w.admin = rec["id"], Path(rec["path"]), Path(rec["admin_dir"])
    assert "feat/ünï-x--y.z" in w.branches()
    d = w.removed()
    assert d["branch_deleted"] is True and "feat/ünï-x--y.z" not in w.branches()
