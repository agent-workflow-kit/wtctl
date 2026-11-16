"""Credentials against a private repository on each forge (see docs/live-testing.md).

The token comes from the variable NAMED by ``WTCTL_LIVE_<FORGE>_TOKEN_ENV``; it goes to the child
process environment only, and every string a test sees is scrubbed of it.
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

PRIVATE = [pytest.param(k, id=k, marks=needs(f"{k}:private")) for k in sorted(FORGES)]
COMMAND = [pytest.param(k, id=k, marks=needs(f"{k}:token_command")) for k in sorted(FORGES)]


@pytest.mark.parametrize("key", PRIVATE)
def test_token_env_header_reaches_a_private_repository(key: str, tmp_path: Path) -> None:
    """D11/D33: the URL-scoped header with the default ``token_user`` is accepted, a throwaway is
    created, and the token is persisted nowhere (mirror, config, FETCH_HEAD, state)."""
    w = worlds.private_world(tmp_path, FORGES[key])
    sc.token_fetch(w)


@pytest.mark.parametrize("key", PRIVATE)
def test_a_private_repository_without_a_token_is_auth_required(key: str, tmp_path: Path) -> None:
    """No token means ``auth_required``, with no prompt and no hang."""
    sha = worlds.private_world(tmp_path / "ref", FORGES[key]).tip("HEAD")
    sc.no_token_is_refused(worlds.private_world(tmp_path / "anon", FORGES[key], mode="none"), sha)


@pytest.mark.parametrize("key", PRIVATE)
def test_a_rejected_token_says_so_and_does_not_fall_back(
    key: str, tmp_path: Path, record_property: Record
) -> None:
    """A7: a made-up token is refused with ``auth_required`` and the message says the configured
    token was rejected, not that none was given. (Whether anonymous access was retried is not
    checked.)"""
    sha = worlds.private_world(tmp_path / "ref", FORGES[key]).tip("HEAD")
    w = worlds.private_world(tmp_path / "bad", FORGES[key], mode="bad")
    record_property("message", sc.bad_token_message(w, sha))


@pytest.mark.parametrize("key", PRIVATE)
def test_port_443_still_gets_the_token(key: str, tmp_path: Path) -> None:
    """The header is scoped to the bare host; the private repository is read through a ``:443``
    URL, so real git matched it."""
    forge = FORGES[key]
    project = liveenv.project_path(forge, "PRIVATE_REPO")
    w = worlds.private_world(tmp_path, forge, clone_url=f"https://{forge.host}:443/{project}.git")
    sc.token_fetch(w)


@pytest.mark.parametrize("key", COMMAND)
def test_token_command_with_the_forge_cli(key: str, tmp_path: Path) -> None:
    """``token_command`` running the forge's own CLI (``gh`` or ``glab``, ``auth token`` only)
    yields a token the forge accepts. The command runs with only HOME and the config directory
    (no keyring, ``GH_TOKEN`` or session bus); one that prints nothing there skips the test."""
    forge = FORGES[key]
    w = worlds.private_world(tmp_path, forge, mode="command")
    try:
        sc.token_command_fetch(w, forge.var("CONFIG_DIR"))
    except sc.TokenCommandSilent as e:
        pytest.skip(str(e))
