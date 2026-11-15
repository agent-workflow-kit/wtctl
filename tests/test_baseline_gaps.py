"""Collection gaps found in review: submodules named like git dir parts, unreadable walk entries,
which config.worktree applies, case-insensitive Claude names, `..` through symlinks, bounds."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.baselib import World, find
from tests.vectorlib import plain_git


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> World:
    return World(tmp_path, private_home)


def _git_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "HEAD").write_text("ref: refs/heads/main\n")


# --- F1, F3: submodule git dirs named like parts of a git dir -------------------------------


@pytest.mark.parametrize("name", ["refs", "objects", "logs"])
def test_a_submodule_named_like_a_git_dir_part_is_still_collected(w: World, name: str) -> None:
    mod = Path(w.common) / "modules" / name
    _git_dir(mod)
    (mod / "hooks").mkdir()
    (mod / "hooks" / "post-checkout").write_text("a")
    (mod / "config").write_text("[core]\n\tfsmonitor = a\n")
    items = w.compute().items
    assert find(items, "git", f"modules/{name}/config")
    before = w.digest()
    (mod / "config").write_text("[core]\n\tfsmonitor = b\n")
    assert w.digest() != before
    (mod / "hooks" / "post-checkout").write_text("b")
    assert w.digest() != before


def test_a_nested_submodule_named_logs_is_collected(w: World) -> None:
    mod = Path(w.common) / "modules" / "a" / "modules" / "logs"
    _git_dir(mod)
    (mod / "config").write_text("x")
    _git_dir(Path(w.common) / "modules" / "a")
    assert find(w.compute().items, "git", "modules/a/modules/logs/config")


def test_objects_refs_logs_of_a_git_dir_are_still_skipped(w: World) -> None:
    mod = Path(w.common) / "modules" / "sub"
    _git_dir(mod)
    for part in ("objects", "refs", "logs"):
        (mod / part).mkdir()
        (mod / part / "config").write_text("inert")
    paths = {i.path for i in w.compute().items}
    assert not any(f"/sub/{p}/" in x for x in paths for p in ("objects", "refs", "logs"))


def test_a_submodule_named_head_does_not_unskip_a_real_git_dir_part(w: World) -> None:
    # HEAD must be a file to make its parent a git dir
    base = Path(w.common) / "modules"
    (base / "HEAD").mkdir(parents=True)
    (base / "logs").mkdir()
    (base / "logs" / "config").write_text("x")
    assert find(w.compute().items, "git", "modules/logs/config")


@pytest.mark.parametrize("kind", ["symlink", "file", "fifo"])
def test_hooks_of_any_kind_and_config_worktree_are_items(w: World, kind: str) -> None:
    mod = Path(w.common) / "modules" / "sub"
    _git_dir(mod)
    if kind == "symlink":
        os.symlink("/nonexistent", mod / "hooks")
    elif kind == "file":
        (mod / "hooks").write_text("x")
    else:
        os.mkfifo(mod / "hooks")
    (mod / "config.worktree").write_text("[core]\n\tfsmonitor = x\n")
    items = w.compute().items
    assert find(items, "git", "modules/sub/hooks")
    assert find(items, "git", "modules/sub/config.worktree")


# --- F2: unreadable walk entries ------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_an_unreadable_directory_in_the_claude_walk_is_an_incomplete_item(w: World) -> None:
    w.repo.write("secret/CLAUDE.md", "x")
    os.chmod(w.wt / "secret", 0o100)
    try:
        items = w.compute().items
    finally:
        os.chmod(w.wt / "secret", 0o755)
    bad = find(items, "claude", "secret")
    assert bad and bad[0].detail == "incomplete"


# --- F4: which config.worktree git reads ----------------------------------------------------


def _git_hooks_path(repo_dir: Path, w: World) -> str:
    r = plain_git(["rev-parse", "--git-path", "hooks"], cwd=repo_dir, home=w.home)
    assert r.returncode == 0
    out = r.stdout.decode().strip()
    return out if os.path.isabs(out) else str((repo_dir / out).resolve())


def _worktree_config_setup(w: World) -> tuple[Path, Path]:
    plain_git(["config", "extensions.worktreeConfig", "true"], cwd=w.repo.main, home=w.home)
    a, b = w.tmp / "hooks-main", w.tmp / "hooks-linked"
    for d in (a, b):
        d.mkdir()
        (d / "pre-commit").write_text("x")
    return a, b


def test_a_linked_worktree_reads_its_own_config_worktree_not_the_common_one(w: World) -> None:
    a, b = _worktree_config_setup(w)
    Path(w.common, "config.worktree").write_text(f"[core]\n\thooksPath = {a}\n")
    git_path = _git_hooks_path(w.wt, w)
    items = w.compute().items
    assert git_path == str(w.common) + "/hooks"  # the oracle: the main one's file is not read
    assert not find(items, "git", "/hooks-main")
    Path(w.repo.admin_dir, "config.worktree").write_text(f"[core]\n\thooksPath = {b}\n")
    assert _git_hooks_path(w.wt, w) == str(b)
    assert find(w.compute().items, "git", "/hooks-linked")
    assert not find(w.compute().items, "git", "/hooks-main")


def test_the_main_worktree_reads_the_common_config_worktree_not_an_admin_one(w: World) -> None:
    a, b = _worktree_config_setup(w)
    Path(w.common, "config.worktree").write_text(f"[core]\n\thooksPath = {a}\n")
    Path(w.repo.admin_dir, "config.worktree").write_text(f"[core]\n\thooksPath = {b}\n")
    assert _git_hooks_path(w.repo.main, w) == str(a)
    main = w.compute(admin_dir=None, worktree=str(w.repo.main), dir_id=_id(w.repo.main))
    assert find(main.items, "git", "/hooks-main")
    assert not find(main.items, "git", "/hooks-linked")
    # the record's admin dir may also be given as the common dir itself
    same = w.compute(admin_dir=w.common, worktree=str(w.repo.main), dir_id=_id(w.repo.main))
    assert find(same.items, "git", "/hooks-main") and not find(same.items, "git", "/hooks-linked")
    assert not any(i.detail for i in same.items)


def test_all_three_root_files_are_still_listed_as_items(w: World) -> None:
    Path(w.common, "config.worktree").write_text("[a]\n\tk = 1\n")
    Path(w.repo.admin_dir, "config.worktree").write_text("[b]\n\tk = 1\n")
    items = w.compute().items
    assert any(i.key == "a.k" and i.path.startswith(w.common) for i in items)
    assert any(i.key == "b.k" and i.path.startswith(w.repo.admin_dir) for i in items)


def _id(p: Path) -> str:
    st = os.stat(p)
    return f"{st.st_dev}:{st.st_ino}"


# --- F5: case-insensitive Claude names ------------------------------------------------------


def test_claude_names_match_case_insensitively(w: World) -> None:
    w.repo.write("claude.md", "x")
    w.repo.write("Claude.Local.MD", "x")
    w.repo.write(".CLAUDE/settings.json", "{}")
    w.repo.write("pkg/.Mcp.JSON", "{}")
    paths = {i.path for i in w.compute().items if i.cls == "claude"}
    assert paths == {"claude.md", "Claude.Local.MD", ".CLAUDE", "pkg/.Mcp.JSON"}


def test_nested_dot_git_is_skipped_exactly(w: World) -> None:
    w.repo.write("sub/.git/CLAUDE.md", "x")
    w.repo.write("sub2/.GIT/CLAUDE.md", "x")
    paths = {i.path for i in w.compute().items if i.cls == "claude"}
    assert paths == {"sub2/.GIT/CLAUDE.md"}


@pytest.mark.parametrize("name", ["CLAUDE.md", "claude.local.md"])
def test_a_fifo_named_like_a_claude_file_is_a_special_item(w: World, name: str) -> None:
    os.mkfifo(w.wt / name)
    item = find(w.compute().items, "claude", name)[0]
    assert item.type == "file" and not item.detail
    plain = w.compute().digest
    os.unlink(w.wt / name)
    assert w.digest() != plain


# --- F16: `..` after a symlink --------------------------------------------------------------


def test_include_with_dotdot_after_a_symlink_resolves_physically(w: World) -> None:
    deep = w.tmp / "elsewhere" / "deep"
    deep.mkdir(parents=True)
    os.symlink(deep, Path(w.common) / "link")
    (w.tmp / "elsewhere" / "x").write_text("[inc]\n\tphysical = 1\n")
    (Path(w.common) / "x").write_text("[inc]\n\tlexical = 1\n")
    w.append_cfg("[include]\n\tpath = link/../x\n")
    keys = {i.key for i in w.compute().items if i.key}
    assert "inc.physical" in keys and "inc.lexical" not in keys


def test_plain_dotdot_without_symlinks_is_the_same(w: World) -> None:
    (Path(w.common) / "d").mkdir()
    (Path(w.common) / "x").write_text("[inc]\n\tk = 1\n")
    w.append_cfg("[include]\n\tpath = d/../x\n")
    assert "inc.k" in {i.key for i in w.compute().items if i.key}


# --- F17: nested listings share the bound ---------------------------------------------------


def test_nested_listings_cannot_exceed_the_entry_bound_together(w: World) -> None:
    for i in range(60):
        w.repo.write(f"f{i}", "x")
    for i in range(60):
        w.repo.write(f".claude/c{i}", "x")
    ok = w.compute(limit=500)
    assert not any(i.detail for i in ok.items)
    assert ok.entries >= 120
    cut = w.compute(limit=100)  # 60 + 61 > 100 although each alone is < 100
    assert any(i.detail == "incomplete" for i in cut.items if i.cls == "claude")


def test_modules_walk_and_its_listings_share_the_bound(w: World) -> None:
    base = Path(w.common) / "modules"
    for n in range(10):
        mod = base / f"m{n}"
        _git_dir(mod)
        (mod / "hooks").mkdir()
        for i in range(10):
            (mod / "hooks" / f"h{i}").write_text("x")
    assert not any(i.detail for i in w.compute(limit=2000).items if i.cls == "git")
    cut = w.compute(limit=60)
    assert any(i.detail == "incomplete" for i in cut.items if i.cls == "git")
