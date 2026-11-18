"""Recursive submodule checks of a removal (docs/interface.md, Content checks, Submodules).

Real git throughout: a bare submodule origin, a worktree with the submodule initialised (its git
dir under the admin entry), and hostile edits of everything an agent can write there.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.removelib import RR
from wtctl import removal_submodules

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
    assert (d["would_remove"], d["reason"]) == (True, None), d["checks"]


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
    assert err["code"] == "dirty" and w.intact()


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
    assert got and all(x["detail"] == "submodule_unpushed" for x in got), details(d, "unpushed_head")
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
    assert w.refused()["code"] == "unpushed" and w.intact()


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
    refused_unverified(w.st(capsys))


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
    w.g(["update-index", "--add", "--cacheinfo", f"160000,{w.g(['rev-parse', 'HEAD'], sub)},vendor/twin"], w.wt)
    w.g(
        ["config", "-f", ".gitmodules", "submodule.twin.path", "vendor/twin"],
        w.wt,
    )
    w.g(["config", "-f", ".gitmodules", "submodule.twin.url", str(w.tmp / "sub.git")], w.wt)
    w.g(["add", ".gitmodules"], w.wt)
    w.g(["commit", "-q", "-m", "twin"], w.wt)
    d = w.st(capsys)
    assert sub_detail("vendor/twin", "gitdir") in details(d), details(d)
    assert w.refused()["code"] == "dirty"


def test_an_embedded_git_dir_in_the_checkout_stays_a_nested_repo(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    (sub / ".git").unlink()
    w.g(["clone", "-q", str(w.tmp / "sub.git"), str(sub)], w.tmp)
    d = w.st(capsys)
    assert {"detail": "nested_repo", "path": f"{SUB}/.git"} in details(d), details(d)


def test_a_path_not_listed_in_gitmodules_stays_a_nested_repo(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    w.g(["rm", "-q", "--cached", "-f", ".gitmodules"], w.wt)
    w.g(["commit", "-q", "-m", "drop gitmodules"], w.wt)
    d = w.st(capsys)
    assert {"detail": "nested_repo", "path": f"{SUB}/.git"} in details(d) or any(
        x["path"] == SUB for x in details(d)
    )
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
        + f"[url \"{w.tmp}/nowhere/\"]\n\tinsteadOf = {w.tmp}/sub.git\n"
    )
    hooks = gd / "hooks"
    hooks.mkdir(exist_ok=True)
    for h in ("post-checkout", "reference-transaction", "pre-commit"):
        (hooks / h).write_text(f"#!/bin/sh\ntouch {marker}\n")
        (hooks / h).chmod(0o755)
    (w.wt / SUB / ".gitattributes").write_text("* filter=x\n")
    d = w.st(capsys)
    assert not marker.exists()
    assert (d["would_remove"], d["reason"]) == (False, "dirty")  # the planted attributes file


def test_a_core_worktree_redirect_in_the_submodule_config_changes_nothing(w: RR, capsys) -> None:  # noqa: ANN001
    sub = world_with_sub(w, capsys)
    decoy = w.tmp / "decoy"
    decoy.mkdir()
    w.g(["clone", "-q", str(w.tmp / "sub.git"), str(decoy / "c")], w.tmp)
    w.g(["config", "core.worktree", str(decoy / "c")], gitdir_of(w))
    (sub / "f.txt").write_text("edited in the real checkout")
    assert_refused_dirty(w.st(capsys), SUB, "tracked")


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
