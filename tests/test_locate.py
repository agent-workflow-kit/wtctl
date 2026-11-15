"""Clone resolution: locate, discovery, clone/auto-clone, anchor identity (docs/interface.md)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import run_cli
from tests.schemacheck import validate
from tests.vectorlib import GIT, plain_git
from wtctl import locate as locate_mod
from wtctl.config import load_config
from wtctl.errors import WtctlError
from wtctl.gateway import Gateway, GitResult
from wtctl.hardgit import Git
from wtctl.locate import Resolver
from wtctl.state import State, state_path

REPO = "example.test/org/repo"
URL = "https://example.test/org/repo.git"


# -- builders ------------------------------------------------------------------------------------


def g(args: list[str], cwd: Path, home: Path) -> str:
    r = plain_git(args, cwd=cwd, home=home)
    assert r.returncode == 0, r.stderr.decode()
    return r.stdout.decode()


class World:
    def __init__(self, tmp: Path, home: Path) -> None:
        self.tmp = tmp
        self.home = home
        self.repos: dict[str, dict[str, Any]] = {}
        self.top: dict[str, Any] = {"root": str(tmp / "wt")}

    def stub(self, path: Path, url: str | None = URL, remote: str = "origin") -> Path:
        """A clone-shaped repository that was never fetched (no network needed)."""
        path.mkdir(parents=True, exist_ok=True)
        g(["init", "-q", "-b", "main"], path, self.home)
        if url is not None:
            g(["config", f"remote.{remote}.url", url], path, self.home)
        return path

    def bare(self, name: str = "remote.git") -> Path:
        """A local bare remote with one commit."""
        work = self.tmp / f"{name}.work"
        work.mkdir()
        g(["init", "-q", "-b", "main"], work, self.home)
        (work / "f.txt").write_text("hello\n")
        g(["add", "f.txt"], work, self.home)
        g(["commit", "-q", "-m", "init"], work, self.home)
        bare = self.tmp / name
        g(["clone", "-q", "--bare", str(work), str(bare)], self.tmp, self.home)
        return bare

    def config(self) -> str:
        data = {**self.top, "repos": self.repos}
        path = self.tmp / "config.yaml"
        path.write_text(json.dumps(data))
        return str(path)

    def cli(self, capsys: pytest.CaptureFixture[str], *argv: str, human: bool = False) -> Any:
        actor = ["--actor", "human:tester"] if human else []
        return run_cli(capsys, "--config", self.config(), "--json", *actor, *argv)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> World:
    return World(tmp_path, private_home)


def make_resolver(w: World, *, runner: Any = None, readonly: bool = False) -> Resolver:
    cfg = load_config(w.config())
    gw = Gateway(git_runner=runner) if runner else Gateway()
    state = State(state_path(), Gateway())
    return Resolver(cfg, state, Git(gw, GIT, str(w.home)), readonly=readonly)


def row(repo: str = REPO) -> dict[str, Any] | None:
    st = State(state_path(), Gateway())
    try:
        return st.get_clone(repo)
    finally:
        st.close()


# -- config source -------------------------------------------------------------------------------


def test_config_entry_resolves_and_matches_the_schema(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0, r.out
    data = r.json()
    validate("wtctl/cli/locate.json", data)
    assert data == {
        "repo": REPO,
        "clone_path": str(clone),
        "common_dir": str(clone / ".git"),
        "source": "config",
        "fetch_url": URL,
        "default_branch": None,
    }
    st = os.stat(clone / ".git")
    stored = row()
    assert stored is not None
    assert stored["common_id"] == f"{st.st_dev}:{st.st_ino}"
    assert stored["fetch_url"] == URL and stored["source"] == "config"


def test_human_output_is_the_clone_path_only(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    code = run_cli_human(capsys, w, "locate", REPO)
    assert code == (0, str(clone) + "\n")


def run_cli_human(capsys: pytest.CaptureFixture[str], w: World, *argv: str) -> tuple[int, str]:
    r = run_cli(capsys, "--config", w.config(), *argv)
    return r.code, r.out


def test_userinfo_in_the_remote_is_stripped_from_the_fetch_url(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "mine", "https://bot:s3cret@example.test/org/repo.git")
    w.repos[REPO] = {"clone": str(clone)}
    r = w.cli(capsys, "locate", REPO)
    assert r.json()["fetch_url"] == URL
    assert "s3cret" not in r.out + r.err
    stored = row()
    assert stored is not None and "s3cret" not in json.dumps(stored)


def test_scp_like_remote_is_accepted_and_reported_stripped(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "mine", "git@example.test:org/repo.git")
    w.repos[REPO] = {"clone": str(clone)}
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0, r.out
    assert r.json()["fetch_url"] == "example.test:org/repo.git"


def test_custom_remote_name(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    clone = w.stub(w.tmp / "mine", URL, remote="upstream")
    w.repos[REPO] = {"clone": str(clone), "remote": "upstream"}
    assert w.cli(capsys, "locate", REPO).code == 0
    w.repos[REPO] = {"clone": str(clone)}
    r = w.cli(capsys, "locate", REPO)
    assert (r.code, r.json()["error"]["code"]) == (12, "remote_mismatch")
    assert r.json()["error"]["details"] == {"cause": "no_remote"}


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://example.test/org/other.git",
        "https://evil.test/org/repo.git",
        "https://example.test/Org/repo.git",
        "http://example.test/org/repo.git",
        "git://example.test/org/repo.git",
        "ext::sh -c true",
        "file:///srv/git/repo.git",
        "/srv/git/repo.git",
        "https://example.test@evil.test/org/repo.git",
        "-oProxyCommand=x@example.test:org/repo",
    ],
)
def test_config_clone_with_a_wrong_remote_is_remote_mismatch(
    w: World, capsys: pytest.CaptureFixture[str], remote_url: str
) -> None:
    clone = w.stub(w.tmp / "mine", remote_url)
    w.repos[REPO] = {"clone": str(clone)}
    r = w.cli(capsys, "locate", REPO)
    assert (r.code, r.json()["error"]["code"]) == (12, "remote_mismatch")
    validate("wtctl/cli/error.json", r.json())


def test_config_clone_without_a_remote_says_no_remote(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "mine", None)
    w.repos[REPO] = {"clone": str(clone)}
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 12 and r.json()["error"]["details"] == {"cause": "no_remote"}


def test_config_dir_that_is_not_a_git_clone_is_remote_mismatch(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (w.tmp / "plain").mkdir()
    w.repos[REPO] = {"clone": str(w.tmp / "plain")}
    r = w.cli(capsys, "locate", REPO)
    assert (r.code, r.json()["error"]["code"]) == (12, "remote_mismatch")


def test_config_path_that_is_a_file_is_path_exists(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    (w.tmp / "afile").write_text("x")
    w.repos[REPO] = {"clone": str(w.tmp / "afile")}
    r = w.cli(capsys, "locate", REPO)
    assert (r.code, r.json()["error"]["reason"]) == (20, "path_exists")


def test_a_linked_worktree_is_not_a_clone(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    main = w.stub(w.tmp / "main")
    (main / "f").write_text("x")
    g(["add", "f"], main, w.home)
    g(["commit", "-q", "-m", "c"], main, w.home)
    wt = w.tmp / "linked"
    g(["worktree", "add", "-q", str(wt), "-b", "side"], main, w.home)
    w.repos[REPO] = {"clone": str(wt)}
    r = w.cli(capsys, "locate", REPO)
    assert (r.code, r.json()["error"]["code"]) == (12, "remote_mismatch")


def test_dot_git_symlink_is_refused(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    real = w.stub(w.tmp / "elsewhere")
    fake = w.tmp / "fake"
    fake.mkdir()
    os.symlink(real / ".git", fake / ".git")
    w.repos[REPO] = {"clone": str(fake)}
    r = w.cli(capsys, "locate", REPO)
    assert (r.code, r.json()["error"]["code"]) == (12, "remote_mismatch")


def test_configured_clone_path_may_be_a_symlink_to_a_clone(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    real = w.stub(w.tmp / "real")
    os.symlink(real, w.tmp / "link")
    w.repos[REPO] = {"clone": str(w.tmp / "link")}
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0 and r.json()["clone_path"] == str(real)


def test_insteadof_rewriting_is_remote_mismatch(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "mine")
    g(["config", "url.https://evil.test/.insteadOf", "https://example.test/"], clone, w.home)
    w.repos[REPO] = {"clone": str(clone)}
    r = w.cli(capsys, "locate", REPO)
    assert (r.code, r.json()["error"]["code"]) == (12, "remote_mismatch")
    assert "insteadOf" in r.json()["error"]["message"]


def test_two_urls_on_the_remote_are_refused(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    clone = w.stub(w.tmp / "mine")
    g(["config", "--add", "remote.origin.url", "https://evil.test/org/repo.git"], clone, w.home)
    w.repos[REPO] = {"clone": str(clone)}
    assert w.cli(capsys, "locate", REPO).code == 12


def test_configured_but_missing_is_not_found_and_ignores_other_sources(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    w.stub(w.tmp / "search" / "elsewhere")
    w.top["search_roots"] = [str(w.tmp / "search")]
    w.repos[REPO] = {"clone": str(w.tmp / "nowhere")}
    r = w.cli(capsys, "locate", REPO)
    assert (r.code, r.json()["error"]["code"]) == (10, "not_found")
    assert "wtctl clone" in r.json()["error"]["message"]
    assert not (w.tmp / "nowhere").exists()


def test_nothing_found_exits_10_with_a_hint(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 10
    msg = r.json()["error"]["message"]
    assert "repos.<id>.clone" in msg and "search_roots" in msg and "wtctl clone" in msg


@pytest.mark.parametrize(
    "bad",
    [
        "--upload-pack=touch /tmp/x/a/b",
        "-x/a/b",
        "example.test/org/..",
        "example.test/../repo",
        "Example.test/org/repo",
        "example.test/org/re\npo",
        "https://example.test/org/repo",
        "example.test",
        "",
    ],
)
def test_bad_repo_ids_exit_2_before_anything_runs(
    w: World, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    for cmd in ("locate", "clone"):
        r = w.cli(capsys, cmd, bad)
        assert r.code == 2, (cmd, r.out, r.err)


# -- discovery -----------------------------------------------------------------------------------


def test_discovery_finds_a_clone_within_the_depth_and_caches_it(
    w: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = w.stub(w.tmp / "search" / "a" / "b" / "repo")
    w.stub(w.tmp / "search" / "a" / "other", "https://example.test/org/other.git")
    w.top["search_roots"] = [str(w.tmp / "search")]
    w.top["search_depth"] = 3
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0, r.out
    assert r.json()["source"] == "discovered" and r.json()["clone_path"] == str(clone)
    assert row()["source"] == "discovered"  # type: ignore[index]

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("the cached hit must not need a walk")

    monkeypatch.setattr(locate_mod, "walk_for_clones", boom)
    r2 = w.cli(capsys, "locate", REPO)
    assert r2.code == 0 and r2.json() == r.json()


def test_discovery_depth_limit(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    w.stub(w.tmp / "search" / "a" / "b" / "repo")
    w.top["search_roots"] = [str(w.tmp / "search")]
    w.top["search_depth"] = 2
    assert w.cli(capsys, "locate", REPO).code == 10
    w.top["search_depth"] = 3
    assert w.cli(capsys, "locate", REPO).code == 0


def test_discovery_does_not_descend_into_a_clone(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    outer = w.stub(w.tmp / "search" / "outer", "https://example.test/org/outer.git")
    w.stub(outer / "vendor" / "repo")
    w.top["search_roots"] = [str(w.tmp / "search")]
    assert w.cli(capsys, "locate", REPO).code == 10


def test_discovery_skips_linked_worktrees(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    main = w.stub(w.tmp / "elsewhere" / "main", "https://example.test/org/other.git")
    (main / "f").write_text("x")
    g(["add", "f"], main, w.home)
    g(["commit", "-q", "-m", "c"], main, w.home)
    wt = w.tmp / "search" / "linked"
    wt.parent.mkdir()
    g(["worktree", "add", "-q", str(wt), "-b", "side"], main, w.home)
    g(["remote", "set-url", "origin", URL], main, w.home)
    w.top["search_roots"] = [str(w.tmp / "search")]
    assert (wt / ".git").is_file()
    assert w.cli(capsys, "locate", REPO).code == 10


def test_discovery_does_not_follow_symlinks(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    w.stub(w.tmp / "outside" / "repo")
    search = w.tmp / "search"
    search.mkdir()
    os.symlink(w.tmp / "outside", search / "link")
    os.symlink(w.tmp / "outside" / "repo", search / "direct")
    os.symlink(search, search / "loop")
    os.symlink("loop", search / "loop2")
    w.top["search_roots"] = [str(search)]
    w.top["search_depth"] = 10
    assert w.cli(capsys, "locate", REPO).code == 10


def test_discovery_ignores_a_dot_git_symlink(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    real = w.stub(w.tmp / "outside" / "repo")
    fake = w.tmp / "search" / "fake"
    fake.mkdir(parents=True)
    os.symlink(real / ".git", fake / ".git")
    w.top["search_roots"] = [str(w.tmp / "search")]
    assert w.cli(capsys, "locate", REPO).code == 10


def test_a_search_root_may_itself_be_a_symlink(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "real" / "repo")
    os.symlink(w.tmp / "real", w.tmp / "code")
    w.top["search_roots"] = [str(w.tmp / "code")]
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0 and r.json()["clone_path"] == str(clone)


def test_discovery_survives_hostile_directory_names(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    search = w.tmp / "search"
    search.mkdir()
    (search / "new\nline").mkdir()
    w.stub(search / "new\nline" / "repo")  # newline paths are refused, never reported
    os.mkdir(os.fsencode(search) + b"/bad\xff\xfename")
    (search / "--upload-pack=x").mkdir()
    (search / "$(touch pwned)").mkdir()
    (search / "emoji-\U0001f600").mkdir()
    good = w.stub(search / "-dash" / "repo")
    w.top["search_roots"] = [str(search)]
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0, r.out
    assert r.json()["clone_path"] == str(good)
    assert not (search / "pwned").exists()


def test_ambiguous_discovery_exits_21_with_candidates(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    a = w.stub(w.tmp / "search" / "a")
    b = w.stub(w.tmp / "search" / "b")
    w.top["search_roots"] = [str(w.tmp / "search")]
    r = w.cli(capsys, "locate", REPO)
    assert (r.code, r.json()["error"]["code"]) == (21, "ambiguous")
    assert r.json()["error"]["candidates"] == [
        {"id": None, "path": str(a)},
        {"id": None, "path": str(b)},
    ]
    validate("wtctl/cli/error.json", r.json())
    # a config entry chooses
    w.repos[REPO] = {"clone": str(b)}
    assert w.cli(capsys, "locate", REPO).json()["clone_path"] == str(b)


def test_overlapping_search_roots_do_not_make_one_clone_ambiguous(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "search" / "a" / "repo")
    w.top["search_roots"] = [str(w.tmp / "search"), str(w.tmp / "search" / "a")]
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0 and r.json()["clone_path"] == str(clone)


def test_cached_hit_is_reverified_and_dropped_when_the_remote_changes(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "search" / "repo")
    w.top["search_roots"] = [str(w.tmp / "search")]
    assert w.cli(capsys, "locate", REPO).code == 0
    g(["remote", "set-url", "origin", "https://example.test/org/moved.git"], clone, w.home)
    assert w.cli(capsys, "locate", REPO).code == 10
    # the row and its recorded identity stay
    assert row() is not None and row()["common_id"]  # type: ignore[index]
    g(["remote", "set-url", "origin", URL], clone, w.home)
    assert w.cli(capsys, "locate", REPO).code == 0


def test_a_cached_hit_that_vanished_is_rediscovered_elsewhere(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "search" / "one")
    w.top["search_roots"] = [str(w.tmp / "search")]
    assert w.cli(capsys, "locate", REPO).code == 0
    os.rename(clone, w.tmp / "search" / "two")  # same inode: the identity is unchanged
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0 and r.json()["clone_path"] == str(w.tmp / "search" / "two")
    assert row()["path"] == str(w.tmp / "search" / "two")  # type: ignore[index]
    assert "differs" not in r.err


def test_discovery_stops_at_the_directory_budget(
    w: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(5):
        (w.tmp / "search" / f"d{i}").mkdir(parents=True)
    w.stub(w.tmp / "search" / "z" / "repo")
    w.top["search_roots"] = [str(w.tmp / "search")]
    monkeypatch.setattr(locate_mod, "MAX_DISCOVERY_DIRS", 3)
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 10


# -- hostile clones ------------------------------------------------------------------------------


def test_planted_clone_config_and_hooks_are_inert(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "search" / "repo")
    marker = w.tmp / "pwned"
    cmd = f"touch {marker}"
    for key, value in [
        ("core.fsmonitor", cmd),
        ("core.sshCommand", cmd),
        ("core.hooksPath", str(clone / "evilhooks")),
        ("core.pager", cmd),
        ("core.editor", cmd),
        ("credential.helper", f"!{cmd}"),
        ("remote.origin.uploadpack", cmd),
        ("remote.origin.receivepack", cmd),
        ("remote.origin.vcs", "evil"),
        ("alias.rev-parse", f"!{cmd}"),
        ("include.path", str(clone / "inc")),
        ("filter.x.smudge", cmd),
        ("diff.external", cmd),
        ("extensions.worktreeConfig", "true"),
    ]:
        g(["config", key, value], clone, w.home)
    (clone / "inc").write_text(f"[core]\n\tfsmonitor = {cmd}\n")
    for hook in ("pre-commit", "post-checkout", "reference-transaction", "fsmonitor-watchman"):
        p = clone / ".git" / "hooks" / hook
        p.parent.mkdir(exist_ok=True)
        p.write_text(f"#!/bin/sh\n{cmd}\n")
        p.chmod(0o755)
    (clone / "evilhooks").mkdir()
    (clone / "evilhooks" / "reference-transaction").write_text(f"#!/bin/sh\n{cmd}\n")
    (clone / "evilhooks" / "reference-transaction").chmod(0o755)
    w.top["search_roots"] = [str(w.tmp / "search")]
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0, r.out
    assert not marker.exists()


def test_a_corrupt_candidate_does_not_break_discovery(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = w.tmp / "search" / "bad"
    (bad / ".git").mkdir(parents=True)  # a .git directory that is not a repository
    garbage = w.stub(w.tmp / "search" / "garbage")
    (garbage / ".git" / "config").write_bytes(b"\x00\x01[[[ not a config\n")
    good = w.stub(w.tmp / "search" / "good")
    w.top["search_roots"] = [str(w.tmp / "search")]
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0, r.out
    assert r.json()["clone_path"] == str(good)


def test_a_remote_url_naming_an_ext_transport_never_runs(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = w.tmp / "pwned"
    clone = w.stub(w.tmp / "search" / "repo", f"ext::sh -c 'touch {marker}'")
    assert clone.exists()
    w.top["search_roots"] = [str(w.tmp / "search")]
    assert w.cli(capsys, "locate", REPO).code == 10
    assert not marker.exists()


# -- objects: SHA-256 ----------------------------------------------------------------------------


def sha256_stub(w: World, path: Path) -> Path:
    path.mkdir(parents=True)
    r = plain_git(["init", "-q", "--object-format=sha256", "-b", "main"], cwd=path, home=w.home)
    if r.returncode != 0:
        pytest.skip("this git cannot create sha256 repositories")
    g(["remote", "add", "origin", URL], path, w.home)
    return path


def test_sha256_clone_is_located_with_a_warning_and_refused_by_create(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = sha256_stub(w, w.tmp / "s256")
    w.repos[REPO] = {"clone": str(clone)}
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0 and "sha256" in r.err
    res = make_resolver(w).locate(REPO)
    assert res.object_format == "sha256"
    with pytest.raises(WtctlError) as exc:
        res.require_sha1()
    assert (exc.value.code, exc.value.fields["details"]) == (
        "create_failed",
        {"cause": "object_format"},
    )


def test_sha1_clone_passes_require_sha1(w: World) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    res = make_resolver(w).locate(REPO)
    assert res.object_format == "sha1"
    res.require_sha1()
    res.require_anchor()


# -- anchor identity -----------------------------------------------------------------------------


def replace_clone(w: World, path: Path) -> None:
    """A new clone at the same path. The old one is kept (moved) so the inode is not reused."""
    os.rename(path, path.with_name(path.name + ".old-elsewhere"))
    w.stub(path)


def test_anchor_identity_is_recorded_once_and_stable(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    w.cli(capsys, "locate", REPO)
    first = row()["common_id"]  # type: ignore[index]
    for _ in range(3):
        assert w.cli(capsys, "locate", REPO).err == ""
    assert row()["common_id"] == first  # type: ignore[index]
    st = os.stat(clone / ".git")
    assert first == f"{st.st_dev}:{st.st_ino}"


def test_a_changed_identity_warns_and_changes_nothing_stored(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    w.cli(capsys, "locate", REPO)
    before = row()
    replace_clone(w, clone)
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0 and "wtctl clone" in r.err
    assert row() == before
    res = make_resolver(w).locate(REPO)
    assert res.anchor_changed
    with pytest.raises(WtctlError) as exc:
        res.require_anchor()
    err = exc.value
    assert (err.code, err.fields["details"]["cause"]) == ("create_failed", "anchor_changed")
    (anchor,) = err.fields["details"]["anchors"]
    assert anchor["changed"] and anchor["anchor"] == "common_dir"
    assert anchor["recorded"] == before["common_id"]  # type: ignore[index]
    validate("wtctl/cli/error.json", {"error": {"code": err.code, "message": "m", **err.fields}})


def test_clone_by_a_non_human_does_not_reaccept(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    w.cli(capsys, "locate", REPO)
    before = row()
    replace_clone(w, clone)
    r = w.cli(capsys, "--actor", "auto:bot", "clone", REPO)
    assert (r.code, r.json()["error"]["code"]) == (13, "confirmation_required")
    assert row() == before


def test_a_human_clone_reaccepts_with_yes(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    w.cli(capsys, "locate", REPO)
    old = row()["common_id"]  # type: ignore[index]
    replace_clone(w, clone)
    r = w.cli(capsys, "--yes", "clone", REPO, human=True)
    assert r.code == 0, r.out
    data = r.json()
    validate("wtctl/cli/clone.json", data)
    assert data["cloned"] is False and data["dry_run"] is False
    (anchor,) = data["anchors"]
    assert anchor["recorded"] == old and anchor["current"] != old and anchor["changed"]
    st = os.stat(clone / ".git")
    assert row()["common_id"] == f"{st.st_dev}:{st.st_ino}"  # type: ignore[index]
    assert w.cli(capsys, "locate", REPO).err == ""


def test_a_human_clone_without_yes_or_tty_exits_13(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    w.cli(capsys, "locate", REPO)
    before = row()
    replace_clone(w, clone)
    r = w.cli(capsys, "clone", REPO, human=True)
    assert (r.code, r.json()["error"]["code"]) == (13, "confirmation_required")
    assert row() == before


def test_reaccept_dry_run_changes_nothing(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    w.cli(capsys, "locate", REPO)
    before = row()
    replace_clone(w, clone)
    r = w.cli(capsys, "--dry-run", "clone", REPO, human=True)
    assert r.code == 0 and r.json()["dry_run"] is True and len(r.json()["anchors"]) == 1
    assert row() == before


def test_reaccept_refuses_a_fuse_anchor(
    w: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    w.cli(capsys, "locate", REPO)
    before = row()
    replace_clone(w, clone)
    monkeypatch.setattr(
        locate_mod,
        "describe_mount",
        lambda path, st: {"fs_type": "fuse.sshfs", "mount_source": "x", "mount_point": "/"},
    )
    r = w.cli(capsys, "--yes", "clone", REPO, human=True)
    err = r.json()["error"]
    assert (r.code, err["reason"], err["details"]["cause"]) == (20, "unlinked", "fuse_anchor")
    validate("wtctl/cli/error.json", r.json())
    assert row() == before


def test_a_moved_discovered_clone_with_another_identity_keeps_the_recorded_one(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    clone = w.stub(w.tmp / "search" / "one")
    w.top["search_roots"] = [str(w.tmp / "search")]
    w.cli(capsys, "locate", REPO)
    before = row()
    os.rename(clone, w.tmp / "moved-out")
    w.stub(w.tmp / "search" / "two")
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0 and "wtctl clone" in r.err
    assert row() == before


def test_mount_description_reads_mountinfo() -> None:
    st = os.stat("/")
    info = locate_mod.describe_mount("/", st)
    assert info["fs_type"] and info["mount_point"].startswith("/")
    assert locate_mod.is_fuse("fuse.sshfs") and not locate_mod.is_fuse("ext4")


# -- clone / auto-clone --------------------------------------------------------------------------


def clone_cfg(w: World, bare: Path) -> Path:
    w.repos[REPO] = {"clone_url": str(bare)}
    return w.tmp / "wt" / "clones" / "example.test" / "org" / "repo"


def no_tmp_dirs(parent: Path) -> bool:
    return not [p for p in parent.iterdir() if ".wtctl-tmp-" in p.name]


def test_clone_command_clones_into_the_location_and_records_it(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = w.bare()
    dest = clone_cfg(w, bare)
    r = w.cli(capsys, "clone", REPO)
    assert r.code == 0, r.out
    data = r.json()
    validate("wtctl/cli/clone.json", data)
    assert data == {
        "repo": REPO,
        "clone_path": str(dest),
        "common_dir": str(dest / ".git"),
        "source": "cloned",
        "fetch_url": str(bare),
        "default_branch": None,
        "cloned": True,
        "anchors": [],
        "dry_run": False,
    }
    assert (dest / "f.txt").read_text() == "hello\n"
    assert no_tmp_dirs(dest.parent)
    st = os.stat(dest / ".git")
    stored = row()
    assert stored is not None
    assert stored["common_id"] == f"{st.st_dev}:{st.st_ino}" and stored["source"] == "cloned"
    # hardened clone: no hooks from templates, protocol v2 recorded, origin is the plain path
    assert (
        not list((dest / ".git" / "hooks").glob("*"))
        if (dest / ".git" / "hooks").exists()
        else True
    )
    assert g(["config", "remote.origin.url"], dest, w.home).strip() == str(bare)


def test_clone_again_returns_the_existing_clone(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = clone_cfg(w, w.bare())
    w.cli(capsys, "clone", REPO)
    r = w.cli(capsys, "clone", REPO)
    assert r.code == 0
    assert (r.json()["cloned"], r.json()["source"]) == (False, "cloned")
    assert r.json()["clone_path"] == str(dest)


def test_locate_does_not_clone_unless_auto_clone(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = clone_cfg(w, w.bare())
    assert w.cli(capsys, "locate", REPO).code == 10
    assert not dest.exists()


def test_auto_clone_through_locate(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    bare = w.bare()
    dest = clone_cfg(w, bare)
    w.top["auto_clone"] = True
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0, r.out
    assert (r.json()["source"], r.json()["clone_path"]) == ("cloned", str(dest))
    r2 = w.cli(capsys, "locate", REPO)  # now the recorded clone
    assert (r2.json()["source"], r2.json()["clone_path"]) == ("cloned", str(dest))


def test_auto_clone_per_repo_and_into_a_configured_path(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = w.bare()
    target = w.tmp / "my" / "spot"
    w.repos[REPO] = {"clone_url": str(bare), "clone": str(target), "auto_clone": True}
    r = w.cli(capsys, "locate", REPO)
    assert r.code == 0, r.out
    assert (r.json()["source"], r.json()["clone_path"]) == ("cloned", str(target))
    assert (target / "f.txt").exists() and no_tmp_dirs(target.parent)
    assert w.cli(capsys, "locate", REPO).json()["source"] == "config"


def test_auto_clone_off_for_the_repo_overrides_global(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = clone_cfg(w, w.bare())
    w.top["auto_clone"] = True
    w.repos[REPO]["auto_clone"] = False
    assert w.cli(capsys, "locate", REPO).code == 10
    assert not dest.exists()


def test_clone_dry_run_creates_nothing(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    bare = w.bare()
    dest = clone_cfg(w, bare)
    r = w.cli(capsys, "--dry-run", "clone", REPO)
    assert r.code == 0, r.out
    validate("wtctl/cli/clone.json", r.json())
    assert r.json()["dry_run"] is True and r.json()["clone_path"] == str(dest)
    assert not (w.tmp / "wt").exists() and row() is None


def test_clone_into_an_empty_directory(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    dest = clone_cfg(w, w.bare())
    dest.mkdir(parents=True)
    r = w.cli(capsys, "clone", REPO)
    assert r.code == 0 and r.json()["cloned"] is True
    assert (dest / "f.txt").exists()


def test_clone_refuses_a_non_empty_destination(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = clone_cfg(w, w.bare())
    dest.mkdir(parents=True)
    (dest / "mine.txt").write_text("precious")
    r = w.cli(capsys, "clone", REPO)
    assert (r.code, r.json()["error"]["reason"]) == (20, "path_exists")
    assert (dest / "mine.txt").read_text() == "precious" and no_tmp_dirs(dest.parent)


def test_clone_refuses_a_destination_that_is_a_file_or_symlink(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = clone_cfg(w, w.bare())
    dest.parent.mkdir(parents=True)
    dest.write_text("x")
    assert w.cli(capsys, "clone", REPO).json()["error"]["reason"] == "path_exists"
    dest.unlink()
    (w.tmp / "target").mkdir()
    os.symlink(w.tmp / "target", dest)
    r = w.cli(capsys, "clone", REPO)
    assert (r.code, r.json()["error"]["reason"]) == (20, "path_exists")
    assert list((w.tmp / "target").iterdir()) == []


def test_existing_matching_clone_at_the_destination_is_reused(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = w.bare()
    dest = clone_cfg(w, bare)
    dest.parent.mkdir(parents=True)
    g(["clone", "-q", str(bare), str(dest)], w.tmp, w.home)
    r = w.cli(capsys, "clone", REPO)
    assert r.code == 0, r.out
    assert (r.json()["cloned"], r.json()["source"]) == (False, "cloned")
    assert row()["source"] == "cloned"  # type: ignore[index]


def test_destination_with_a_clone_of_another_repo_is_path_exists(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = clone_cfg(w, w.bare())
    w.stub(dest, "https://example.test/org/other.git")
    r = w.cli(capsys, "clone", REPO)
    assert (r.code, r.json()["error"]["reason"]) == (20, "path_exists")


def test_destination_with_a_headless_clone_is_path_exists(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = clone_cfg(w, w.bare())
    w.stub(dest, str(w.tmp / "remote.git"))  # right remote, but no commit: HEAD is unborn
    r = w.cli(capsys, "clone", REPO)
    assert (r.code, r.json()["error"]["reason"]) == (20, "path_exists")


def test_a_failed_clone_leaves_no_partial_directory(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = clone_cfg(w, w.tmp / "does-not-exist.git")
    r = w.cli(capsys, "clone", REPO)
    assert (r.code, r.json()["error"]["code"]) == (12, "clone_failed")
    assert not dest.exists() and no_tmp_dirs(dest.parent)
    assert row() is None


def test_leftover_temp_directories_are_never_reused(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = clone_cfg(w, w.bare())
    dest.parent.mkdir(parents=True)
    left = dest.parent / "repo.wtctl-tmp-deadbeef"
    left.mkdir()
    (left / "junk").write_text("x")
    r = w.cli(capsys, "clone", REPO)
    assert r.code == 0
    assert (left / "junk").exists() and not (dest / "junk").exists()


def test_a_symlinked_parent_directory_is_refused(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = clone_cfg(w, w.bare())
    elsewhere = w.tmp / "elsewhere"
    elsewhere.mkdir()
    dest.parent.parent.mkdir(parents=True)
    os.symlink(elsewhere, dest.parent)  # clones/example.test/org -> elsewhere
    r = w.cli(capsys, "clone", REPO)
    assert (r.code, r.json()["error"]["code"]) == (12, "clone_failed")
    assert list(elsewhere.iterdir()) == []


def test_clone_url_scheme_problems_are_remote_mismatch(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = w.tmp / "pwned"
    bads = (f"ext::sh -c 'touch {marker}'", "git://example.test/org/repo", "-oProxy=x", "rel/p")
    for bad in bads:
        w.repos[REPO] = {"clone_url": bad}
        r = w.cli(capsys, "clone", REPO)
        assert (r.code, r.json()["error"]["code"]) == (12, "remote_mismatch"), bad
    assert not marker.exists() and not (w.tmp / "wt").exists()


def test_a_template_that_does_not_match_the_repo_is_refused(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    w.top["hosts"] = {"example.test": {"clone_url": "https://evil.test/{path}.git"}}
    r = w.cli(capsys, "clone", REPO)
    assert (r.code, r.json()["error"]["code"]) == (12, "remote_mismatch")


def test_file_url_clone_url_is_cloned_by_plain_path(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = w.bare()
    w.repos[REPO] = {"clone_url": f"file://{bare}"}
    r = w.cli(capsys, "clone", REPO)
    assert r.code == 0, r.out
    assert r.json()["fetch_url"] == str(bare)


def test_clone_from_a_hostile_source_stays_inert(
    w: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hooks and config a remote carries are not transferred or run by the clone."""
    bare = w.bare()
    marker = w.tmp / "pwned"
    hook = bare / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    g(["config", "core.fsmonitor", f"touch {marker}"], bare, w.home)
    dest = clone_cfg(w, bare)
    r = w.cli(capsys, "clone", REPO)
    assert r.code == 0, r.out
    assert not marker.exists()
    assert not (dest / ".git" / "hooks" / "post-checkout").exists()
    assert "fsmonitor" not in (dest / ".git" / "config").read_text()


# -- failures classified from git's result (fake runner) --------------------------------------


def fake_runner(result: GitResult, seen: list[list[str]]):  # type: ignore[no-untyped-def]
    def run(argv: list[str], **kw: Any) -> GitResult:
        seen.append(argv)
        if "clone" in argv:
            target = argv[-1]
            os.makedirs(os.path.join(target, "partial"))
            kw_env = kw.get("env", {})
            seen.append([kw_env.get("GIT_ALLOW_PROTOCOL", "")])
            return result
        return Gateway().git(argv, **kw)

    return run


@pytest.mark.parametrize(
    ("result", "code", "details"),
    [
        (GitResult(128, b"", b"fatal: could not read Username for x"), "auth_required", None),
        (GitResult(128, b"", b"Permission denied (publickey)."), "auth_required", None),
        (GitResult(128, b"", b"fatal: repository not found"), "clone_failed", None),
        (GitResult(-9, b"", b"partial", timed_out=True), "clone_failed", {"cause": "timeout"}),
    ],
)
def test_clone_failures_are_classified_and_clean_up(
    w: World, result: GitResult, code: str, details: dict[str, str] | None
) -> None:
    dest = clone_cfg(w, w.bare())
    seen: list[list[str]] = []
    res = make_resolver(w, runner=fake_runner(result, seen))
    with pytest.raises(WtctlError) as exc:
        res.clone(REPO)
    assert exc.value.code == code
    assert exc.value.fields.get("details") == details
    assert not dest.exists() and no_tmp_dirs(dest.parent)
    assert ["file"] in seen  # protocol allowlist: exactly the scheme in use


def test_clone_uses_the_timeout_and_a_network_scheme(w: World) -> None:
    clone_cfg(w, w.bare())
    w.top["clone_timeout"] = "7m"
    captured: dict[str, Any] = {}

    def runner(argv: list[str], **kw: Any) -> GitResult:
        if "clone" in argv:
            captured.update(argv=argv, **kw)
            return GitResult(128, b"", b"fatal: x")
        return Gateway().git(argv, **kw)

    with pytest.raises(WtctlError):
        make_resolver(w, runner=runner).clone(REPO)
    assert captured["timeout"] == 420
    argv = captured["argv"]
    assert "--template=" in argv and "--" in argv and "protocol.version=2" in argv
    assert argv.index("--") < argv.index(str(w.tmp / "remote.git"))
    assert captured["env"]["GIT_ALLOW_PROTOCOL"] == "file"
    assert captured["env"]["GIT_CEILING_DIRECTORIES"]
    assert "GIT_DIR" not in captured["env"]


def test_https_clone_url_allows_only_https(w: World) -> None:
    captured: dict[str, Any] = {}

    def runner(argv: list[str], **kw: Any) -> GitResult:
        if "clone" in argv:
            captured.update(argv=argv, **kw)
            return GitResult(128, b"", b"fatal: x")
        return Gateway().git(argv, **kw)

    with pytest.raises(WtctlError):
        make_resolver(w, runner=runner).clone(REPO)
    assert captured["env"]["GIT_ALLOW_PROTOCOL"] == "https"
    assert captured["argv"][-2] == URL
    tmp_prefix = str(w.tmp / "wt/clones/example.test/org/repo.wtctl-tmp-")
    assert captured["argv"][-1].startswith(tmp_prefix)


# -- concurrency ---------------------------------------------------------------------------------


def test_concurrent_auto_clones_clone_once(w: World) -> None:
    bare = w.bare()
    dest = clone_cfg(w, bare)
    w.top["auto_clone"] = True
    cfg = w.config()
    env = {
        k: v
        for k, v in os.environ.items()
        if k in ("PATH", "HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")
    }
    cmd = [sys.executable, "-m", "wtctl", "--config", cfg, "--json", "locate", REPO]
    procs = [
        subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(3)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, (out, err)
        outs.append(json.loads(out))
    assert {o["clone_path"] for o in outs} == {str(dest)}
    assert (dest / "f.txt").exists() and no_tmp_dirs(dest.parent)
    st = row()
    assert st is not None and st["source"] == "cloned"


def test_second_resolver_waiting_for_the_lease_reuses_the_clone(w: World) -> None:
    """The re-check under the lease: a clone made meanwhile is reused, not cloned over."""
    bare = w.bare()
    dest = clone_cfg(w, bare)
    res = make_resolver(w)
    first = res.clone(REPO)
    assert first.cloned
    again = make_resolver(w).clone(REPO)
    assert not again.cloned and again.clone_path == str(dest)


# -- misc ----------------------------------------------------------------------------------------


def test_default_branch_comes_from_the_row(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    w.cli(capsys, "locate", REPO)
    st = State(state_path(), Gateway())
    st.put_clone(REPO, default_branch="main")
    st.close()
    assert w.cli(capsys, "locate", REPO).json()["default_branch"] == "main"


def test_multiple_repos_keep_separate_rows(w: World, capsys: pytest.CaptureFixture[str]) -> None:
    other = "example.test/org/other"
    a = w.stub(w.tmp / "a")
    b = w.stub(w.tmp / "b", "https://example.test/org/other.git")
    w.repos[REPO] = {"clone": str(a)}
    w.repos[other] = {"clone": str(b)}
    w.cli(capsys, "locate", REPO)
    w.cli(capsys, "locate", other)
    assert row(REPO)["path"] == str(a) and row(other)["path"] == str(b)  # type: ignore[index]


# -- races between checking and using ------------------------------------------------------------


def test_a_clone_swapped_while_it_is_verified_is_refused(
    w: World, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = w.stub(w.tmp / "mine")
    w.repos[REPO] = {"clone": str(clone)}
    real_open = locate_mod.open_dir_nofollow
    calls: list[str] = []

    def swapping_open(path: str) -> int:
        calls.append(path)
        if path.endswith("/mine/.git") and len(calls) == 1:
            os.rename(clone / ".git", w.tmp / "old-git")  # the attacker swaps the directory
            os.mkdir(clone / ".git")
        return real_open(path)

    monkeypatch.setattr(locate_mod, "open_dir_nofollow", swapping_open)
    r = w.cli(capsys, "locate", REPO)
    assert (r.code, r.json()["error"]["code"]) == (12, "remote_mismatch")
    assert "changed while" in r.json()["error"]["message"]
    assert row() is None


def test_discovery_walk_refuses_a_directory_swapped_for_a_symlink(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between listing a directory and opening it, the entry becomes a symlink: not entered."""
    search = w.tmp / "search"
    (search / "a").mkdir(parents=True)
    w.stub(w.tmp / "outside" / "repo")
    real_stat = os.stat

    def swapping_stat(path: Any, *a: Any, **k: Any) -> os.stat_result:
        st = real_stat(path, *a, **k)
        if path == "a" and k.get("dir_fd") is not None and (search / "a").is_dir():
            os.rmdir(search / "a")
            os.symlink(w.tmp / "outside", search / "a")
        return st

    monkeypatch.setattr(os, "stat", swapping_stat)
    budget = locate_mod._Budget(100, float("inf"))
    found = list(locate_mod.walk_for_clones(str(search), 5, budget, Gateway().clock))
    assert found == []
