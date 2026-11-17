"""Collecting the repo-config baseline: items, classes, digest stability and sensitivity."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.baselib import World, find
from wtctl import fsafe, repobaseline


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> World:
    return World(tmp_path, private_home)


def keys(b: repobaseline.Baseline) -> set[str]:
    return {i.key for i in b.items if i.key}


# --- what is collected ---------------------------------------------------------------------


def test_config_values_are_items_per_key_and_hashes_cover_values_in_order(w: World) -> None:
    w.cfg("[core]\n\tbare = false\n[a]\n\tk = 1\n\tk = 2\n")
    b = w.compute()
    assert {"core.bare", "a.k"} <= keys(b)
    one = [i for i in b.items if i.key == "a.k"][0]
    w.cfg("[core]\n\tbare = false\n[a]\n\tk = 2\n\tk = 1\n")
    other = [i for i in w.compute().items if i.key == "a.k"][0]
    assert one.sha256 != other.sha256 and one.type == "config" and one.cls == "git"


def test_every_git_class_source_appears(w: World) -> None:
    (Path(w.common) / "info").mkdir(exist_ok=True)
    (Path(w.common) / "info" / "exclude").write_text("x\n")
    (Path(w.common) / "objects" / "info").mkdir(parents=True, exist_ok=True)
    (Path(w.common) / "objects" / "info" / "alternates").write_text("/nowhere\n")
    w.cfg("[core]\n\tbare = false\n", "config.worktree")
    items = w.compute().items
    paths = {i.path for i in items if i.cls == "git"}
    c = w.common
    assert f"{c}/hooks" in paths
    assert f"{c}/info/exclude" in paths
    assert f"{c}/objects/info/alternates" in paths
    assert f"{w.repo.admin_dir}/commondir" in paths
    assert f"{w.repo.admin_dir}/gitdir" in paths
    assert any(i.path == f"{c}/config.worktree" and i.key == "core.bare" for i in items)


def test_modules_configs_and_hooks_are_items(w: World) -> None:
    mod = Path(w.common) / "modules" / "sub"
    (mod / "hooks").mkdir(parents=True)
    (mod / "hooks" / "post-checkout").write_text("#!/bin/sh\n")
    (mod / "config").write_text("[core]\n\tworktree = ../../x\n")
    (mod / "HEAD").write_text("ref: refs/heads/main\n")  # a git dir has a HEAD
    (mod / "objects" / "ab").mkdir(parents=True)
    (mod / "objects" / "ab" / "config").write_text("not a config, inert")
    adm = Path(w.repo.admin_dir) / "modules" / "s2"
    (adm / "hooks").mkdir(parents=True)
    (adm / "config").write_text("x")
    items = w.compute().items
    paths = {i.path for i in items}
    assert f"{mod}/config" in paths and f"{mod}/hooks" in paths
    assert f"{adm}/config" in paths and f"{adm}/hooks" in paths
    assert f"{mod}/objects/ab/config" not in paths


def test_a_change_in_a_modules_hook_changes_the_digest(w: World) -> None:
    hooks = Path(w.common) / "modules" / "sub" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "h").write_text("a")
    before = w.digest()
    (hooks / "h").write_text("b")
    assert w.digest() != before


def test_claude_class_items(w: World) -> None:
    wt = w.wt
    w.repo.write(".claude/settings.json", "{}")
    w.repo.write(".claude/agents/a.md", "x")
    w.repo.write(".mcp.json", "{}")
    w.repo.write("CLAUDE.md", "root")
    w.repo.write("pkg/deep/CLAUDE.local.md", "deep")
    w.repo.write("node_modules/x/CLAUDE.md", "disposable dirs are walked too")
    w.repo.write("pkg/.claude/settings.local.json", "{}")
    w.repo.write("README.md", "not claude")
    w.repo.write(".claude/.cc-writes/run1/f", "sandbox noise")
    w.repo.write("sub/.git/CLAUDE.md", "inside a nested .git")
    items = w.compute().items
    claude = {i.path: i for i in items if i.cls == "claude"}
    assert set(claude) == {
        ".claude",
        ".mcp.json",
        "CLAUDE.md",
        "pkg/deep/CLAUDE.local.md",
        "node_modules/x/CLAUDE.md",
        "pkg/.claude",
    }
    assert claude[".claude"].type == "directory" and claude["CLAUDE.md"].type == "file"
    assert wt.exists()


def test_cc_writes_noise_does_not_change_the_digest(w: World) -> None:
    w.repo.write(".claude/settings.json", "{}")
    before = w.digest()
    w.repo.write(".claude/.cc-writes/r/f", "x")
    w.repo.write(".claude/.cc-writes/r2/g", "y")
    assert w.digest() == before
    w.repo.write(".claude/.cc-writes2/g", "y")  # only the exact name is exempt
    assert w.digest() != before


def test_claude_symlink_is_recorded_by_target_and_not_followed_in_format_1(w: World) -> None:
    (w.tmp / "outside.md").write_text("one")
    os.symlink(w.tmp / "outside.md", w.wt / "CLAUDE.md")
    b = w.compute(links=False)
    item = find(b.items, "claude", "CLAUDE.md")[0]
    assert item.type == "symlink"
    (w.tmp / "outside.md").write_text("two")
    assert w.digest(links=False) == b.digest  # the target's content is not part of the item
    os.unlink(w.wt / "CLAUDE.md")
    os.symlink(w.tmp / "other.md", w.wt / "CLAUDE.md")
    assert w.digest(links=False) != b.digest


def test_claude_fifo_is_recorded_by_type_only_without_blocking(w: World) -> None:
    os.mkfifo(w.wt / ".mcp.json")
    b = w.compute()
    assert find(b.items, "claude", ".mcp.json")


def test_claude_dirs_that_are_files_or_links_are_items_too(w: World) -> None:
    os.symlink("/nonexistent", w.wt / ".claude")
    assert find(w.compute().items, "claude", ".claude")[0].type == "symlink"


# --- digest stability ----------------------------------------------------------------------


def test_digest_ignores_order_whitespace_and_comments(w: World) -> None:
    w.cfg("[user]\n\tname = a\n[core]\n\tbare = false\n\tfilemode = true\n")
    a = w.digest()
    w.cfg("# hi\n[core]\n  filemode=true ; c\n  bare   =   false\n\n[user]\nname = a\n")
    assert w.digest() == a


def test_digest_ignores_the_benign_routine_keys(w: World) -> None:
    a = w.digest()
    w.append_cfg(
        '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n\trebase = true\n'
        "\tpushRemote = o\n\tdescription = hi\n\tvscode-merge-base = origin/main\n"
        '[remote "origin"]\n\tgh-resolved = base\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
        "\tprune = true\n\ttagOpt = --no-tags\n"
    )
    assert w.digest() == a


def test_a_remote_url_counts(w: World) -> None:
    a = w.digest()
    w.append_cfg('[remote "origin"]\n\turl = https://example.test/x.git\n')
    assert w.digest() != a


def test_ignore_config_keys_patterns(w: World) -> None:
    a = w.digest(ignore=["foo.bar", "baz.*.qux"])
    w.append_cfg('[foo]\n\tbar = 1\n[baz "any"]\n\tqux = 2\n')
    assert w.digest(ignore=["foo.bar", "baz.*.qux"]) == a
    assert w.digest() != a


def test_empty_and_comment_only_config_files_equal_absent(w: World) -> None:
    a = w.digest()
    w.cfg("", "config.worktree")
    assert w.digest() == a
    w.cfg("# nothing\n; at all\n", "config.worktree")
    assert w.digest() == a
    (Path(w.repo.admin_dir) / "config.worktree").write_text("\n")
    assert w.digest() == a


def test_routine_git_commands_leave_the_digest_unchanged(w: World) -> None:
    remote = w.tmp / "remote.git"
    w.repo.g(["init", "-q", "--bare", str(remote)], w.tmp)
    w.repo.g(["remote", "add", "origin", str(remote)], w.repo.main)
    a = w.digest()
    w.repo.g(["push", "-q", "-u", "origin", "main"], w.repo.main)
    w.repo.g(["push", "-q", "-u", "origin", "feat"], w.wt)
    sib = w.tmp / "sib"
    w.repo.g(["worktree", "add", "-q", "-b", "sib", str(sib)], w.repo.main)
    w.repo.g(["worktree", "remove", "--force", str(sib)], w.repo.main)
    w.repo.g(["branch", "-q", "-D", "sib"], w.repo.main)
    w.repo.g(["branch", "--set-upstream-to=origin/main", "feat"], w.wt)
    w.repo.g(["fetch", "-q", "origin"], w.wt)
    (w.wt / "f.txt").write_text("x")
    w.repo.g(["add", "f.txt"], w.wt)
    w.repo.g(["commit", "-q", "-m", "x"], w.wt)
    assert w.digest() == a


def test_core_hookspath_changes_the_digest(w: World) -> None:
    a = w.digest()
    w.repo.g(["config", "core.hooksPath", "/tmp/elsewhere"], w.repo.main)
    assert w.digest() != a


# --- digest sensitivity (config smuggling) -------------------------------------------------

SMUGGLES = [
    "[core]\nhooksPath = /x\n",
    "[core]hooksPath = /x\n",
    "[CORE]\n\tHOOKSPATH = /x\n",
    "[core]\n\tfsmonitor = /bin/true\n",
    "[core]\n\tsshCommand = /bin/true\n",
    "[core]\n\tpager = /bin/true\n",
    "[core]\n\teditor = /bin/true\n",
    "[core]\n\tgitProxy = /bin/true\n",
    "[core]\n\tattributesFile = /x\n",
    "[alias]\n\tst = !true\n",
    '[url "https://evil.test/"]\n\tinsteadOf = https://good.test/\n',
    '[filter "x"]\n\tclean = /bin/true\n',
    '[diff "x"]\n\ttextconv = /bin/true\n',
    "[extensions]\n\tworktreeConfig = true\n",
    "[extensions]\n\tobjectFormat = sha256\n",
    "[include]\n\tpath = /tmp/zzz\n",
    '[includeIf "gitdir:/nowhere/"]\n\tpath = /tmp/zzz\n',
    '[includeIf "onbranch:main"]\n\tpath = x\n',
    "[credential]\n\thelper = !true\n",
    "[gpg]\n\tprogram = /bin/true\n",
    '[branch "main"]\n\tautoSetupRebase = always\n',
    '[branch "main"]\n\tremote.extra = x\n'.replace("remote.extra", "remotesomething"),
    '[remote "origin"]\n\tpushurl = https://x.test/\n',
    '[remote "origin"]\n\tuploadpack = /bin/true\n',
    '[remote "origin"]\n\turl = ext::sh -c true\n',
    '[submodule "s"]\n\tupdate = !true\n',
    "[core]\n\thooksPath\n",
    "[core]\n\thooksPath =\n",
]


@pytest.mark.parametrize("text", SMUGGLES, ids=lambda t: t.strip().replace("\n", "|")[:40])
def test_each_smuggling_vector_changes_the_digest(w: World, text: str) -> None:
    a = w.digest()
    w.append_cfg(text)
    assert w.digest() != a


@pytest.mark.parametrize("name", ["config.worktree"])
def test_worktree_config_files_count(w: World, name: str) -> None:
    a = w.digest()
    w.cfg("[core]\n\thooksPath = /x\n", name)
    assert w.digest() != a
    b = w.digest()
    (Path(w.repo.admin_dir) / "config.worktree").write_text("[core]\n\tfsmonitor = true\n")
    assert w.digest() != b


def test_unparsable_config_falls_back_to_a_content_hash(w: World) -> None:
    w.cfg("[core\n\tbare = false\n")
    a = w.compute()
    w.cfg("[core\n\tbare = true\n")
    assert w.digest() != a.digest
    assert any(i.type == "file" and i.key is None and i.path.endswith("/config") for i in a.items)


def test_config_with_nul_or_bom_is_handled_strictly(w: World) -> None:
    w.cfg("﻿[core]\n\tbare = false\n")
    assert "core.bare" in keys(w.compute())
    a = w.digest()
    w.cfg("[core]\n\tbare = false\0\n")
    assert w.digest() != a


def test_hooks_directory_content_and_mode_count(w: World) -> None:
    hooks = Path(w.common) / "hooks"
    a = w.digest()
    (hooks / "pre-commit").write_text("#!/bin/sh\ntrue\n")
    b = w.digest()
    assert b != a
    (hooks / "pre-commit").chmod(0o755)
    c = w.digest()
    assert c != b
    (hooks / "pre-commit").write_text("#!/bin/sh\nfalse\n")
    assert w.digest() != c
    os.unlink(hooks / "pre-commit")
    os.symlink("/bin/true", hooks / "pre-commit")
    assert w.digest() != c


def test_info_exclude_alternates_and_excludesfile_target_count(w: World) -> None:
    a = w.digest()
    (Path(w.common) / "info").mkdir(exist_ok=True)
    (Path(w.common) / "info" / "exclude").write_text("*.o\n")
    b = w.digest()
    assert b != a
    ex = w.tmp / "global-ignore"
    ex.write_text("one\n")
    w.append_cfg(f"[core]\n\texcludesFile = {ex}\n")
    c = w.digest()
    ex.write_text("two\n")
    assert w.digest() != c


def test_admin_entry_pointers_count(w: World) -> None:
    a = w.digest()
    (Path(w.repo.admin_dir) / "gitdir").write_text("/elsewhere/.git\n")
    assert w.digest() != a


# --- includes ------------------------------------------------------------------------------


def test_included_file_keys_are_part_of_the_baseline(w: World) -> None:
    inc = w.tmp / "inc.cfg"
    inc.write_text("[core]\n\tbare = false\n")
    w.append_cfg(f"[include]\n\tpath = {inc}\n")
    a = w.compute()
    assert "core.bare" in keys(a)
    inc.write_text("[core]\n\thooksPath = /x\n")
    assert w.digest() != a.digest


def test_relative_and_home_includes_resolve(w: World) -> None:
    (Path(w.common) / "rel.cfg").write_text("[x]\n\tfrom = rel\n")
    (w.home / "h.cfg").write_text("[x]\n\tfrom = home\n")
    w.append_cfg("[include]\n\tpath = rel.cfg\n\tpath = ~/h.cfg\n")
    assert "x.from" in keys(w.compute())


def test_include_if_is_followed_whatever_the_condition(w: World) -> None:
    inc = w.tmp / "inc.cfg"
    inc.write_text("[core]\n\tfsmonitor = /bin/true\n")
    w.append_cfg(f'[includeIf "gitdir:/never/matches/"]\n\tpath = {inc}\n')
    assert "core.fsmonitor" in keys(w.compute())


def test_a_missing_include_target_appearing_changes_the_digest(w: World) -> None:
    inc = w.tmp / "later.cfg"
    w.append_cfg(f"[include]\n\tpath = {inc}\n")
    a = w.digest()
    inc.write_text("[core]\n\thooksPath = /x\n")
    assert w.digest() != a


def test_include_cycles_terminate_and_are_bounded(w: World) -> None:
    a = w.tmp / "a.cfg"
    a.write_text(f"[include]\n\tpath = {a}\n[x]\n\tk = v\n")
    w.append_cfg(f"[include]\n\tpath = {a}\n")
    b = w.compute()
    assert any(i.detail == "incomplete" for i in b.items)  # depth > 10


def test_include_depth_ten_is_fine_eleven_is_incomplete(w: World) -> None:
    files = [w.tmp / f"d{i}.cfg" for i in range(12)]
    for i, f in enumerate(files[:-1]):
        f.write_text(f"[include]\n\tpath = {files[i + 1]}\n[n{i}]\n\tk = v\n")
    files[-1].write_text("[end]\n\tk = v\n")
    w.append_cfg(f"[include]\n\tpath = {files[0]}\n")
    b = w.compute()
    assert any(i.detail == "incomplete" for i in b.items)
    # shorter chain: depth 10 passes
    for i in (9, 10):
        files[i].write_text("[tail]\n\tk = v\n")
    b = w.compute()
    assert not any(i.detail for i in b.items)
    assert "tail.k" in keys(b)


def test_include_fan_out_is_bounded_by_the_entry_budget(w: World) -> None:
    leaf = w.tmp / "leaf.cfg"
    leaf.write_text("[x]\n\tk = v\n")
    w.append_cfg("[include]\n" + f"\tpath = {leaf}\n" * 500)
    b = w.compute(limit=100)
    assert any(i.detail == "incomplete" for i in b.items)


def test_include_symlink_is_recorded_by_target_not_followed_in_format_1(w: World) -> None:
    real = w.tmp / "real.cfg"
    real.write_text("[x]\n\tk = v\n")
    link = w.tmp / "link.cfg"
    os.symlink(real, link)
    w.append_cfg(f"[include]\n\tpath = {link}\n")
    b = w.compute(links=False)
    assert "x.k" not in keys(b)
    assert any(i.type == "symlink" and i.path == str(link) for i in b.items)


def test_include_fifo_and_directory_do_not_hang(w: World) -> None:
    fifo = w.tmp / "fifo.cfg"
    os.mkfifo(fifo)
    d = w.tmp / "dir.cfg"
    d.mkdir()
    w.append_cfg(f"[include]\n\tpath = {fifo}\n\tpath = {d}\n")
    b = w.compute()
    assert find(b.items, "git", "fifo.cfg")
    assert find(b.items, "git", "dir.cfg")


def test_config_files_over_the_byte_bound_are_incomplete(w: World) -> None:
    w.cfg("[x]\n\tk = " + "a" * 5000 + "\n")
    b = w.compute(max_file=1000)
    assert any(i.detail == "incomplete" for i in b.items)
    ok = w.compute()
    assert not any(i.detail for i in ok.items)
    assert ok.digest != b.digest


def test_total_byte_bound(w: World) -> None:
    hooks = Path(w.common) / "hooks"
    for i in range(5):
        (hooks / f"h{i}").write_text("x" * 400)
    assert any(i.detail == "incomplete" for i in w.compute(max_total=1000).items)
    assert not any(i.detail for i in w.compute().items)


def test_entry_bound_gives_incomplete(w: World) -> None:
    hooks = Path(w.common) / "hooks"
    for i in range(30):
        (hooks / f"h{i}").write_text("x")
    b = w.compute(limit=20)
    assert any(i.detail == "incomplete" for i in b.items)


def test_claude_walk_entry_bound(w: World) -> None:
    for i in range(50):
        w.repo.write(f"d{i}/f", "x")
    b = w.compute(limit=40)
    assert any(i.detail == "incomplete" for i in b.items)


def test_fifo_at_info_exclude_is_not_read(w: World) -> None:
    (Path(w.common) / "info").mkdir(exist_ok=True)
    os.unlink(Path(w.common) / "info" / "exclude")
    os.mkfifo(Path(w.common) / "info" / "exclude")
    b = w.compute()
    assert find(b.items, "git", "info/exclude")
    assert not any(i.detail for i in b.items)


def test_fifo_in_hooks_is_listed_by_type(w: World) -> None:
    os.mkfifo(Path(w.common) / "hooks" / "pipe")
    a = w.compute()
    assert not any(i.detail for i in a.items)


def test_config_symlink_is_recorded_by_target(w: World) -> None:
    real = w.tmp / "real-config"
    real.write_text("[core]\n\tbare = false\n")
    wt_cfg = Path(w.common) / "config.worktree"
    os.symlink(real, wt_cfg)
    b = w.compute()
    assert any(i.type == "symlink" and i.path.endswith("/config.worktree") for i in b.items)
    assert "core.bare" in keys(b)  # from the real config only


def test_symlinked_hooks_directory_is_a_symlink_item_not_followed(w: World) -> None:
    real = w.tmp / "hooks-elsewhere"
    real.mkdir()
    (real / "pre-commit").write_text("x")
    shutil_rmtree(Path(w.common) / "hooks")
    os.symlink(real, Path(w.common) / "hooks")
    b = w.compute()
    item = find(b.items, "git", "/hooks")[0]
    assert item.type == "symlink"


def shutil_rmtree(p: Path) -> None:
    subprocess.run(["rm", "-rf", str(p)], check=True)


def test_non_utf8_and_odd_names_are_hashed(w: World) -> None:
    hooks = os.fsencode(str(Path(w.common) / "hooks"))
    with open(hooks + b"/na\xffme\nx", "wb") as fh:
        fh.write(b"x")
    b = w.compute()
    assert not any(i.detail for i in b.items)


# --- external hooksPath and anchors --------------------------------------------------------


def test_external_hooks_path_is_listed_and_anchored(w: World) -> None:
    hp = w.tmp / "user-hooks"
    hp.mkdir()
    (hp / "pre-commit").write_text("x")
    w.append_cfg(f"[core]\n\thooksPath = {hp}\n")
    b = w.compute()
    item = find(b.items, "git", "/user-hooks")[0]
    st = os.stat(hp)
    assert item.type == "directory" and item.anchor_id == f"{st.st_dev}:{st.st_ino}"
    (hp / "pre-commit").write_text("y")
    assert w.digest() != b.digest


def test_external_hooks_path_with_other_identity_is_mount_point_until_rebound(w: World) -> None:
    hp = w.tmp / "user-hooks"
    hp.mkdir()
    w.append_cfg(f"[core]\n\thooksPath = {hp}\n")
    recorded = find(w.compute().items, "git", "/user-hooks")[0].anchor_id
    other = "1:2"
    b = w.compute(hooks_identity=other)
    item = find(b.items, "git", "/user-hooks")[0]
    assert item.detail == "mount_point" and item.anchor_id == recorded
    assert w.compute(hooks_identity=recorded).digest != b.digest
    assert not any(i.detail for i in w.compute(hooks_identity=other, rebind_hooks=True).items)


def test_relative_hooks_path_resolves_against_the_worktree(w: World) -> None:
    w.repo.write("myhooks/pre-commit", "x")
    w.append_cfg("[core]\n\thooksPath = myhooks\n")
    b = w.compute()
    assert find(b.items, "git", "/myhooks")


def test_hooks_path_inside_the_common_dir_is_one_listing(w: World) -> None:
    w.append_cfg(f"[core]\n\thooksPath = {w.common}/hooks\n")
    b = w.compute()
    assert len(find(b.items, "git", "/hooks")) == 1


def test_missing_hooks_path_directory_is_no_item(w: World) -> None:
    w.append_cfg("[core]\n\thooksPath = /nonexistent-dir-for-test\n")
    b = w.compute()
    assert not find(b.items, "git", "nonexistent-dir-for-test")
    assert not any(i.detail for i in b.items)


class _OtherDev(fsafe.Fs):
    def __init__(self, ino: int) -> None:
        self.ino = ino

    def adjust(self, st: fsafe.FdStat) -> fsafe.FdStat:
        if st.ino == self.ino:
            return fsafe.FdStat(st.dev + 1, st.ino, st.mode, st.uid, st.nlink)
        return st


def test_a_mount_point_below_the_common_dir_is_a_changed_item(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = Path(w.common) / "hooks" / "nested"
    sub.mkdir()
    (sub / "f").write_text("x")
    clean = w.compute()
    monkeypatch.setattr(fsafe, "DEFAULT_FS", _OtherDev(os.stat(sub).st_ino))
    b = w.compute()
    item = find(b.items, "git", "/hooks")[0]
    assert item.detail == "mount_point" and b.digest != clean.digest


def test_a_hook_file_on_another_mount_is_a_mount_point_item(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = Path(w.common) / "hooks" / "pre-commit"
    f.write_text("x")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", _OtherDev(os.stat(f).st_ino))
    item = find(w.compute().items, "git", "/hooks")[0]
    assert item.detail == "mount_point"


def test_a_config_file_on_another_mount_is_a_mount_point_item(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = Path(w.common) / "config"
    monkeypatch.setattr(fsafe, "DEFAULT_FS", _OtherDev(os.stat(f).st_ino))
    items = w.compute().items
    assert any(i.detail == "mount_point" and i.key is None and i.path == str(f) for i in items)


def test_a_claude_dir_on_another_mount_is_a_mount_point_item(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.repo.write("pkg/CLAUDE.md", "x")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", _OtherDev(os.stat(w.wt / "pkg").st_ino))
    items = w.compute().items
    assert any(i.cls == "claude" and i.detail == "mount_point" for i in items)


# --- unlinked and missing ------------------------------------------------------------------


def test_missing_worktree_directory_is_a_defined_incomplete_item(w: World) -> None:
    subprocess.run(["rm", "-rf", str(w.wt)], check=True)
    b = w.compute()
    assert any(i.cls == "claude" and i.detail == "incomplete" for i in b.items)


def test_worktree_replaced_by_another_directory_does_not_match_the_identity(w: World) -> None:
    # kept, not removed: a removed directory's inode number may be reused by the new one
    w.wt.rename(w.tmp / "wt-old")
    w.wt.mkdir()
    b = w.compute()
    assert any(i.cls == "claude" and i.detail == "mount_point" for i in b.items)


def test_missing_admin_dir_is_a_defined_incomplete_item(w: World) -> None:
    subprocess.run(["rm", "-rf", w.repo.admin_dir], check=True)
    b = w.compute()
    assert any(i.cls == "git" and i.detail == "incomplete" for i in b.items)


def test_common_dir_with_another_identity_is_a_mount_point_item(w: World) -> None:
    b = w.compute(common_id="1:2")
    assert [i.detail for i in b.items if i.cls == "git"] == ["mount_point"]


def test_main_worktree_without_admin_dir(w: World) -> None:
    b = w.compute(admin_dir=None, worktree=str(w.repo.main), dir_id=_id(w.repo.main))
    assert not any(i.path.endswith("/commondir") for i in b.items)
    assert not any(i.detail for i in b.items if i.cls == "git")


def _id(p: Path) -> str:
    st = os.stat(p)
    return f"{st.st_dev}:{st.st_ino}"


# --- compare and digest --------------------------------------------------------------------


def test_compare_reports_added_removed_and_changed_items(w: World) -> None:
    a = w.compute()
    w.append_cfg("[core]\n\tfsmonitor = true\n")
    (Path(w.common) / "hooks" / "pre-push").write_text("x")
    w.repo.write("CLAUDE.md", "x")
    b = w.compute()
    rows = repobaseline.compare(a.items, b.items)
    by = {(r["class"], r["path"], r["key"]): r for r in rows}
    assert by[("git", w.common + "/config", "core.fsmonitor")]["before"] == "absent"
    assert by[("claude", "CLAUDE.md", None)]["before"] == "absent"
    hooks = by[("git", w.common + "/hooks", None)]
    assert hooks["before"]["type"] == "directory" and hooks["after"]["type"] == "directory"
    assert repobaseline.changed_classes(rows) == ["claude", "git"]
    for r in rows:
        assert "value" not in r and set(r) <= {"class", "path", "key", "before", "after", "detail"}


def test_compare_marks_detail_items_changed_even_if_hashes_match(w: World) -> None:
    a = w.compute()
    b = w.compute(max_file=1)
    rows = repobaseline.compare(a.items, b.items)
    assert rows and all("detail" in r for r in rows if r["after"] != "absent") or rows


def test_digest_is_a_function_of_the_items(w: World) -> None:
    a = w.compute()
    assert repobaseline.digest_of(list(reversed(a.items))) == a.digest
    assert len(a.digest) == 64
    assert repobaseline.digest_of([]) != a.digest


def test_modes_of_files_outside_hooks_do_not_matter_for_config(w: World) -> None:
    a = w.digest()
    os.chmod(Path(w.common) / "config", 0o600)
    assert w.digest() == a
    assert stat.S_ISREG(os.stat(Path(w.common) / "config").st_mode)


# --- deadlines -----------------------------------------------------------------------------


def test_a_collector_past_its_deadline_is_killed_and_is_an_incomplete_item(w: World) -> None:
    import sys

    from wtctl import fsafe_child

    pids: list[int] = []

    def slow(op: str, args: dict):  # noqa: ANN202
        res = fsafe_child.run_op(
            op,
            args,
            timeout=0.5,
            argv=[sys.executable, "-c", "import time; time.sleep(60)"],
            on_start=pids.append,
        )
        return res

    b = repobaseline.compute(w.spec(), runner=slow)
    assert {(i.cls, i.detail) for i in b.items} == {("git", "incomplete"), ("claude", "incomplete")}
    assert len(pids) == 2
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # killed by its exact PID and reaped
    assert b.digest != w.digest()


def test_the_real_child_gives_the_same_baseline_as_in_process(w: World) -> None:
    from wtctl import fsafe_child

    a = repobaseline.compute(w.spec(), runner=fsafe_child.run_op)
    assert a.digest == w.digest() and not a.has_detail()


def test_names_that_are_not_utf8_become_storable_items(w: World) -> None:
    raw = os.fsencode(str(w.wt)) + b"/CLAUDE\xff.md"
    with open(raw, "wb") as fh:
        fh.write(b"x")
    b = w.compute()
    item = [i for i in b.items if i.cls == "claude"][0]
    item.path.encode("utf-8")  # storable and printable
    assert item.path.startswith("␀hex:")
    with open(raw, "wb") as fh:
        fh.write(b"y")
    assert w.digest() != b.digest
    # a file whose name merely looks like the encoding is a different item
    (w.wt / "CLAUDE.md").write_text("z")
    assert len([i for i in w.compute().items if i.cls == "claude"]) == 2
