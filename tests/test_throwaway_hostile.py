"""Throwaways hold third-party content: nothing it names runs, nothing is written outside.

Hostile trees are written as raw objects (porcelain would refuse to build most of them) into both
the remote and the clone, so every case runs against the ``remote`` and the ``local`` source. A
marker file proves a payload ran; ``outside`` is a canary directory no write may reach.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from tests.throwlib import TW, blob, commit_of, leaks, ref, tree_body, write_object
from tests.vectorlib import plain_git

SOURCES = ["remote", "local"]
Spec = dict[str, Any]  # name -> (mode, payload) or a nested Spec


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> TW:
    t = TW(tmp_path, private_home).build()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "canary").write_text("untouched")
    (tmp_path / "markers").mkdir()
    return t


def tree_of(git_dir: Path, spec: Spec) -> str:
    entries = []
    for name, item in spec.items():
        if isinstance(item, dict):
            entries.append(("40000", name, tree_of(git_dir, item)))
        else:
            mode, payload = item
            sha = payload if mode == "160000" else blob(git_dir, payload)
            entries.append((mode, name, sha))
    return write_object(git_dir, "tree", tree_body(entries))


def plant(w: TW, spec: Spec, name: str = "evil") -> str:
    """The same hostile commit (child of c3) in the remote and in the clone."""
    sha = ""
    for gd in (w.remote, w.clone / ".git"):
        sha = commit_of(gd, tree_of(gd, spec), parent=w.shas["c3"])
        ref(gd, f"refs/heads/{name}", sha)
    return sha


def file_(text: str = "x") -> tuple[str, bytes]:
    return "100644", text.encode()


def intact(w: TW) -> None:
    assert (w.tmp / "outside").iterdir().__next__().name == "canary"
    assert sorted(os.listdir(w.tmp / "outside")) == ["canary"]
    assert (w.tmp / "outside" / "canary").read_text() == "untouched"
    assert os.listdir(w.tmp / "markers") == []


# the outcome of a refused object: the fetch's fsck refuses it for a remote source, the local
# pack's strict index refuses it for a local one
REFUSED_AS = {"remote": "fetch_failed", "local": "create_failed"}


def refused_and_clean(w: TW, sha: str, source: str, code: str | None = None) -> None:
    err = w.failed(w.throw("--source", source, ref=sha), 12)
    assert err["code"] == (code or REFUSED_AS[source])
    assert leaks(w.troot) == [] and w.records() == [] and w.leases() == []
    intact(w)


def test_the_helper_builds_a_benign_hostile_tree_that_is_accepted(w: TW) -> None:
    # the control for every refusal below: the same builder, a harmless name, is accepted
    sha = plant(w, {"gitx": file_("a"), "dir": {"f": file_("b")}})
    for source in SOURCES:
        rec = w.made("--source", source, ref=sha)
        assert (Path(rec["path"]) / "dir" / "f").read_text() == "b"


# -- trees that name the repository's own metadata ---------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", [".git", ".GIT", ".Git", "git~1", ".git ", ".git."])
def test_a_tree_with_a_dot_git_file_fails_closed(w: TW, source: str, name: str) -> None:
    sha = plant(w, {name: file_("gitdir: /somewhere\n"), "f": file_()})
    refused_and_clean(w, sha, source)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", [".git", ".GIT"])
def test_a_tree_with_a_dot_git_directory_planting_hooks_fails_closed(
    w: TW, source: str, name: str, tmp_path: Path
) -> None:
    hook = f"#!/bin/sh\ntouch {tmp_path}/markers/hit\n".encode()
    sha = plant(w, {name: {"hooks": {"post-checkout": ("100755", hook)}}, "f": file_()})
    refused_and_clean(w, sha, source)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("name", ["..", ".", "a/b", ""])
def test_entries_that_escape_their_directory_fail_closed(w: TW, source: str, name: str) -> None:
    sha = plant(w, {name: file_("pwned"), "f": file_()})
    refused_and_clean(w, sha, source)


@pytest.mark.parametrize("source", SOURCES)
def test_a_symlinked_gitmodules_fails_closed(w: TW, source: str, tmp_path: Path) -> None:
    sha = plant(w, {".gitmodules": ("120000", str(tmp_path / "outside" / "canary").encode())})
    refused_and_clean(w, sha, source)


@pytest.mark.parametrize("source", SOURCES)
def test_a_name_that_is_both_a_symlink_and_a_directory_fails_closed(
    w: TW, source: str, tmp_path: Path
) -> None:
    # `link` is a symlink to outside and a directory: a checkout that followed it would write
    # `pwned` into the canary directory
    out = str(tmp_path / "outside").encode()
    sha = ""
    for gd in (w.remote, w.clone / ".git"):
        inner = tree_of(gd, {"pwned": file_("pwned")})
        link = blob(gd, out)
        tree = write_object(
            gd, "tree", tree_body([("120000", "link", link), ("40000", "link", inner)])
        )
        sha = commit_of(gd, tree, parent=w.shas["c3"])
        ref(gd, "refs/heads/evil", sha)
    refused_and_clean(w, sha, source)
    assert not (tmp_path / "outside" / "pwned").exists()


# -- content that is accepted but inert --------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_symlinks_are_created_as_links_and_never_followed(
    w: TW, source: str, tmp_path: Path
) -> None:
    out = str(tmp_path / "outside")
    sha = plant(
        w,
        {
            "link": ("120000", out.encode()),
            "up": ("120000", b"../../.."),
            "abs": ("120000", b"/etc/passwd"),
            "d": {"inner": ("120000", out.encode())},
            "f": file_(),
        },
    )
    rec = w.made("--source", source, ref=sha)
    p = Path(rec["path"])
    assert os.readlink(p / "link") == out and os.readlink(p / "d" / "inner") == out
    assert stat.S_ISLNK(os.lstat(p / "abs").st_mode)
    intact(w)
    assert sorted(os.listdir(tmp_path / "outside")) == ["canary"]
    # and removing the throwaway removes the links, not what they name
    w.removed(rec["id"])
    assert not p.exists() and (tmp_path / "outside" / "canary").read_text() == "untouched"
    assert os.path.exists("/etc/passwd")


@pytest.mark.parametrize("source", SOURCES)
def test_gitmodules_and_gitlinks_run_nothing_and_clone_nothing(
    w: TW, source: str, tmp_path: Path
) -> None:
    mark = tmp_path / "markers" / "hit"
    modules = (
        f'[submodule "sub"]\n\tpath = sub\n\turl = ext::sh -c "touch {mark}"\n'
        f'[submodule "up"]\n\tpath = up\n\turl = ../../../elsewhere\n'
    )
    sha = plant(
        w,
        {
            ".gitmodules": file_(modules),
            "sub": ("160000", w.shas["c1"]),
            "up": ("160000", w.shas["c2"]),
        },
    )
    r = w.throw("--source", source, ref=sha)
    intact(w)
    if r.code == 0:  # accepted: the gitlinks are empty directories, never populated
        p = Path(r.json()["record"]["path"])
        assert os.listdir(p / "sub") == [] and os.listdir(p / "up") == []
        assert (p / ".gitmodules").read_text() == modules
        assert not (p / ".git" / "modules").exists()
    else:
        assert leaks(w.troot) == [] and w.records() == []


def plant_executable_config(w: TW, hit: Path) -> None:
    """Config in the clone that would run ``touch hit`` from every vector it can."""
    hooks = w.tmp / "evil-hooks"
    hooks.mkdir()
    for name in ("post-checkout", "post-merge", "pre-commit", "reference-transaction"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch {hit}\n")
        (hooks / name).chmod(0o755)
    cfg = w.clone / ".git" / "config"
    cfg.write_text(
        cfg.read_text() + f'[filter "evil"]\n\tclean = touch {hit}\n\tsmudge = touch {hit}\n'
        f"\tprocess = touch {hit}\n\trequired = true\n"
        f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = touch {hit}\n"
        f"\tsshCommand = touch {hit}\n\tattributesFile = /dev/null\n"
        f"[http]\n\textraHeader = X-Evil: 1\n"
        f"[diff]\n\texternal = touch {hit}\n"
    )


def test_planted_clone_config_and_gitattributes_are_inert(w: TW, tmp_path: Path) -> None:
    hit = tmp_path / "markers" / "hit"
    plant_executable_config(w, hit)
    attrs = b"* filter=evil\n*.txt text eol=crlf ident\n*.bin -text diff=evil merge=evil\n"
    sha = plant(w, {".gitattributes": ("100644", attrs), "a.txt": file_("line\n"),
                    "b.bin": file_("$Id$\n")})  # fmt: skip
    # the control: unhardened git, in the same clone, runs the payload
    ctl = tmp_path / "control"
    ctl.mkdir()
    plain_git(["--git-dir", str(w.clone / ".git"), "--work-tree", str(ctl), "read-tree", "-u",
               "--reset", sha], cwd=tmp_path, home=w.home)  # fmt: skip
    assert hit.exists(), "the vector is dead: the test proves nothing"
    hit.unlink()
    for source in ("local", "remote"):
        rec = w.made("--source", source, ref=sha)
        p = Path(rec["path"])
        assert not hit.exists(), source
        # `* -filter` blanks filter drivers; git's built-in eol conversion runs no program
        assert (p / "a.txt").read_bytes() == b"line\r\n"
        assert (p / "b.bin").read_bytes() == b"$Id$\n"
        cfg = (p / ".git" / "config").read_text().lower()
        assert "evil" not in cfg and "hookspath" not in cfg and "insteadof" not in cfg


def test_a_clone_whose_url_is_rewritten_is_not_a_source(w: TW) -> None:
    cfg = w.clone / ".git" / "config"
    cfg.write_text(cfg.read_text() + f'[url "{w.tmp}/elsewhere"]\n\tinsteadOf = {w.remote}\n')
    assert w.failed(w.throw("--source", "local"), 12)["code"] == "remote_mismatch"
    rec = w.made("--source", "auto")
    assert rec["source"] == "remote" and rec["clone_path"] is None
    assert rec["fetch_url"] == str(w.remote)


def test_lfs_pointers_stay_pointers_and_no_filter_driver_is_honored(w: TW, tmp_path: Path) -> None:
    hit = tmp_path / "markers" / "hit"
    fake = tmp_path / "bin" / "git-lfs"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\ntouch {hit}\ncat\n")
    fake.chmod(0o755)
    (w.home / ".gitconfig").write_text(
        f'[filter "lfs"]\n\tprocess = {fake} filter-process\n\tsmudge = {fake} smudge -- %f\n'
        f"\tclean = {fake} clean -- %f\n\trequired = true\n"
    )
    w.repo_cfg["create"] = {"allow_filters": ["lfs"]}  # honored for worktrees, never throwaways
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"0" * 64 + b"\nsize 9\n"
    sha = plant(w, {".gitattributes": ("100644", b"*.bin filter=lfs diff=lfs merge=lfs -text\n"),
                    "big.bin": ("100644", pointer)})  # fmt: skip
    (tmp_path / "ctl").mkdir()
    plain_git(["--git-dir", str(w.clone / ".git"), "--work-tree", str(tmp_path / "ctl"),
               "read-tree", "-u", "--reset", sha], cwd=tmp_path, home=w.home,
              env={"GIT_CONFIG_GLOBAL": str(w.home / ".gitconfig")})  # fmt: skip
    assert hit.exists(), "the vector is dead: the test proves nothing"
    hit.unlink()
    for source in SOURCES:
        p = Path(w.made("--source", source, ref=sha)["path"])
        assert (p / "big.bin").read_bytes() == pointer and not hit.exists()


# -- big and awkward trees ---------------------------------------------------------------------


def deep_spec(depth: int) -> Spec:
    spec: Spec = {"leaf": file_("bottom")}
    for _ in range(depth):
        spec = {"d": spec}
    return spec


def deep_commit(w: TW, depth: int) -> str:
    sha = ""
    for gd in (w.remote, w.clone / ".git"):
        # bottom-up and iterative: a recursive builder would hit the interpreter's limit
        tree = write_object(gd, "tree", tree_body([("100644", "leaf", blob(gd, b"bottom"))]))
        for _ in range(depth):
            tree = write_object(gd, "tree", tree_body([("40000", "d", tree)]))
        sha = commit_of(gd, tree, parent=w.shas["c3"])
        ref(gd, "refs/heads/evil", sha)
    return sha


@pytest.mark.parametrize("source", SOURCES)
def test_a_very_deep_tree_is_created_and_removed_without_recursion(w: TW, source: str) -> None:
    sha = deep_commit(w, 1200)
    r = w.throw("--source", source, ref=sha)
    if r.code != 0:  # a path too long for the kernel fails closed
        assert w.failed(r, 12)["code"] == "create_failed"
        assert leaks(w.troot) == [] and w.records() == []
        return
    rec = r.json()["record"]
    leaf = Path(rec["path"]).joinpath(*["d"] * 1200, "leaf")
    assert leaf.read_text() == "bottom" if len(str(leaf)) < 4000 else True
    w.removed(rec["id"])
    assert leaks(w.troot) == [] and w.state_of(rec["id"]) == "removed"


@pytest.mark.parametrize("source", SOURCES)
def test_a_wide_tree_is_created_and_removed(w: TW, source: str) -> None:
    spec: Spec = {f"f{i:05d}": file_(str(i)) for i in range(3000)}
    sha = plant(w, spec)
    rec = w.made("--source", source, ref=sha)
    p = Path(rec["path"])
    assert len(os.listdir(p)) == 3001 and (p / "f02999").read_text() == "2999"
    w.removed(rec["id"])
    assert leaks(w.troot) == []


def case_sensitive(path: Path) -> bool:
    (path / "Aa").write_text("1")
    try:
        return not (path / "aa").exists()
    finally:
        (path / "Aa").unlink()


@pytest.mark.parametrize("source", SOURCES)
def test_case_colliding_paths_never_leave_the_throwaway(w: TW, source: str, tmp_path: Path) -> None:
    sha = plant(w, {"Readme": file_("upper"), "readme": file_("lower"),
                    "Dir": {"x": file_("1")}, "dir": {"x": file_("2")}})  # fmt: skip
    rec = w.made("--source", source, ref=sha)
    p = Path(rec["path"])
    names = sorted(os.listdir(p))
    if case_sensitive(tmp_path):
        assert names == [".git", "Dir", "Readme", "dir", "readme"]
    else:  # a case-insensitive file system folds them: fewer names, still all inside
        assert len(names) < 5
    intact(w)


# -- huge objects are bounded by the clone's pack limit ----------------------------------------


def test_a_source_commit_with_a_missing_object_fails_closed(w: TW) -> None:
    sha = plant(w, {"f": file_("will be lost")})
    lost = w.g(["rev-parse", f"{sha}:f"])
    obj = w.clone / ".git" / "objects" / lost[:2] / lost[2:]
    obj.chmod(0o644)
    obj.unlink()
    err = w.failed(w.throw("--source", "local", ref=sha), 12)
    assert err["code"] == "create_failed"
    assert leaks(w.troot) == [] and w.records() == []


# -- trees that expand to more than they should ------------------------------------------------


def bomb(git_dir: Path, depth: int) -> str:
    """A tree DAG with two entries per level: ``2 ** depth`` files from a few objects."""
    node = write_object(git_dir, "tree", tree_body([("100644", "f", blob(git_dir, b"x"))]))
    for _ in range(depth):
        node = write_object(
            git_dir, "tree", tree_body([("40000", "a", node), ("40000", "b", node)])
        )
    return node


def plant_tree(w: TW, make: Any) -> str:
    sha = ""
    for gd in (w.remote, w.clone / ".git"):
        sha = commit_of(gd, make(gd), parent=w.shas["c3"])
        ref(gd, "refs/heads/evil", sha)
    return sha


def tree_too_large(w: TW, sha: str, source: str) -> None:
    err = w.failed(w.throw("--source", source, ref=sha), 12)
    assert err["code"] == "create_failed" and err["details"] == {"cause": "tree_too_large"}
    assert leaks(w.troot) == [] and w.records() == [] and w.leases() == []
    intact(w)


@pytest.mark.parametrize("source", SOURCES)
def test_a_tree_dag_with_too_many_entries_is_refused_before_anything_is_written(
    w: TW, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import packs

    monkeypatch.setattr(packs, "MAX_TREE_ENTRIES", 50)
    tree_too_large(w, plant_tree(w, lambda gd: bomb(gd, 6)), source)  # 64 files
    ok = plant_tree(w, lambda gd: bomb(gd, 5))  # 32 files: within the bound
    assert w.made("--source", source, ref=ok)["state"] == "active"


@pytest.mark.parametrize("source", SOURCES)
def test_blobs_summing_past_the_byte_bound_are_refused(
    w: TW, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import packs

    monkeypatch.setattr(packs, "MAX_TREE_BYTES", 1000)

    def big(gd: Path) -> str:
        items = [("100644", f"f{i}", blob(gd, bytes([i]) * 400)) for i in range(3)]
        return write_object(gd, "tree", tree_body(items))

    tree_too_large(w, plant_tree(w, big), source)


@pytest.mark.parametrize("source", SOURCES)
def test_a_listing_past_the_output_cap_is_refused(
    w: TW, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import packs

    monkeypatch.setattr(packs, "MAX_TREE_LIST_BYTES", 200)
    tree_too_large(w, plant_tree(w, lambda gd: bomb(gd, 6)), source)
