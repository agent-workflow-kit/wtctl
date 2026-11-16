"""Builders of a :class:`LiveWorld` for each kind of live target, from the environment contract.

Each builder assumes its prerequisite already passed (``liveenv.unmet``) and raises ``KeyError`` or
``ValueError`` otherwise, so a test that forgot its ``needs`` marker fails loudly instead of
reaching a real forge with a half-built configuration.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from tests.live import liveenv
from tests.live.harness import LiveWorld
from tests.live.liveenv import Forge

BOGUS_TOKEN = "invalid-" + "0" * 32  # obviously not a credential; the forge must refuse it


def _project(forge: Forge, suffix: str, environ: Mapping[str, str] | None) -> str:
    path = liveenv.project_path(forge, suffix, environ)
    if path is None:
        raise KeyError(f"{forge.var(suffix)} is unset or malformed")
    return path


def public_world(
    base: Path,
    forge: Forge,
    *,
    clone_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> LiveWorld:
    """The public repository, read anonymously: no token configured, none in the environment."""
    return LiveWorld(
        base,
        host=forge.host,
        project=_project(forge, "PUBLIC_REPO", environ),
        clone_url=clone_url,
    )


def private_world(
    base: Path,
    forge: Forge,
    *,
    mode: str = "token_env",
    clone_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> LiveWorld:
    """The private repository. ``mode``: ``token_env`` (the maintainer's token), ``none`` (no
    token configured), ``bad`` (the same variable holds a made-up value) or ``command`` (a
    ``token_command`` through the forge's own CLI)."""
    project = _project(forge, "PRIVATE_REPO", environ)
    name = liveenv.token_env_name(forge.var("TOKEN_ENV"), environ)
    if mode == "none":
        return LiveWorld(base, host=forge.host, project=project, clone_url=clone_url)
    if mode == "command":
        argv = liveenv.token_command(forge, environ)
        if argv is None:
            raise KeyError(f"{forge.var('TOKEN_COMMAND')} is unset or malformed")
        cfg_dir = liveenv.config_dir(forge, environ)
        extra = {forge.config_dir_var: cfg_dir} if cfg_dir else {}
        return LiveWorld(
            base, host=forge.host, project=project, clone_url=clone_url,
            token_command=argv, child_env=extra,
        )  # fmt: skip
    if name is None:
        raise KeyError(f"{forge.var('TOKEN_ENV')} is unset or malformed")
    if mode == "bad":
        tokens = {name: BOGUS_TOKEN}
    elif mode == "token_env":
        value = liveenv.token_value(forge.var("TOKEN_ENV"), environ)
        if value is None:
            raise KeyError(f"the variable named by {forge.var('TOKEN_ENV')} is unset")
        tokens = {name: value}
    else:
        raise ValueError(mode)
    return LiveWorld(
        base, host=forge.host, project=project, clone_url=clone_url,
        token_env=name, tokens=tokens,
    )  # fmt: skip


def push_world(base: Path, forge: Forge, *, environ: Mapping[str, str] | None = None) -> LiveWorld:
    """The disposable repository with the write token."""
    name = liveenv.token_env_name(forge.var("PUSH_TOKEN_ENV"), environ)
    value = liveenv.token_value(forge.var("PUSH_TOKEN_ENV"), environ)
    if name is None or value is None:
        raise KeyError(f"{forge.var('PUSH_TOKEN_ENV')} is unset or names an unset variable")
    return LiveWorld(
        base, host=forge.host, project=_project(forge, "PUSH_REPO", environ),
        token_env=name, tokens={name: value},
    )  # fmt: skip


def lfs_world(base: Path, *, environ: Mapping[str, str] | None = None) -> LiveWorld:
    """The public git-lfs repository. No token: the smudge during ``git worktree add`` is
    anonymous, and the host is one of the two forges (``valid_repo_id``)."""
    repo = liveenv.getenv("WTCTL_LIVE_LFS_REPO", environ)
    if repo is None or not liveenv.valid_repo_id(repo):
        raise KeyError("WTCTL_LIVE_LFS_REPO is unset or malformed")
    host, _, project = repo.partition("/")
    return LiveWorld(base, host=host, project=project, allow_filters=["lfs"])
