"""The live suite's world: a private HOME, a config file and wtctl run as a child process.

Two seams carry every outside effect, so a unit test can prove nothing reaches the network when a
prerequisite is missing: :func:`spawn` (the one place a child process is started) and
:func:`make_gateway` (the runner behind wtctl's own hardened git, used for the calls the consumer
makes itself). Credentials live only in the child's environment; argv and the config file hold
variable NAMES, and every string handed back to a test has been through :meth:`LiveWorld.scrub`.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.live import liveenv
from wtctl.config import Config, HostConfig
from wtctl.credentials import NetworkKind, prepare_network_auth, run_network_git
from wtctl.gateway import Gateway, GitResult
from wtctl.hardgit import Git

Record = Callable[[str, object], None]  # pytest's ``record_property`` fixture
ACTOR = "human:live"
FRESH_KEYS = frozenset({"core.repositoryformatversion", "core.filemode", "core.bare"})
# Token shapes of the common forge CLIs, scrubbed from output even when the value is not known
# to the test (a ``token_command`` prints its own).
_TOKEN_SHAPES = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{10,})"
)


@dataclass
class Raw:
    code: int
    out: bytes
    err: bytes
    timed_out: bool = False


def spawn(
    argv: list[str], *, env: dict[str, str], cwd: str, timeout: float, stdin: bytes | None = None
) -> Raw:
    """Start one child in its own session and wait at most ``timeout`` seconds.

    On a timeout the group of the exact pid started here is killed and the child reaped.
    """
    proc = subprocess.Popen(  # noqa: S603
        argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = proc.communicate()
    return Raw(proc.returncode, out or b"", err or b"", timed_out)


def make_gateway() -> Gateway:
    """The runner for wtctl's own hardened git in the consumer-side calls."""
    return Gateway()


@dataclass
class Out:
    code: int
    out: str
    err: str
    timed_out: bool = False

    def json(self) -> Any:
        return json.loads(self.out)

    def error(self) -> dict[str, Any]:
        data = self.json()
        return data["error"] if isinstance(data, dict) and "error" in data else {}

    def describe(self) -> str:
        return f"exit {self.code}{' (timed out)' if self.timed_out else ''}\n{self.out}\n{self.err}"


class LiveWorld:
    """One repository on one host, as the consumer would configure it.

    ``tokens`` maps variable names to values; the child's environment gets exactly those and the
    config names the variable in ``token_env`` (or runs ``token_command``). ``clone_url`` replaces
    the host's URL template (a local remote in the offline unit tests, a variant spelling of the
    forge URL in the live ones).
    """

    def __init__(
        self,
        base: Path,
        *,
        host: str,
        project: str,
        clone_url: str | None = None,
        token_env: str | None = None,
        token_command: list[str] | None = None,
        tokens: dict[str, str] | None = None,
        child_env: dict[str, str] | None = None,
        allow_filters: list[str] | None = None,
        scheme: str = "https",
        network_timeout: str = "90s",
        clone_timeout: str = "10m",
    ) -> None:
        self.base = base
        self.host = host
        self.project = project
        self.repo_id = f"{host}/{project}"
        self.clone_url_override = clone_url
        self.token_env = token_env
        self.token_command = token_command
        self.tokens = dict(tokens or {})
        self.extra_env = dict(child_env or {})
        self.allow_filters = allow_filters
        self.scheme = scheme
        self.network_timeout = network_timeout
        self.clone_timeout = clone_timeout
        self.home = base / "home"
        self.root = base / "wt"
        self.config_path = base / "cfg" / "config.json"
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / ".config").mkdir(exist_ok=True)
        self.git_bin = shutil.which("git", path=liveenv.PATH_SNAPSHOT) or "/usr/bin/git"

    # -- identity of the target ------------------------------------------------------------

    @property
    def fetch_url(self) -> str:
        return self.clone_url_override or f"https://{self.host}/{self.project}.git"

    # -- secrets ---------------------------------------------------------------------------

    @property
    def secrets(self) -> list[str]:
        return sorted({v for v in self.tokens.values() if v}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        """Remove every known credential (and its base64 forms) and token-shaped strings."""
        for secret in self.secrets:
            b64 = base64.b64encode(secret.encode()).decode()
            for form in (secret, b64, b64.rstrip("=")):
                text = text.replace(form, "***")
        return _TOKEN_SHAPES.sub("***", text)

    # -- configuration and environment -----------------------------------------------------

    def config_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "root": str(self.root),
            "lease_wait": "30s",
            "network_timeout": self.network_timeout,
            "clone_timeout": self.clone_timeout,
            "git_bin": self.git_bin,
        }
        host: dict[str, Any] = {}
        if self.token_env:
            host["token_env"] = self.token_env
        if self.token_command:
            host["token_command"] = list(self.token_command)
        if host:
            data["hosts"] = {self.host: host}
        repo: dict[str, Any] = {}
        if self.clone_url_override:
            repo["clone_url"] = self.clone_url_override
        if self.allow_filters is not None:
            repo["create"] = {"allow_filters": list(self.allow_filters)}
        data["repos"] = {self.repo_id: repo}
        return data

    def write_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.config_data()))

    def env(self) -> dict[str, str]:
        """The child's whole environment: nothing inherited except PATH (and what is named)."""
        h = self.home
        return {
            "PATH": liveenv.PATH_SNAPSHOT,
            "HOME": str(h),
            "XDG_CONFIG_HOME": str(h / ".config"),
            "XDG_STATE_HOME": str(h / ".local" / "state"),
            "XDG_DATA_HOME": str(h / ".local" / "share"),
            "XDG_CACHE_HOME": str(h / ".cache"),
            "GIT_CONFIG_GLOBAL": str(h / ".gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            **self.tokens,
            **self.extra_env,
        }

    # -- running ---------------------------------------------------------------------------

    def _out(self, raw: Raw) -> Out:
        return Out(
            raw.code,
            self.scrub(raw.out.decode("utf-8", "replace")),
            self.scrub(raw.err.decode("utf-8", "replace")),
            raw.timed_out,
        )

    def wtctl(self, *argv: str, timeout: float = 900.0, actor: str = ACTOR) -> Out:
        """``wtctl --json <argv>`` as a real child process."""
        self.write_config()
        full = [
            sys.executable,
            "-m",
            "wtctl",
            "--config",
            str(self.config_path),
            "--json",
            "--actor",
            actor,
            *argv,
        ]
        return self._out(spawn(full, env=self.env(), cwd=str(self.base), timeout=timeout))

    def plain(self, argv: list[str], *, cwd: Path | None = None, timeout: float = 120.0) -> Out:
        """A non-wtctl program (git, git-lfs) with the same private environment."""
        return self._out(spawn(argv, env=self.env(), cwd=str(cwd or self.base), timeout=timeout))

    # -- the consumer's own hardened git (network) -----------------------------------------

    @property
    def git(self) -> Git:
        return Git(make_gateway(), self.git_bin, str(self.home))

    def _config(self) -> Config:
        host = HostConfig(token_env=self.token_env, token_command=self.token_command)
        return Config(
            hosts={self.host: host} if (self.token_env or self.token_command) else {},
            network_timeout=self.network_timeout,
            clone_timeout=self.clone_timeout,
        )

    def net_git(
        self,
        args: list[str],
        *,
        url: str,
        git_dir: Path,
        work_tree: Path | None = None,
        owned_keys: frozenset[str] = FRESH_KEYS,
        kind: NetworkKind = NetworkKind.NETWORK,
    ) -> GitResult:
        """One network git call under wtctl's credential rules; the output is scrubbed."""
        cfg = self._config()
        auth = prepare_network_auth(
            cfg, url, environ=self.env(), gateway=make_gateway(), warn=lambda m: None
        )
        result = run_network_git(
            self.git,
            args,
            cfg=cfg,
            auth=auth,
            kind=kind,
            tool_owned=True,
            owned_keys=owned_keys,
            git_dir=git_dir,
            work_tree=work_tree,
            cwd=work_tree or git_dir,
            allow_shallow=True,
        )
        return GitResult(
            result.returncode,
            self.scrub(result.out).encode(),
            self.scrub(result.err).encode(),
            result.timed_out,
        )

    def scratch(self) -> Path:
        """An empty bare repository for ``ls-remote`` (git needs a repository to run network
        calls in; wtctl's own hardened call checks its config before a token goes near it)."""
        path = self.base / "scratch.git"
        if not path.exists():
            out = self.plain([self.git_bin, "init", "-q", "--bare", "--template=", str(path)])
            if out.code != 0:
                raise AssertionError(f"could not create the scratch repository: {out.err}")
        return path

    def ls_remote(self, ref: str = "HEAD", *, symref: bool = False) -> list[tuple[str, str]]:
        """``(object-or-target, ref)`` pairs; with ``symref`` the first row is the symref line."""
        args = ["ls-remote"]
        if symref:
            args.append("--symref")
        args += ["--", self.fetch_url, ref]
        res = self.net_git(args, url=self.fetch_url, git_dir=self.scratch())
        if res.returncode != 0:
            raise AssertionError(f"ls-remote failed ({res.returncode}): {res.err}")
        rows = []
        for line in res.out.splitlines():
            left, _, right = line.partition("\t")
            rows.append((left, right))
        return rows

    def tip(self, ref: str = "HEAD") -> str:
        rows = [r for r in self.ls_remote(ref) if re.fullmatch(r"[0-9a-f]{40}", r[0])]
        if not rows:
            raise AssertionError(f"the remote has no {ref}")
        return rows[0][0]

    def default_branch(self) -> str:
        for left, right in self.ls_remote("HEAD", symref=True):
            if left.startswith("ref: refs/heads/") and right == "HEAD":
                return left.removeprefix("ref: refs/heads/")
        raise AssertionError("the remote reports no default branch")
