"""The claude walk skips only VERIFIED linked worktrees beneath it (docs/interface.md, Baseline,
Linked worktrees; issue #71). Hostile cases first: a planted directory must never hide content."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.baselib import World
from wtctl import baselinelinked, baselinesvc, repobaseline, worktrees
from wtctl.repobaseline import Baseline

KEY = baselinelinked.LINKED_KEY
# the claude-class digest of the fixed tree in `tree()` as recorded by the code before #71
GOLDEN = "6dc87875a556ab437e0b2d014b7445bb179aae630b289d69b06e320214299d59"


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> World:
    return World(tmp_path, private_home)


def main_of(w: World) -> Path:
    return w.repo.main


def listed(w: World) -> list[str]:
    return baselinesvc.listed_worktrees(w.repo.git, w.common)


def spec(w: World, **over: object) -> repobaseline.Spec:
    m = main_of(w)
    st = os.stat(m)
    base: dict[str, object] = {
        "worktree": str(m),
        "dir_id": f"{st.st_dev}:{st.st_ino}",
        "admin_dir": None,
        "listed": listed(w),
    }
    return w.spec(**{**base, **over})


def run(w: World, **over: object) -> Baseline:
    return repobaseline.compute(spec(w, **over), runner=repobaseline.inproc)


def cdigest(b: Baseline) -> str:
    return repobaseline.digest_of([i for i in b.items if i.cls == "claude"])


def cd(w: World, **over: object) -> str:
    return cdigest(run(w, **over))


def linked_items(b: Baseline) -> list[repobaseline.Item]:
    return [i for i in b.items if i.key == KEY]


def add_wt(w: World, rel: str, branch: str | None = None) -> Path:
    path = main_of(w) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    w.repo.g(
        [
            "worktree",
            "add",
            "-q",
            "-b",
            branch or "b-" + rel.replace("/", "-").lstrip("."),
            str(path),
        ],
        main_of(w),
    )
    return path


def admin_of(w: World, path: Path) -> Path:
    return Path(w.repo.g(["rev-parse", "--absolute-git-dir"], path))


def plant(path: Path) -> None:
    (path / "CLAUDE.md").write_text("planted instructions\n")
    (path / ".claude").mkdir(exist_ok=True)
    (path / ".claude" / "settings.json").write_text('{"hooks": "planted"}\n')


def tree(w: World) -> None:
    m = main_of(w)
    (m / "CLAUDE.md").write_text("root rules\n")
    (m / ".claude").mkdir()
    (m / ".claude" / "settings.json").write_text('{"a":1}\n')
    (m / ".claude" / "hooks").mkdir()
    h = m / ".claude" / "hooks" / "h.sh"
    h.write_text("#!/bin/sh\n")
    h.chmod(0o755)
    (m / "sub").mkdir()
    (m / "sub" / "CLAUDE.local.md").write_text("sub\n")
    (m / ".mcp.json").write_text("{}\n")


# --- digests of trees without linked worktrees are unchanged -------------------------------


@pytest.mark.parametrize("links", [False, True])
@pytest.mark.parametrize("linked", [False, True])
def test_a_tree_without_linked_worktrees_keeps_its_digest(
    w: World, links: bool, linked: bool
) -> None:
    tree(w)
    # the worktree `wt` is outside the walked tree: git lists it, nothing is beneath main
    b = run(w, links=links, linked=linked)
    assert cdigest(b) == GOLDEN
    assert not linked_items(b)


def test_format_2_collection_ignores_the_listing(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    assert wt.is_dir()
    old = run(w, linked=False)
    assert not linked_items(old)
    assert old.digest == run(w, linked=False, listed=[]).digest


# --- the skip ---------------------------------------------------------------------------------


@pytest.mark.parametrize("rel", [".claude/worktrees/x", "nested/dir/x", "x"])
def test_edits_in_a_verified_linked_worktree_do_not_change_the_main_baseline(
    w: World, rel: str
) -> None:
    tree(w)
    wt = add_wt(w, rel)
    before = run(w)
    assert [i.path for i in linked_items(before)] == [rel]
    (wt / "CLAUDE.md").write_text("edited\n")
    (wt / ".claude").mkdir(exist_ok=True)
    (wt / ".claude" / "settings.json").write_text("{}\n")
    (wt / ".claude" / "hooks").mkdir()
    (wt / ".claude" / "hooks" / "evil.sh").write_text("x\n")
    (wt / "src").mkdir(exist_ok=True)
    (wt / "src" / "CLAUDE.md").write_text("deep\n")
    (wt / "README.md").write_text("changed\n")
    after = run(w)
    assert after.digest == before.digest
    assert [(i.path, i.sha256) for i in after.items] == [(i.path, i.sha256) for i in before.items]


def test_the_same_edit_is_seen_without_the_skip(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    before = run(w, linked=False)
    (wt / "CLAUDE.md").write_text("edited\n")
    assert run(w, linked=False).digest != before.digest


def test_the_item_is_identity_only(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    (item,) = linked_items(run(w))
    assert (item.cls, item.type, item.detail) == ("claude", "linked_worktree", None)
    assert item.sha256 == baselinelinked.identity_hash(".claude/worktrees/x", admin_of(w, wt).name)
    (wt / "CLAUDE.md").write_text("anything\n")
    assert linked_items(run(w)) == [item]


def test_the_item_is_for_the_main_record_not_a_linked_one(w: World) -> None:
    tree(w)
    add_wt(w, ".claude/worktrees/x")
    own = repobaseline.compute(
        w.spec(listed=listed(w)), runner=repobaseline.inproc
    )  # the record of `wt`, a linked worktree with nothing beneath it
    assert not linked_items(own)


def test_a_linked_record_skips_linked_worktrees_beneath_it(w: World) -> None:
    sub = w.wt / "inner"
    w.repo.g(["worktree", "add", "-q", "-b", "inner", str(sub)])
    spc = w.spec(listed=listed(w))
    one = repobaseline.compute(spc, runner=repobaseline.inproc)
    assert [i.path for i in linked_items(one)] == ["inner"]
    (sub / "CLAUDE.md").write_text("edited\n")
    assert repobaseline.compute(spc, runner=repobaseline.inproc).digest == one.digest


def test_nested_linked_worktrees_are_one_skip(w: World) -> None:
    tree(w)
    outer = add_wt(w, ".claude/worktrees/outer")
    w.repo.g(["worktree", "add", "-q", "-b", "innermost", str(outer / "innermost")], main_of(w))
    b = run(w)
    assert [i.path for i in linked_items(b)] == [".claude/worktrees/outer"]
    (outer / "innermost" / "CLAUDE.md").write_text("x\n")
    assert run(w).digest == b.digest


def test_adding_or_removing_a_linked_worktree_beneath_changes_the_digest(w: World) -> None:
    tree(w)
    base = run(w)
    wt = add_wt(w, ".claude/worktrees/x")
    with_wt = run(w)
    assert cdigest(with_wt) != cdigest(base)
    rows = repobaseline.compare(base.items, with_wt.items)
    assert [(r["path"], r["key"], r["before"]) for r in rows if r["key"] == KEY] == [
        (".claude/worktrees/x", KEY, "absent")
    ]
    w.repo.g(["worktree", "remove", "--force", str(wt)], main_of(w))
    os.rmdir(main_of(w) / ".claude" / "worktrees")  # git leaves the empty parent directory
    gone = run(w)
    rows = repobaseline.compare(with_wt.items, gone.items)
    assert [(r["key"], r["after"]) for r in rows if r["key"] == KEY] == [(KEY, "absent")]
    assert cdigest(gone) == cdigest(base)


def test_routine_git_leaves_the_claude_digest_alone(w: World) -> None:
    tree(w)
    before = cd(w)
    other = w.tmp / "elsewhere"
    w.repo.g(["worktree", "add", "-q", "-b", "else", str(other)], main_of(w))
    w.repo.g(["branch", "side"], main_of(w))
    w.repo.g(["worktree", "remove", "--force", str(other)], main_of(w))
    assert cd(w) == before


# --- planted directories are walked ---------------------------------------------------------


def planted_dir(w: World, rel: str = ".claude/worktrees/fake") -> Path:
    d = main_of(w) / rel
    d.mkdir(parents=True)
    return d


def test_a_dir_with_a_forged_git_file_that_git_does_not_list_is_walked(w: World) -> None:
    tree(w)
    d = planted_dir(w)
    (d / ".git").write_text(f"gitdir: {w.common}/worktrees/{admin_of(w, w.wt).name}\n")
    before = run(w)
    assert not linked_items(before)
    plant(d)
    assert cdigest(run(w)) != cdigest(before)


def test_a_dir_whose_git_file_names_a_missing_admin_entry_is_walked(w: World) -> None:
    tree(w)
    d = planted_dir(w, "fake")
    (d / ".git").write_text(f"gitdir: {w.common}/worktrees/nothing\n")
    before = run(w, listed=[str(d)])  # even if it were handed in as listed
    assert not linked_items(before)
    plant(d)
    assert cdigest(run(w, listed=[str(d)])) != cdigest(before)


def test_a_listed_dir_with_a_mismatching_back_pointer_is_walked(w: World) -> None:
    tree(w)
    d = planted_dir(w, "fake")
    # an existing admin entry whose gitdir names another worktree
    (d / ".git").write_text(f"gitdir: {admin_of(w, w.wt)}\n")
    got = run(w, listed=[str(d)])
    assert not linked_items(got)
    plant(d)
    assert cdigest(run(w, listed=[str(d)])) != cdigest(got)


def test_a_listed_worktree_whose_admin_gitdir_moved_is_walked(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    admin = admin_of(w, wt)
    (admin / "gitdir").write_text(str(w.tmp / "elsewhere" / ".git") + "\n")
    assert str(wt) not in listed(w)
    b = run(w)
    assert not linked_items(b)
    (wt / "CLAUDE.md").write_text("edited\n")
    assert cdigest(run(w)) != cdigest(b)
    # and with the directory handed in anyway: the back-pointer still refuses it
    assert not linked_items(run(w, listed=[str(wt)]))


def test_a_git_file_naming_another_clones_admin_entry_is_walked(w: World, tmp_path: Path) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    other = tmp_path / "other"
    other.mkdir()
    w.repo.g(["init", "-q", "-b", "main"], other)
    w.repo.g(["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
              "-m", "c"], other)  # fmt: skip
    oth = other.parent / "other-wt"
    w.repo.g(["worktree", "add", "-q", "-b", "y", str(oth)], other)
    foreign = admin_of(w, oth)
    (wt / ".git").write_text(f"gitdir: {foreign}\n")
    b = run(w)
    assert not linked_items(b)
    (wt / "CLAUDE.md").write_text("edited\n")
    assert cdigest(run(w)) != cdigest(b)


@pytest.mark.parametrize("kind", ["dir", "symlink", "fifo"])
def test_a_git_that_is_not_a_regular_file_is_walked(w: World, kind: str) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    admin = admin_of(w, wt)
    os.remove(wt / ".git")
    if kind == "dir":
        (wt / ".git").mkdir()
    elif kind == "symlink":
        real = w.tmp / "realgit"
        real.write_text(f"gitdir: {admin}\n")
        os.symlink(real, wt / ".git")
    else:
        os.mkfifo(wt / ".git")
    b = run(w, listed=[str(wt)])
    assert not linked_items(b)
    (wt / "CLAUDE.md").write_text("edited\n")
    assert cdigest(run(w, listed=[str(wt)])) != cdigest(b)


def test_an_unlisted_dir_that_is_otherwise_perfect_is_walked(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    b = run(w, listed=[])
    assert not linked_items(b)
    (wt / "CLAUDE.md").write_text("edited\n")
    assert cdigest(run(w, listed=[])) != cdigest(b)


def test_a_worktree_with_a_deleted_admin_entry_is_not_skipped(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    shutil.rmtree(admin_of(w, wt))
    assert str(wt) not in listed(w)
    b = run(w)
    assert not linked_items(b)
    (wt / "CLAUDE.md").write_text("edited\n")
    assert cdigest(run(w)) != cdigest(b)


def test_a_locked_worktree_is_not_skipped(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    w.repo.g(["worktree", "lock", str(wt)], main_of(w))
    assert str(wt) not in listed(w)
    b = run(w)
    assert not linked_items(b)
    (wt / "CLAUDE.md").write_text("edited\n")
    assert cdigest(run(w)) != cdigest(b)


def test_a_directory_that_stops_being_verified_changes_the_digest(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    before = run(w)
    (wt / ".git").write_text(f"gitdir: {admin_of(w, w.wt)}\n")  # no longer points home
    after = run(w, listed=[str(wt)])
    assert not linked_items(after)
    assert cdigest(after) != cdigest(before)


def test_a_claude_named_directory_is_never_skipped(w: World) -> None:
    tree(w)
    m = main_of(w)
    hidden = m / "CLAUDE.md-dir"
    hidden.mkdir()
    w.repo.g(["worktree", "add", "-q", "--detach", str(hidden / "x"), "HEAD"], m)
    for name in (".claude",):
        assert str(m / name) not in listed(w)
    # a verified worktree called CLAUDE.md or .claude would hide the very thing the baseline
    # exists for: the protection is by name, whatever git says
    lv = baselinelinked.LinkedWorktrees(
        repobaseline.fsafe.open_anchor(str(m)), w.common, w.common_id, [str(m / ".claude")],
        lambda n: n == ".claude",
    )  # fmt: skip
    try:
        assert not lv.check(str(m), repobaseline.fsafe.Entry((".claude",), "dir"))
    finally:
        lv.anchor.close()
        lv.close()


# --- symlinks ---------------------------------------------------------------------------------


def test_a_symlink_in_place_of_a_verified_worktree_is_walked(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    before = run(w)
    real = w.tmp / "swap"
    real.mkdir()
    plant(real)
    shutil.rmtree(wt)
    os.symlink(real, wt)
    after = run(w, listed=[str(wt)])
    assert not linked_items(after)
    assert cdigest(after) != cdigest(before)
    (real / "CLAUDE.md").write_text("again\n")
    assert cdigest(run(w, listed=[str(wt)])) != cdigest(after)


def test_a_link_to_a_verified_worktree_does_not_skip_the_target(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    os.symlink(wt, main_of(w) / "ln")
    b = run(w)
    assert [i.path for i in linked_items(b)] == [".claude/worktrees/x"]


# --- the race ---------------------------------------------------------------------------------


def test_a_directory_replaced_after_verification_is_incomplete(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    real = baselinelinked.LinkedWorktrees._verify  # pyright: ignore[reportPrivateUsage]
    calls = {"n": 0}

    def swap(self, parts):  # noqa: ANN001, ANN202
        out = real(self, parts)
        calls["n"] += 1
        if calls["n"] == 1:  # between the verification and the end of the walk
            os.rename(wt, w.tmp / "x-moved")
            wt.mkdir()
            plant(wt)
        return out

    monkeypatch.setattr(baselinelinked.LinkedWorktrees, "_verify", swap)
    b = run(w)
    assert not linked_items(b)
    bad = [i for i in b.items if i.path == ".claude/worktrees/x"]
    assert [i.detail for i in bad] == ["incomplete"]
    assert b.has_detail()


def test_a_directory_swapped_before_the_walk_opens_it_is_not_skipped(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the identity verified is the one the walk opened: a different directory is walked
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    real = baselinelinked.LinkedWorktrees._verify  # pyright: ignore[reportPrivateUsage]

    def lie(self, parts):  # noqa: ANN001, ANN202
        got = real(self, parts)
        assert got is not None
        return baselinelinked.Verified(
            got.rel, repobaseline.fsafe.Identity(got.identity.dev, got.identity.ino + 1), got.admin
        )

    monkeypatch.setattr(baselinelinked.LinkedWorktrees, "_verify", lie)
    (wt / "CLAUDE.md").write_text("walked\n")
    assert not linked_items(run(w))


# --- cost -------------------------------------------------------------------------------------


def test_verification_is_per_listed_directory_not_per_directory(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree(w)
    for i in range(3):
        add_wt(w, f".claude/worktrees/w{i}")
    for i in range(60):
        (main_of(w) / "plain" / f"d{i}").mkdir(parents=True)
    n = {"verify": 0, "list": 0}
    real = baselinelinked.LinkedWorktrees._verify  # pyright: ignore[reportPrivateUsage]
    real_list = worktrees.worktree_entries

    def counted(self, parts):  # noqa: ANN001, ANN202
        n["verify"] += 1
        return real(self, parts)

    def counted_list(*a, **k):  # noqa: ANN002, ANN003, ANN202
        n["list"] += 1
        return real_list(*a, **k)

    monkeypatch.setattr(baselinelinked.LinkedWorktrees, "_verify", counted)
    monkeypatch.setattr(worktrees, "worktree_entries", counted_list)
    s = spec(w)
    assert n["list"] == 1
    repobaseline.compute(s, runner=repobaseline.inproc)
    assert n["verify"] == 6  # three worktrees, verified and re-verified once each
    assert n["list"] == 1


def test_a_failed_listing_hands_in_nothing(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from wtctl.errors import WtctlError

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise WtctlError("internal", "forged")

    monkeypatch.setattr(worktrees, "worktree_entries", boom)
    assert listed(w) == []


# --- admin names, listing failures, never-skipped names (round 1) -----------------------------


@pytest.mark.parametrize("name", ["x+y", "_u", "é", "a@b"])
def test_a_worktree_with_an_unusual_but_valid_admin_name_is_skipped(w: World, name: str) -> None:
    tree(w)
    wt = add_wt(w, f".claude/worktrees/{name}", branch="b-" + name.encode().hex())
    assert admin_of(w, wt).name == name
    b = run(w)
    assert [i.path for i in linked_items(b)] == [f".claude/worktrees/{name}"]
    before = cdigest(b)
    (wt / "CLAUDE.md").write_text("edited\n")
    assert cd(w) == before


def test_a_forged_nested_admin_name_is_walked(w: World) -> None:
    tree(w)
    d = planted_dir(w, "fake")
    admin = Path(w.common) / "worktrees" / "fx" / "y"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text(f"{d}/.git\n")
    (admin / "commondir").write_text(f"{w.common}\n")
    (d / ".git").write_text(f"gitdir: {admin}\n")
    before = run(w, listed=[str(d)])
    assert not linked_items(before)
    (d / "CLAUDE.md").write_text("planted\n")
    assert cdigest(run(w, listed=[str(d)])) != cdigest(before)


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\nb", "a\rb", "a\0b"])
def test_admin_name_must_be_one_plain_component(bad: str) -> None:
    assert not baselinelinked.is_admin_name(bad)


@pytest.mark.parametrize("good", ["x", "x+y", "_u", "é", "a@b", "feat-1.2", ".dot", "...x"])
def test_admin_name_accepts_any_other_single_component(good: str) -> None:
    assert baselinelinked.is_admin_name(good)


def test_a_regular_file_in_place_of_the_admin_directory_hands_in_nothing(w: World) -> None:
    import shutil as sh

    wt = add_wt(w, ".claude/worktrees/x")
    sh.rmtree(Path(w.common) / "worktrees")
    (Path(w.common) / "worktrees").write_text("junk\n")
    assert wt.exists()
    assert listed(w) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores modes")
def test_an_unreadable_admin_directory_hands_in_nothing(w: World) -> None:
    add_wt(w, ".claude/worktrees/x")
    adm = Path(w.common) / "worktrees"
    adm.chmod(0)
    try:
        assert listed(w) == []
    finally:
        adm.chmod(0o755)


def test_a_worktree_path_with_a_newline_is_not_skipped(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/a\nb", branch="b-nl")
    assert listed(w) == []
    b = run(w)
    assert not linked_items(b)
    (wt / "CLAUDE.md").write_text("edited\n")
    assert cdigest(run(w)) != cdigest(b)


def test_a_prunable_worktree_is_not_skipped(w: World) -> None:
    tree(w)
    wt = add_wt(w, ".claude/worktrees/x")
    shutil.rmtree(wt)  # git lists it as prunable
    assert str(wt) not in listed(w)
    wt.mkdir()
    (wt / "CLAUDE.md").write_text("planted\n")
    assert not linked_items(run(w))


@pytest.mark.parametrize(
    "rel",
    ["CLAUDE.md", "sub/claude.md", "CLAUDE-x.md", ".mcp.json", ".claude"],
)
def test_a_verified_worktree_at_a_claude_named_path_is_walked(w: World, rel: str) -> None:
    wt = add_wt(w, rel)
    assert str(wt) in listed(w)  # git lists it: only the name protects the content
    b = run(w)
    assert not [i for i in linked_items(b) if i.path == rel]
    before = cdigest(b)
    (wt / "CLAUDE.md").write_text("planted\n")
    assert cdigest(run(w)) != before


def test_lazy_git_caches_a_failed_open() -> None:
    from wtctl.errors import WtctlError

    attempts: list[int] = []

    def opener():
        attempts.append(1)
        raise WtctlError("internal", "no usable git")

    lazy = baselinesvc.LazyGit(opener)
    with pytest.raises(WtctlError) as first:
        lazy.get()
    with pytest.raises(WtctlError) as second:
        lazy.get()
    assert len(attempts) == 1
    assert second.value is first.value
