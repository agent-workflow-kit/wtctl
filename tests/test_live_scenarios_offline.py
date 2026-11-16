"""The live scenarios, run in CI against a local bare repository (no network, no forge).

These prove the logic of the scenarios and the shapes of the CLI output they read; what only a
real forge can show (server behavior, credentials, ssh) is the live tests' job.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.live import scenarios as sc
from tests.live.harness import LiveWorld

GENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**GENV, "HOME": str(cwd)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


@pytest.fixture
def world(tmp_path: Path) -> LiveWorld:
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "trunk")
    for i in range(12):
        (work / f"f{i}.txt").write_text(f"{i}\n")
        git(work, "add", ".")
        git(work, "commit", "-q", "-m", f"c{i}")
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(work), str(remote))
    git(remote, "config", "uploadpack.allowFilter", "true")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/trunk")
    return LiveWorld(
        tmp_path / "w", host="forge.example", project="org/repo", clone_url=str(remote)
    )


def test_tip_by_sha(world: LiveWorld) -> None:
    assert sc.tip_by_sha(world, depth=1) == world.tip("HEAD")


def test_unadvertised_sha_is_a_non_tip_commit(world: LiveWorld) -> None:
    old = sc.unadvertised_sha(world, skip=5)
    assert old != world.tip("HEAD") and sc.SHA_RE.fullmatch(old)


def test_unadvertised_sha_needs_enough_history(world: LiveWorld) -> None:
    with pytest.raises(AssertionError, match="more than 21 commits"):
        sc.unadvertised_sha(world, skip=20)


def test_full_history_strict(world: LiveWorld) -> None:
    sc.full_history_strict(world)


def test_fetch_sha_and_observe(world: LiveWorld) -> None:
    sc.fetch_sha(world, world.tip("HEAD"))
    assert sc.observe_sha(world, world.tip("HEAD")) == "fetched"
    assert sc.observe_sha(world, "1" * 40) == "ref_not_found"


def test_mirror_flow(world: LiveWorld) -> None:
    got = sc.mirror_flow(world)
    assert got["default_branch"] == "trunk"


@pytest.mark.parametrize("depth", [50, 1])
def test_push_fast_forward_creates_and_deletes_its_branch(world: LiveWorld, depth: int) -> None:
    branch = sc.push_fast_forward(world, depth, "wtctl-live-")
    assert branch.startswith("wtctl-live-")
    remote = Path(world.fetch_url)
    assert git(remote, "branch", "--list", "--format=%(refname:short)") == "trunk"


def test_push_deletes_its_branch_when_a_step_fails(
    world: LiveWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = world.net_git
    seen: list[str] = []

    def failing(args: list[str], **kw: object):  # noqa: ANN202
        seen.append(args[0])
        if args[0] == "push" and not args[-1].startswith(":"):
            raise AssertionError("boom")
        return real(args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(world, "net_git", failing)
    with pytest.raises(AssertionError, match="boom"):
        sc.push_fast_forward(world, 1, "wtctl-live-")
    assert seen[-1] == "push"  # the deletion ran after the failure
    remote = Path(world.fetch_url)
    assert git(remote, "branch", "--list", "--format=%(refname:short)") == "trunk"


def test_the_throwaway_is_removed_when_the_body_fails(world: LiveWorld) -> None:
    paths: list[Path] = []
    with pytest.raises(RuntimeError), sc.throwaway(world, world.tip("HEAD"), depth=1) as rec:
        paths.append(Path(rec["path"]))
        raise RuntimeError("body failed")
    assert paths and not paths[0].exists()


@pytest.mark.parametrize(
    ("prefix", "suffix", "default"),
    [("", "abcdef12", "main"), ("wtctl-live-", "zz", "main"), ("x", "abcdef12", "xabcdef12"),
     ("-bad", "abcdef12", "main"), ("a b", "abcdef12", "main")],
)  # fmt: skip
def test_safe_branch_refuses_dangerous_names(prefix: str, suffix: str, default: str) -> None:
    with pytest.raises(ValueError):
        sc.safe_branch(prefix, suffix, default)


def test_safe_branch_builds_the_name() -> None:
    assert sc.safe_branch("wtctl-live-", "0123456789ab", "main") == "wtctl-live-0123456789ab"


def test_secret_scan_finds_a_planted_token(tmp_path: Path) -> None:
    w = LiveWorld(tmp_path / "w", host="forge.example", project="o/r", tokens={"T": "tok-1234567"})
    (w.base / "leak.txt").write_text("xx tok-1234567 yy")
    with pytest.raises(AssertionError, match="leak.txt"):
        sc.assert_secret_absent(w)
