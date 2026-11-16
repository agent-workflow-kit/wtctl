"""The live scenarios, run in CI against a local bare repository (no network, no forge).

These prove the logic of the scenarios and the shapes of the CLI output they read; what only a
real forge can show (server behavior, credentials, ssh) is the live tests' job.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.live import scenarios as sc
from tests.live.harness import LiveWorld

ROOT = Path(__file__).resolve().parents[1]

GENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
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


def test_unadvertised_sha_skips_when_every_candidate_is_advertised(world: LiveWorld) -> None:
    remote = Path(world.fetch_url)
    for i, sha in enumerate(git(remote, "rev-list", "trunk").split()):
        git(remote, "branch", f"b{i}", sha)
    with pytest.raises(sc.NoUnadvertisedCommit, match="all advertised"):
        sc.unadvertised_sha(world, skip=5)


def test_unadvertised_sha_never_picks_an_advertised_commit(world: LiveWorld) -> None:
    remote = Path(world.fetch_url)
    shas = git(remote, "rev-list", "trunk").split()
    git(remote, "branch", "pinned", shas[5])  # the first candidate is a branch tip
    old = sc.unadvertised_sha(world, skip=5)
    assert old != shas[5] and old in shas


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


def test_the_mirror_check_fails_for_a_mirror_that_is_not_partial(world: LiveWorld) -> None:
    sc.mirror_flow(world)
    (mirror,) = (world.home / ".local" / "state" / "wtctl" / "mirrors").glob("*.git")
    git(mirror, "config", "--unset", "extensions.partialclone")
    with pytest.raises(AssertionError, match="partial clone"):
        sc.assert_mirror_is_treeless(world)


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


def test_push_records_the_branch_and_ref_before_the_first_push(world: LiveWorld) -> None:
    recorded: dict[str, object] = {}
    branch = sc.push_fast_forward(world, 1, "wtctl-live-", lambda k, v: recorded.update({k: v}))
    assert recorded == {"push_branch": branch, "push_ref": f"refs/heads/{branch}"}


def test_push_never_deletes_a_branch_that_already_exists(
    world: LiveWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sc.secrets, "token_hex", lambda _n: "0123456789ab")
    remote = Path(world.fetch_url)
    git(remote, "branch", "wtctl-live-0123456789ab", "trunk")
    before = git(remote, "rev-parse", "wtctl-live-0123456789ab")
    with pytest.raises(AssertionError, match="already exists"):
        sc.push_fast_forward(world, 1, "wtctl-live-")
    assert git(remote, "rev-parse", "wtctl-live-0123456789ab") == before


def test_a_failed_delete_names_the_branch_and_keeps_the_original_failure(
    world: LiveWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl.gateway import GitResult

    real = world.net_git
    monkeypatch.setattr(sc.secrets, "token_hex", lambda _n: "0123456789ab")

    def flaky(args: list[str], **kw: object):  # noqa: ANN202
        if args[0] == "push" and args[-1].startswith(":"):
            return GitResult(1, b"", b"remote: permission denied")
        res = real(args, **kw)  # type: ignore[arg-type]
        if args[0] == "push":
            raise AssertionError("boom after the push")
        return res

    monkeypatch.setattr(world, "net_git", flaky)
    with pytest.raises(AssertionError, match="boom after the push") as e:
        sc.push_fast_forward(world, 1, "wtctl-live-")
    notes = "\n".join(getattr(e.value, "__notes__", []))
    assert "wtctl-live-0123456789ab" in notes and "permission denied" in notes
    remote = Path(world.fetch_url)
    assert "wtctl-live-0123456789ab" in git(remote, "branch", "--list", "--format=%(refname:short)")


def test_a_failed_delete_after_success_names_the_branch(
    world: LiveWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl.gateway import GitResult

    real = world.net_git

    def flaky(args: list[str], **kw: object):  # noqa: ANN202
        if args[0] == "push" and args[-1].startswith(":"):
            return GitResult(1, b"", b"remote: nope")
        return real(args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(world, "net_git", flaky)
    with pytest.raises(AssertionError, match=r"could not delete the disposable branch wtctl-live-"):
        sc.push_fast_forward(world, 1, "wtctl-live-")


@pytest.mark.parametrize(
    "prefix",
    ["main", ":", "+", "*", "/", "//", ".lock", "a\n", "", "-x", "..", "a//b", "a/.b", "a.lock/",
     "*.lock", "refs/heads/x", "HEAD", "release/", "trunk"],
)  # fmt: skip
def test_a_hostile_prefix_never_reaches_the_remote(world: LiveWorld, prefix: str) -> None:
    with pytest.raises(ValueError):
        sc.push_fast_forward(world, 1, prefix)
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
     ("-bad", "abcdef12", "main"), ("a b", "abcdef12", "main"), ("main", "abcdef12", "x"),
     ("a//b", "abcdef12", "x"), ("a/.b", "abcdef12", "x"), ("a.lock/", "abcdef12", "x"),
     ("refs/heads/x", "abcdef12", "x"), ("release/", "abcdef12", "x")],
)  # fmt: skip
def test_safe_branch_refuses_dangerous_names(prefix: str, suffix: str, default: str) -> None:
    with pytest.raises(ValueError):
        sc.safe_branch(prefix, suffix, default)


def test_safe_branch_builds_the_name() -> None:
    assert sc.safe_branch("wtctl-live-", "0123456789ab", "main") == "wtctl-live-0123456789ab"


def _scan_world(tmp_path: Path, **kw: object) -> LiveWorld:
    return LiveWorld(
        tmp_path / "w",
        host="forge.example",
        project="o/r",
        tokens={"T": "tok-1234567"},
        **kw,  # type: ignore[arg-type]
    )


def test_secret_scan_finds_a_planted_token(tmp_path: Path) -> None:
    w = _scan_world(tmp_path)
    (w.base / "leak.txt").write_text("xx tok-1234567 yy")
    with pytest.raises(AssertionError, match="leak.txt"):
        sc.assert_secret_absent(w)


@pytest.mark.parametrize("user", ["x-access-token", "oauth2", "ab"])
@pytest.mark.parametrize("strip", [False, True])
def test_secret_scan_finds_a_planted_basic_header(tmp_path: Path, user: str, strip: bool) -> None:
    w = _scan_world(tmp_path, token_user=user)
    payload = base64.b64encode(f"{user}:tok-1234567".encode()).decode()
    payload = payload.rstrip("=") if strip else payload
    (w.base / "cfg").mkdir(parents=True)
    (w.base / "cfg" / "config").write_text(
        f"[http]\n\textraHeader = Authorization: Basic {payload}\n"
    )
    with pytest.raises(AssertionError, match="cfg/config"):
        sc.assert_secret_absent(w)


def test_secret_scan_finds_a_plain_base64_of_the_token(tmp_path: Path) -> None:
    w = _scan_world(tmp_path)
    (w.base / "x").write_bytes(base64.b64encode(b"tok-1234567"))
    with pytest.raises(AssertionError, match="x"):
        sc.assert_secret_absent(w)


def test_secret_scan_is_quiet_for_a_clean_world(tmp_path: Path) -> None:
    w = _scan_world(tmp_path)
    (w.base / "ok.txt").write_text("nothing here")
    sc.assert_secret_absent(w)


def test_secret_scan_includes_the_output_of_a_token_command(tmp_path: Path) -> None:
    w = LiveWorld(
        tmp_path / "w", host="forge.example", project="o/r",
        token_command=["sh", "-c", "printf cmd-token-9876543"],
    )  # fmt: skip
    sc.assert_secret_absent(w)  # runs the command; nothing planted yet
    (w.base / "leak").write_text("cmd-token-9876543")
    with pytest.raises(AssertionError, match="leak"):
        sc.assert_secret_absent(w)


SHOWLOCALS_TEST = """
from pathlib import Path
from tests.live import scenarios as sc
from tests.live.harness import LiveWorld

SECRET = "tok-" + "ABCDEFG1234567"


def make(tmp_path):
    w = LiveWorld(tmp_path / "w", host="forge.example", project="o/r", tokens={"T": SECRET})
    (w.base / "leak.txt").write_text(SECRET)
    return w


def test_leak(tmp_path):
    w = make(tmp_path)
    sc.assert_secret_absent(w)
"""


def test_a_failing_secret_scan_shows_no_secret_under_showlocals(tmp_path: Path) -> None:
    (tmp_path / "test_leak.py").write_text(SHOWLOCALS_TEST)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-l", "-q", "--tb=long",
         str(tmp_path / "test_leak.py")],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": str(ROOT),
             "LC_ALL": "C"},
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    out = r.stdout + r.stderr
    assert "1 failed" in out, out
    assert "leak.txt" in out
    assert "ABCDEFG1234567" not in out
