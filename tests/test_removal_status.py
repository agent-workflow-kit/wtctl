"""Integrity, locks, baseline, holders, record states, throwaways and the verdict of ``status``."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import REPO
from tests.removallib import RW
from tests.schemacheck import validate
from wtctl.gateway import GitResult, ProcessInfo
from wtctl.procscan import ProcScanner


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RW:  # noqa: ANN001
    return RW(tmp_path, private_home)


def step_of(d: dict[str, Any]) -> str:
    assert d["checks"]["integrity"]["status"] == "fail"
    return d["checks"]["integrity"]["details"][0]["step"]


def unlinked(d: dict[str, Any]) -> None:
    assert (d["would_remove"], d["reason"]) == (False, "unlinked")
    for n in ("tracked", "untracked", "ignored", "nested_repos", "stash", "unpushed_head",
              "unpushed_branches", "fetch", "baseline"):  # fmt: skip
        assert d["checks"][n]["status"] == "skipped", n


# -- integrity: every pointer forged is `unlinked` --------------------------------------------


def test_dotgit_naming_another_admin_dir(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / ".git").write_text(f"gitdir: {w.clone}/.git/worktrees/other\n")
    d = w.st(capsys)
    assert step_of(d) == "dotgit"
    assert d["checks"]["integrity"]["details"][0]["expected"] == str(w.admin)
    unlinked(d)


def test_dotgit_is_not_a_file(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / ".git").unlink()
    (w.wt / ".git").mkdir()
    assert step_of(w.st(capsys)) == "dotgit"


def test_dotgit_symlink_is_not_followed(w: RW, capsys, tmp_path: Path) -> None:  # noqa: ANN001
    w.start(capsys)
    real = tmp_path / "realgit"
    (w.wt / ".git").rename(real)
    os.symlink(real, w.wt / ".git")
    assert step_of(w.st(capsys)) == "dotgit"


def test_gitdir_back_pointer_forged(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.admin / "gitdir").write_text("/somewhere/else/.git\n")
    d = w.st(capsys)
    assert step_of(d) == "admin_gitdir"
    unlinked(d)


def test_commondir_forged(w: RW, capsys, tmp_path: Path) -> None:  # noqa: ANN001
    w.start(capsys)
    other = tmp_path / "other.git"
    shutil.copytree(w.clone / ".git", other)
    (w.admin / "commondir").write_text(f"{other}\n")
    d = w.st(capsys)
    assert step_of(d) == "commondir"
    unlinked(d)


def test_admin_entry_missing(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    shutil.rmtree(w.admin)
    unlinked(w.st(capsys))


def test_worktree_directory_replaced_has_another_identity(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    old = Path(str(w.wt) + ".old")
    w.wt.rename(old)
    w.wt.mkdir()
    shutil.copy(old / ".git", w.wt / ".git")
    d = w.st(capsys)
    assert step_of(d) in ("root", "path")
    unlinked(d)


def test_worktree_directory_replaced_by_a_symlink(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    old = Path(str(w.wt) + ".old")
    w.wt.rename(old)
    os.symlink(old, w.wt)
    unlinked(w.st(capsys))


def test_git_common_dir_disagreement(w: RW, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import run_git

    def runner(argv, **kw):  # noqa: ANN001, ANN202
        r = run_git(argv, **kw)
        if "--git-common-dir" in argv:
            return GitResult(0, b"/not/the/common/dir\n", b"", False)
        return r

    w.start(capsys)
    w.git_runner = runner
    d = w.st(capsys)
    assert step_of(d) == "git_common_dir"
    assert d["checks"]["integrity"]["details"][0]["actual"] == "/not/the/common/dir"
    unlinked(d)


def test_git_failing_in_the_integrity_step_is_unlinked_not_pass(w: RW, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import run_git

    def runner(argv, **kw):  # noqa: ANN001, ANN202
        if "--git-common-dir" in argv:
            return GitResult(128, b"", b"fatal", False)
        return run_git(argv, **kw)

    w.start(capsys)
    w.git_runner = runner
    assert step_of(w.st(capsys)) == "git_common_dir"


def test_forged_pointers_do_not_make_status_run_git_in_the_forged_place(
    w: RW,
    capsys,
    tmp_path: Path,  # noqa: ANN001
) -> None:
    marker = tmp_path / "ran"
    other = tmp_path / "evil"
    w.start(capsys)
    shutil.copytree(w.clone / ".git", other)
    (other / "config").write_text(f"[core]\n\tfsmonitor = touch {marker}\n")
    (w.wt / ".git").write_text(f"gitdir: {other}\n")
    w.st(capsys)
    assert not marker.exists()


# -- locked ------------------------------------------------------------------------------------


def test_locked_worktree(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["worktree", "lock", "--reason", "on a usb stick", str(w.wt)])
    d = w.st(capsys)
    assert d["checks"]["locked"] == {
        "status": "fail",
        "details": [{"lock_reason": "on a usb stick\n"}],
    }
    assert (d["would_remove"], d["reason"]) == (False, "locked")
    assert d["force_overrides"] == []  # nothing overrides a lock


def test_lock_file_that_is_a_directory_or_fifo_still_locks(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.admin / "locked").mkdir()
    assert w.st(capsys)["reason"] == "locked"
    (w.admin / "locked").rmdir()
    os.mkfifo(w.admin / "locked")
    assert w.st(capsys)["reason"] == "locked"  # never opened: no hang


# -- baseline ----------------------------------------------------------------------------------


def test_git_class_baseline_change_refuses(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["config", "alias.zz", "status"])
    d = w.st(capsys)
    assert d["checks"]["baseline"]["status"] == "fail"
    assert d["checks"]["baseline"]["details"][0]["class"] == "git"
    assert (d["would_remove"], d["reason"]) == (False, "repo_config_changed")
    assert d["force_overrides"] == []  # --force does not override it


def test_claude_class_baseline_change_is_a_warning_only(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec(
        "DELETE FROM baseline_items WHERE record_id = ? AND class = 'claude'", w.rid
    )  # no-op when none were recorded
    (w.wt / "CLAUDE.md").write_text("x")
    d = w.st(capsys)
    # an untracked CLAUDE.md is dirty and a baseline change at once; the baseline part is a warn
    assert d["checks"]["baseline"]["status"] == "warn"
    assert d["checks"]["baseline"]["details"][0]["class"] == "claude"
    assert (d["would_remove"], d["reason"]) == (False, "dirty")
    assert any("claude" in x for x in d["warnings"])


def test_digest_only_disagreement_is_a_git_class_failure(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET baseline_digest = ? WHERE id = ?", "f" * 64, w.rid)
    d = w.st(capsys)
    assert d["checks"]["baseline"]["status"] == "fail"
    assert d["reason"] == "repo_config_changed"


# -- held --------------------------------------------------------------------------------------


def test_holders_other_than_mine_block(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "agent-1")
    d = w.st(capsys)
    assert d["checks"]["held"] == {"status": "fail", "details": [{"holder": "agent-1"}]}
    assert (d["would_remove"], d["reason"]) == (False, "held")
    assert d["force_overrides"] == ["held"]
    mine = w.st(capsys, "--holder", "agent-1")
    assert mine["checks"]["held"]["status"] == "pass" and mine["would_remove"] is True
    other = w.st(capsys, "--holder", "agent-2")
    assert other["checks"]["held"]["details"] == [{"holder": "agent-1"}]


# -- in use -----------------------------------------------------------------------------------


def test_in_use_by_cwd(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.scanner.procs = [ProcessInfo(4242, "vim", str(w.wt / "sub"), ())]
    d = w.st(capsys)
    assert d["checks"]["in_use"] == {
        "status": "fail",
        "details": [{"pid": 4242, "command": "vim", "path": str(w.wt / "sub")}],
    }
    assert (d["would_remove"], d["reason"]) == (False, "in_use")
    assert d["force_overrides"] == []  # nothing overrides in_use
    assert w.scanner.targets and w.scanner.targets[-1] == str(w.wt)


def test_scanner_unavailable_is_never_a_clean_bill(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.scanner.fail = True
    d = w.st(capsys)
    assert d["checks"]["in_use"] == {"status": "unavailable", "details": []}
    assert d["would_remove"] is None and d["reason"] is None
    assert any("in-use" in x for x in d["warnings"])


# -- precedence --------------------------------------------------------------------------------


def test_reason_precedence(w: RW, capsys) -> None:  # noqa: ANN001
    """main_worktree, unlinked, locked, repo_config_changed, in_use, held, dirty, unpushed."""
    w.start(capsys, "--holder", "h")
    w.push_work("a")  # unpushed
    (w.wt / "dirty.txt").write_text("x")  # dirty
    me = ("--holder", "h")  # the caller's own holder does not block
    d = w.st(capsys, *me)
    assert d["reason"] == "dirty"
    assert "untracked" in d["force_overrides"] and "unpushed_head" in d["force_overrides"]
    assert w.st(capsys)["reason"] == "held"
    w.scanner.procs = [ProcessInfo(1, "x", str(w.wt), ())]
    assert w.st(capsys, *me)["reason"] == "in_use"
    w.g(["config", "alias.zz", "status"])
    assert w.st(capsys, *me)["reason"] == "repo_config_changed"
    w.g(["worktree", "lock", str(w.wt)])
    assert w.st(capsys, *me)["reason"] == "locked"
    (w.admin / "gitdir").write_text("/x/.git\n")
    assert w.st(capsys, *me)["reason"] == "unlinked"
    # an adopted main worktree is refused before anything else
    clone_id = os.stat(w.clone)
    w.db_exec(
        "UPDATE records SET path = ?, dir_id = ?, admin_dir = NULL, base_dir = NULL, owned = 0 "
        "WHERE id = ?",
        str(w.clone), f"{clone_id.st_dev}:{clone_id.st_ino}", w.rid,
    )  # fmt: skip
    d = w.st(capsys, *me)
    assert (d["would_remove"], d["reason"], d["human_required"]) == (False, "main_worktree", True)


def test_dirty_beats_unpushed_and_both_are_reported(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.push_work("a")
    (w.wt / "c1.txt").write_text("edit")
    d = w.st(capsys)
    assert d["reason"] == "dirty"
    assert d["checks"]["unpushed_head"]["status"] == "fail"


# -- record states ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["creating", "unlinked", "missing", "clone_missing"])
def test_states_that_are_not_active_report_unlinked(w: RW, capsys, state: str) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET state = ? WHERE id = ?", state, w.rid)
    d = w.st(capsys)
    unlinked(d)
    assert d["checks"]["in_use"]["status"] == "pass"  # still checked


@pytest.mark.parametrize("state", ["removed", "gone", "forgotten"])
def test_tombstones_are_not_found(w: RW, capsys, state: str) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET state = ?, removed_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
              state, w.rid)  # fmt: skip
    r = w.status(capsys)
    assert r.code == 10
    validate("wtctl/cli/error.json", r.json())


def test_unknown_record_is_not_found(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    assert w.status(capsys, target="wt-aaaaaaaa").code == 10


def test_selector_by_path_and_prefix(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    assert w.st(capsys, target=str(w.wt))["id"] == w.rid
    assert w.st(capsys, target=w.rid[:7])["id"] == w.rid


def test_adopted_linked_worktree_needs_a_human(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET owned = 0 WHERE id = ?", w.rid)
    d = w.st(capsys)
    assert d["human_required"] is True and d["would_remove"] is True


def test_partial_with_intact_pointers_runs_the_normal_checks(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec(
        "UPDATE records SET state = 'partial', last_error = 'could not delete x' WHERE id = ?",
        w.rid,
    )
    (w.wt / "left.txt").write_text("x")
    d = w.st(capsys)
    assert d["human_required"] is True and d["reason"] == "dirty"
    assert any("partial" in x for x in d["warnings"])


def test_partial_whose_pointers_are_gone_skips_content_checks(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    (w.wt / ".git").unlink()
    (w.wt / "left.txt").write_text("x")  # unchecked: the confirmation is the human's look
    d = w.st(capsys)
    assert d["human_required"] is True
    assert (d["would_remove"], d["reason"]) == (True, None)
    assert d["checks"]["untracked"]["status"] == "skipped"
    assert any("partial" in x for x in d["warnings"])
    w.scanner.procs = [ProcessInfo(9, "sh", str(w.wt), ())]
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (False, "in_use")


def test_partial_whose_directory_is_another_directory_is_unlinked(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    (w.wt / ".git").unlink()
    old = Path(str(w.wt) + ".old")
    w.wt.rename(old)
    w.wt.mkdir()
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (False, "unlinked")


# -- throwaways ------------------------------------------------------------------------------


def make_throwaway(w: RW, capsys) -> dict:  # noqa: ANN001
    """A throwaway record and its directory (``create --throwaway`` is another issue)."""
    from wtctl.gateway import Gateway
    from wtctl.state import State

    root = w.tmp / "throw"
    root.mkdir()
    st = State(w.db, Gateway())
    rid = st.allocate_id()
    path = root / rid
    path.mkdir()
    (path / "f.txt").write_text("f")
    ident = lambda p: f"{os.stat(p).st_dev}:{os.stat(p).st_ino}"  # noqa: E731
    st.insert_record(
        {
            "id": rid, "object_type": "throwaway", "state": "active", "repo": REPO,
            "path": str(path), "base_dir": str(root), "root_id": ident(root),
            "dir_id": ident(path), "clone_path": str(w.clone),
        },
        holders=[],
    )  # fmt: skip
    st.record_anchor(str(root), ident(root))
    st.close()
    return {"id": rid, "path": str(path)}


def test_throwaway_has_only_the_in_use_check(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    t = make_throwaway(w, capsys)
    d = w.st(capsys, target=t["id"])
    assert list(d["checks"]) == ["in_use"]
    assert (d["object_type"], d["would_remove"], d["human_required"]) == ("throwaway", True, False)
    assert d["head"] is None and d["current_branch"] is None
    (Path(t["path"]) / "scratch.txt").write_text("anything")  # throwaways are never content-checked
    assert w.st(capsys, target=t["id"])["would_remove"] is True
    w.scanner.procs = [ProcessInfo(3, "make", t["path"], ())]
    d = w.st(capsys, target=t["id"])
    assert (d["would_remove"], d["reason"]) == (False, "in_use")


def test_throwaway_refuses_worktree_only_flags(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    t = make_throwaway(w, capsys)
    assert w.status(capsys, "--holder", "x", target=t["id"]).code == 2
    assert w.status(capsys, "--merged-head", "a" * 40, target=t["id"]).code == 2


def test_throwaway_state_missing_is_unlinked(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    t = make_throwaway(w, capsys)
    w.db_exec("UPDATE records SET state = 'missing' WHERE id = ?", t["id"])
    d = w.st(capsys, target=t["id"])
    assert (d["would_remove"], d["reason"]) == (False, "unlinked")


# -- remove --dry-run ------------------------------------------------------------------------


def test_dry_run_is_status_plus_the_flag(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "x.txt").write_text("x")
    s = w.st(capsys)
    r = w.status(capsys, "--dry-run", cmd=("remove",))
    assert r.code == 0, (r.out, r.err)  # exit 0 whatever it found
    d = r.json()
    validate("wtctl/cli/remove.json", d)
    assert d.pop("dry_run") is True
    assert d == s


def test_dry_run_ignores_force_keep_branch_and_yes_but_human_only_flags_need_a_human(
    w: RW,
    capsys,  # noqa: ANN001
) -> None:
    w.start(capsys)
    r = w.status(capsys, "--dry-run", "--keep-branch", "--yes", cmd=("remove",))
    assert r.code == 0 and r.json()["dry_run"] is True
    assert w.status(capsys, "--dry-run", "--force", cmd=("remove",)).code == 2
    assert w.status(capsys, "--dry-run", "--force", cmd=("remove",), human=True).code == 0


def test_dry_run_takes_merged_head_and_holder(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "me")
    r = w.status(capsys, "--dry-run", "--holder", "me", "--merged-head", "a" * 40, cmd=("remove",))
    assert r.code == 0 and r.json()["checks"]["held"]["status"] == "pass"


def test_dry_run_changes_nothing_on_disk(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    before = (w.snapshot(), w.rows("SELECT * FROM records"))
    w.status(capsys, "--dry-run", cmd=("remove",))
    w.status(capsys)
    after = (w.snapshot(), w.rows("SELECT * FROM records"))
    assert before[0] == after[0]
    assert [tuple(r) for r in before[1]] == [tuple(r) for r in after[1]]
    assert w.wt.is_dir()


def test_remove_without_dry_run_is_not_implemented_here(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    r = w.status(capsys, "--yes", cmd=("remove",))
    assert r.code == 1 and w.wt.is_dir()


def test_status_rejects_dry_run(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    assert w.status(capsys, "--dry-run").code == 2


# -- golden ----------------------------------------------------------------------------------


def normalized(d: dict[str, Any], w: RW) -> str:
    import json

    text = json.dumps(d, indent=1, sort_keys=False)
    for real, fake in (
        (w.rid, "<id>"), (str(w.admin), "<admin>"), (str(w.wt), "<wt>"),
        (w.g(["rev-parse", "HEAD"], w.wt), "<head>"),
    ):  # fmt: skip
        text = text.replace(real, fake)
    return text


def test_golden_json_of_a_dirty_worktree(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "agent-1")
    (w.wt / "notes.txt").write_text("keep")
    (w.wt / "c1.txt").write_text("edit")
    d = w.st(capsys, "--holder", "agent-1")
    golden = Path(__file__).parent / "golden" / "status_dirty.json"
    text = normalized(d, w) + "\n"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(text)
    assert text == golden.read_text()


def test_golden_json_of_a_clean_worktree(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    d = w.st(capsys)
    golden = Path(__file__).parent / "golden" / "status_clean.json"
    text = normalized(d, w) + "\n"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(text)
    assert text == golden.read_text()


def test_human_output_is_one_verdict_line_and_the_failures(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "new\nline.txt").write_text("x")
    import contextlib
    import io

    from wtctl.cli import main
    from wtctl.context import Runtime
    from wtctl.gateway import Gateway

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rt = Runtime(stdin_isatty=False, me_name="a", gateway=Gateway(scanner=w.scanner))
        main(["--config", w.config(), "--actor", "auto:t", "status", w.rid], runtime=rt)
    text = out.getvalue()
    assert f"{w.rid}: would NOT be removed (dirty)" in text
    assert "untracked: fail" in text and "\n  untracked" in text
    assert REPO and "new\nline" not in text


# -- bootstrap re-run: in_use -------------------------------------------------------------------


def test_bootstrap_rerun_is_refused_while_a_process_uses_the_worktree(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.scanner.procs = [ProcessInfo(77, "vim", str(w.wt), ())]
    r = w.status(capsys, cmd=("bootstrap",), human=True)
    assert r.code == 20, (r.out, r.err)
    err = r.json()["error"]
    validate("wtctl/cli/error.json", r.json())
    assert err["reason"] == "in_use" and err["processes"][0]["pid"] == 77
    assert w.status(capsys, "--dry-run", cmd=("bootstrap",), human=True).code == 20
    w.scanner.procs = []
    assert w.status(capsys, cmd=("bootstrap",), human=True).code == 0


# -- end to end: real child processes and the real scanner ----------------------------------------


@pytest.fixture
def real_w(tmp_path: Path, private_home: Path) -> RW:
    """No in-process runner: every walk runs in its own child under the deadline."""
    return RW(tmp_path, private_home)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")
def test_status_end_to_end_with_child_walks_and_the_real_process_scanner(
    real_w: RW,
    capsys,  # noqa: ANN001
) -> None:
    from tests.test_inuse import Sleeper

    w = real_w
    w.start(capsys)
    w.scanner = ProcScanner()  # type: ignore[assignment]
    (w.wt / "sub").mkdir()
    d = w.st(capsys)
    assert d["would_remove"] is True, d
    sleeper = Sleeper("", w.wt / "sub", w.tmp / "ready")
    try:
        d = w.st(capsys)
        assert (d["would_remove"], d["reason"]) == (False, "in_use")
        assert [p["pid"] for p in d["checks"]["in_use"]["details"]] == [sleeper.pid]
        (w.wt / "vendor" / "x").mkdir(parents=True)
        w.g(["init", "-q"], w.wt / "vendor" / "x")
        d = w.st(capsys)
        assert d["checks"]["nested_repos"]["details"] == [
            {"detail": "nested_repo", "path": "vendor/x/.git"}
        ]
    finally:
        sleeper.stop()  # killed by its recorded PID only
