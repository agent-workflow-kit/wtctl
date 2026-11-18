"""Recursive submodule checks of a removal (docs/interface.md, Content checks, Submodules).

Real git throughout: a bare submodule origin, a worktree with the submodule initialised (its git
dir under the admin entry), and hostile edits of everything an agent can write there.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import script
from tests.removelib import RR
from wtctl import fsafe, fsafe_child, removal_content, removal_submodules
from wtctl.gateway import GitResult, run_git
from wtctl.mirror import Pushed

SUB = "vendor/sub"
FILE_ALLOW = ["-c", "protocol.file.allow=always"]


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RR:  # noqa: ANN001
    return RR(tmp_path, private_home)


def bare_origin(w: RR, name: str, *, nested: Path | None = None, at: str = "inner") -> Path:
    """A bare repository with one commit (and, with ``nested``, a gitlink to it at ``at``)."""
    src = w.tmp / f"{name}-src"
    src.mkdir()
    w.g(["init", "-q", "-b", "main"], src)
    (src / "f.txt").write_text(name)
    w.g(["add", "."], src)
    if nested is not None:
        w.g([*FILE_ALLOW, "submodule", "add", "-q", str(nested), at], src)
    w.g(["commit", "-q", "-m", name], src)
    bare = w.tmp / f"{name}.git"
    w.g(["clone", "-q", "--bare", str(src), str(bare)], w.tmp)
    return bare


def add_sub(w: RR, capsys, origin: Path, rel: str = SUB, *, push: bool = True) -> Path:  # noqa: ANN001
    """Initialise ``origin`` as a submodule of the worktree, commit and push the superproject."""
    w.g([*FILE_ALLOW, "submodule", "add", "-q", str(origin), rel], w.wt)
    w.g([*FILE_ALLOW, "submodule", "update", "--init", "--recursive", "-q"], w.wt)
    w.g(["commit", "-q", "-m", f"add {rel}"], w.wt)
    if push:
        w.push()
    # a new submodule git dir is a repo-config baseline change: a human accepts it
    assert w.cli(capsys, "baseline", "accept", w.rid, "--yes", human=True).code == 0
    return w.wt / rel


def gitdir_of(w: RR, rel: str = SUB) -> Path:
    return w.admin / "modules" / rel


def world_with_sub(w: RR, capsys, rel: str = SUB) -> Path:  # noqa: ANN001
    w.start(capsys)
    return add_sub(w, capsys, bare_origin(w, "sub"), rel)


def details(d: dict[str, Any], name: str = "nested_repos") -> list[dict[str, Any]]:
    return d["checks"][name]["details"]


def sub_detail(path: str, reason: str) -> dict[str, Any]:
    return {"detail": "submodule", "path": path, "reason": reason}


def assert_refused_dirty(d: dict[str, Any], path: str, reason: str) -> None:
    assert sub_detail(path, reason) in details(d), details(d)
    assert (d["would_remove"], d["reason"]) == (False, "dirty")


# -- clean and pushed: removed without --force ----------------------------------------------------


def test_clean_pushed_submodule_passes_every_check(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    assert (sub / ".git").is_file() and gitdir_of(w).is_dir()
    d = w.st(capsys)
    assert d["checks"]["nested_repos"] == {"status": "pass", "details": []}
    assert (d["would_remove"], d["reason"]) == (True, None)
    assert d["force_overrides"] == []


def test_clean_pushed_submodule_is_removed_without_force(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    out = w.removed()
    assert out["record"]["state"] == "removed"
    assert not w.wt.exists() and not w.admin.exists()


def test_clean_pushed_submodule_after_an_update_with_a_new_pointer(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["checkout", "-q", "-b", "next"], sub)
    (sub / "g.txt").write_text("g")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "g"], sub)
    w.g(["push", "-q", "origin", "next"], sub)
    w.g(["checkout", "-q", "--detach", "HEAD"], sub)
    w.g(["branch", "-q", "-D", "next"], sub)
    w.g(["add", SUB], w.wt)
    w.g(["commit", "-q", "-m", "bump"], w.wt)
    w.push()
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (True, None), (
        d["checks"]["unpushed_head"],
        details(d),
    )


def test_nested_clean_submodules_pass(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    inner = bare_origin(w, "inner")
    outer = bare_origin(w, "outer", nested=inner, at="deep")
    add_sub(w, capsys, outer)
    assert (w.wt / SUB / "deep" / ".git").is_file()
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (True, None), d["checks"]
    assert w.removed()["record"]["state"] == "removed"


def test_an_uninitialised_submodule_still_passes(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    add_sub(w, capsys, bare_origin(w, "sub"))
    w.g(["submodule", "deinit", "-f", SUB], w.wt)
    import shutil

    shutil.rmtree(gitdir_of(w))  # the git dir goes with the deinit here: nothing is left to lose
    d = w.st(capsys)
    assert d["checks"]["nested_repos"]["status"] == "pass", details(d)


# -- dirty ----------------------------------------------------------------------------------------


def test_an_edited_tracked_file_in_the_submodule_refuses_and_names_it(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (sub / "f.txt").write_text("edited")
    assert_refused_dirty(w.st(capsys), SUB, "tracked")
    err = w.refused()
    assert err["reason"] == "dirty" and w.intact()


def test_a_staged_change_in_the_submodule_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (sub / "f.txt").write_text("staged")
    w.g(["add", "f.txt"], sub)
    assert_refused_dirty(w.st(capsys), SUB, "tracked")


def test_an_untracked_file_in_the_submodule_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (sub / "notes.txt").write_text("unsaved")
    assert_refused_dirty(w.st(capsys), SUB, "untracked")


def test_an_ignored_file_in_the_submodule_refuses_unless_disposable(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (gitdir_of(w) / "info").mkdir(exist_ok=True)
    (gitdir_of(w) / "info" / "exclude").write_text("*.keep\nnode_modules/\n")
    (sub / "a.keep").write_text("x")
    d = w.st(capsys)
    assert_refused_dirty(d, SUB, "ignored")
    (sub / "a.keep").unlink()
    (sub / "node_modules").mkdir()
    (sub / "node_modules" / "x.js").write_text("x")
    d = w.st(capsys)
    assert d["checks"]["nested_repos"]["status"] == "pass", details(d)


def test_a_hidden_change_in_the_submodule_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["update-index", "--assume-unchanged", "f.txt"], sub)
    (sub / "f.txt").write_text("hidden edit")
    assert_refused_dirty(w.st(capsys), SUB, "hidden_changes")


def test_a_stash_in_the_submodule_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (sub / "f.txt").write_text("stashed work")
    w.g(["stash", "-q"], sub)
    assert_refused_dirty(w.st(capsys), SUB, "stash")


def test_info_attributes_of_the_submodule_refuse(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    (gitdir_of(w) / "info").mkdir(exist_ok=True)
    (gitdir_of(w) / "info" / "attributes").write_text("* ident\n")
    assert_refused_dirty(w.st(capsys), SUB, "attributes")


def test_a_staged_pointer_change_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (sub / "g.txt").write_text("g")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "g"], sub)
    w.g(["push", "-q", "origin", "HEAD:main"], sub)
    w.g(["add", SUB], w.wt)  # staged in the superproject, not committed: status never shows it
    assert_refused_dirty(w.st(capsys), SUB, "gitlink_changed")


def test_a_dirty_nested_submodule_is_named_by_its_full_path(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    inner = bare_origin(w, "inner")
    add_sub(w, capsys, bare_origin(w, "outer", nested=inner, at="deep"))
    (w.wt / SUB / "deep" / "f.txt").write_text("edited")
    assert_refused_dirty(w.st(capsys), f"{SUB}/deep", "tracked")


def test_a_nested_repo_hidden_in_a_disposable_dir_of_the_submodule_refuses(
    w: RR,
    capsys,  # noqa: ANN001
) -> None:
    sub = world_with_sub(w, capsys)
    (gitdir_of(w) / "info").mkdir(exist_ok=True)
    (gitdir_of(w) / "info" / "exclude").write_text("node_modules/\n")
    deep = sub / "node_modules" / "pkg"
    deep.mkdir(parents=True)
    w.g(["init", "-q"], deep)
    d = w.st(capsys)
    assert {"detail": "nested_repo", "path": f"{SUB}/node_modules/pkg/.git"} in details(d)
    assert d["would_remove"] is False


# -- unpushed -------------------------------------------------------------------------------------


def assert_refused_unpushed(d: dict[str, Any], path: str) -> None:
    got = [x for x in details(d, "unpushed_head") if x.get("submodule") == path]
    assert got and all(x["detail"] == "submodule_unpushed" for x in got), details(
        d, "unpushed_head"
    )
    assert (d["would_remove"], d["reason"]) == (False, "unpushed")


def test_an_unpushed_commit_in_the_submodule_refuses_and_names_it(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (sub / "g.txt").write_text("g")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "local only"], sub)
    sha = w.g(["rev-parse", "HEAD"], sub)
    d = w.st(capsys)
    assert_refused_unpushed(d, SUB)
    assert details(d, "unpushed_head")[0]["sha"] == sha
    assert w.refused()["reason"] == "unpushed" and w.intact()


def test_a_pushed_commit_the_superproject_does_not_point_at_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (sub / "g.txt").write_text("g")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "g"], sub)
    w.g(["push", "-q", "origin", "HEAD:main"], sub)
    assert_refused_unpushed(w.st(capsys), SUB)  # the recorded pointer is the only evidence kept


def test_a_local_branch_of_the_submodule_with_its_own_commit_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["checkout", "-q", "-b", "side"], sub)
    (sub / "g.txt").write_text("g")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "side"], sub)
    w.g(["checkout", "-q", "--detach", "main"], sub)
    assert_refused_unpushed(w.st(capsys), SUB)


def test_a_forged_remote_tracking_ref_does_not_vouch_for_a_new_commit(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (sub / "g.txt").write_text("g")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "local only"], sub)
    w.g(["update-ref", "refs/remotes/origin/main", "HEAD"], sub)
    assert_refused_unpushed(w.st(capsys), SUB)


def test_a_local_tag_on_an_unpushed_commit_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (sub / "g.txt").write_text("g")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "g"], sub)
    w.g(["tag", "keep"], sub)
    w.g([*FILE_ALLOW, "checkout", "-q", "--detach", "main"], sub)
    assert_refused_unpushed(w.st(capsys), SUB)


def test_an_unknown_ref_namespace_in_the_submodule_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["update-ref", "refs/mywork/x", "HEAD"], sub)
    assert_refused_dirty(w.st(capsys), SUB, "refs")


def test_a_clean_submodule_beside_an_unpushed_one_names_only_the_unpushed(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    add_sub(w, capsys, bare_origin(w, "one"), "a/one")
    two = add_sub(w, capsys, bare_origin(w, "two"), "b/two")
    (two / "g.txt").write_text("g")
    w.g(["add", "."], two)
    w.g(["commit", "-q", "-m", "g"], two)
    d = w.st(capsys)
    assert [x["submodule"] for x in details(d, "unpushed_head")] == ["b/two"]
    assert details(d) == []


# -- hostile links --------------------------------------------------------------------------------


def set_gitfile(sub: Path, text: str) -> None:
    (sub / ".git").write_text(text)


def refused_unverified(d: dict[str, Any]) -> None:
    assert sub_detail(SUB, "gitdir") in details(d), details(d)
    assert (d["would_remove"], d["reason"]) == (False, "dirty")


def test_a_gitfile_pointing_outside_the_modules_area_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    elsewhere = w.tmp / "elsewhere.git"
    w.g(["clone", "-q", "--bare", str(w.tmp / "sub.git"), str(elsewhere)], w.tmp)
    set_gitfile(sub, f"gitdir: {elsewhere}\n")
    refused_unverified(w.st(capsys))


def test_a_gitfile_pointing_at_the_admin_entry_or_the_common_dir_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    for target in (w.admin, w.clone / ".git", w.admin / "modules"):
        set_gitfile(sub, f"gitdir: {target}\n")
        refused_unverified(w.st(capsys))


def test_a_relative_gitfile_that_escapes_with_dotdot_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    rel = os.path.relpath(w.tmp / "sub.git", sub)
    set_gitfile(sub, f"gitdir: {rel}\n")
    refused_unverified(w.st(capsys))
    set_gitfile(sub, f"gitdir: {os.path.relpath(gitdir_of(w), sub)}/../../../../../../sub.git\n")
    refused_unverified(w.st(capsys))


@pytest.mark.parametrize(
    "text",
    ["gitdir:\n", "gitdir: \n", "garbage\n", "gitdir: x\ny\n", "", "gitdir: /a\0b\n"],
)
def test_a_malformed_gitfile_refuses(w: RR, capsys, text: str) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    set_gitfile(sub, text)
    refused_unverified(w.st(capsys))


def test_a_symlinked_module_git_dir_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    real = gitdir_of(w)
    moved = w.tmp / "moved-gitdir"
    real.rename(moved)
    os.symlink(moved, real)
    d = w.st(capsys)
    assert sub_detail(SUB, "gitdir") in details(d), details(d)
    assert d["would_remove"] is False


def test_a_symlinked_modules_directory_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    mods = w.admin / "modules"
    moved = w.tmp / "moved-modules"
    mods.rename(moved)
    os.symlink(moved, mods)
    d = w.st(capsys)
    assert sub_detail(SUB, "gitdir") in details(d), details(d)
    assert d["would_remove"] is False


def test_a_module_git_dir_with_alternates_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    alt = gitdir_of(w) / "objects" / "info" / "alternates"
    alt.parent.mkdir(parents=True, exist_ok=True)
    alt.write_text(str(w.tmp / "sub.git" / "objects") + "\n")
    refused_unverified(w.st(capsys))


def test_a_module_git_dir_with_a_commondir_or_worktrees_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    (gitdir_of(w) / "commondir").write_text("../../../..\n")
    refused_unverified(w.st(capsys))


def test_a_module_git_dir_without_head_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    (gitdir_of(w) / "HEAD").unlink()
    d = w.st(capsys)
    assert d["would_remove"] is False
    assert any(x["detail"] == "submodule" and x["path"] == SUB for x in details(d)), details(d)


def test_two_submodules_sharing_one_git_dir_refuse(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    other = w.wt / "vendor" / "twin"
    other.mkdir()
    (other / ".git").write_text((sub / ".git").read_text())
    w.g(
        [
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{w.g(['rev-parse', 'HEAD'], sub)},vendor/twin",
        ],
        w.wt,
    )
    w.g(
        ["config", "-f", ".gitmodules", "submodule.twin.path", "vendor/twin"],
        w.wt,
    )
    w.g(["config", "-f", ".gitmodules", "submodule.twin.url", str(w.tmp / "sub.git")], w.wt)
    w.g(["add", ".gitmodules"], w.wt)
    w.g(["commit", "-q", "-m", "twin"], w.wt)
    d = w.st(capsys)
    assert sub_detail("vendor/twin", "gitdir") in details(d), details(d)
    assert w.refused()["reason"] in ("dirty", "repo_config_changed")


def test_an_embedded_git_dir_in_the_checkout_stays_a_nested_repo(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    import shutil

    shutil.rmtree(sub)
    w.g(["clone", "-q", str(w.tmp / "sub.git"), str(sub)], w.tmp)
    d = w.st(capsys)
    assert {"detail": "nested_repo", "path": f"{SUB}/.git"} in details(d), details(d)


def test_a_path_not_listed_in_gitmodules_stays_a_nested_repo(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["rm", "-q", "--cached", "-f", ".gitmodules"], w.wt)
    w.g(["commit", "-q", "-m", "drop gitmodules"], w.wt)
    d = w.st(capsys)
    assert {"detail": "nested_repo", "path": f"{SUB}/.git"} in details(d), details(d)
    assert d["would_remove"] is False and (sub / ".git").exists()


def test_a_planted_fsmonitor_and_hook_in_the_submodule_never_run(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    marker = w.tmp / "ran"
    gd = gitdir_of(w)
    cfg = gd / "config"
    cfg.write_text(
        cfg.read_text()
        + f"[core]\n\tfsmonitor = touch {marker}\n\thooksPath = {w.tmp}/hooks\n"
        + f"[diff]\n\texternal = touch {marker}\n"
        + f'[filter "x"]\n\tclean = touch {marker}\n\tsmudge = touch {marker}\n'
        + f'[url "{w.tmp}/nowhere/"]\n\tinsteadOf = {w.tmp}/sub.git\n'
    )
    hooks = gd / "hooks"
    hooks.mkdir(exist_ok=True)
    for h in ("post-checkout", "reference-transaction", "pre-commit"):
        (hooks / h).write_text(f"#!/bin/sh\ntouch {marker}\n")
        (hooks / h).chmod(0o755)
    (w.wt / SUB / ".gitattributes").write_text("* filter=x\n")
    d = w.st(capsys)
    assert not marker.exists()
    assert d["would_remove"] is False  # the planted config is a baseline change


def test_a_core_worktree_redirect_in_the_submodule_config_changes_nothing(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    decoy = w.tmp / "decoy"
    decoy.mkdir()
    w.g(["clone", "-q", str(w.tmp / "sub.git"), str(decoy / "c")], w.tmp)
    w.g(["config", "core.worktree", str(decoy / "c")], gitdir_of(w))
    (sub / "f.txt").write_text("edited in the real checkout")
    assert sub_detail(SUB, "tracked") in details(w.st(capsys))


# -- bounds ---------------------------------------------------------------------------------------


def test_a_depth_bomb_refuses_beyond_the_depth_limit(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    l3 = bare_origin(w, "l3")
    l2 = bare_origin(w, "l2", nested=l3, at="n")
    l1 = bare_origin(w, "l1", nested=l2, at="n")
    add_sub(w, capsys, l1)
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (True, None), d["checks"]
    monkeypatch.setattr(removal_submodules, "MAX_DEPTH", 2)
    d = w.st(capsys)
    assert sub_detail(f"{SUB}/n/n", "too_deep") in details(d), details(d)
    assert d["would_remove"] is False


def test_a_submodule_bomb_refuses_beyond_the_count_limit(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    for i in range(3):
        add_sub(w, capsys, bare_origin(w, f"s{i}"), f"m/s{i}", push=False)
    w.push()
    monkeypatch.setattr(removal_submodules, "MAX_SUBMODULES", 2)
    d = w.st(capsys)
    assert [x for x in details(d) if x.get("reason") == "too_many"], details(d)
    assert d["would_remove"] is False


def test_an_orphan_git_dir_below_a_submodule_git_dir_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    orphan = gitdir_of(w) / "modules" / "old"
    orphan.mkdir(parents=True)
    (orphan / "HEAD").write_text("ref: refs/heads/main\n")
    d = w.st(capsys)
    assert {"detail": "submodule", "path": str(orphan)} in [
        {"detail": x["detail"], "path": x["path"]} for x in details(d)
    ], details(d)
    assert d["would_remove"] is False


# -- helpers for the review round ----------------------------------------------------------------

GIT_ID = "-c user.name=t -c user.email=t@example.test"


def detached_commit(w: RR, sub: Path, name: str = "g") -> str:
    """A new commit on a detached HEAD: no branch holds it."""
    w.g(["checkout", "-q", "--detach"], sub)
    (sub / f"{name}.txt").write_text(name)
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", name], sub)
    return w.g(["rev-parse", "HEAD"], sub)


def side_commit(w: RR, sub: Path, name: str = "g") -> str:
    """A new commit on top of ``HEAD`` that no ref and no reflog reaches yet."""
    return w.g(["commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", name], sub)


def head_of(w: RR, sub: Path) -> str:
    return w.g(["rev-parse", "HEAD"], sub)


def unpushed_shas(d: dict[str, Any]) -> list[str]:
    return [x["sha"] for x in details(d, "unpushed_head") if x.get("submodule") == SUB]


def fake_sub_git(
    w: RR, match: Callable[[list[str]], bool], reply: Callable[[list[str], dict[str, Any]], Any]
) -> list[tuple[list[str], dict[str, Any]]]:
    """Answer the submodule's git commands that ``match`` with ``reply`` (which may raise)."""
    seen: list[tuple[list[str], dict[str, Any]]] = []
    gd = f"--git-dir={gitdir_of(w)}"

    def runner(argv: list[str], **kw: Any) -> GitResult:
        if gd in argv and match(argv):
            seen.append((list(argv), kw))
            return reply(argv, kw)  # type: ignore[no-any-return]
        return run_git(argv, **kw)

    w.git_runner = runner
    return seen


def is_rev_list(kind: str) -> Callable[[list[str]], bool]:
    """The four rev-list calls of the pushed check: the reflog, a, b and c."""

    def match(argv: list[str]) -> bool:
        if "rev-list" not in argv:
            return False
        if kind == "reflog":
            return "-g" in argv
        if kind == "branches":
            return "--stdin" in argv
        if kind == "tags":
            return "--tags" in argv
        return "--tags" not in argv and "-g" not in argv and "--stdin" not in argv

    return match


# -- R1: refs that name something other than a commit ---------------------------------------------


def make_object(w: RR, sub: Path, kind: str) -> str:
    (w.tmp / "payload.txt").write_text("only copy of this data")
    blob = w.g(["hash-object", "-w", str(w.tmp / "payload.txt")], sub)
    return blob if kind != "tree" else w.g(["rev-parse", "HEAD^{tree}"], sub)


@pytest.mark.parametrize("kind", ["blob", "tree"])
def test_a_lightweight_tag_of_a_non_commit_refuses(w: RR, capsys, kind: str) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["tag", "t", make_object(w, sub, kind)], sub)
    assert_refused_dirty(w.st(capsys), SUB, "refs")


def test_an_annotated_tag_of_a_blob_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["tag", "-a", "-m", "m", "a", make_object(w, sub, "blob")], sub)
    assert_refused_dirty(w.st(capsys), SUB, "refs")


def test_a_tag_of_a_tag_of_a_blob_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["tag", "-a", "-m", "m", "a", make_object(w, sub, "blob")], sub)
    w.g(["tag", "-a", "-m", "m", "b", "a"], sub)
    assert_refused_dirty(w.st(capsys), SUB, "refs")


def test_a_blob_under_refs_remotes_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["update-ref", "refs/remotes/origin/x", make_object(w, sub, "blob")], sub)
    assert_refused_dirty(w.st(capsys), SUB, "refs")


def test_an_annotated_tag_of_a_pushed_commit_passes(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["tag", "-a", "-m", "m", "rel", "HEAD"], sub)
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (True, None), details(d)


# -- R3: commits only the reflog still reaches ----------------------------------------------------


def test_a_commit_only_the_reflog_reaches_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    recorded = head_of(w, sub)
    w.g(["checkout", "-q", "-b", "tmp"], sub)
    (sub / "g.txt").write_text("g")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "reflog only"], sub)
    lost = head_of(w, sub)
    w.g(["checkout", "-q", "main"], sub)
    w.g(["update-ref", "-d", "refs/heads/tmp"], sub)
    assert head_of(w, sub) == recorded
    assert w.g(["rev-list", "--all", "--count"], sub) == "1"  # no ref reaches it any more
    d = w.st(capsys)
    assert lost in unpushed_shas(d), details(d, "unpushed_head")
    assert (d["would_remove"], d["reason"]) == (False, "unpushed")


def test_a_reflog_of_only_recorded_history_stays_clean(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["checkout", "-q", "--detach", "HEAD~0"], sub)
    w.g(["checkout", "-q", "main"], sub)
    assert w.g(["reflog", "show", "--format=%H", "HEAD"], sub)
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (True, None), details(d)


def test_a_fresh_checkout_of_an_older_pointer_stays_clean(w: RR, capsys) -> None:  # noqa: ANN001
    """The reflog of a submodule that was moved to a newer, pushed commit and recorded there."""
    sub = world_with_sub(w, capsys)
    (sub / "g.txt").write_text("g")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "g"], sub)
    w.g(["push", "-q", "origin", "HEAD:main"], sub)
    w.g(["add", SUB], w.wt)
    w.g(["commit", "-q", "-m", "bump"], w.wt)
    w.push()
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (True, None), details(d)


# -- R4: remote-tracking refs vouch for tags and the recorded commit ------------------------------


def test_a_tag_reachable_from_a_remote_tracking_ref_passes(w: RR, capsys) -> None:  # noqa: ANN001
    """Documents the trust model: remote-tracking refs are forgeable but vouch for tags."""
    sub = world_with_sub(w, capsys)
    other = side_commit(w, sub)
    w.g(["tag", "keep", other], sub)
    w.g(["update-ref", "refs/remotes/origin/other", other], sub)
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (True, None), details(d, "unpushed_head")


def test_the_same_tag_without_a_remote_tracking_ref_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    other = side_commit(w, sub)
    w.g(["tag", "keep", other], sub)
    d = w.st(capsys)
    assert unpushed_shas(d) == [other], details(d, "unpushed_head")


# -- R7: the pushed rules one by one --------------------------------------------------------------


def test_a_tag_on_an_unpushed_commit_that_no_branch_holds_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    lost = side_commit(w, sub)
    w.g(["tag", "keep", lost], sub)
    d = w.st(capsys)
    assert unpushed_shas(d) == [lost], details(d, "unpushed_head")


def test_a_recorded_commit_no_remote_tracking_ref_reaches_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    recorded = head_of(w, sub)
    for ref in w.g(["for-each-ref", "--format=%(refname)", "refs/remotes"], sub).split():
        w.g(["update-ref", "--no-deref", "-d", ref], sub)
    d = w.st(capsys)
    assert unpushed_shas(d) == [recorded], details(d, "unpushed_head")


@pytest.mark.parametrize("reflog", [True, False])
def test_a_detached_head_ahead_of_the_recorded_commit_refuses(
    w: RR,
    capsys,
    reflog: bool,  # noqa: ANN001
) -> None:
    sub = world_with_sub(w, capsys)
    ahead = detached_commit(w, sub)
    if not reflog:  # without a reflog only HEAD itself names the commit
        shutil.rmtree(gitdir_of(w) / "logs")
    d = w.st(capsys)
    assert unpushed_shas(d) == [ahead], details(d, "unpushed_head")


def test_a_branch_ahead_of_the_recorded_commit_refuses_without_any_reflog(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    recorded = head_of(w, sub)
    ahead = side_commit(w, sub)
    w.g(["update-ref", "refs/heads/side", ahead], sub)
    shutil.rmtree(gitdir_of(w) / "logs")
    assert head_of(w, sub) == recorded
    d = w.st(capsys)
    assert unpushed_shas(d) == [ahead], details(d, "unpushed_head")


def test_a_recorded_commit_missing_from_the_submodule_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    have = head_of(w, sub)
    w.g(["update-index", "--cacheinfo", f"160000,{'1' * 40},{SUB}"], w.wt)
    w.g(["commit", "-q", "-m", "points nowhere"], w.wt)
    w.push()
    d = w.st(capsys)
    assert have in unpushed_shas(d), details(d, "unpushed_head")


@pytest.mark.parametrize("text", ["garbage\n", "ref: refs/heads/unborn\n"])
def test_an_unreadable_head_of_the_submodule_refuses(w: RR, capsys, text: str) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    (gitdir_of(w) / "HEAD").write_text(text)
    assert_refused_dirty(w.st(capsys), SUB, "unreadable")


def test_a_head_that_does_not_resolve_to_a_commit_refuses_unreadable(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    fake_sub_git(
        w,
        lambda a: "HEAD^{commit}" in a and "rev-parse" in a,
        lambda a, kw: GitResult(0, b"not-a-sha\n", b""),
    )
    assert_refused_dirty(w.st(capsys), SUB, "unreadable")


@pytest.mark.parametrize("kind", ["reflog", "branches", "tags", "recorded"])
@pytest.mark.parametrize("failure", ["exit", "timeout", "garbage"])
def test_a_failing_rev_list_refuses_with_refs(w: RR, capsys, kind: str, failure: str) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    answer = {
        "exit": GitResult(128, b"", b"fatal"),
        "timeout": GitResult(0, b"", b"", True),
        "garbage": GitResult(0, b"zz\n", b""),
    }[failure]
    seen = fake_sub_git(w, is_rev_list(kind), lambda a, kw: answer)
    d = w.st(capsys)
    assert seen, "the rev-list call was never made"
    assert_refused_dirty(d, SUB, "refs")


def test_an_os_error_while_checking_a_submodule_refuses_unreadable(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)

    def boom(argv: list[str], kw: dict[str, Any]) -> GitResult:
        raise OSError("disk went away")

    fake_sub_git(w, is_rev_list("recorded"), boom)
    assert_refused_dirty(w.st(capsys), SUB, "unreadable")


# -- R5: the git calls honour the removal deadline ------------------------------------------------


def test_git_calls_in_a_submodule_are_capped_by_the_remaining_budget(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world_with_sub(w, capsys)
    monkeypatch.setattr(fsafe_child.Budget, "remaining", lambda self: 7.0)
    # (hardened git's own `config --list` probe has a fixed 30 second limit of its own)
    seen = fake_sub_git(w, lambda a: "--list" not in a, lambda a, kw: run_git(a, **kw))
    d = w.st(capsys)
    got = [kw.get("timeout") for _, kw in seen]
    assert seen and all(t is not None and 0 < t <= 7.0 for t in got), got
    assert d["would_remove"] is True


def expire_after_first_submodule_call(
    w: RR, monkeypatch: pytest.MonkeyPatch, rel: str = SUB
) -> None:
    """The budget has time until the first git command of the submodule has run."""
    state = {"spent": False}
    monkeypatch.setattr(
        fsafe_child.Budget, "remaining", lambda self: 0.0 if state["spent"] else 100.0
    )
    gd = f"--git-dir={gitdir_of(w, rel)}"

    def runner(argv: list[str], **kw: Any) -> GitResult:
        r = run_git(argv, **kw)
        if gd in argv:
            state["spent"] = True
        return r

    w.git_runner = runner


def test_an_exhausted_budget_during_a_submodule_check_refuses_with_deadline(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world_with_sub(w, capsys)
    expire_after_first_submodule_call(w, monkeypatch)
    assert_refused_dirty(w.st(capsys), SUB, "deadline")


def test_submodules_after_a_deadline_are_covered_without_more_findings(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    add_sub(w, capsys, bare_origin(w, "one"), "a/one", push=False)
    add_sub(w, capsys, bare_origin(w, "two"), "b/two")
    expire_after_first_submodule_call(w, monkeypatch, "a/one")
    d = w.st(capsys)
    subs = [x for x in details(d) if x.get("reason") or x["detail"] == "nested_repo"]
    assert subs == [sub_detail("a/one", "deadline")], subs


# -- R2: the step-9 re-check covers the submodules ------------------------------------------------


def mutate_sub(w: RR, body: str) -> None:
    w.top["hooks"] = {"pre-remove": [{"cmd": script(w.tmp / "m.sh", body)}]}


def test_a_submodule_edit_after_the_confirmation_refuses_even_forced(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    mutate_sub(w, f"echo edit >> {sub}/f.txt")
    err = w.refused("--force", human=True)
    assert err["reason"] == "dirty"
    assert sub_detail(SUB, "tracked") in err["checks"]["nested_repos"]["details"]
    assert w.intact() and sub.is_dir()


def test_a_new_untracked_file_in_a_submodule_after_the_confirmation_refuses(
    w: RR,
    capsys,  # noqa: ANN001
) -> None:
    sub = world_with_sub(w, capsys)
    mutate_sub(w, f"echo n > {sub}/late.txt")
    err = w.refused("--force", human=True)
    assert err["reason"] == "dirty"
    assert sub_detail(SUB, "untracked") in err["checks"]["nested_repos"]["details"]
    assert w.intact()


def test_a_new_commit_in_a_submodule_after_the_confirmation_refuses_unpushed(
    w: RR,
    capsys,  # noqa: ANN001
) -> None:
    sub = world_with_sub(w, capsys)
    mutate_sub(w, f"git -C {sub} {GIT_ID} commit -q --allow-empty -m late")
    err = w.refused("--force", human=True)
    assert err["reason"] == "unpushed"
    got = err["checks"]["unpushed_head"]["details"]
    assert [x["detail"] for x in got] == ["submodule_unpushed"] and got[0]["submodule"] == SUB
    assert w.intact()


def test_a_submodule_finding_already_forced_at_step_six_does_not_refuse_at_step_nine(
    w: RR,
    capsys,  # noqa: ANN001
) -> None:
    sub = world_with_sub(w, capsys)
    (sub / "old.txt").write_text("x")  # reported at step 6 and overridden
    mutate_sub(w, "true")
    out = w.removed("--force", human=True, tty=False)
    assert out["record"]["state"] == "removed"


def test_an_unchanged_submodule_passes_the_recheck(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    mutate_sub(w, "true")
    assert w.removed()["record"]["state"] == "removed"


# -- R7h: hostile git files and git directories ---------------------------------------------------


def test_an_oversized_gitfile_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    set_gitfile(sub, "gitdir: " + "a" * 5000 + "\n")
    refused_unverified(w.st(capsys))


@pytest.mark.parametrize("suffix", [" \n", "\r\n", "\t\n", "\n\n"])
def test_a_gitfile_naming_the_real_dir_with_trailing_junk_refuses(
    w: RR,
    capsys,
    suffix: str,  # noqa: ANN001
) -> None:
    sub = world_with_sub(w, capsys)
    set_gitfile(sub, f"gitdir: {gitdir_of(w)}{suffix}")
    refused_unverified(w.st(capsys))


def test_a_gitfile_naming_the_real_dir_after_two_spaces_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    set_gitfile(sub, f"gitdir:  {gitdir_of(w)}\n")
    refused_unverified(w.st(capsys))


def test_a_lookalike_git_dir_inside_the_common_dir_but_outside_modules_refuses(
    w: RR,
    capsys,  # noqa: ANN001
) -> None:
    sub = world_with_sub(w, capsys)
    look = w.clone / ".git" / "lookalike"
    shutil.copytree(gitdir_of(w), look)
    set_gitfile(sub, f"gitdir: {look}\n")
    refused_unverified(w.st(capsys))


def test_a_module_git_dir_whose_objects_is_not_a_directory_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    objects = gitdir_of(w) / "objects"
    objects.rename(w.tmp / "moved-objects")
    objects.write_text("not a directory")
    refused_unverified(w.st(capsys))


def test_a_worktrees_directory_in_the_module_git_dir_refuses_with_its_own_reason(
    w: RR,
    capsys,  # noqa: ANN001
) -> None:
    world_with_sub(w, capsys)
    (gitdir_of(w) / "worktrees").mkdir()
    assert_refused_dirty(w.st(capsys), SUB, "worktrees")


@pytest.mark.parametrize("as_what", ["dir", "link"])
def test_info_attributes_that_is_not_a_regular_file_refuses(
    w: RR,
    capsys,
    as_what: str,  # noqa: ANN001
) -> None:
    world_with_sub(w, capsys)
    info = gitdir_of(w) / "info"
    info.mkdir(exist_ok=True)
    if as_what == "dir":
        (info / "attributes").mkdir()
    else:
        os.symlink("/dev/null", info / "attributes")
    assert_refused_dirty(w.st(capsys), SUB, "attributes")


def test_info_that_is_a_file_refuses_as_attributes(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    info = gitdir_of(w) / "info"
    shutil.rmtree(info, ignore_errors=True)
    info.write_text("not a directory")
    assert_refused_dirty(w.st(capsys), SUB, "attributes")


@pytest.mark.parametrize("as_what", ["dir", "link"])
def test_alternates_that_is_not_a_regular_file_refuses(
    w: RR,
    capsys,
    as_what: str,  # noqa: ANN001
) -> None:
    world_with_sub(w, capsys)
    info = gitdir_of(w) / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    if as_what == "dir":
        (info / "alternates").mkdir()
    else:
        os.symlink("/dev/null", info / "alternates")
    refused_unverified(w.st(capsys))


def test_an_unopenable_objects_info_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    info = gitdir_of(w) / "objects" / "info"
    shutil.rmtree(info, ignore_errors=True)
    info.write_text("not a directory")
    refused_unverified(w.st(capsys))


def test_an_empty_alternates_file_is_not_a_problem(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    info = gitdir_of(w) / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text("")
    d = w.st(capsys)
    assert d["checks"]["nested_repos"]["status"] == "pass", details(d)


# -- R7i: inside a submodule ----------------------------------------------------------------------


def nested_world(w: RR, capsys) -> Path:  # noqa: ANN001
    w.start(capsys)
    inner = bare_origin(w, "inner")
    return add_sub(w, capsys, bare_origin(w, "outer", nested=inner, at="deep"))


@pytest.mark.parametrize(
    ("state", "reason"),
    [("not_dir", "tracked"), ("unreadable", "tracked"), ("contents", "gitlink_contents")],
)
def test_a_gitlink_state_inside_a_submodule_is_reported(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    reason: str,
) -> None:
    nested_world(w, capsys)
    real = removal_content.Hasher.gitlink_state

    def fake(self: Any, rel: str) -> str:
        return state if rel == f"{SUB}/deep" else real(self, rel)  # type: ignore[no-any-return]

    monkeypatch.setattr(removal_content.Hasher, "gitlink_state", fake)
    d = w.st(capsys)
    assert sub_detail(SUB, reason) in details(d), details(d)


def test_a_file_written_into_a_deinitialised_nested_submodule_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    sub = nested_world(w, capsys)
    w.g(["submodule", "deinit", "-f", "deep"], sub)
    (sub / "deep" / "x.txt").write_text("hidden from git")
    d = w.st(capsys)
    assert sub_detail(SUB, "gitlink_contents") in details(d), details(d)


def test_an_unmerged_entry_in_the_submodule_refuses_as_tracked(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    blob = w.g(["rev-parse", "HEAD:f.txt"], sub)
    index = f"0 {'0' * 40}\tf.txt\n100644 {blob} 1\tf.txt\n100644 {blob} 2\tf.txt\n"
    w.g(["update-index", "--index-info"], sub, stdin=index.encode())
    assert_refused_dirty(w.st(capsys), SUB, "tracked")


def test_a_file_named_like_a_directory_only_disposable_pattern_is_ignored_content(
    w: RR,
    capsys,  # noqa: ANN001
) -> None:
    sub = world_with_sub(w, capsys)
    (gitdir_of(w) / "info").mkdir(exist_ok=True)
    (gitdir_of(w) / "info" / "exclude").write_text("node_modules\n")
    (sub / "node_modules").write_text("a file, not a directory")
    assert_refused_dirty(w.st(capsys), SUB, "ignored")


def test_more_refs_than_the_limit_refuse(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world_with_sub(w, capsys)
    monkeypatch.setattr(removal_submodules, "MAX_REFS", 2)
    assert_refused_dirty(w.st(capsys), SUB, "refs")


def test_submodules_after_the_count_limit_are_covered_without_more_findings(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    for i in range(4):
        add_sub(w, capsys, bare_origin(w, f"s{i}"), f"m/s{i}", push=False)
    w.push()
    monkeypatch.setattr(removal_submodules, "MAX_SUBMODULES", 2)
    d = w.st(capsys)
    found = [x for x in details(d) if x.get("reason") or x["detail"] == "nested_repo"]
    assert [x.get("reason") for x in found] == ["too_many"], found


def test_a_failure_of_the_whole_walk_is_incomplete(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world_with_sub(w, capsys)

    def boom(self: Any) -> None:
        raise fsafe.FsafeError("identity", "gone")

    monkeypatch.setattr(removal_submodules._Walk, "run", boom)
    d = w.st(capsys)
    assert {"detail": "incomplete", "path": None} in details(d)
    assert d["would_remove"] is False


def test_an_orphan_walk_that_is_cut_short_is_incomplete(
    w: RR,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world_with_sub(w, capsys)
    (gitdir_of(w) / "modules" / "a" / "b").mkdir(parents=True)
    monkeypatch.setattr(removal_content, "MODULES_MAX_DEPTH", 0)
    d = w.st(capsys)
    assert {"detail": "incomplete", "path": None} in details(d), details(d)


def test_an_unopenable_modules_directory_below_a_submodule_is_incomplete(w: RR, capsys) -> None:  # noqa: ANN001
    world_with_sub(w, capsys)
    os.symlink(w.tmp, gitdir_of(w) / "modules")
    d = w.st(capsys)
    assert {"detail": "incomplete", "path": None} in details(d), details(d)
    assert d["would_remove"] is False


def test_entries_below_a_refused_submodule_are_not_reported_again(w: RR, capsys) -> None:  # noqa: ANN001
    sub = nested_world(w, capsys)
    set_gitfile(sub, "garbage\n")
    d = w.st(capsys)
    assert sub_detail(SUB, "gitdir") in details(d)
    assert not [x for x in details(d) if "deep" in (x.get("path") or "")], details(d)


def test_outcome_covers_exact_entries_and_everything_below_a_refused_directory() -> None:
    out = removal_submodules.Outcome()
    out.handled.add(("a", ".git"))
    out.refused_dirs.add(("r",))
    assert out.covers(("a", ".git"))
    assert out.covers(("r", ".git")) and out.covers(("r", "x", "y", ".git"))
    assert not out.covers(("b", ".git")) and not out.covers(("rr", ".git"))
    assert not out.covers(("a", "c", ".git"))


# -- R7j: the unpushed_head check with submodule findings -----------------------------------------


class _Mirror:
    def __init__(self, pushed: Pushed) -> None:
        self.answer = pushed

    def is_pushed(self, sha: str, tips: object) -> Pushed:
        return self.answer


def _inputs(
    extra: list[dict[str, Any]], *, tips: object | None, pushed: Pushed | None = None
) -> removal_content.Inputs:
    return removal_content.Inputs(
        git=None,  # type: ignore[arg-type]
        state=None,
        record={},
        config=None,
        disposable=[],
        mirror=_Mirror(pushed) if pushed else None,  # type: ignore[arg-type]
        tips=tips,  # type: ignore[arg-type]
        sub_unpushed=extra,
    )


EXTRA = [{"sha": "b" * 40, "detail": "submodule_unpushed", "submodule": SUB}]
HEAD = "a" * 40


def test_submodule_findings_fail_unpushed_head_without_tips() -> None:
    got = removal_content.unpushed_head(_inputs(EXTRA, tips=None), HEAD)
    assert got == {"status": "fail", "details": EXTRA}


def test_no_tips_and_no_submodule_findings_skips() -> None:
    assert removal_content.unpushed_head(_inputs([], tips=None), HEAD)["status"] == "skipped"


def test_submodule_findings_fail_a_pushed_head() -> None:
    inp = _inputs(EXTRA, tips=object(), pushed=Pushed(True))
    assert removal_content.unpushed_head(inp, HEAD) == {"status": "fail", "details": EXTRA}


def test_submodule_findings_follow_the_finding_of_an_unpushed_head() -> None:
    inp = _inputs(EXTRA, tips=object(), pushed=Pushed(False, "not_ancestor"))
    got = removal_content.unpushed_head(inp, HEAD)
    assert got["status"] == "fail"
    assert got["details"] == [{"sha": HEAD, "detail": "not_reachable"}, *EXTRA]


def test_a_pushed_head_without_submodule_findings_passes() -> None:
    inp = _inputs([], tips=object(), pushed=Pushed(True))
    assert removal_content.unpushed_head(inp, HEAD) == {"status": "pass", "details": []}
