"""Credentials for network git: the HTTPS token header, SSH settings and network timeouts.

wtctl never stores a credential. A token comes from ``hosts.<host>.token_env`` or
``hosts.<host>.token_command``, lives only in this process's memory (wrapped in :class:`Secret`)
and in the environment of the one git child that needs it, as a URL-scoped
``http.https://<host>/.extraHeader`` delivered through ``Git.build(extra_config=...)``
(``GIT_CONFIG_COUNT``). It is never in argv, a file, state, a log, an error message or any output.

Failure messages name the host and a failure category, never the credential helper's output.
"""

from __future__ import annotations

import base64
import enum
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote

from . import urls
from .config import Config
from .gateway import Gateway, GitResult
from .errors import WtctlError
from .hardgit import Git, parse_config_list

# A token command gets this long, and may print this much. The token itself is much shorter.
TOKEN_COMMAND_TIMEOUT = 10.0
TOKEN_COMMAND_MAX_OUTPUT = 16 * 1024
MAX_TOKEN_LENGTH = 4096
MAX_USER_LENGTH = 256

_VISIBLE_ASCII = re.compile(r"[\x21-\x7e]+\Z")
_URL_CHARS = re.compile(r"[\x21-\x7e]+\Z")  # no space, control or non-ASCII character
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SCP_LIKE = re.compile(r"[^/:]+:")
_HELPER_SYNTAX = re.compile(r"[A-Za-z0-9+.-]+::")  # git's ``ext::`` style transport helpers
# A plain local path: absolute or explicitly relative (home-relative: see _is_home_path).
_LOCAL_PATH_PREFIXES = ("/", "./", "../")
MAX_TIMEOUT_SECONDS = 24 * 86400  # poll() takes milliseconds in a C int: 25 days overflow


def _is_home_path(url: str) -> bool:
    """``~`` is a path unless git would read a colon before any slash as scp-like ssh."""
    if not url.startswith("~"):
        return False
    colon = url.find(":")
    return colon < 0 or 0 <= url.find("/") < colon


Warn = Callable[[str], None]


class Secret:
    """A credential. ``repr``, ``str`` and ``format`` never show it; only :meth:`reveal` does."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __reduce__(self) -> tuple[()]:
        raise TypeError("a Secret is never serialized")

    def __copy__(self) -> Secret:
        return self

    def __deepcopy__(self, memo: object) -> Secret:
        return self


# --------------------------------------------------------------------------------------------
# The fetch URL's scheme and host
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchTarget:
    """What credentials need to know about the URL of one network call."""

    scheme: str  # "https", "ssh" or "file"
    hostport: str | None  # lowercase ``host[:port]`` of an https URL, default port dropped


def fetch_target(url: str) -> FetchTarget:
    """Scheme and host of an already normalized fetch URL (userinfo stripped).

    A URL starting with ``-`` (git would read it as an option), or holding a control character or
    leading whitespace, is refused. A plain local path (``/``, ``./``, ``../``, ``~`` or no
    scheme-like prefix) is kind ``file`` and may contain spaces and non-ASCII characters; only a
    string with ``://`` or scp-like syntax gets the URL grammar checks. The scheme must be
    lowercase (git refuses another spelling). The port must be 1-65535 without leading zeros;
    exactly ``:443`` is dropped.

    An https URL's host comes from the ``urls`` module (the one parser of host and port grammar);
    this function adds the checks for a URL that goes to argv. Anything unexpected raises
    ``ValueError`` (the message never contains the URL, which could carry a secret).
    """
    if not url or url.startswith("-") or url[:1].isspace() or _CONTROL.search(url):
        raise ValueError("unsupported fetch URL (option-like, or control or leading whitespace)")
    if url.startswith(_LOCAL_PATH_PREFIXES) or _is_home_path(url):
        return FetchTarget("file", None)
    if _HELPER_SYNTAX.match(url):
        raise ValueError("unsupported fetch URL (transport helper syntax)")
    if "://" not in url and not _SCP_LIKE.match(url):
        return FetchTarget("file", None)
    if not _URL_CHARS.fullmatch(url) or "\\" in url:
        raise ValueError("unsupported fetch URL (whitespace, control or non-ASCII character)")
    if "://" not in url:
        return FetchTarget("ssh", None)  # scp-like ``[user@]host:path``
    scheme, _, rest = url.partition("://")
    if scheme in ("ssh", "file"):
        return FetchTarget(scheme, None)
    if scheme != "https":
        raise ValueError(f"unsupported fetch URL scheme {scheme[:16]!r}")
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if "@" in authority:
        raise ValueError("fetch URL carries userinfo; it must be stripped first")
    hostport = urls.url_host(url)
    if hostport is None or urls.url_scheme(url) != "https":
        raise ValueError("unsupported host in fetch URL")
    return FetchTarget("https", hostport)


# --------------------------------------------------------------------------------------------
# Token resolution
# --------------------------------------------------------------------------------------------


def _valid_token(text: str) -> bool:
    return len(text) <= MAX_TOKEN_LENGTH and _VISIBLE_ASCII.fullmatch(text) is not None


def _from_command(
    argv: Sequence[str],
    hostport: str,
    *,
    environ: Mapping[str, str],
    drop_env: frozenset[str] = frozenset(),
    gateway: Gateway,
    warn: Warn,
    timeout: float,
) -> Secret | None:
    command = [hostport if a == "{host}" else a for a in argv]
    env = {k: v for k, v in environ.items() if k not in drop_env}
    result = gateway.token_command(
        command, env=env, timeout=timeout, max_bytes=TOKEN_COMMAND_MAX_OUTPUT
    )
    if result.failure is not None:
        warn(f"token_command for {hostport} failed ({result.failure}); continuing without a token")
        return None
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        warn(f"token_command for {hostport} printed invalid output; continuing without a token")
        return None
    text = text.rstrip("\r\n")
    if not text:
        warn(f"token_command for {hostport} printed no token; continuing without a token")
        return None
    if not _valid_token(text):
        warn(f"token_command for {hostport} printed an invalid token; continuing without one")
        return None
    return Secret(text)


def _other_hosts_token_env(cfg: Config, hostport: str) -> frozenset[str]:
    """The ``token_env`` names of every other configured host (not this host's own)."""
    own = cfg.host(hostport).token_env
    return frozenset(
        h.token_env
        for key, h in cfg.hosts.items()
        if key != hostport and h.token_env is not None and h.token_env != own
    )


def resolve_token(
    cfg: Config,
    hostport: str,
    *,
    environ: Mapping[str, str],
    gateway: Gateway,
    warn: Warn,
    command_timeout: float = TOKEN_COMMAND_TIMEOUT,
) -> Secret | None:
    """The token for ``hosts.<hostport>``: ``token_env`` if set and non-empty, else the command.

    ``None`` (with a warning for a failure) means an anonymous request. A non-empty
    ``token_env`` value that is not a valid token is not replaced by the command: the explicit
    setting is wrong, and a silent fallback would hide it.
    """
    host = cfg.host(hostport)
    if host.token_env is not None:
        value = environ.get(host.token_env, "")
        if value.strip():
            text = value.rstrip("\r\n")
            if _valid_token(text):
                return Secret(text)
            warn(f"token_env for {hostport} is not a valid token; continuing without a token")
            return None
    if host.token_command is not None:
        return _from_command(
            host.token_command,
            hostport,
            environ=environ,
            drop_env=_other_hosts_token_env(cfg, hostport),
            gateway=gateway,
            warn=warn,
            timeout=command_timeout,
        )
    return None


def token_header(hostport: str, token_user: str, token: Secret) -> tuple[str, Secret]:
    """The git config entry ``(key, value)`` that sends the token to ``https://<hostport>/`` only.

    The key is URL-scoped, so git sends the header to no other host or port; redirects are off
    in the hardened invocation. The value is wrapped so it cannot leak through a ``repr``.
    """
    if urls.url_host(f"https://{hostport}/x") != hostport:
        raise ValueError("invalid host for a token header")
    if (
        not token_user
        or len(token_user) > MAX_USER_LENGTH
        or ":" in token_user
        or not _VISIBLE_ASCII.fullmatch(token_user)
    ):
        raise ValueError("invalid token_user (visible ASCII without ':' required)")
    basic = base64.b64encode(f"{token_user}:{token.reveal()}".encode("ascii")).decode("ascii")
    return f"http.https://{hostport}/.extraHeader", Secret(f"Authorization: Basic {basic}")


# --------------------------------------------------------------------------------------------
# The credentials of one network call
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NetworkAuth:
    """Everything a network git call takes from credentials; the secrets stay out of ``repr``."""

    scheme: str
    ssh_auth_sock: str | None = None
    hostport: str | None = None  # https only: the host the token (if any) is scoped to
    _header: tuple[str, Secret] | None = field(default=None, repr=False)
    _token: Secret | None = field(default=None, repr=False)

    @property
    def has_token(self) -> bool:
        return self._header is not None

    @property
    def extra_config(self) -> tuple[tuple[str, str], ...]:
        """For ``Git.build(extra_config=...)``: the URL-scoped header, if there is a token."""
        if self._header is None:
            return ()
        return ((self._header[0], self._header[1].reveal()),)

    def scrub(self, result: GitResult) -> GitResult:
        """Replace any echo of the credential in git's output (a server can print what it likes).

        Covered: the token, its percent-encoded form, base64 of the token and of ``user:token``
        (padded and unpadded, and the percent-encoded base64), and the whole header value. Not
        covered: a server that splits the secret across lines or re-encodes it some other way.
        """
        secrets: set[str] = set()
        if self._token is not None:
            token = self._token.reveal()
            b64 = base64.b64encode(token.encode("ascii")).decode("ascii")
            secrets |= {token, quote(token, safe=""), b64, b64.rstrip("=")}
        if self._header is not None:
            value = self._header[1].reveal()
            blob = value.removeprefix("Authorization: Basic ")
            secrets |= {
                value,
                blob,
                blob.rstrip("="),
                quote(blob, safe=""),
                quote(blob.rstrip("="), safe=""),
            }
        out, err = result.stdout, result.stderr
        for secret in sorted(secrets, key=len, reverse=True):
            out = out.replace(secret.encode(), b"***")
            err = err.replace(secret.encode(), b"***")
        return GitResult(result.returncode, out, err, result.timed_out)


def prepare_network_auth(
    cfg: Config,
    url: str,
    *,
    environ: Mapping[str, str],
    gateway: Gateway,
    warn: Warn,
    command_timeout: float = TOKEN_COMMAND_TIMEOUT,
) -> NetworkAuth:
    """Credentials for one network call to ``url`` (the URL actually used, userinfo stripped).

    https: the token of ``hosts.<host>`` as a header scoped to that host, if there is one; ssh:
    the caller's ``SSH_AUTH_SOCK``; file: nothing. The caller's ``GIT_SSH_COMMAND`` and the rest
    of its environment never reach git (hardgit builds the environment from scratch).

    Raises ``WtctlError`` (``remote_mismatch``) for a URL :func:`fetch_target` refuses; the
    message never contains the URL, which could carry a secret.
    """
    try:
        target = fetch_target(url)
    except ValueError as e:
        raise WtctlError("remote_mismatch", f"unusable fetch URL: {e}") from e
    if target.scheme == "ssh":
        sock = environ.get("SSH_AUTH_SOCK")
        if sock and sock.startswith("/") and "\0" not in sock and "\n" not in sock:
            return NetworkAuth("ssh", ssh_auth_sock=sock)
        return NetworkAuth("ssh")
    if target.scheme != "https" or target.hostport is None:
        return NetworkAuth(target.scheme)
    token = resolve_token(
        cfg,
        target.hostport,
        environ=environ,
        gateway=gateway,
        warn=warn,
        command_timeout=command_timeout,
    )
    if token is None:
        return NetworkAuth("https", hostport=target.hostport)
    try:
        header = token_header(target.hostport, cfg.host(target.hostport).token_user, token)
    except ValueError as e:
        warn(f"{e} for {target.hostport}; continuing without a token")
        return NetworkAuth("https", hostport=target.hostport)
    return NetworkAuth("https", hostport=target.hostport, _header=header, _token=token)


# --------------------------------------------------------------------------------------------
# Timeouts and the network git runner
# --------------------------------------------------------------------------------------------


class NetworkKind(enum.Enum):
    """Which wall-clock limit a network call runs under (interface.md, Hardened git)."""

    NETWORK = "network_timeout"  # later mirror refreshes, ls-remote --symref, shallow fetches
    CLONE = "clone_timeout"  # clones, a mirror's first refresh, tree fetches, --depth 0 fetches


def timeout_seconds(cfg: Config, kind: NetworkKind) -> float:
    """The limit in seconds, capped at 24 days (a huge duration would overflow the runner)."""
    seconds = cfg.network_timeout_s if kind is NetworkKind.NETWORK else cfg.clone_timeout_s
    return float(min(seconds, MAX_TIMEOUT_SECONDS))


# Subcommands whose argv carries a remote or URL; each needs a literal ``--`` before it, so an
# option-like URL can never be read as an option (``fetch_target`` already refuses such a URL).
_NEEDS_SEPARATOR = frozenset({"clone", "fetch", "ls-remote", "push"})


def _require_separator(args: Sequence[str]) -> None:
    if args and args[0] in _NEEDS_SEPARATOR and "--" not in args:
        raise ValueError(f"a network {args[0]} needs a literal `--` before the remote or URL")


def check_owned_config(git: Git, git_dir: str, owned_keys: frozenset[str], cwd: str) -> None:
    """Refuse a repository whose config holds a key wtctl did not write.

    A token goes only to a repository whose config wtctl wrote: a planted ``http.proxy``,
    ``url.*.insteadOf``, ``http.<url>.proxy``, ``http.sslVerify`` or an include would redirect or
    expose the header. ``owned_keys`` are the lowercase-section keys (``git config --list`` form)
    that wtctl writes; any other key, an include included, is refused.
    """
    # --file reads the repository's own file only (a plain --list also prints the -c overrides).
    r = git.run(
        ["config", "--file", os.path.join(git_dir, "config"), "--no-includes", "--list", "-z"],
        git_dir=git_dir,
        cwd=cwd,
        timeout=30,
    )
    if r.returncode != 0 or r.timed_out:
        raise WtctlError("internal", f"could not read the config of {git_dir}")
    keys = {key for key, _ in parse_config_list(r.stdout)}
    foreign = sorted(keys - owned_keys)
    if foreign:
        raise WtctlError(
            "unsafe_repo_config",
            "the mirror's config holds keys wtctl did not write; a token is not sent",
            details={"keys": [k[:200] for k in foreign[:20]]},
        )


def rejected_token_error(auth: NetworkAuth, error: WtctlError | None) -> WtctlError | None:
    """The 401 message when a token was actually sent: the credential, not its absence, failed.

    ``error`` is :func:`~wtctl.hardgit.outcome_error`'s result; the code stays ``auth_required``.
    """
    if error is None or error.code != "auth_required" or not auth.has_token:
        return error
    return WtctlError(
        "auth_required", f"the remote rejected the configured token for {auth.hostport}"
    )


def run_network_git(
    git: Git,
    args: Sequence[str],
    *,
    cfg: Config,
    auth: NetworkAuth,
    kind: NetworkKind,
    tool_owned: bool,
    owned_keys: frozenset[str] | None = None,
    stdin: bytes | None = None,
    **build: object,
) -> GitResult:
    """Run one network git call with its credentials, timeout and low-speed limits.

    ``build`` takes ``Git.build``'s location arguments (``git_dir``, ``work_tree``, ``cwd``,
    ``private_dir``). ``tool_owned`` is the caller's statement that the repository is one wtctl
    owns (mirror, fresh throwaway, new clone directory), as ``Git.build`` requires. The output
    is scrubbed of the credential.

    ``clone``, ``fetch``, ``ls-remote`` and ``push`` need a literal ``--`` in ``args`` (a
    ``ValueError`` otherwise). A call that carries a token inside a repository (``git_dir``)
    passes ``owned_keys``, and the repository's config must hold no other key
    (:func:`check_owned_config`); a clone into a new directory has no repository config to check.
    Use :func:`rejected_token_error` on the classified outcome for the 401 message.
    """
    _require_separator(args)
    if auth.has_token and build.get("git_dir") is not None:
        if owned_keys is None:
            raise ValueError("a token goes only to a repository whose config is checked")
        cwd = build.get("cwd") or build["git_dir"]
        check_owned_config(git, str(build["git_dir"]), owned_keys, str(cwd))
    # TODO(#5): when network calls are logged, log only the redacted argv and `scrub`bed text.
    result = git.run(
        args,
        stdin=stdin,
        timeout=timeout_seconds(cfg, kind),
        scheme=auth.scheme,
        tool_owned=tool_owned,
        extra_config=auth.extra_config,
        ssh_auth_sock=auth.ssh_auth_sock,
        **build,  # type: ignore[arg-type]
    )
    return auth.scrub(result)
