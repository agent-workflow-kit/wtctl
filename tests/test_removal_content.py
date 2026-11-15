"""The content checks of a removal (docs/interface.md, Content checks), against real git."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.removallib import RW, inproc  # noqa: F401
from tests.vectorlib import plain_git

PY = sys.executable


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RW:  # noqa: ANN001
    return RW(tmp_path, private_home)


def paths(d: dict[str, Any], name: str) -> list[str]:
    return [x["path"] for x in d["checks"][name]["details"]]


def failing(d: dict[str, Any]) -> set[str]:
    return {k for k, v in d["checks"].items() if v["status"] == "fail"}


def tracked_file(w: RW, rel: str = "README.md") -> Path:
    return w.wt / rel


# -- a clean worktree ------------------------------------------------------------------------


def test_clean_worktree_passes_every_check(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    d = w.st(capsys)
    assert d["would_remove"] is True and d["reason"] is None
    assert failing(d) == set() and d["force_overrides"] == []
    assert d["head"] == w.g(["rev-parse", "HEAD"], w.wt) and d["current_branch"] == "work"


# -- tracked ---------------------------------------------------------------------------------


def test_unstaged_edit_is_tracked_dirty(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "c1.txt").write_text("edited")
    d = w.st(capsys)
    assert d["checks"]["tracked"] == {
        "status": "fail",
        "details": [{"path": "c1.txt", "detail": "changed"}],
    }
    assert (d["would_remove"], d["reason"]) == (False, "dirty")
    assert "tracked" in d["force_overrides"]


def test_staged_and_deleted_and_renamed_are_tracked_dirty(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "new.txt").write_text("n")
    w.g(["add", "new.txt"], w.wt)
    (w.wt / "c2.txt").unlink()
    w.g(["mv", "c3.txt", "moved.txt"], w.wt)
    d = w.st(capsys)
    assert set(paths(d, "tracked")) >= {"new.txt", "c2.txt", "moved.txt"}


def test_conflicted_index_is_tracked_dirty(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["checkout", "-q", "-b", "other", "main"], w.clone)
    w.g(["checkout", "-q", "main"], w.clone)
    (w.wt / "c1.txt").write_text("a")
    w.g(["commit", "-q", "-am", "a"], w.wt)
    w.g(["checkout", "-q", "-b", "side", "HEAD~1"], w.wt)
    (w.wt / "c1.txt").write_text("b")
    w.g(["commit", "-q", "-am", "b"], w.wt)
    r = plain_git(["merge", "work"], cwd=w.wt, home=w.home)
    assert r.returncode != 0
    d = w.st(capsys)
    assert "c1.txt" in paths(d, "tracked")


def test_forged_index_stat_data_does_not_hide_an_edit(w: RW, capsys) -> None:  # noqa: ANN001
    """SAFE2-3: same size, restored mtime, and a config that stops git comparing ctime."""
    w.start(capsys)
    f = tracked_file(w, "c1.txt")
    old = f.stat().st_mtime_ns - 60 * 10**9  # not racily clean once the index is written
    os.utime(f, ns=(old, old))
    w.g(["config", "core.trustctime", "false"], w.wt)
    w.g(["update-index", "--refresh"], w.wt)
    w.g(["config", "core.checkstat", "minimal"], w.wt)
    f.write_text("c9")  # "c1" -> "c9": same size
    os.utime(f, ns=(old, old))
    assert w.g(["status", "--porcelain"], w.wt) == ""  # plain git is fooled: the precondition
    d = w.st(capsys)
    assert "c1.txt" in paths(d, "tracked")


def test_assume_unchanged_is_hidden_changes(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["update-index", "--assume-unchanged", "c1.txt"], w.wt)
    (w.wt / "c1.txt").write_text("edited")
    assert w.g(["status", "--porcelain"], w.wt) == ""
    d = w.st(capsys)
    details = d["checks"]["tracked"]["details"]
    assert {"path": "c1.txt", "detail": "hidden_changes"} in details
    assert {"path": "c1.txt", "detail": "changed"} in details  # the private index sees the edit
    assert d["reason"] == "dirty"


def test_hidden_flag_fails_even_when_the_file_is_unedited(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["update-index", "--skip-worktree", "c2.txt"], w.wt)
    d = w.st(capsys)
    assert d["checks"]["tracked"]["details"] == [{"path": "c2.txt", "detail": "hidden_changes"}]


def test_sparse_checkout_is_always_dirty(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.g(["sparse-checkout", "set", "--no-cone", "c1.txt"], w.wt)
    d = w.st(capsys)
    assert d["checks"]["tracked"]["status"] == "fail"
    assert {x["detail"] for x in d["checks"]["tracked"]["details"]} >= {"hidden_changes"}


def test_planted_filter_driver_does_not_run(w: RW, capsys, tmp_path: Path) -> None:  # noqa: ANN001
    marker = tmp_path / "ran"
    w.start(capsys)
    w.g(["config", "filter.x.clean", f"touch {marker}; cat"], w.wt)
    (w.wt / ".gitattributes").write_text("*.txt filter=x\n")
    w.g(["config", "core.fsmonitor", f"touch {marker}"], w.wt)
    w.g(["config", "diff.external", f"touch {marker}"], w.wt)
    w.st(capsys)
    assert not marker.exists()


# -- untracked -------------------------------------------------------------------------------


def test_untracked_file_is_dirty(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "notes.txt").write_text("keep me")
    d = w.st(capsys)
    assert d["checks"]["untracked"]["details"] == [{"path": "notes.txt"}]
    assert (d["would_remove"], d["reason"]) == (False, "dirty")


@pytest.mark.parametrize(
    "name",
    ["new\nline.txt", "ünï-☃.txt", "with space.txt", "-dash.txt", "tab\there", "\x1b[31mred", ".GIT-x"],
)
def test_hostile_untracked_names_are_reported(w: RW, capsys, name: str) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / name).write_text("x")
    d = w.st(capsys)
    assert name in paths(d, "untracked")


def test_undecodable_name_is_reported_escaped(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    open(os.path.join(os.fsencode(w.wt), b"bad\xffname"), "wb").write(b"x")
    d = w.st(capsys)
    assert d["checks"]["untracked"]["status"] == "fail"
    assert any("bad" in p for p in paths(d, "untracked"))


def test_untracked_symlink_is_reported_and_not_followed(w: RW, capsys, tmp_path: Path) -> None:  # noqa: ANN001
    w.start(capsys)
    os.symlink("/etc", w.wt / "link")
    d = w.st(capsys)
    assert paths(d, "untracked") == ["link"]


def test_untracked_fifo_does_not_hang_or_pass_as_data(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    os.mkfifo(w.wt / "pipe")
    d = w.st(capsys)  # must return; a FIFO holds no data
    assert d["would_remove"] in (True, False)


def test_untracked_in_a_new_directory(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "deep" / "er").mkdir(parents=True)
    (w.wt / "deep" / "er" / "f.txt").write_text("x")
    d = w.st(capsys)
    assert paths(d, "untracked") == ["deep/er/f.txt"]


def test_forged_untracked_cache_does_not_hide_a_new_file(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "sub").mkdir()
    (w.wt / "sub" / "a.txt").write_text("a")
    w.g(["config", "core.untrackedCache", "true"], w.wt)
    w.g(["update-index", "--untracked-cache"], w.wt)
    w.g(["status", "-uall"], w.wt)  # fills the cache
    w.g(["status", "-uall"], w.wt)
    sub = w.wt / "sub"
    before = sub.stat()
    (sub / "hidden.txt").write_text("secret")
    os.utime(sub, ns=(before.st_atime_ns, before.st_mtime_ns))
    d = w.st(capsys)
    assert "sub/hidden.txt" in paths(d, "untracked")


# -- bootstrap-created exemptions -----------------------------------------------------------



def test_bootstrap_created_untracked_file_is_exempt_while_unchanged(w: RW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg = {}
    w.build()
    w.repo_cfg["bootstrap"] = {"steps": [{"argv": [PY, "-c", "open('gen.txt','w').write('g')"]}]}
    w.start_built(capsys)
    d = w.st(capsys)
    assert d["checks"]["untracked"]["status"] == "pass", d
    assert d["would_remove"] is True


def test_bootstrap_created_file_edited_afterwards_is_dirty(w: RW, capsys) -> None:  # noqa: ANN001
    w.build()
    w.repo_cfg["bootstrap"] = {"steps": [{"argv": [PY, "-c", "open('gen.txt','w').write('g')"]}]}
    w.start_built(capsys)
    (w.wt / "gen.txt").write_text("agent work")
    d = w.st(capsys)
    assert paths(d, "untracked") == ["gen.txt"]


def test_exemption_does_not_extend_to_another_path_with_the_same_content(w: RW, capsys) -> None:  # noqa: ANN001
    w.build()
    w.repo_cfg["bootstrap"] = {"steps": [{"argv": [PY, "-c", "open('gen.txt','w').write('g')"]}]}
    w.start_built(capsys)
    (w.wt / "copy.txt").write_text("g")
    d = w.st(capsys)
    assert paths(d, "untracked") == ["copy.txt"]


def test_exempt_file_replaced_by_symlink_is_dirty(w: RW, capsys) -> None:  # noqa: ANN001
    w.build()
    w.repo_cfg["bootstrap"] = {"steps": [{"argv": [PY, "-c", "open('gen.txt','w').write('g')"]}]}
    w.start_built(capsys)
    (w.wt / "gen.txt").unlink()
    os.symlink("README.md", w.wt / "gen.txt")
    d = w.st(capsys)
    assert paths(d, "untracked") == ["gen.txt"]


# -- ignored ---------------------------------------------------------------------------------


def test_ignored_file_that_is_not_disposable_is_dirty(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / ".gitignore").write_text("*.secret\n")
    w.g(["add", ".gitignore"], w.wt)
    w.g(["commit", "-q", "-m", "ignore"], w.wt)
    (w.wt / "keys.secret").write_text("k")
    d = w.st(capsys)
    assert "keys.secret" in paths(d, "ignored")


def base_ignore(w: RW, text: str) -> None:
    """A tracked .gitignore in the remote's main, before the worktree is created."""
    (w.clone / ".gitignore").write_text(text)
    w.g(["add", ".gitignore"])
    w.g(["commit", "-q", "-m", "ign"])
    w.g(["push", "-q", "origin", "main"])


def commit_gitignore(w: RW, text: str) -> None:
    (w.wt / ".gitignore").write_text(text)
    w.g(["add", ".gitignore"], w.wt)
    w.g(["commit", "-q", "-m", "ignore"], w.wt)


@pytest.mark.parametrize(
    ("rel", "disposable"),
    [
        ("node_modules/pkg/index.js", True),
        ("pkg/node_modules/x.js", True),  # no slash in the pattern: any depth
        ("src/__pycache__/m.pyc", True),
        ("deep/a/b/c.pyc", True),
        ("build/out.o", True),
        ("build2/out.o", False),  # prefix games
        ("xbuild/out.o", False),
        ("builds/out.o", False),
        ("notbuild", False),
    ],
)
def test_disposable_globs(w: RW, capsys, rel: str, disposable: bool) -> None:  # noqa: ANN001
    w.start(capsys)
    commit_gitignore(w, "node_modules/\n*.pyc\n__pycache__/\nbuild/\nbuild2/\nxbuild/\nbuilds/\nnotbuild\n")
    f = w.wt / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x")
    d = w.st(capsys)
    assert (d["checks"]["ignored"]["status"] == "pass") is disposable, d["checks"]["ignored"]


def test_a_file_named_like_a_directory_only_pattern_is_not_disposable(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    commit_gitignore(w, "build\n")
    (w.wt / "build").write_text("this is a file, not a build dir")
    d = w.st(capsys)
    assert paths(d, "ignored") == ["build"]


def test_per_repo_disposable_replaces_the_global_list(w: RW, capsys) -> None:  # noqa: ANN001
    w.build()
    w.repo_cfg["remove"] = {"disposable": ["only-this/"]}
    w.start_built(capsys)
    commit_gitignore(w, "node_modules/\nonly-this/\n")
    (w.wt / "node_modules").mkdir()
    (w.wt / "node_modules" / "a").write_text("x")
    (w.wt / "only-this").mkdir()
    (w.wt / "only-this" / "a").write_text("x")
    d = w.st(capsys)
    assert paths(d, "ignored") == ["node_modules/"]


def test_bootstrap_created_ignored_dir_is_exempt_on_a_component_boundary(w: RW, capsys) -> None:  # noqa: ANN001
    w.build()
    base_ignore(w, "cache/\ncache2/\n")
    w.repo_cfg["remove"] = {"disposable": ["nothing-matches-this/"]}
    w.repo_cfg["bootstrap"] = {
        "steps": [{"argv": [PY, "-c", "import os; os.makedirs('cache/x'); open('cache/x/f','w').write('1')"]}]
    }
    w.start_built(capsys)
    (w.wt / "cache2").mkdir()
    (w.wt / "cache2" / "f").write_text("agent")
    d = w.st(capsys)
    assert paths(d, "ignored") == ["cache2/"]


def test_copied_env_file_is_exempt_until_its_content_changes(w: RW, capsys) -> None:  # noqa: ANN001
    w.build()
    base_ignore(w, ".env\n")
    (w.clone / ".env").write_text("SECRET=1\n")
    w.repo_cfg["copy_env"] = [".env"]
    w.repo_cfg["remove"] = {"disposable": ["zzz/"]}
    w.start_built(capsys, "--copy-env")
    assert (w.wt / ".env").read_text() == "SECRET=1\n"
    assert w.st(capsys)["checks"]["ignored"]["status"] == "pass"
    (w.wt / ".env").write_text("SECRET=changed\n")
    assert paths(w.st(capsys), "ignored") == [".env"]


# -- nested repositories ----------------------------------------------------------------------


def test_nested_repository_is_dirty(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "vendor" / "lib").mkdir(parents=True)
    w.g(["init", "-q"], w.wt / "vendor" / "lib")
    d = w.st(capsys)
    assert d["checks"]["nested_repos"]["details"] == [
        {"detail": "nested_repo", "path": "vendor/lib/.git"}
    ]
    assert d["reason"] == "dirty"


def test_nested_repository_inside_an_ignored_directory_is_found(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    commit_gitignore(w, "node_modules/\n")
    (w.wt / "node_modules" / "p").mkdir(parents=True)
    w.g(["init", "-q"], w.wt / "node_modules" / "p")
    d = w.st(capsys)
    assert [x["path"] for x in d["checks"]["nested_repos"]["details"]] == [
        "node_modules/p/.git"
    ]
    assert d["checks"]["ignored"]["status"] == "pass"  # node_modules/ is disposable


def test_dotgit_aliases_count_as_nested(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "a" / ".GIT").mkdir(parents=True)
    (w.wt / "b" / "git~1").mkdir(parents=True)
    d = w.st(capsys)
    got = {x["path"] for x in d["checks"]["nested_repos"]["details"]}
    assert got == {"a/.GIT", "b/git~1"}


def test_nested_repo_recorded_by_bootstrap_is_exempt(w: RW, capsys) -> None:  # noqa: ANN001
    w.build()
    code = (
        "import os,subprocess; os.makedirs('node_modules/p');"
        "subprocess.run(['git','init','-q','node_modules/p'],check=True)"
    )
    w.repo_cfg["bootstrap"] = {"steps": [{"argv": [PY, "-c", code]}]}
    w.start_built(capsys)
    rows = w.rows("SELECT path FROM bootstrap_nested WHERE record_id = ?", w.rid)
    assert [r["path"] for r in rows] == ["node_modules/p/.git"]
    assert w.st(capsys)["checks"]["nested_repos"]["status"] == "pass"
    # a second nested repository next to it is not exempt
    w.g(["init", "-q"], w.wt / "node_modules")
    d = w.st(capsys)
    assert [x["path"] for x in d["checks"]["nested_repos"]["details"]] == ["node_modules/.git"]


def test_initialized_submodule_is_reported_as_submodule(w: RW, capsys, tmp_path: Path) -> None:  # noqa: ANN001
    w.start(capsys)
    sub = tmp_path / "subsrc"
    sub.mkdir()
    w.g(["init", "-q", "-b", "main"], sub)
    (sub / "f").write_text("f")
    w.g(["add", "."], sub)
    w.g(["commit", "-q", "-m", "s"], sub)
    w.g(["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub"], w.wt)
    w.g(["commit", "-q", "-m", "add submodule"], w.wt)
    d = w.st(capsys)
    assert {"detail": "submodule", "path": "vendor/sub/.git"} in d["checks"]["nested_repos"]["details"]


def test_a_dotgit_file_pointing_outside_modules_is_a_nested_repo_not_a_submodule(  # noqa: ANN201
    w: RW, capsys, tmp_path: Path
) -> None:
    w.start(capsys)
    (w.wt / "x").mkdir()
    (w.wt / "x" / ".git").write_text(f"gitdir: {tmp_path}/elsewhere\n")
    d = w.st(capsys)
    assert d["checks"]["nested_repos"]["details"] == [{"detail": "nested_repo", "path": "x/.git"}]


def test_walk_beyond_the_limit_is_incomplete(w: RW, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.top["remove"] = {"nested_scan_limit": 5}
    for i in range(10):
        (w.wt / f"d{i}").mkdir()
    d = w.st(capsys)
    assert d["checks"]["nested_repos"]["details"] == [{"detail": "incomplete", "path": None}]
    # the baseline walk shares the limit and is incomplete too: it wins by precedence
    assert d["reason"] == "repo_config_changed" and "nested_repos" in d["force_overrides"]


def test_mount_point_below_the_root_fails_nested_repos(w: RW, capsys, monkeypatch) -> None:  # noqa: ANN001
    from wtctl import fsafe_child, removalfs

    w.start(capsys)
    real = removalfs.RUNNER

    def fake(op: str, args: dict, **kw: Any):  # noqa: ANN202
        res = real(op, args, **kw)
        if op == "nested_scan":
            res.value["mounts"].append(["node_modules", "fuse"])
        return res

    monkeypatch.setattr(removalfs, "RUNNER", fake)
    d = w.st(capsys)
    assert d["checks"]["nested_repos"]["details"] == [
        {"detail": "mount_point", "path": "node_modules/fuse"}
    ]
    assert fsafe_child  # noqa: S101


def test_walk_deadline_is_incomplete(w: RW, capsys, monkeypatch) -> None:  # noqa: ANN001
    from wtctl import fsafe, fsafe_child, removalfs

    w.start(capsys)

    def timed_out(op: str, args: dict, **kw: Any):  # noqa: ANN202
        if op == "nested_scan":
            return fsafe_child.OpResult(
                False, error=fsafe.FsafeError("deadline", "killed"), timed_out=True
            )
        return fsafe_child.OpResult(True, value=fsafe_child.OPS[op](args))

    monkeypatch.setattr(removalfs, "RUNNER", timed_out)
    d = w.st(capsys)
    assert d["checks"]["nested_repos"]["details"] == [{"detail": "incomplete", "path": None}]
