"""The ``dir_ids`` and ``nested_scan`` child operations, the disposable matcher and the private
index plumbing in ``hardgit``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wtctl import fsafe, fsafe_child
from wtctl.hardgit import CALLER_OVERRIDE_KEYS, Git, build_env
from wtctl.ignorematch import Disposable


def args(root: Path, **extra: object) -> dict[str, object]:
    st = os.stat(root)
    return {"anchor": {"path": str(root), "identity": [st.st_dev, st.st_ino]}, "chain": [], **extra}


def real(op: str, a: dict[str, object]) -> fsafe_child.OpResult:
    """In the child process under the deadline, as ``status`` runs it."""
    return fsafe_child.run_op(op, a, timeout=30)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    t = tmp_path / "t"
    (t / "a" / "b").mkdir(parents=True)
    (t / "c").mkdir()
    (t / "a" / "f").write_text("f")
    return t


# -- dir_ids -----------------------------------------------------------------------------------


def test_dir_ids_lists_the_root_and_every_directory(tree: Path) -> None:
    r = real("dir_ids", args(tree))
    assert r.ok and r.value is not None
    ids = {(r.value["ids"][i], r.value["ids"][i + 1]) for i in range(0, len(r.value["ids"]), 2)}
    expect = {
        (os.stat(p).st_dev, os.stat(p).st_ino)
        for p in (tree, tree / "a", tree / "a" / "b", tree / "c")
    }
    assert ids == expect and r.value["complete"] is True


def test_dir_ids_does_not_follow_a_symlinked_directory(tree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, tree / "link")
    r = real("dir_ids", args(tree))
    st = os.stat(outside)
    assert r.value is not None and st.st_ino not in r.value["ids"]


def test_dir_ids_stops_at_the_limit_and_says_so(tree: Path) -> None:
    r = real("dir_ids", args(tree, limit=2))
    assert r.ok and r.value is not None and r.value["complete"] is False
    assert len(r.value["ids"]) >= 2  # what it listed still counts


def test_dir_ids_refuses_a_root_with_another_identity(tree: Path) -> None:
    a = args(tree)
    a["root_identity"] = [1, 1]
    r = real("dir_ids", a)
    assert not r.ok and r.error is not None


# -- nested_scan -------------------------------------------------------------------------------


def test_nested_scan_finds_dotgit_files_dirs_and_aliases_without_descending(tree: Path) -> None:
    (tree / ".git").write_text("gitdir: /x\n")  # the root's own
    (tree / "a" / "b" / ".git").mkdir()
    (tree / "a" / "b" / ".git" / "inner").mkdir()
    (tree / "c" / ".git").write_text("gitdir: /elsewhere/modules/x\n")
    (tree / "c" / ".GIT").mkdir()
    r = real("nested_scan", args(tree))
    assert r.ok and r.value is not None
    got = {tuple(g["path"]): (g["kind"], g["content"]) for g in r.value["gits"]}
    assert got[("a", "b", ".git")] == ("dir", None)
    assert got[("c", ".git")] == ("file", "gitdir: /elsewhere/modules/x\n")
    assert got[("c", ".GIT")][0] == "dir"
    assert not any(len(p) > 3 for p in got)  # nothing below a found .git
    assert r.value["mounts"] == [] and r.value["unreadable"] == []


def test_nested_scan_symlinked_dotgit_is_reported_not_followed(tree: Path, tmp_path: Path) -> None:
    os.symlink(tmp_path, tree / "c" / ".git")
    r = real("nested_scan", args(tree))
    assert r.value is not None
    assert [(g["path"], g["kind"], g["content"]) for g in r.value["gits"]] == [
        (["c", ".git"], "symlink", None)
    ]


def test_nested_scan_limit_is_an_error_not_a_pass(tree: Path) -> None:
    r = real("nested_scan", args(tree, limit=2))
    assert not r.ok and r.error is not None and r.error.kind == "limit"


def test_nested_scan_unreadable_directory_is_reported(tree: Path) -> None:
    (tree / "c").chmod(0)
    try:
        r = real("nested_scan", args(tree))
    finally:
        (tree / "c").chmod(0o755)
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    assert r.value is not None and r.value["unreadable"] == [["c"]]


def test_nested_scan_pointer_content_is_bounded(tree: Path) -> None:
    (tree / "c" / ".git").write_text("gitdir: " + "x" * 100_000)
    r = real("nested_scan", args(tree))
    assert r.value is not None and len(r.value["gits"][0]["content"]) == fsafe_child.NESTED_READ


# -- Entry identity -----------------------------------------------------------------------------


def test_walk_entries_carry_the_directory_identity(tree: Path) -> None:
    a = fsafe.open_anchor(str(tree))
    try:
        got = {
            e.path: (e.dev, e.ino)
            for e in fsafe.walk_tree(a, a.fd, root_path=str(tree))
            if e.kind == "dir"
        }
    finally:
        a.close()
    st = os.stat(tree / "a")
    assert got[("a",)] == (st.st_dev, st.st_ino)


# -- the disposable matcher ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("patterns", "rel", "is_dir", "expected"),
    [
        (["build/"], "build", True, True),
        (["build/"], "build", False, False),  # directories only
        (["build/"], "build/x/y", False, True),  # below an excluded directory
        (["build/"], "a/build", True, True),  # no slash inside: any depth
        (["build/"], "build2", True, False),
        (["build/"], "xbuild", True, False),
        (["/build/"], "a/build", True, False),  # a leading slash anchors at the root
        (["/build/"], "build", True, True),
        (["a/b"], "a/b", False, True),  # a slash inside anchors
        (["a/b"], "x/a/b", False, False),
        (["*.pyc"], "x/y/z.pyc", False, True),
        (["*.pyc"], "x/y/z.pyc.keep", False, False),
        (["*.pyc"], "dir.pyc/inner", False, True),
        (["a?c"], "abc", False, True),
        (["a?c"], "a/c", False, False),  # ? does not cross a slash
        (["[ab]x"], "bx", False, True),
        (["[!ab]x"], "bx", False, False),
        (["**/cache"], "p/q/cache", False, True),
        (["cache/**"], "cache/p/q", False, True),
        (["a/**/z"], "a/z", False, True),
        (["a/**/z"], "a/b/c/z", False, True),
        (["!keep"], "keep", False, False),  # negation is not supported: matches nothing
        (["#c"], "#c", False, False),
        ([""], "x", False, False),
        (["/"], "x", False, False),
        ([r"\*star"], "*star", False, True),
        ([r"\*star"], "xstar", False, False),
        (["a[b"], "a[b", False, True),  # an unterminated class is literal
        (["(x)+"], "(x)+", False, True),  # regex metacharacters are literal
        (["(x)+"], "xx", False, False),
        (["name"], "new\nline/name", False, True),
    ],
)
def test_disposable_matching(patterns: list[str], rel: str, is_dir: bool, expected: bool) -> None:
    assert Disposable(patterns).matches(rel, is_dir=is_dir) is expected


# -- hardgit ---------------------------------------------------------------------------------------


def test_index_file_goes_into_the_environment_and_nothing_else_of_the_callers_does() -> None:
    env = build_env(git_bin="/usr/bin/git", home="/h", index_file="/p/index")
    assert env["GIT_INDEX_FILE"] == "/p/index"
    assert "GIT_INDEX_FILE" not in build_env(git_bin="/usr/bin/git", home="/h")


def test_status_stat_overrides_are_allowed_and_others_are_not(tmp_path: Path) -> None:
    from tests.vectorlib import GIT, plain_git
    from wtctl.gateway import Gateway

    repo = tmp_path / "r"
    repo.mkdir()
    plain_git(["init", "-q"], cwd=repo, home=tmp_path)
    git = Git(Gateway(), GIT, str(tmp_path))
    gd, idx = str(repo / ".git"), str(tmp_path / "idx")
    cmd = git.build(
        ["status"], git_dir=gd, work_tree=str(repo), index_file=idx,
        overrides=[("core.untrackedCache", "false"), ("core.ignoreStat", "false")],
    )  # fmt: skip
    assert "core.untrackedCache=false" in " ".join(cmd.argv) and cmd.env["GIT_INDEX_FILE"] == idx
    assert {"core.untrackedcache", "core.checkstat", "core.trustctime", "core.ignorestat"} <= (
        CALLER_OVERRIDE_KEYS
    )
    for bad in (("core.fsmonitor", "x"), ("core.hooksPath", "/tmp")):
        with pytest.raises(ValueError):
            git.build(["status"], git_dir=gd, work_tree=str(repo), overrides=[bad])
    with pytest.raises(ValueError):
        git.build(["status"], git_dir=gd, work_tree=str(repo), index_file="relative/index")


# -- real mounts ---------------------------------------------------------------------------------


def test_real_mount_point_below_the_root_is_reported_and_never_crossed(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess
    import sys

    unshare = shutil.which("unshare")
    probe = tmp_path / "probe"
    probe.mkdir()
    if (
        unshare is None
        or subprocess.run(
            [unshare, "-Urm", "mount", "-t", "tmpfs", "none", str(probe)],
            capture_output=True,
            check=False,
        ).returncode
        != 0
    ):
        pytest.skip("unprivileged user and mount namespaces are unavailable here")
    work = tmp_path / "ns"
    work.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    scenario = Path(__file__).with_name("removal_scenario.py")
    r = subprocess.run(
        [unshare, "-Urm", sys.executable, str(scenario), str(work)],
        capture_output=True, text=True, timeout=120, env=env, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["mounts"] == [["mnt"]]
    assert out["gits"] == []  # the repository on the mount was not reached
    assert out["dir_ids_has_mounted_dir"] is False and out["dir_ids_mounts"] == 1
