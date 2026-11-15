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
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from .config import Config
from .gateway import Gateway, GitResult
from .hardgit import Git

# A token command gets this long, and may print this much. The token itself is much shorter.
TOKEN_COMMAND_TIMEOUT = 10.0
TOKEN_COMMAND_MAX_OUTPUT = 16 * 1024
MAX_TOKEN_LENGTH = 4096
MAX_USER_LENGTH = 256

_VISIBLE_ASCII = re.compile(r"[\x21-\x7e]+\Z")
_HOSTPORT = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?\Z"
)
_URL_CHARS = re.compile(r"[\x21-\x7e]+\Z")  # no space, control character, backslash is checked
_SCP_LIKE = re.compile(r"[^/:]+:")

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
    """Scheme and host of an already normalized fetch URL (userinfo stripped, no whitespace).

    TODO(#11): switch to the ``urls`` module's parser once it lands; this minimal helper only
    refuses what it cannot place with certainty. Anything unexpected raises ``ValueError`` (the
    message never contains the URL, which could carry a secret).
    """
    if not _URL_CHARS.fullmatch(url) or "\\" in url:
        raise ValueError("unsupported fetch URL (whitespace, control or non-ASCII character)")
    if "://" not in url:
        # scp-like ``[user@]host:path`` is ssh; a colon after the first slash is a plain path.
        return FetchTarget("ssh" if _SCP_LIKE.match(url) else "file", None)
    scheme, _, rest = url.partition("://")
    scheme = scheme.lower()
    if scheme in ("ssh", "file"):
        return FetchTarget(scheme, None)
    if scheme != "https":
        raise ValueError(f"unsupported fetch URL scheme {scheme[:16]!r}")
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if "@" in authority:
        raise ValueError("fetch URL carries userinfo; it must be stripped first")
    hostport = authority.lower()
    if hostport.endswith(":443"):
        hostport = hostport[: -len(":443")]
    if not _HOSTPORT.fullmatch(hostport):
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
    gateway: Gateway,
    warn: Warn,
    timeout: float,
) -> Secret | None:
    command = [hostport if a == "{host}" else a for a in argv]
    result = gateway.token_command(
        command, env=dict(environ), timeout=timeout, max_bytes=TOKEN_COMMAND_MAX_OUTPUT
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
    if not _HOSTPORT.fullmatch(hostport):
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
    _header: tuple[str, Secret] | None = field(default=None, repr=False)
    _token: Secret | None = field(default=None, repr=False)

    @property
    def extra_config(self) -> tuple[tuple[str, str], ...]:
        """For ``Git.build(extra_config=...)``: the URL-scoped header, if there is a token."""
        if self._header is None:
            return ()
        return ((self._header[0], self._header[1].reveal()),)

    def scrub(self, result: GitResult) -> GitResult:
        """Replace any echo of the credential in git's output (a server can print what it likes)."""
        secrets: list[str] = []
        if self._token is not None:
            secrets.append(self._token.reveal())
        if self._header is not None:
            value = self._header[1].reveal()
            secrets += [value, value.removeprefix("Authorization: Basic ")]
        out, err = result.stdout, result.stderr
        for s in secrets:
            out = out.replace(s.encode(), b"***")
            err = err.replace(s.encode(), b"***")
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
    """
    target = fetch_target(url)
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
        return NetworkAuth("https")
    try:
        header = token_header(target.hostport, cfg.host(target.hostport).token_user, token)
    except ValueError as e:
        warn(f"{e} for {target.hostport}; continuing without a token")
        return NetworkAuth("https")
    return NetworkAuth("https", _header=header, _token=token)


# --------------------------------------------------------------------------------------------
# Timeouts and the network git runner
# --------------------------------------------------------------------------------------------


class NetworkKind(enum.Enum):
    """Which wall-clock limit a network call runs under (interface.md, Hardened git)."""

    NETWORK = "network_timeout"  # later mirror refreshes, ls-remote --symref, shallow fetches
    CLONE = "clone_timeout"  # clones, a mirror's first refresh, tree fetches, --depth 0 fetches


def timeout_seconds(cfg: Config, kind: NetworkKind) -> float:
    return float(cfg.network_timeout_s if kind is NetworkKind.NETWORK else cfg.clone_timeout_s)


def run_network_git(
    git: Git,
    args: Sequence[str],
    *,
    cfg: Config,
    auth: NetworkAuth,
    kind: NetworkKind,
    tool_owned: bool,
    stdin: bytes | None = None,
    **build: object,
) -> GitResult:
    """Run one network git call with its credentials, timeout and low-speed limits.

    ``build`` takes ``Git.build``'s location arguments (``git_dir``, ``work_tree``, ``cwd``,
    ``private_dir``). ``tool_owned`` is the caller's statement that the repository is one wtctl
    owns (mirror, fresh throwaway, new clone directory), as ``Git.build`` requires. The output
    is scrubbed of the credential.
    """
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
