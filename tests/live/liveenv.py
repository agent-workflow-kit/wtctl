# ruff: noqa: E501  (the variable descriptions are prose data)
"""The environment contract of the live suite: variable names, parsing and prerequisites.

Everything a live test needs from outside comes from ``WTCTL_LIVE_*`` variables, read from a
snapshot of ``os.environ`` taken at import (the shared test fixtures clear ``WTCTL_*`` variables
and move HOME before a test body runs). A variable that names a token (``*_TOKEN_ENV``) holds the
NAME of another variable; the token itself is read only by :func:`token_value` and never
printed, logged or put in argv.

No network access happens here: this module only decides whether a test may run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass

ENV_SNAPSHOT: Mapping[str, str] = dict(os.environ)
PATH_SNAPSHOT = ENV_SNAPSHOT.get("PATH", "/usr/bin:/bin")
REAL_HOME = ENV_SNAPSHOT.get("HOME", "")

DEFAULT_PUSH_PREFIX = "wtctl-live-"
DEFAULT_GITHUB_TOKEN_COMMAND = ("gh", "auth", "token", "--hostname", "{host}")


@dataclass(frozen=True)
class Forge:
    key: str  # "github" or "gitlab"
    host: str
    cli: str  # the forge's own CLI, used by the token_command tests
    config_dir_var: str  # the CLI's config directory variable
    config_subdir: str  # the CLI's directory under XDG_CONFIG_HOME
    default_token_command: tuple[str, ...] | None

    @property
    def upper(self) -> str:
        return self.key.upper()

    def var(self, suffix: str) -> str:
        return f"WTCTL_LIVE_{self.upper}_{suffix}"


GITHUB = Forge("github", "github.com", "gh", "GH_CONFIG_DIR", "gh", DEFAULT_GITHUB_TOKEN_COMMAND)
GITLAB = Forge("gitlab", "gitlab.com", "glab", "GLAB_CONFIG_DIR", "glab-cli", None)
FORGES: dict[str, Forge] = {GITHUB.key: GITHUB, GITLAB.key: GITLAB}


@dataclass(frozen=True)
class EnvVar:
    name: str
    doc: str


def _forge_vars() -> list[EnvVar]:
    out: list[EnvVar] = []
    for f in FORGES.values():
        out += [
            EnvVar(
                f.var("PUBLIC_REPO"),
                f"project path of a small, stable PUBLIC repository on {f.host} with at least 8 commits and years of history",
            ),
            EnvVar(
                f.var("PRIVATE_REPO"),
                f"project path of a PRIVATE repository on {f.host} that the token can read",
            ),
            EnvVar(
                f.var("TOKEN_ENV"),
                f"NAME of the variable holding a read token for {f.host} (private-repository tests)",
            ),
            EnvVar(
                f.var("PUSH_REPO"),
                f"project path of a disposable repository on {f.host} the push token may create and delete branches in",
            ),
            EnvVar(
                f.var("PUSH_TOKEN_ENV"),
                f"NAME of the variable holding a write token for {f.host} (push tests)",
            ),
            EnvVar(
                f.var("FORK_PR_HEAD"),
                f"full sha of a {'pull' if f.key == 'github' else 'merge'} request head from a fork of the public repository, on no branch",
            ),
            EnvVar(
                f.var("FORK_ONLY_SHA"),
                "optional: full sha of a commit present only in a fork of the public repository (observed, not asserted)",
            ),
            EnvVar(
                f.var("TOKEN_COMMAND"),
                f"optional JSON argv printing a token for {f.host}, run with only HOME and the config directory ({'default: gh auth token --hostname {host}' if f.key == 'github' else 'required for the token_command test'}); gh and glab may only run `auth token`",
            ),
            EnvVar(
                f.var("CONFIG_DIR"),
                f"optional: the {f.cli} configuration directory handed to the token command (default: the maintainer's own, when it exists)",
            ),
        ]
    return out


VARS: tuple[EnvVar, ...] = (
    *_forge_vars(),
    EnvVar("WTCTL_LIVE_PUSH_BRANCH_PREFIX", f"optional prefix of the disposable branches the push tests create (default {DEFAULT_PUSH_PREFIX})"),
    EnvVar("WTCTL_LIVE_SSH_EXPECT", "optional: ok or auth_required, the outcome the ssh test must see (default: either)"),
    EnvVar("WTCTL_LIVE_LFS_REPO", "repository id (host/path) of a PUBLIC repository with git-lfs files; the host must be github.com or gitlab.com (the LFS download is anonymous, so no token is used)"),
    EnvVar("WTCTL_LIVE_LFS_REF", "full sha in that repository whose tree holds the LFS file"),
    EnvVar("WTCTL_LIVE_LFS_PATH", "path of a git-lfs tracked file at that sha"),
)  # fmt: skip
VAR_NAMES: frozenset[str] = frozenset(v.name for v in VARS)

_PATH_RE = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_BAD_PATH_PARTS = {".", "..", ".git"}
_BRANCH_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_RESERVED_BRANCHES = frozenset({"head", "main", "master", "trunk", "develop", "release", "refs"})
# Variables a token variable must never name: the child's isolation and the tools' own settings.
_RESERVED_ENV_RE = re.compile(
    r"(?:HOME|PATH|LANG|LANGUAGE|USER|SHELL|TMPDIR|PWD|PYTHONPATH|"
    r"(?:XDG|GIT|SSH|WTCTL|LC|LD|PYTHON)_.*)"
)
ALLOWED_TOKEN_COMMAND_CLIS = frozenset({"gh", "glab"})


def getenv(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The variable's value, stripped; blank counts as unset. ``name`` must be documented."""
    if name not in VAR_NAMES:
        raise KeyError(f"undocumented live variable {name}")
    value = (ENV_SNAPSHOT if environ is None else environ).get(name, "").strip()
    return value or None


def valid_project_path(value: str) -> bool:
    if not _PATH_RE.fullmatch(value) or len(value) > 400:
        return False
    return not any(p in _BAD_PATH_PARTS or p.endswith(".git") for p in value.split("/"))


def project_path(forge: Forge, suffix: str, environ: Mapping[str, str] | None = None) -> str | None:
    value = getenv(forge.var(suffix), environ)
    return value if value is not None and valid_project_path(value) else None


def valid_repo_id(value: str) -> bool:
    """``host/path`` on one of the two public forges: a token configured for a forge must never
    be sent anywhere else, so any other host is refused."""
    return valid_project_path(value) and value.split("/", 1)[0] in {f.host for f in FORGES.values()}


def valid_sha(value: str) -> bool:
    return bool(_SHA_RE.fullmatch(value))


def valid_token_env_name(name: str) -> bool:
    """A plain identifier that is not one of the variables the child's isolation depends on."""
    return bool(_VAR_NAME_RE.fullmatch(name)) and not _RESERVED_ENV_RE.fullmatch(name)


def token_env_name(var_name_var: str, environ: Mapping[str, str] | None = None) -> str | None:
    name = getenv(var_name_var, environ)
    return name if name is not None and valid_token_env_name(name) else None


def token_value(var_name_var: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The token named by the variable ``var_name_var`` (itself holding a variable name)."""
    name = token_env_name(var_name_var, environ)
    if name is None:
        return None
    value = (ENV_SNAPSHOT if environ is None else environ).get(name, "").strip()
    return value or None


def token_command(forge: Forge, environ: Mapping[str, str] | None = None) -> list[str] | None:
    raw = getenv(forge.var("TOKEN_COMMAND"), environ)
    if raw is None:
        return list(forge.default_token_command) if forge.default_token_command else None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    ok = isinstance(data, list) and data and all(isinstance(a, str) and a for a in data)
    if not ok or not token_command_is_read_only(data):
        return None
    return list(data)


def token_command_is_read_only(argv: list[str]) -> bool:
    """A forge CLI may only run ``auth token``: nothing else of ``gh`` or ``glab`` is ever run
    with the maintainer's credentials."""
    if os.path.basename(argv[0]) in ALLOWED_TOKEN_COMMAND_CLIS:
        return argv[1:3] == ["auth", "token"]
    return True


def config_dir(forge: Forge, environ: Mapping[str, str] | None = None) -> str | None:
    """The forge CLI's config directory for its token command, or None when there is none."""
    explicit = getenv(forge.var("CONFIG_DIR"), environ)
    if explicit is not None:
        return explicit if os.path.isdir(explicit) else None
    env = ENV_SNAPSHOT if environ is None else environ
    base = env.get("XDG_CONFIG_HOME") or (os.path.join(env.get("HOME", ""), ".config"))
    candidate = os.path.join(base, forge.config_subdir)
    return candidate if os.path.isdir(candidate) else None


def valid_branch_prefix(prefix: str) -> bool:
    """A prefix of a disposable branch: ``name``, ``name/`` or ``dir/name`` (at most one ``/``),
    every component plain (no leading ``.``, no ``..``, no ``.lock`` ending), none of the names
    a real branch has, so ``<prefix><hex>`` is a valid and unremarkable ref name."""
    if not prefix or len(prefix) > 41 or ".." in prefix:
        return False
    parts = prefix.split("/")
    if len(parts) > 2:
        return False
    if len(parts) == 2 and parts[1] == "":
        parts = parts[:1]
    for part in parts:
        if not _BRANCH_COMPONENT_RE.fullmatch(part) or part.endswith(".lock"):
            return False
    return parts[0].lower() not in _RESERVED_BRANCHES


def push_prefix(environ: Mapping[str, str] | None = None) -> str:
    value = getenv("WTCTL_LIVE_PUSH_BRANCH_PREFIX", environ) or DEFAULT_PUSH_PREFIX
    return value if valid_branch_prefix(value) else DEFAULT_PUSH_PREFIX


# -- prerequisites ---------------------------------------------------------------------------

Which = Callable[[str], str | None]


def _which(name: str) -> str | None:
    return shutil.which(name, path=PATH_SNAPSHOT)


def unmet(
    need: str, environ: Mapping[str, str] | None = None, which: Which | None = None
) -> str | None:
    """Why ``need`` is not met (a skip message naming variables, never values), else None.

    Needs: ``<forge>:public``, ``<forge>:private``, ``<forge>:push``, ``<forge>:fork``,
    ``<forge>:token_command``, ``bin:<name>``, ``lfs``.
    """
    which = which or _which
    kind, _, arg = need.partition(":")
    if kind == "bin":
        return None if which(arg) else f"the {arg} binary is not on PATH"
    if need == "lfs":
        missing = [
            n
            for n in ("WTCTL_LIVE_LFS_REPO", "WTCTL_LIVE_LFS_REF", "WTCTL_LIVE_LFS_PATH")
            if getenv(n, environ) is None
        ]
        if missing:
            return f"set {', '.join(missing)}"
        repo = getenv("WTCTL_LIVE_LFS_REPO", environ) or ""
        sha = getenv("WTCTL_LIVE_LFS_REF", environ) or ""
        if not valid_repo_id(repo) or not valid_sha(sha):
            return (
                "WTCTL_LIVE_LFS_REPO (a github.com or gitlab.com repository id) or "
                "WTCTL_LIVE_LFS_REF (a full sha) is malformed"
            )
        return None if which("git-lfs") else "the git-lfs binary is not on PATH"
    forge = FORGES.get(kind)
    if forge is None:
        raise ValueError(f"unknown need {need!r}")
    if arg == "public":
        if project_path(forge, "PUBLIC_REPO", environ) is None:
            return f"set {forge.var('PUBLIC_REPO')} to a public project path (owner/name)"
        return None
    if arg == "private":
        if project_path(forge, "PRIVATE_REPO", environ) is None:
            return f"set {forge.var('PRIVATE_REPO')} to a private project path"
        if token_value(forge.var("TOKEN_ENV"), environ) is None:
            return f"set {forge.var('TOKEN_ENV')} to the name of a set token variable"
        return None
    if arg == "push":
        if project_path(forge, "PUSH_REPO", environ) is None:
            return f"set {forge.var('PUSH_REPO')} to a disposable project path"
        if token_value(forge.var("PUSH_TOKEN_ENV"), environ) is None:
            return f"set {forge.var('PUSH_TOKEN_ENV')} to the name of a set token variable"
        return None
    if arg == "fork":
        why = unmet(f"{forge.key}:public", environ, which)
        if why:
            return why
        head = getenv(forge.var("FORK_PR_HEAD"), environ)
        if head is None or not valid_sha(head):
            return f"set {forge.var('FORK_PR_HEAD')} to a full 40-hex sha"
        return None
    if arg == "token_command":
        why = unmet(f"{forge.key}:private", environ, which)
        if why:
            return why
        argv = token_command(forge, environ)
        if argv is None:
            return (
                f"set {forge.var('TOKEN_COMMAND')} to a JSON argv "
                "(gh and glab may only run `auth token`)"
            )
        if not which(argv[0]):
            return f"the {argv[0]} binary is not on PATH"
        return None
    raise ValueError(f"unknown need {need!r}")


def all_unmet(
    needs: list[str], environ: Mapping[str, str] | None = None, which: Which | None = None
) -> list[str]:
    return [m for n in needs if (m := unmet(n, environ, which))]
