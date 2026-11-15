"""``remove`` of a worktree: the happy path, confirmation and the refusals."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.removelib import RR


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RR:  # noqa: ANN001
    return RR(tmp_path, private_home)


def test_removes_a_clean_pushed_worktree_and_its_branch(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    assert "work" in w.branches()
    d = w.removed()
    assert d["record"]["state"] == "removed" and d["record"]["removed_at"]
    assert d["branch_deleted"] is True and d["branch_kept_reason"] is None
    assert d["overridden"] == []
    assert not w.wt.exists() and not w.admin.exists()
    assert "work" not in w.branches()
    assert w.state_of() == "removed"


# -- confirmation ------------------------------------------------------------------------------


def test_without_tty_and_without_yes_nothing_changes(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    err = w.refused(code=13, yes=False)
    assert err["code"] == "confirmation_required"
    assert w.intact() and "work" in w.branches()


def test_tty_prompt_accepts_y(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    d = w.removed(yes=False, tty=True, stdin="y\n", human=True)
    assert d["record"]["state"] == "removed" and not w.wt.exists()


@pytest.mark.parametrize("answer", ["", "n\n", "no\n", "yes please\n", "\n"])
def test_tty_prompt_declined_changes_nothing(w: RR, capsys, answer: str) -> None:  # noqa: ANN001
    w.start(capsys)
    err = w.refused(code=13, yes=False, tty=True, stdin=answer, human=True)
    assert err["code"] == "confirmation_required"
    assert w.intact()


def test_yes_skips_only_the_prompt_never_a_refusal(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "dirty.txt").write_text("x")
    assert w.refused()["reason"] == "dirty"
    assert w.intact()


# -- refusals and their precedence -----------------------------------------------------------------


def test_dirty_refuses_with_the_check_results(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "dirty.txt").write_text("x")
    err = w.refused()
    assert err["reason"] == "dirty"
    assert err["checks"]["untracked"]["details"] == [{"path": "dirty.txt"}]
    assert w.intact()


def test_unpushed_commit_refuses_unpushed(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    err = w.refused()
    assert err["reason"] == "unpushed"
    assert err["checks"]["unpushed_head"]["status"] == "fail"
    assert w.intact()


def test_dirty_beats_unpushed(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    (w.wt / "c1.txt").write_text("edit")
    assert w.refused()["reason"] == "dirty"


def test_pushed_work_is_removed_and_the_branch_deleted(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    w.push()
    d = w.removed()
    assert d["branch_deleted"] is True and "work" not in w.branches()


def test_held_refuses_and_names_every_holder(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "agent-1")
    err = w.refused()
    assert err["reason"] == "held" and err["details"]["holders"] == ["agent-1"]
    assert err["checks"]["held"]["details"] == [{"holder": "agent-1"}]
    assert w.intact()
    err = w.refused("--holder", "agent-2")
    assert err["details"]["holders"] == ["agent-1"]


def test_the_callers_own_holder_does_not_block(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "agent-1")
    d = w.removed("--holder", "agent-1")
    assert d["overridden"] == [] and not w.wt.exists()


def test_in_use_refuses_and_force_does_not_override(w: RR, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import ProcessInfo

    w.start(capsys)
    w.scanner.procs = [ProcessInfo(4242, "vim", str(w.wt / "sub"), ())]
    err = w.refused()
    assert err["reason"] == "in_use" and err["processes"][0]["pid"] == 4242
    err = w.refused("--force", human=True)
    assert err["reason"] == "in_use"
    assert w.intact()


def test_unavailable_scanner_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.scanner.fail = True
    err = w.refused()
    assert err["reason"] == "in_use" and err["processes"] == []
    assert w.refused("--force", human=True)["reason"] == "in_use"
    assert w.intact()


def test_locked_refuses_and_nothing_overrides_it(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["worktree", "lock", "--reason", "usb", str(w.wt)])
    err = w.refused()
    assert err["reason"] == "locked" and err["details"]["lock_reason"] == "usb\n"
    assert w.refused("--force", human=True)["reason"] == "locked"
    assert w.intact()


def test_git_class_baseline_change_refuses_even_forced(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["config", "alias.zz", "status"])
    err = w.refused()
    assert err["reason"] == "repo_config_changed" and err["items"][0]["class"] == "git"
    assert w.refused("--force", human=True)["reason"] == "repo_config_changed"
    assert w.intact()


def test_claude_class_baseline_change_only_warns(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET baseline_digest = baseline_digest WHERE id = ?", w.rid)
    (w.wt / "CLAUDE.md").write_text("x")
    w.db_exec("DELETE FROM baseline_items WHERE record_id = ? AND class = 'claude'", w.rid)
    err = w.refused()
    assert err["reason"] == "dirty"  # reached the content checks: the claude change did not refuse
    assert err["checks"]["baseline"]["status"] == "warn"


@pytest.mark.parametrize("step", ["dotgit", "admin_gitdir", "commondir"])
def test_forged_pointers_are_unlinked_and_nothing_is_deleted(
    w: RR,
    capsys,
    step: str,  # noqa: ANN001
) -> None:
    w.start(capsys)
    other = w.tmp / "elsewhere"
    (other / "worktrees" / "x").mkdir(parents=True)
    if step == "dotgit":
        (w.wt / ".git").write_text(f"gitdir: {other}/worktrees/x\n")
    elif step == "admin_gitdir":
        (w.admin / "gitdir").write_text(f"{other}/.git\n")
    else:
        (w.admin / "commondir").write_text(f"{other}\n")
    err = w.refused("--force", human=True)
    assert err["reason"] == "unlinked"
    assert w.wt.is_dir() and w.admin.is_dir() and other.is_dir()
    assert w.state_of() == "active"


def test_reason_precedence(w: RR, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import ProcessInfo

    w.start(capsys, "--holder", "h")
    w.push_work("a")  # unpushed
    (w.wt / "dirty.txt").write_text("x")  # dirty
    assert w.refused("--holder", "h")["reason"] == "dirty"
    assert w.refused()["reason"] == "held"
    w.scanner.procs = [ProcessInfo(1, "x", str(w.wt), ())]
    assert w.refused("--holder", "h")["reason"] == "in_use"
    w.g(["config", "alias.zz", "status"])
    assert w.refused("--holder", "h")["reason"] == "repo_config_changed"
    w.g(["worktree", "lock", str(w.wt)])
    assert w.refused("--holder", "h")["reason"] == "locked"
    (w.admin / "gitdir").write_text("/x/.git\n")
    assert w.refused("--holder", "h")["reason"] == "unlinked"
    assert w.wt.is_dir() and w.admin.is_dir()


@pytest.mark.parametrize("state", ["creating", "unlinked", "missing", "clone_missing"])
def test_records_that_are_not_active_are_unlinked(w: RR, capsys, state: str) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET state = ? WHERE id = ?", state, w.rid)
    assert w.refused()["reason"] == "unlinked"
    assert w.wt.is_dir() and w.admin.is_dir()


def test_tombstones_exit_10_with_the_state(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.removed()
    err = w.refused(code=10)
    assert err["details"]["state"] == "removed" and err["details"]["removed_at"]


def test_unknown_record_and_usage_errors(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    assert w.refused(code=10, target="wt-aaaaaaaa")["code"] == "not_found"
    assert w.refused("--merged-head", "abc", code=2)["code"] == "usage"
    assert w.refused("--hook", "pre-create=true", code=2)["code"] == "usage"
    assert w.refused("--holder", "bad holder!", code=2)["code"] == "usage"
    assert w.intact()


def test_selector_by_path_and_prefix(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.removed(target=str(w.wt))
    assert not w.wt.exists()


# -- --force ---------------------------------------------------------------------------------


def test_force_needs_a_human(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "dirty.txt").write_text("x")
    assert w.refused("--force", code=2)["code"] == "usage"
    assert w.intact()


def test_force_overrides_dirty_unpushed_and_held(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "agent-1")
    w.push_work("a")
    (w.wt / "dirty.txt").write_text("x")
    d = w.removed("--force", human=True)
    assert set(d["overridden"]) == {"held", "untracked", "unpushed_head"}
    assert d["branch_deleted"] is False and d["branch_kept_reason"] == "unpushed"
    assert "work" in w.branches()  # the commits stay reachable
    assert not w.wt.exists() and not w.admin.exists()


def test_force_with_a_fetch_failure_keeps_the_branch(w: RR, capsys) -> None:  # noqa: ANN001
    import shutil

    w.start(capsys)
    shutil.rmtree(w.remote)
    err = w.refused(code=12)
    assert err["code"] == "fetch_failed" and w.intact()
    d = w.removed("--force", human=True)
    assert "fetch" in d["overridden"] and d["checks"]["fetch"]["status"] == "fail"
    assert d["branch_deleted"] is False and d["branch_kept_reason"] == "unpushed"
    assert "work" in w.branches()
    assert not w.wt.exists()


# -- throwaways and flags ---------------------------------------------------------------------


def test_throwaway_flags_are_usage_errors(w: RR, capsys) -> None:  # noqa: ANN001
    import sqlite3

    w.start(capsys)
    conn = sqlite3.connect(w.db)
    conn.execute(
        "UPDATE records SET object_type = 'throwaway', owned = NULL WHERE id = ?", (w.rid,)
    )
    conn.commit()
    conn.close()
    for flag in (["--keep-branch"], ["--holder", "h"], ["--merged-head", "a" * 40]):
        assert w.refused(*flag, code=2)["code"] == "usage"


# -- golden ------------------------------------------------------------------------------------


def normalized(d: dict, w: RR, head: str) -> str:  # noqa: ANN001
    import json

    text = json.dumps(d, indent=1, sort_keys=False)
    rec = d.get("record") or {}
    for volatile in ("created", "updated", "removed_at", "dir_id", "common_id", "baseline_digest"):
        if rec.get(volatile):
            text = text.replace(str(rec[volatile]), f"<{volatile}>")
    for real, fake in (
        (w.rid, "<id>"), (str(w.admin), "<admin>"), (str(w.wt.parent), "<wt-parent>"),
        (str(w.tmp), "<tmp>"), (head, "<head>"),
    ):  # fmt: skip
        text = text.replace(real, fake)
    return text + "\n"


def check_golden(name: str, text: str) -> None:
    import os

    golden = Path(__file__).parent / "golden" / name
    if os.environ.get("UPDATE_GOLDEN"):
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(text)
    assert text == golden.read_text()


def test_golden_json_of_a_removal(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "agent-1")
    head = w.g(["rev-parse", "HEAD"], w.wt)
    d = w.removed("--holder", "agent-1")
    check_golden("remove_ok.json", normalized(d, w, head))


def test_golden_json_of_a_refusal(w: RR, capsys) -> None:  # noqa: ANN001
    import json

    w.start(capsys)
    head = w.g(["rev-parse", "HEAD"], w.wt)
    (w.wt / "notes.txt").write_text("keep")
    r = w.rm()
    assert r.code == 20
    check_golden("remove_refused_dirty.json", normalized(json.loads(r.out), w, head))


def test_admin_entry_replaced_by_a_symlink_is_never_followed(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    moved = Path(str(w.admin) + ".moved")
    w.admin.rename(moved)
    w.admin.symlink_to(moved)
    err = w.refused("--force", human=True)
    assert err["reason"] == "unlinked"
    assert w.wt.is_dir() and (moved / "gitdir").exists() and w.state_of() == "active"
