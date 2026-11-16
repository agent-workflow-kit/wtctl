"""Anonymous reads of a public repository on each forge (see docs/live-testing.md).

Run: ``WTCTL_LIVE_GITHUB_PUBLIC_REPO=owner/name make live`` (and the GitLab twin).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live import liveenv, worlds
from tests.live import scenarios as sc
from tests.live.harness import Record
from tests.live.liveenv import FORGES

pytestmark = pytest.mark.live
needs = pytest.mark.needs

FORGE_PARAMS = [pytest.param(key, id=key, marks=needs(f"{key}:public")) for key in sorted(FORGES)]


@pytest.mark.parametrize("key", FORGE_PARAMS)
@pytest.mark.parametrize("depth", [1, 50])
def test_fetch_the_tip_by_sha(key: str, depth: int, tmp_path: Path) -> None:
    """A1/A6: an anonymous fetch of a full sha, shallow and deeper, with no ref fetched."""
    w = worlds.public_world(tmp_path, FORGES[key])
    sc.tip_by_sha(w, depth)


@pytest.mark.parametrize("key", FORGE_PARAMS)
def test_fetch_an_unadvertised_sha(key: str, tmp_path: Path, record_property: Record) -> None:
    """A commit that no advertised ref points at, asked for by sha (``allowAnySHA1InWant`` differs
    per forge). Skipped, with the reason recorded, when every candidate is advertised."""
    w = worlds.public_world(tmp_path, FORGES[key])
    try:
        sc.unadvertised_sha(w)
    except sc.NoUnadvertisedCommit as e:
        record_property("unadvertised_outcome", f"skipped: {e}")
        pytest.skip(str(e))
    record_property("unadvertised_outcome", "fetched")


@pytest.mark.parametrize("key", FORGE_PARAMS)
def test_strict_fsck_accepts_legitimate_old_history(key: str, tmp_path: Path) -> None:
    """``fetch.fsckObjects=true`` must not refuse the real full history of a long-lived
    repository (git's fsck reports old-style commits and trees as warnings only). Fetches the
    whole history of the public repository."""
    w = worlds.public_world(tmp_path, FORGES[key])
    sc.full_history_strict(w)


@pytest.mark.parametrize("key", FORGE_PARAMS)
def test_port_443_in_the_url_matches_the_bare_host(key: str, tmp_path: Path) -> None:
    """A ``:443`` URL (which wtctl normalizes to the bare host) is fetched anonymously by real git.
    No token is configured here: the token-header half is in test_live_private.py."""
    forge = FORGES[key]
    project = liveenv.project_path(forge, "PUBLIC_REPO")
    w = worlds.public_world(tmp_path, forge, clone_url=f"https://{forge.host}:443/{project}.git")
    sc.tip_by_sha(w, 1)


@pytest.mark.parametrize("key", FORGE_PARAMS)
def test_a_redirecting_url_is_not_followed_blindly(
    key: str, tmp_path: Path, record_property: Record
) -> None:
    """``http.followRedirects=false``: a URL without ``.git`` (the forges redirect it) is
    fetched directly or refused with exit 12, never hung on. The outcome is recorded for the
    checklist; that no token leaks is not checked here (the world has none)."""
    forge = FORGES[key]
    project = liveenv.project_path(forge, "PUBLIC_REPO")
    w = worlds.public_world(tmp_path / "ref", forge)
    sha = w.tip("HEAD")
    r = worlds.public_world(tmp_path / "redir", forge, clone_url=f"https://{forge.host}/{project}")
    record_property("redirect_outcome", sc.redirect_outcome(r, sha))


@needs("bin:ssh")
@pytest.mark.parametrize("key", FORGE_PARAMS)
def test_ssh_never_prompts(key: str, tmp_path: Path, record_property: Record) -> None:
    """Real ssh in batch mode: no key means ``auth_required`` at once, not a hung prompt. ssh
    reads the maintainer's real identity and ``known_hosts`` (a fetch only, nothing is written).
    ``WTCTL_LIVE_SSH_EXPECT`` pins the expected outcome (run it once with no usable key and
    once with a passphrase-locked key and no agent, both ``auth_required``)."""
    forge = FORGES[key]
    project = liveenv.project_path(forge, "PUBLIC_REPO")
    sha = worlds.public_world(tmp_path / "ref", forge).tip("HEAD")
    w = worlds.public_world(
        tmp_path / "ssh", forge, clone_url=f"ssh://git@{forge.host}/{project}.git"
    )
    expect = liveenv.getenv("WTCTL_LIVE_SSH_EXPECT")
    assert expect in (None, "ok", "auth_required"), "WTCTL_LIVE_SSH_EXPECT: ok or auth_required"
    record_property("ssh_outcome", sc.ssh_batchmode(w, sha, expect))


FORK_PARAMS = [pytest.param(key, id=key, marks=needs(f"{key}:fork")) for key in sorted(FORGES)]


@pytest.mark.parametrize("key", FORK_PARAMS)
def test_a_fork_request_head_is_fetched_by_sha(key: str, tmp_path: Path) -> None:
    """A pull or merge request head from a fork, on no branch, fetched by its sha."""
    forge = FORGES[key]
    w = worlds.public_world(tmp_path, forge)
    sc.fetch_sha(w, liveenv.getenv(forge.var("FORK_PR_HEAD")) or "")


@pytest.mark.parametrize("key", FORK_PARAMS)
def test_a_fork_only_commit_is_observed(key: str, tmp_path: Path, record_property: Record) -> None:
    """A commit in a fork and in no PR or MR head: the outcome (fetched or ``ref_not_found``)
    is recorded, not asserted; it differs per forge."""
    forge = FORGES[key]
    sha = liveenv.getenv(forge.var("FORK_ONLY_SHA"))
    if sha is None or not liveenv.valid_sha(sha):
        pytest.skip(f"set {forge.var('FORK_ONLY_SHA')} to a full 40-hex sha")
    record_property("fork_only_outcome", sc.observe_sha(worlds.public_world(tmp_path, forge), sha))
