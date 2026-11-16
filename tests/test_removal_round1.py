"""Findings of the first review of the removal checks (R1 to R19): each test pins one of them.

The suite avoids asserting git's stderr wording (CI runs a newer git than a laptop does).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from tests.removallib import RW
from wtctl import fsafe, removal_content
from wtctl.gateway import GitResult, run_git
from wtctl.ignorematch import Disposable
from wtctl.state import State, clone_lease


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RW:  # noqa: ANN001
    return RW(tmp_path, private_home)


def partial(w: RW, *, drop_dotgit: bool = False) -> None:
    w.db_exec("UPDATE records SET state = 'partial' WHERE id = ?", w.rid)
    if drop_dotgit:
        (w.wt / ".git").unlink()


# -- R1, R5, R11: the partial fallback ------------------------------------------------------------


def test_r1_partial_with_pointers_gone_and_no_scanner_is_unknown(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    partial(w, drop_dotgit=True)
    w.scanner.fail = True
    d = w.st(capsys)
    assert d["checks"]["in_use"]["status"] == "unavailable"
    assert (d["would_remove"], d["reason"]) == (None, None)


def test_r5_partial_with_intact_pointers_and_a_failed_git_step_is_not_true(
    w: RW,
    capsys,  # noqa: ANN001
) -> None:
    w.start(capsys)
    partial(w)
    (w.wt / "left.txt").write_text("x")

    def runner(argv, **kw):  # noqa: ANN001, ANN202
        if "--git-common-dir" in argv:
            return GitResult(0, b"", b"", True)
        return run_git(argv, **kw)

    w.git_runner = runner
    d = w.st(capsys)
    assert d["would_remove"] is not True
    assert d["checks"]["integrity"]["details"][0]["step"] == "git_common_dir"


def test_r11_partial_fallback_walks_the_recorded_root(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    partial(w, drop_dotgit=True)
    row = w.rows("SELECT base_dir, path FROM records WHERE id = ?", w.rid)[0]
    base, path = Path(row["base_dir"]), Path(row["path"])
    rel = path.relative_to(base)
    old = Path(str(base) + ".old")
    base.rename(old)  # a new directory now stands where the recorded root was
    base.mkdir()
    (base / rel).parent.mkdir(parents=True, exist_ok=True)
    (old / rel).rename(base / rel)  # the worktree directory itself keeps its identity
    d = w.st(capsys)
    assert (d["would_remove"], d["reason"]) == (False, "unlinked")


# -- R6, R9: the stash check ----------------------------------------------------------------------


def edit_stash_on_old_commit(w: RW) -> tuple[str, str]:
    """An unpushed stash whose first parent is an ancestor of (but not) HEAD."""
    c1 = w.push_work("a")
    c2 = w.push_work("b")
    w.g(["checkout", "-q", "--detach", c1], w.wt)
    (w.wt / "c1.txt").write_text("stashed edit")
    w.g(["stash", "push", "-q"], w.wt)
    w.g(["checkout", "-q", "work"], w.wt)
    return c1, c2


def test_r6_stash_probe_that_times_out_is_not_a_pass(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "c1.txt").write_text("stashed edit")
    w.g(["stash", "push", "-q"], w.wt)

    def runner(argv, **kw):  # noqa: ANN001, ANN202
        if "--verify" in argv and "refs/stash" in argv:
            return GitResult(-9, b"", b"", True)
        return run_git(argv, **kw)

    w.git_runner = runner
    r = w.status(capsys)
    assert r.code != 0 or r.json()["checks"]["stash"]["status"] != "pass"
    if r.code == 0:
        assert r.json()["would_remove"] is not True


def test_r9_a_planted_shallow_file_does_not_hide_a_stash(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    _c1, c2 = edit_stash_on_old_commit(w)
    assert w.st(capsys)["checks"]["stash"]["status"] == "fail"
    (w.clone / ".git" / "shallow").write_text(c2 + "\n")
    assert w.st(capsys)["checks"]["stash"]["status"] == "fail"


def test_r19c_a_stash_whose_ancestry_is_in_doubt_counts(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.commit("clone-only", w.clone)  # unrelated to the worktree's HEAD
    (w.clone / "c1.txt").write_text("stashed edit")
    w.g(["stash", "push", "-q"], w.clone)
    assert w.st(capsys)["checks"]["stash"]["status"] == "pass"

    def runner(argv, **kw):  # noqa: ANN001, ANN202
        if "merge-base" in argv:
            return GitResult(129, b"", b"", False)
        return run_git(argv, **kw)

    w.git_runner = runner
    assert w.st(capsys)["checks"]["stash"]["status"] == "fail"


# -- R17: failures inside status are reports or `internal`, never create_failed / exit 20 ---------


def test_r17_branch_listing_failure_is_internal_not_create_failed(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)

    def runner(argv, **kw):  # noqa: ANN001, ANN202
        if "for-each-ref" in argv:
            return GitResult(128, b"", b"", False)
        return run_git(argv, **kw)

    w.git_runner = runner
    r = w.status(capsys)
    assert r.code == 1, (r.out, r.err)
    assert '"internal"' in r.out + r.err


def test_r17_identity_change_mid_scan_is_a_failed_check_not_exit_20(
    w: RW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)

    def boom(self: Any) -> None:
        raise fsafe.FsafeError("identity", "directory replaced")

    monkeypatch.setattr(removal_content.Hasher, "__enter__", boom)
    d = w.st(capsys)  # exit 0 and a valid report
    assert d["checks"]["integrity"]["status"] == "fail"
    assert (d["would_remove"], d["reason"]) == (False, "unlinked")


# -- R2: files below a gitlink path ---------------------------------------------------------------


def add_gitlink(w: RW, rel: str = "vendor/sub") -> None:
    sha = w.g(["rev-parse", "HEAD"], w.wt)
    w.g(["update-index", "--add", "--cacheinfo", f"160000,{sha},{rel}"], w.wt)
    w.g(["commit", "-q", "-m", "gitlink"], w.wt)


def test_r2_empty_or_absent_gitlink_directory_is_clean(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    add_gitlink(w)
    d = w.st(capsys)
    for n in ("tracked", "untracked", "nested_repos"):
        assert d["checks"][n]["status"] == "pass", n
    (w.wt / "vendor" / "sub").mkdir(parents=True)
    d = w.st(capsys)
    for n in ("tracked", "untracked", "nested_repos"):
        assert d["checks"][n]["status"] == "pass", n


@pytest.mark.parametrize("rel", ["work.txt", "a/b/c/deep.txt"])
def test_r2_files_below_an_uninitialised_gitlink_are_not_clean(w: RW, capsys, rel: str) -> None:  # noqa: ANN001
    w.start(capsys)
    add_gitlink(w)
    f = w.wt / "vendor" / "sub" / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("unsaved work")
    d = w.st(capsys)
    assert d["checks"]["nested_repos"]["details"] == [{"detail": "submodule", "path": "vendor/sub"}]
    assert d["would_remove"] is False and d["reason"] == "dirty"


def test_s3_a_huge_gitlink_directory_is_not_listed_in_full(
    w: RW,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w.start(capsys)
    add_gitlink(w)
    sub = w.wt / "vendor" / "sub"
    sub.mkdir(parents=True)
    for i in range(10):
        (sub / f"f{i}").write_text("x")
    real_scandir = os.scandir

    class Limited:
        """A directory listing that fails if it is read beyond its second entry."""

        def __init__(self, it: Any) -> None:
            self.it, self.n = it, 0

        def __enter__(self) -> Limited:
            return self

        def __exit__(self, *a: object) -> None:
            self.it.close()

        def __iter__(self) -> Limited:
            return self

        def __next__(self) -> Any:
            self.n += 1
            if self.n > 2:
                raise AssertionError("listed more than the first entries")
            return next(self.it)

    real_listdir = os.listdir
    ino = sub.stat().st_ino

    inside = [False]  # only gitlink_state is bounded; the nested-repo walk lists the directory

    def is_gitlink(path: Any) -> bool:
        return inside[0] and isinstance(path, int) and os.fstat(path).st_ino == ino

    orig_state = removal_content.Hasher.gitlink_state

    def state(self: Any, rel: str) -> str:
        inside[0] = True
        try:
            return orig_state(self, rel)
        finally:
            inside[0] = False

    monkeypatch.setattr(removal_content.Hasher, "gitlink_state", state)

    def scandir(path: Any = ".") -> Any:
        it = real_scandir(path)
        return Limited(it) if is_gitlink(path) else it

    def listdir(path: Any = ".") -> Any:
        assert not is_gitlink(path), "listed the whole directory"
        return real_listdir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(os, "listdir", listdir)
    d = w.st(capsys)
    assert d["checks"]["nested_repos"]["details"] == [{"detail": "submodule", "path": "vendor/sub"}]


@pytest.mark.parametrize("how", ["file", "symlink"])
def test_r2_a_file_or_symlink_in_place_of_a_gitlink_is_tracked_dirty(
    w: RW,
    capsys,
    how: str,  # noqa: ANN001
) -> None:
    w.start(capsys)
    add_gitlink(w)
    p = w.wt / "vendor" / "sub"
    p.parent.mkdir(parents=True, exist_ok=True)
    if how == "file":
        p.write_text("my notes")
    else:
        os.symlink("/etc", p)
    d = w.st(capsys)
    assert {"path": "vendor/sub", "detail": "changed"} in d["checks"]["tracked"]["details"]


# -- R3: submodule git dirs under the admin entry or the common dir -------------------------------


def test_r3_submodule_git_dir_with_unpushed_commits_survives_a_deinit(
    w: RW,
    capsys,
    tmp_path: Path,  # noqa: ANN001
) -> None:
    w.start(capsys)
    sub = tmp_path / "subsrc"
    sub.mkdir()
    w.g(["init", "-q", "-b", "main"], sub)
    (sub / "f").write_text("f")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "s"], sub)
    w.g(
        ["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub"], w.wt
    )
    w.g(["commit", "-q", "-m", "add submodule"], w.wt)
    w.g(["commit", "--allow-empty", "-q", "-m", "unpushed"], w.wt / "vendor" / "sub")
    w.g(["submodule", "deinit", "-f", "vendor/sub"], w.wt)
    assert not (w.wt / "vendor" / "sub" / ".git").exists()
    d = w.st(capsys)
    details = d["checks"]["nested_repos"]["details"]
    assert any(x["detail"] == "submodule" and "/modules/" in x["path"] for x in details), details
    assert d["checks"]["nested_repos"]["status"] == "fail"


# -- R7: bracket classes and trailing spaces of disposable patterns -------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("a/b[!x]c", "a/b/c", False),  # a class never matches "/" (git, WM_PATHNAME)
        ("a/b[!x]c", "a/byc", True),
        ("a/b[^x]c", "a/b/c", False),
        ("x[a-z/]y", "x/y", False),
        ("x[a-z/]y", "xmy", True),
        ("x[+-0]y", "x/y", False),  # the range "+" to "0" spans "/"
        ("x[+-0]y", "x.y", True),
        ("x[a\\-c]y", "xby", False),  # "\-" is a literal "-", not a range
        ("x[a\\-c]y", "x-y", True),
        ("x[a\\-c]y", "xcy", True),
        ("x[a\\]b]y", "x]y", True),
        ("foo ", "foo", True),  # an unescaped trailing space is dropped
        ("foo ", "foo ", False),
        ("foo\\ ", "foo ", True),  # a quoted one stays
        ("foo\\ ", "foo", False),
        ("foo/ ", "foo", False),  # trimmed to "foo/": directories only
        ("foo\\", "foo\\", False),  # a trailing backslash matches nothing
    ],
)
def test_r7_pattern_matches_what_git_matches(pattern: str, path: str, expected: bool) -> None:
    assert Disposable([pattern]).matches(path, is_dir=False) is expected


# -- R10, R19e, R19f: the reflog ------------------------------------------------------------------

A, B, Z = "a" * 40, "b" * 40, "0" * 40


def test_r10_entry_in_the_start_second_is_attributed() -> None:
    # the recorded start has milliseconds; reflog timestamps are whole seconds
    got = removal_content.reflog_tips([(Z, A, 999), (A, B, 1000)], 1000.7, None)
    assert got == {B}


def test_r10_time_anchor_needs_an_entry_before_the_start_second() -> None:
    assert removal_content.reflog_tips([(Z, A, 1000)], 1000.7, None) is None
    assert removal_content.reflog_tips([(Z, A, 1000)], 1000.0, None) is None
    assert removal_content.reflog_tips([(Z, A, 999)], 1000.0, None) is None


def test_r10_an_empty_set_with_only_a_time_anchor_is_unavailable() -> None:
    assert removal_content.reflog_tips([(Z, A, 5)], 1000.7, "c" * 40) is None
    # ... but with the start entry present, nothing happening since is fine
    assert removal_content.reflog_tips([(Z, A, 5)], 1000.7, A) == set()


def test_r19e_reflog_size_cap_is_exact(
    w: RW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    size = (w.admin / "logs" / "HEAD").stat().st_size
    monkeypatch.setattr(removal_content, "REFLOG_MAX_BYTES", size)
    assert w.st(capsys)["checks"]["unpushed_branches"]["status"] == "pass"
    monkeypatch.setattr(removal_content, "REFLOG_MAX_BYTES", size - 1)
    d = w.st(capsys)
    assert d["checks"]["unpushed_branches"]["details"][0]["detail"] == "reflog_unavailable"


# -- R12: info/attributes -------------------------------------------------------------------------


def commit_ident_file(w: RW) -> Path:
    f = w.wt / "id.txt"
    f.write_text("$Id$\n")
    w.g(["add", "."], w.wt)
    w.g(["commit", "-q", "-m", "ident"], w.wt)
    return f


def test_r12_info_attributes_that_hides_an_edit_fails_tracked(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    f = commit_ident_file(w)
    info = w.clone / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* ident\n")
    f.write_text("$Id: forged$\n")  # git expands and collapses $Id$, so it sees no change
    d = w.st(capsys)
    assert d["checks"]["tracked"]["status"] == "fail"
    assert d["checks"]["tracked"]["details"] == [
        {"path": "info/attributes", "detail": "attributes"}
    ]


def test_r12_empty_info_attributes_is_fine(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    info = w.clone / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("")
    assert w.st(capsys)["checks"]["tracked"]["status"] == "pass"


# -- R13: overrides and config that must be inert -------------------------------------------------


def test_r13_repo_config_attributes_file_is_not_read(w: RW, capsys, tmp_path: Path) -> None:  # noqa: ANN001
    w.start(capsys)
    f = commit_ident_file(w)
    attrs = tmp_path / "attrs"
    attrs.write_text("* ident\n")
    w.g(["config", "core.attributesFile", str(attrs)], w.clone)
    f.write_text("$Id: forged$\n")
    d = w.st(capsys)
    assert [x["path"] for x in d["checks"]["tracked"]["details"]] == ["id.txt"]


def test_r13_repo_config_hooks_path_runs_no_hook(w: RW, capsys, tmp_path: Path) -> None:  # noqa: ANN001
    marker = tmp_path / "hook-ran"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("post-index-change", "reference-transaction", "post-checkout"):
        h = hooks / name
        h.write_text(f"#!/bin/sh\ntouch {marker}\n")
        h.chmod(0o755)
    w.start(capsys)
    w.g(["config", "core.hooksPath", str(hooks)], w.clone)
    w.st(capsys)
    assert not marker.exists()


def test_r13_hostile_stat_config_does_not_hide_an_edit(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    for key, value in (
        ("core.ignoreStat", "true"),
        ("core.checkStat", "minimal"),
        ("core.trustctime", "false"),
        ("core.untrackedCache", "true"),
    ):
        w.g(["config", key, value], w.clone)
    (w.wt / "c1.txt").write_text("edited")
    (w.wt / "new.txt").write_text("new")
    d = w.st(capsys)
    assert [x["path"] for x in d["checks"]["tracked"]["details"]] == ["c1.txt"]
    assert [x["path"] for x in d["checks"]["untracked"]["details"]] == ["new.txt"]


# -- R14: split index -----------------------------------------------------------------------------


def test_r14_split_index_writes_nothing_into_the_real_git_dirs(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["config", "core.splitIndex", "true"], w.clone)

    def shared() -> set[str]:
        out: set[str] = set()
        for d in (w.admin, w.clone / ".git"):
            out |= {f"{d}/{n}" for n in os.listdir(d) if n.startswith("sharedindex.")}
        return out

    before = shared()
    w.st(capsys)
    assert shared() == before


# -- R19a, b, d: tests the first review found missing ---------------------------------------------


def test_r19b_an_unreadable_directory_makes_nested_repos_incomplete(w: RW, capsys) -> None:  # noqa: ANN001
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    w.start(capsys)
    locked = w.wt / "locked-dir"
    locked.mkdir()
    (locked / "x").write_text("x")
    locked.chmod(0)
    try:
        d = w.st(capsys)
    finally:
        locked.chmod(stat.S_IRWXU)
    assert {"detail": "incomplete", "path": None} in d["checks"]["nested_repos"]["details"]
    assert d["checks"]["nested_repos"]["status"] == "fail"


def test_r19d_the_fetch_waits_for_the_lease_at_most_5_seconds(
    w: RW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    w.start(capsys)
    w.top["lease_wait"] = "30m"
    seen: list[Any] = []
    orig = State.lease

    def spy(self: State, name: str, **kw: Any) -> Any:
        seen.append((name, kw.get("wait")))
        return orig(self, name, **kw)

    monkeypatch.setattr(State, "lease", spy)
    w.st(capsys)
    assert [wait for name, wait in seen if name == clone_lease(str(w.clone))] == [5]


def test_s1_status_still_passes_when_a_process_of_mine_cannot_be_inspected(w: RW, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import ProcessInfo

    w.start(capsys)
    w.scanner.procs = [ProcessInfo(77, "hidden", None, inspected=False)]
    d = w.st(capsys)
    assert d["checks"]["in_use"]["status"] == "pass"
    assert any("could not be inspected" in x for x in d["warnings"]), d
    assert d["would_remove"] is True


def test_r3_a_huge_modules_directory_is_incomplete_not_a_pass(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.top["remove"] = {"nested_scan_limit": 50}
    for i in range(60):
        (w.admin / "modules" / f"m{i}").mkdir(parents=True)
    d = w.st(capsys)
    assert {"detail": "incomplete", "path": None} in d["checks"]["nested_repos"]["details"]
    assert d["checks"]["nested_repos"]["status"] == "fail"
