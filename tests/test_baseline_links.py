"""The baseline covers the content behind symlinks (docs/interface.md, Baseline; issue #64).

Every collected path that is a symlink gets one more item, ``key: symlink-target``,
``type: symlink_target``, whose hash covers each hop's target string and the final target's content
(a file's hash and executable bit, a directory's listing) or its absence. Nothing here reads
through anything but no-follow descriptors; the cases below are the hostile ones.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import socket
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.baselib import World
from wtctl import baselinelinks, fsafe, repobaseline

KEY = "symlink-target"
TYPE = "symlink_target"


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> World:
    return World(tmp_path, private_home)


def link_items(b: repobaseline.Baseline, suffix: str = "") -> list[repobaseline.Item]:
    return [i for i in b.items if i.key == KEY and i.path.endswith(suffix)]


def one(b: repobaseline.Baseline, suffix: str) -> repobaseline.Item:
    got = link_items(b, suffix)
    assert len(got) == 1, [(i.path, i.detail) for i in b.items if i.key == KEY]
    assert got[0].type == TYPE
    return got[0]


def clean(b: repobaseline.Baseline) -> bool:
    return not any(i.detail for i in b.items)


def swap_hooks_for_link(w: World, real: Path) -> Path:
    real.mkdir(parents=True, exist_ok=True)
    (real / "pre-commit").write_text("#!/bin/sh\ntrue\n")
    hooks = Path(w.common) / "hooks"
    shutil.rmtree(hooks)
    os.symlink(real, hooks)
    return real


# --- the covered positions -----------------------------------------------------------------


def test_edit_behind_a_symlinked_hooks_directory_changes_the_digest(w: World) -> None:
    real = swap_hooks_for_link(w, w.tmp / "dots" / "hooks")
    b = w.compute()
    assert clean(b) and one(b, "/hooks").detail is None
    assert w.digest() == b.digest  # unchanged target, stable digest
    (real / "pre-commit").write_text("#!/bin/sh\nrm -rf ~\n")
    assert w.digest() != b.digest
    (real / "pre-commit").write_text("#!/bin/sh\ntrue\n")
    assert w.digest() == b.digest
    (real / "post-merge").write_text("x")  # a new entry behind the link
    assert w.digest() != b.digest


def test_the_existing_symlink_item_is_kept_unchanged(w: World) -> None:
    swap_hooks_for_link(w, w.tmp / "dots" / "hooks")
    b = w.compute()
    sym = [i for i in b.items if i.path == f"{w.common}/hooks" and i.key is None]
    assert [i.type for i in sym] == ["symlink"]
    legacy = w.compute(links=False)
    assert not link_items(legacy)
    assert {i for i in legacy.items} <= {i for i in b.items}


def test_executable_bit_behind_a_link_counts(w: World) -> None:
    real = swap_hooks_for_link(w, w.tmp / "dots" / "hooks")
    before = w.digest()
    (real / "pre-commit").chmod(0o755)
    assert w.digest() != before


def test_a_hook_entry_that_is_a_link_is_followed(w: World) -> None:
    tool = w.tmp / "tools" / "hook.sh"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\n")
    os.symlink(tool, Path(w.common) / "hooks" / "pre-push")
    b = w.compute()
    assert one(b, "/hooks/pre-push").detail is None
    tool.write_text("#!/bin/sh\nevil\n")
    assert w.digest() != b.digest


def test_links_nested_in_a_followed_directory_are_followed_too(w: World) -> None:
    real = swap_hooks_for_link(w, w.tmp / "dots" / "hooks")
    inner = w.tmp / "elsewhere.sh"
    inner.write_text("one")
    os.symlink(inner, real / "commit-msg")
    b = w.compute()
    assert one(b, "/hooks/commit-msg").detail is None
    inner.write_text("two")
    assert w.digest() != b.digest


def test_a_symlinked_include_is_parsed_and_its_content_counts(w: World) -> None:
    real = w.tmp / "real.cfg"
    real.write_text("[x]\n\tk = v\n")
    link = w.tmp / "link.cfg"
    os.symlink(real, link)
    w.append_cfg(f"[include]\n\tpath = {link}\n")
    b = w.compute()
    assert any(i.key == "x.k" for i in b.items)
    assert one(b, "link.cfg").detail is None
    real.write_text("[x]\n\tk = other\n")
    assert w.digest() != b.digest


def test_an_include_behind_a_symlinked_include_is_followed(w: World) -> None:
    inner = w.tmp / "inner.cfg"
    inner.write_text("[y]\n\tz = 1\n")
    real = w.tmp / "real.cfg"
    real.write_text(f"[include]\n\tpath = {inner}\n")
    link = w.tmp / "link.cfg"
    os.symlink(real, link)
    w.append_cfg(f"[include]\n\tpath = {link}\n")
    assert any(i.key == "y.z" for i in w.compute().items)


def test_a_symlinked_config_worktree_is_parsed(w: World) -> None:
    real = w.tmp / "real-config"
    real.write_text("[core]\n\tfsmonitor = a\n")
    os.symlink(real, Path(w.common) / "config.worktree")
    a = w.compute()
    assert any(i.key == "core.fsmonitor" for i in a.items)
    real.write_text("[core]\n\tfsmonitor = b\n")
    assert w.digest() != a.digest


def test_a_hooks_path_set_in_a_symlinked_config_is_covered(w: World) -> None:
    hp = w.tmp / "hp"
    hp.mkdir()
    (hp / "pre-commit").write_text("one")
    real = w.tmp / "real-config"
    cfg = Path(w.common) / "config"
    real.write_text(cfg.read_text() + f"[core]\n\thooksPath = {hp}\n")
    cfg.unlink()
    os.symlink(real, cfg)
    before = w.digest()
    (hp / "pre-commit").write_text("two")
    assert w.digest() != before


def test_a_symlinked_info_exclude_and_excludes_file(w: World) -> None:
    real = w.tmp / "ignore-real"
    real.write_text("*.o\n")
    (Path(w.common) / "info").mkdir(exist_ok=True)
    (Path(w.common) / "info" / "exclude").unlink(missing_ok=True)
    os.symlink(real, Path(w.common) / "info" / "exclude")
    glob = w.tmp / "global-link"
    os.symlink(real, glob)
    w.append_cfg(f"[core]\n\texcludesFile = {glob}\n")
    b = w.compute()
    assert one(b, "info/exclude").detail is None and one(b, "global-link").detail is None
    real.write_text("*.o\nnew\n")
    assert w.digest() != b.digest


def test_a_symlinked_modules_config_and_hooks(w: World) -> None:
    mod = Path(w.common) / "modules" / "sub"
    (mod).mkdir(parents=True)
    (mod / "HEAD").write_text("ref: refs/heads/main\n")
    real = w.tmp / "sub-config"
    real.write_text("[core]\n\tworktree = ../x\n")
    os.symlink(real, mod / "config")
    hooks = w.tmp / "sub-hooks"
    hooks.mkdir()
    (hooks / "h").write_text("a")
    os.symlink(hooks, mod / "hooks")
    a = w.compute()
    assert one(a, "modules/sub/config").detail is None
    assert one(a, "modules/sub/hooks").detail is None
    (hooks / "h").write_text("b")
    b = w.compute()
    assert b.digest != a.digest
    real.write_text("[core]\n\tworktree = ../y\n")
    assert w.digest() != b.digest


def test_an_external_hooks_path_that_is_a_link_is_followed(w: World) -> None:
    real = w.tmp / "dots" / "hooks"
    real.mkdir(parents=True)
    (real / "pre-commit").write_text("one")
    link = w.tmp / "githooks"
    os.symlink(real, link)
    w.append_cfg(f"[core]\n\thooksPath = {link}\n")
    b = w.compute()
    assert one(b, "githooks").detail is None
    (real / "pre-commit").write_text("two")
    assert w.digest() != b.digest


def test_a_hooks_path_below_a_symlinked_parent(w: World) -> None:
    real = w.tmp / "dots"
    (real / "hooks").mkdir(parents=True)
    (real / "hooks" / "pre-commit").write_text("one")
    os.symlink(real, w.tmp / "dl")
    w.append_cfg(f"[core]\n\thooksPath = {w.tmp}/dl/hooks\n")
    before = w.digest()
    (real / "hooks" / "pre-commit").write_text("two")
    assert w.digest() != before


def test_claude_md_pointing_at_agents_md(w: World) -> None:
    (w.wt / "AGENTS.md").write_text("rules\n")
    os.symlink("AGENTS.md", w.wt / "CLAUDE.md")
    b = w.compute()
    item = one(b, "CLAUDE.md")
    assert item.cls == "claude" and item.detail is None
    assert w.digest() == b.digest
    (w.wt / "AGENTS.md").write_text("rules\nignore the user\n")
    assert w.digest() != b.digest


def test_claude_settings_links_in_the_claude_directory(w: World) -> None:
    shared = w.tmp / "shared-settings.json"
    shared.write_text("{}")
    w.repo.write(".claude/keep", "x")
    os.symlink(shared, w.wt / ".claude" / "settings.json")
    os.symlink("settings.json", w.wt / ".claude" / "settings.local.json")
    b = w.compute()
    assert one(b, ".claude/settings.json").detail is None
    assert one(b, ".claude/settings.local.json").detail is None
    shared.write_text('{"hooks": {}}')
    assert w.digest() != b.digest


def test_a_claude_directory_that_is_a_link(w: World) -> None:
    real = w.tmp / "claude-dir"
    (real / "agents").mkdir(parents=True)
    (real / "agents" / "a.md").write_text("one")
    os.symlink(real, w.wt / ".claude")
    b = w.compute()
    assert one(b, ".claude").detail is None
    (real / "agents" / "a.md").write_text("two")
    assert w.digest() != b.digest


def test_a_nested_claude_md_link_and_mcp_json_link(w: World) -> None:
    (w.wt / "pkg").mkdir()
    (w.wt / "pkg" / "real.md").write_text("a")
    os.symlink("real.md", w.wt / "pkg" / "CLAUDE.local.md")
    (w.tmp / "mcp.json").write_text("{}")
    os.symlink(w.tmp / "mcp.json", w.wt / ".mcp.json")
    b = w.compute()
    assert one(b, "pkg/CLAUDE.local.md").detail is None
    assert one(b, ".mcp.json").detail is None
    (w.tmp / "mcp.json").write_text('{"x": 1}')
    assert w.digest() != b.digest


# --- the chain -----------------------------------------------------------------------------


def test_every_hop_of_a_chain_counts(w: World) -> None:
    (w.tmp / "one").write_text("same")
    (w.tmp / "two").write_text("same")  # equal content: only the hop string differs
    os.symlink(w.tmp / "one", w.tmp / "mid")
    os.symlink(w.tmp / "mid", w.wt / "CLAUDE.md")
    a = w.compute()
    os.unlink(w.tmp / "mid")
    os.symlink(w.tmp / "two", w.tmp / "mid")
    assert w.digest() != a.digest


def test_a_link_through_a_symlinked_directory_component(w: World) -> None:
    real = w.tmp / "realdir"
    real.mkdir()
    (real / "f.md").write_text("one")
    os.symlink(real, w.tmp / "linkdir")
    os.symlink(w.tmp / "linkdir" / "f.md", w.wt / "CLAUDE.md")
    b = w.compute()
    assert one(b, "CLAUDE.md").detail is None
    (real / "f.md").write_text("two")
    assert w.digest() != b.digest


def test_relative_dotdot_target_resolves_physically(w: World) -> None:
    real = w.tmp / "a" / "b"
    real.mkdir(parents=True)
    (w.tmp / "a" / "f.md").write_text("one")
    os.symlink(real, w.tmp / "viab")
    # `viab/../f.md` is a/f.md physically (viab -> a/b), not tmp/f.md
    os.symlink(w.tmp / "viab" / ".." / "f.md", w.wt / "CLAUDE.md")
    b = w.compute()
    assert one(b, "CLAUDE.md").detail is None
    (w.tmp / "a" / "f.md").write_text("two")
    assert w.digest() != b.digest


def test_absolute_target_inside_the_same_tree(w: World) -> None:
    (w.tmp / "abs.md").write_text("one")
    os.symlink(str(w.tmp / "abs.md"), w.wt / "CLAUDE.md")
    b = w.compute()
    assert one(b, "CLAUDE.md").detail is None
    assert clean(b)


def test_the_target_text_of_a_hop_is_part_of_the_item(w: World) -> None:
    (w.tmp / "x.md").write_text("same")
    os.symlink(w.tmp / "x.md", w.wt / "CLAUDE.md")
    a = w.compute()
    os.unlink(w.wt / "CLAUDE.md")
    os.symlink("../x.md", w.wt / "CLAUDE.md")  # the same file, another spelling
    assert w.digest() != a.digest


# --- defined outcomes for the hostile and the broken ---------------------------------------


def test_a_link_loop_is_incomplete_and_detected_early(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.symlink(w.tmp / "b", w.tmp / "a")
    os.symlink(w.tmp / "a", w.tmp / "b")
    os.symlink(w.tmp / "a", w.wt / "CLAUDE.md")
    calls: list[str] = []
    real = os.readlink

    def spy(path, *, dir_fd=None):  # noqa: ANN001, ANN202
        calls.append(os.fsdecode(path))
        return real(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "readlink", spy)
    b = w.compute()
    monkeypatch.undo()
    item = one(b, "CLAUDE.md")
    assert item.detail == "incomplete"
    # CLAUDE.md, a, b, then a again is the repeated state: not sixteen hops
    assert len(calls) < baselinelinks.MAX_HOPS // 2


def test_a_self_link_is_incomplete(w: World) -> None:
    os.symlink("self", w.tmp / "self")
    os.symlink(w.tmp / "self", w.wt / "CLAUDE.md")
    assert one(w.compute(), "CLAUDE.md").detail == "incomplete"


def test_a_directory_link_that_points_at_its_own_ancestor_is_incomplete(w: World) -> None:
    hooks = Path(w.common) / "hooks"
    os.symlink("..", hooks / "up")  # hooks/up -> .git
    b = w.compute()
    bad = [i for i in link_items(b, "hooks/up") if i.detail == "incomplete"]
    assert bad and b.has_detail()  # the loop is reported, never "unchanged"


def test_a_chain_longer_than_the_bound_is_incomplete(w: World) -> None:
    final = w.tmp / "end.md"
    final.write_text("x")
    prev = final
    for n in range(16):  # with CLAUDE.md itself: 17 links, one over the bound of 16
        link = w.tmp / f"hop{n}"
        os.symlink(prev, link)
        prev = link
    os.symlink(prev, w.wt / "CLAUDE.md")
    assert one(w.compute(), "CLAUDE.md").detail == "incomplete"


def test_a_chain_at_the_bound_is_complete(w: World) -> None:
    final = w.tmp / "end.md"
    final.write_text("x")
    prev = final
    for n in range(15):  # CLAUDE.md itself is the 16th and last hop
        link = w.tmp / f"hop{n}"
        os.symlink(prev, link)
        prev = link
    os.symlink(prev, w.wt / "CLAUDE.md")
    assert one(w.compute(), "CLAUDE.md").detail is None


def test_a_dangling_link_is_a_stable_item_that_changes_when_the_target_appears(
    w: World,
) -> None:
    os.symlink(w.tmp / "later.md", w.wt / "CLAUDE.md")
    a = w.compute()
    item = one(a, "CLAUDE.md")
    assert item.detail is None  # a recordable state: committed links often dangle elsewhere
    assert w.digest() == a.digest
    (w.tmp / "later.md").write_text("now it exists")
    assert w.digest() != a.digest


def test_a_link_through_a_file_is_dangling_not_a_crash(w: World) -> None:
    (w.tmp / "file").write_text("x")
    os.symlink(w.tmp / "file" / "below", w.wt / "CLAUDE.md")
    assert one(w.compute(), "CLAUDE.md").detail is None


def test_an_empty_target_is_dangling(w: World) -> None:
    # symlink("") is refused by the kernel on Linux; a "." target is a directory (the worktree)
    os.symlink(".", w.wt / "CLAUDE.md")
    b = w.compute()
    assert link_items(b, "CLAUDE.md")  # the worktree holds itself: bounded, no crash
    assert b.has_detail()


@pytest.mark.skipif(not os.path.isdir("/dev/shm"), reason="needs a second file system")
def test_a_target_on_another_device_is_an_uncovered_item(w: World) -> None:
    if os.stat("/dev/shm").st_dev == os.stat(w.tmp).st_dev:
        pytest.skip("/dev/shm shares the device")
    other = Path("/dev/shm") / f"wtctl-link-{os.getpid()}.md"
    other.write_text("elsewhere")
    try:
        os.symlink(other, w.wt / "CLAUDE.md")
        b = w.compute()
        got = link_items(b, "CLAUDE.md")
        assert [(i.type, i.detail) for i in got] == [("symlink_uncovered", None)]
        assert not b.has_detail()
    finally:
        other.unlink()


@pytest.mark.skipif(not os.path.isdir("/dev/shm"), reason="needs a second file system")
def test_a_link_through_a_directory_on_another_device_blocks_at_the_hooks_dir(w: World) -> None:
    if os.stat("/dev/shm").st_dev == os.stat(w.tmp).st_dev:
        pytest.skip("/dev/shm shares the device")
    d = Path("/dev/shm") / f"wtctl-linkdir-{os.getpid()}"
    d.mkdir()
    try:
        (d / "hooks").mkdir()
        shutil.rmtree(Path(w.common) / "hooks")
        os.symlink(d / "hooks", Path(w.common) / "hooks")
        got = link_items(w.compute(), "/hooks")
        assert [(i.type, i.detail) for i in got] == [("symlink_target", "mount_point")]
    finally:
        shutil.rmtree(d)


def test_a_target_whose_mount_id_differs_is_an_uncovered_item(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind mount on the same device: only the mount id tells."""
    from wtctl import gateway

    (w.tmp / "bound").mkdir()
    (w.tmp / "bound" / "f.md").write_text("x")
    os.symlink(w.tmp / "bound" / "f.md", w.wt / "CLAUDE.md")
    real = gateway.fd_mount_id

    def fake(fd: int) -> int | None:
        base = real(fd)
        try:
            where = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return base
        if where.startswith(str(w.tmp / "bound")):
            return (base or 0) + 4242
        return base

    monkeypatch.setattr(gateway, "fd_mount_id", fake)
    if real(os.open(w.tmp, os.O_RDONLY)) is None:
        pytest.skip("no mount ids on this platform")
    got = link_items(w.compute(), "CLAUDE.md")
    assert [(i.type, i.detail) for i in got] == [("symlink_uncovered", None)]


def test_special_targets_are_recorded_by_type_and_never_opened(w: World) -> None:
    os.mkfifo(w.tmp / "pipe")
    os.symlink(w.tmp / "pipe", w.wt / "CLAUDE.md")
    a = w.compute()
    assert one(a, "CLAUDE.md").detail is None  # a fifo is never read: no hang
    os.unlink(w.wt / "CLAUDE.md")
    (w.tmp / "plain").write_text("x")
    os.symlink(w.tmp / "plain", w.wt / "CLAUDE.md")
    assert w.digest() != a.digest


def test_a_socket_target_is_recorded_by_type(w: World) -> None:
    s = socket.socket(socket.AF_UNIX)
    try:
        path = w.tmp / "s"
        try:
            s.bind(str(path))
        except OSError:
            pytest.skip("socket path too long")
        os.symlink(path, w.wt / "CLAUDE.md")
        assert one(w.compute(), "CLAUDE.md").detail is None
        assert stat.S_ISSOCK(os.lstat(path).st_mode)
    finally:
        s.close()


def test_a_device_target_is_never_read(w: World) -> None:
    os.symlink("/dev/zero", w.wt / "CLAUDE.md")  # endless if it were read
    item = link_items(w.compute(), "CLAUDE.md")[0]
    assert item.detail is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_an_unreadable_target_is_incomplete(w: World) -> None:
    f = w.tmp / "secret.md"
    f.write_text("x")
    f.chmod(0)
    os.symlink(f, w.wt / "CLAUDE.md")
    try:
        assert one(w.compute(), "CLAUDE.md").detail == "incomplete"
    finally:
        f.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_a_target_below_an_unsearchable_directory_is_incomplete(w: World) -> None:
    d = w.tmp / "locked"
    d.mkdir()
    (d / "f.md").write_text("x")
    d.chmod(0)
    os.symlink(d / "f.md", w.wt / "CLAUDE.md")
    try:
        assert one(w.compute(), "CLAUDE.md").detail == "incomplete"
    finally:
        d.chmod(0o755)


def test_a_target_over_the_file_bound_is_incomplete(w: World) -> None:
    (w.tmp / "big.md").write_bytes(b"x" * 5000)
    os.symlink(w.tmp / "big.md", w.wt / "CLAUDE.md")
    assert one(w.compute(max_file=1000), "CLAUDE.md").detail == "incomplete"
    assert one(w.compute(), "CLAUDE.md").detail is None


def test_the_byte_budget_covers_link_targets(w: World) -> None:
    base = w.compute().bytes
    for n in range(4):
        (w.tmp / f"t{n}.md").write_bytes(b"x" * 3000)
        os.symlink(w.tmp / f"t{n}.md", w.wt / f"CLAUDE{n}.md")
    assert not w.compute().has_detail()
    b = w.compute(max_total=base + 7000)
    assert any(i.detail == "incomplete" for i in link_items(b))


def test_the_entry_budget_covers_hops_and_target_listings(w: World) -> None:
    real = w.tmp / "big-dir"
    real.mkdir()
    for n in range(200):
        (real / f"f{n}").write_text("x")
    os.symlink(real, w.wt / ".claude")
    assert one(w.compute(limit=50), ".claude").detail == "incomplete"
    assert one(w.compute(), ".claude").detail is None


def test_a_deep_target_tree_is_bounded_not_a_stack_overflow(w: World) -> None:
    real = w.tmp / "deep"
    p = real
    for _ in range(60):
        p = p / "d"
    p.mkdir(parents=True)
    os.symlink(real, w.wt / ".claude")
    assert one(w.compute(limit=40), ".claude").detail == "incomplete"


def test_nesting_of_followed_directories_is_bounded(w: World) -> None:
    """dir -> dir with a link to dir -> ...: followed to a fixed nesting depth, then incomplete."""
    prev = w.tmp / "lvl0"
    prev.mkdir()
    (prev / "f").write_text("leaf")
    for n in range(1, repobaseline.MAX_LINK_NEST + 3):
        d = w.tmp / f"lvl{n}"
        d.mkdir()
        os.symlink(prev, d / "next")
        prev = d
    os.symlink(prev, w.wt / ".claude")
    b = w.compute()
    assert any(i.detail == "incomplete" for i in link_items(b))


def test_hostile_names_and_targets(w: World) -> None:
    raw = os.fsencode(str(w.wt)) + b"/CLAUDE\xff.md"
    os.symlink(b"/nonexistent/\xff\xfe\n|\x01", raw)
    (w.wt / "CLAUDE.local.md").symlink_to("../" * 40 + "etc/hostname")
    (w.wt / "CLAUDE.x.md").symlink_to("a/" * 500 + "z")
    (w.wt / "claude.long.md").symlink_to("n" * 300)  # a component over NAME_MAX
    b = w.compute()
    assert len(link_items(b)) >= 4
    assert all(i.detail in (None, "incomplete", "mount_point") for i in link_items(b))
    assert w.digest() == b.digest  # deterministic, printable paths


def test_a_link_to_the_root_or_the_whole_tree_is_bounded(w: World) -> None:
    os.symlink("/", w.wt / ".claude")
    b = w.compute(limit=500)
    assert one(b, ".claude").detail in ("incomplete", "mount_point")


# --- races ---------------------------------------------------------------------------------


def _after_content_read(
    monkeypatch: pytest.MonkeyPatch, marker: str, action: Callable[[], None]
) -> None:
    """Run ``action`` once, right after the content of the file named ``marker`` was read."""
    real = repobaseline._read_fd
    done: list[int] = []

    def wrapped(fd, budget, keep):  # noqa: ANN001, ANN202
        out = real(fd, budget, keep)
        if not done and os.readlink(f"/proc/self/fd/{fd}").endswith("/" + marker):
            done.append(1)
            action()
        return out

    monkeypatch.setattr(repobaseline, "_read_fd", wrapped)


def test_a_link_retargeted_between_collection_and_recheck_is_incomplete(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    (w.tmp / "one.md").write_text("one")
    (w.tmp / "two.md").write_text("two")
    link = w.wt / "CLAUDE.md"
    os.symlink(w.tmp / "one.md", link)

    def retarget() -> None:
        os.unlink(link)
        os.symlink(w.tmp / "two.md", link)

    _after_content_read(monkeypatch, "one.md", retarget)
    assert one(w.compute(), "CLAUDE.md").detail == "incomplete"


def test_a_middle_hop_retargeted_after_collection_is_incomplete(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    (w.tmp / "one.md").write_text("one")
    (w.tmp / "two.md").write_text("two")
    os.symlink(w.tmp / "one.md", w.tmp / "mid")
    os.symlink(w.tmp / "mid", w.wt / "CLAUDE.md")

    def retarget() -> None:
        os.unlink(w.tmp / "mid")
        os.symlink(w.tmp / "two.md", w.tmp / "mid")

    _after_content_read(monkeypatch, "one.md", retarget)
    assert one(w.compute(), "CLAUDE.md").detail == "incomplete"


def test_the_final_file_replaced_after_it_was_read_is_incomplete(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = w.tmp / "t.md"
    target.write_text("one")
    os.symlink(target, w.wt / "CLAUDE.md")

    def replace() -> None:
        os.unlink(target)
        target.write_text("one")  # same bytes, another inode

    _after_content_read(monkeypatch, "t.md", replace)
    assert one(w.compute(), "CLAUDE.md").detail == "incomplete"


def test_a_hop_swapped_for_a_regular_file_while_reading_is_incomplete(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    (w.tmp / "one.md").write_text("one")
    os.symlink(w.tmp / "one.md", w.tmp / "mid")
    os.symlink(w.tmp / "mid", w.wt / "CLAUDE.md")
    real = os.readlink
    state = {"armed": False}

    def spy(path, *, dir_fd=None):  # noqa: ANN001, ANN202
        if os.fsdecode(path) == "mid" and not state["armed"]:
            state["armed"] = True
            os.unlink(w.tmp / "mid")
            (w.tmp / "mid").write_text("now a file")
        return real(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "readlink", spy)
    assert one(w.compute(), "CLAUDE.md").detail == "incomplete"


# --- the digest ----------------------------------------------------------------------------


def test_a_tree_without_links_has_the_same_digest_in_both_formats(w: World) -> None:
    w.repo.write(".claude/settings.json", "{}")
    w.repo.write("CLAUDE.md", "x")
    w.append_cfg("[core]\n\tfsmonitor = x\n")
    assert w.digest() == w.digest(links=False)


def test_the_legacy_collector_never_follows(w: World) -> None:
    (w.tmp / "o.md").write_text("one")
    os.symlink(w.tmp / "o.md", w.wt / "CLAUDE.md")
    a = w.compute(links=False)
    (w.tmp / "o.md").write_text("two")
    assert w.digest(links=False) == a.digest
    assert not link_items(a)


def test_a_complete_item_is_never_both_recorded_and_incomplete(w: World) -> None:
    (w.tmp / "o.md").write_text("one")
    os.symlink(w.tmp / "o.md", w.wt / "CLAUDE.md")
    b = w.compute()
    rows = [i for i in b.items if i.key == KEY]
    assert rows and all(len(i.sha256) == 64 for i in rows)


def test_routine_git_changes_leave_the_digest_alone_with_links_present(w: World) -> None:
    swap_hooks_for_link(w, w.tmp / "dots" / "hooks")
    (w.wt / "AGENTS.md").write_text("rules")
    os.symlink("AGENTS.md", w.wt / "CLAUDE.md")
    before = w.digest()
    w.repo.g(["config", "branch.feat.remote", "origin"])
    w.repo.g(["config", "branch.feat.merge", "refs/heads/feat"])
    w.repo.g(["branch", "side"], w.repo.main)
    w.repo.g(["worktree", "add", "-q", "-b", "sibling", str(w.tmp / "sib")], w.repo.main)
    w.repo.g(["worktree", "remove", "--force", str(w.tmp / "sib")], w.repo.main)
    assert w.digest() == before


# --- round 1 review fixes ------------------------------------------------------------------


def nested_claude_links(w: World, depth: int) -> None:
    """.claude -> lvl<depth>, each lvl<i> holding `next` -> lvl<i-1>: ``depth`` links nested
    inside the first followed directory."""
    prev = w.tmp / "lvl0"
    prev.mkdir()
    (prev / "f").write_text("leaf")
    for n in range(1, depth + 1):
        d = w.tmp / f"lvl{n}"
        d.mkdir()
        os.symlink(prev, d / "next")
        prev = d
    os.symlink(prev, w.wt / ".claude")


def test_nesting_of_four_links_is_complete(w: World) -> None:
    nested_claude_links(w, 4)
    assert not any(i.detail for i in link_items(w.compute()))


def test_nesting_of_five_links_is_incomplete(w: World) -> None:
    nested_claude_links(w, 5)
    assert any(i.detail == "incomplete" for i in link_items(w.compute()))


def test_a_swapped_anchor_directory_is_refused_by_resolve(w: World) -> None:
    (w.wt / "CLAUDE.md").write_text("old")
    anchor = fsafe.open_anchor(str(w.wt), expected=repobaseline._identity(w.repo.dir_id))
    try:
        os.rename(w.wt, w.tmp / "old-wt")
        w.wt.mkdir()
        (w.wt / "CLAUDE.md").write_text("new")
        with pytest.raises(fsafe.FsafeError) as e:
            baselinelinks.resolve(anchor, f"{w.wt}/CLAUDE.md", lambda: None).close()
        assert e.value.kind == "identity"
    finally:
        anchor.close()


class _OtherDeviceSpecials(fsafe.Fs):
    """FIFOs and sockets report another device (a /dev/null-like node on a device file system)."""

    def adjust(self, st: fsafe.FdStat) -> fsafe.FdStat:
        if stat.S_ISFIFO(st.mode) or stat.S_ISSOCK(st.mode):
            return dataclasses.replace(st, dev=st.dev + 1)
        return st


def test_a_special_target_on_another_device_stays_type_only(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: never opened, and the item hash is the one from before cross-device links."""
    import hashlib

    os.mkfifo(w.tmp / "pipe")
    os.symlink(w.tmp / "pipe", w.wt / "CLAUDE.md")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", _OtherDeviceSpecials())
    item = link_items(w.compute(), "CLAUDE.md")[0]
    assert (item.type, item.detail) == ("symlink_target", None)
    hop = str(w.tmp / "pipe").encode()
    h = hashlib.sha256(b"wtctl-link-v1\n" + b"hop:%d:" % len(hop) + hop + b"\nspecial:fifo\n")
    assert item.sha256 == h.hexdigest()


def test_a_regular_file_on_another_device_is_uncovered_and_not_read(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Files(fsafe.Fs):
        def adjust(self, st: fsafe.FdStat) -> fsafe.FdStat:
            return dataclasses.replace(st, dev=st.dev + 1) if st.ino == ino else st

    (w.tmp / "plain").write_text("x")
    ino = os.stat(w.tmp / "plain").st_ino
    os.symlink(w.tmp / "plain", w.wt / "CLAUDE.md")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", Files())
    item = link_items(w.compute(), "CLAUDE.md")[0]
    assert (item.type, item.detail) == ("symlink_uncovered", None)


def claude_dir_link(w: World, where: str) -> Path:
    real = w.tmp / "claude-real"
    (real / "agents").mkdir(parents=True)
    (real / "agents" / "a.md").write_text("one")
    link = w.wt / where
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(real, link)
    return real


@pytest.mark.parametrize("where", [".claude", "pkg/.claude"])
def test_cc_writes_behind_a_symlinked_claude_directory_is_ignored(w: World, where: str) -> None:
    real = claude_dir_link(w, where)
    before = w.digest()
    (real / ".cc-writes").mkdir()
    (real / ".cc-writes" / "x").write_text("churn")
    assert w.digest() == before
    (real / ".cc-writes" / "x").write_text("more churn")
    assert w.digest() == before
    (real / "agents" / "a.md").write_text("two")  # real content still counts
    assert w.digest() != before


def test_cc_writes_behind_a_link_not_named_claude_still_counts(w: World) -> None:
    real = w.tmp / "other-real"
    (real / ".cc-writes").mkdir(parents=True)
    (real / ".cc-writes" / "x").write_text("one")
    (w.wt / ".claude").mkdir()
    os.symlink(real, w.wt / ".claude" / "sub")
    before = w.digest()
    (real / ".cc-writes" / "x").write_text("two")
    assert w.digest() != before


def gitdir_link(w: World, name: str = "sub") -> tuple[Path, Path]:
    real = w.tmp / "gitdirs" / name
    (real / "hooks").mkdir(parents=True)
    (real / "objects").mkdir()
    (real / "HEAD").write_text("ref: refs/heads/main\n")
    (real / "config").write_text("[core]\n\tfsmonitor = a\n")
    (real / "hooks" / "h").write_text("a")
    modules = Path(w.common) / "modules"
    modules.mkdir(exist_ok=True)
    os.symlink(real, modules / name)
    return real, modules / name


def test_a_symlinked_submodule_git_dir_is_followed(w: World) -> None:
    real, _ = gitdir_link(w)
    a = w.compute()
    assert clean(a) and one(a, "modules/sub").detail is None
    assert any(i.path.endswith("modules/sub/config") for i in a.items)
    (real / "config").write_text("[core]\n\tfsmonitor = b\n")
    b = w.compute()
    assert b.digest != a.digest
    (real / "hooks" / "h").write_text("b")
    c = w.compute()
    assert c.digest != b.digest
    (real / "hooks" / "new").write_text("n")
    assert w.digest() != c.digest


def test_a_symlinked_submodule_git_dir_skips_objects_and_refs(w: World) -> None:
    real, _ = gitdir_link(w)
    before = w.digest()
    (real / "objects" / "ab").write_text("pack")
    assert w.digest() == before


def test_a_symlinked_submodule_git_dir_is_ignored_by_the_legacy_collector(w: World) -> None:
    real, _ = gitdir_link(w)
    before = w.digest(links=False)
    (real / "config").write_text("[core]\n\tfsmonitor = b\n")
    assert w.digest(links=False) == before


def test_a_symlinked_submodule_git_dir_that_loops_is_incomplete(w: World) -> None:
    modules = Path(w.common) / "modules"
    modules.mkdir()
    os.symlink("../modules", modules / "again")
    b = w.compute()
    assert any(i.detail == "incomplete" for i in link_items(b, "modules/again"))


def test_a_retargeted_submodule_git_dir_link_changes_the_digest(w: World) -> None:
    real, link = gitdir_link(w)
    other = w.tmp / "gitdirs" / "other"
    shutil.copytree(real, other, symlinks=True)
    before = w.digest()
    link.unlink()
    os.symlink(other, link)
    assert w.digest() != before
