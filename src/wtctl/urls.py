"""Repo id grammar, URL normalization and the clone fetch URL rules.

Rules are in docs/interface.md (Identifiers, Clone resolution > URL normalization and Clone fetch
URL). Everything here is a pure function over untrusted text: a URL read from a clone's config
(which an agent may have written) or from a remote. The parser is deliberately strict and small
instead of general: anything outside the table in the interface (percent-encoding, query or
fragment, non-ASCII or IDN hosts, trailing dots, empty path segments, ``.``/``..`` segments,
unknown schemes such as ``ext::`` or ``git://``, a leading ``-``, control characters) does not
normalize, and a URL that does not normalize is a ``remote_mismatch`` for the caller.

Seam for #12 (credentials): :func:`url_host` and :func:`url_scheme` give the host (``name`` or
``name:port``, lowercase) and the network scheme (``https``, ``ssh`` or ``file``) of a clone URL
that normalizes; #12 should use them to pick ``hosts.<host>`` settings and ``GIT_ALLOW_PROTOCOL``.
"""

from __future__ import annotations

import re

from .errors import UsageError, WtctlError

_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_HOSTNAME = rf"{_LABEL}(?:\.{_LABEL})*"
_HOST_RE = re.compile(_HOSTNAME)
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")
_USER_RE = re.compile(r"[A-Za-z0-9._~-]+")
_PORT_RE = re.compile(r"[1-9][0-9]{0,4}")
# Userinfo of an https/ssh URL: anything printable except what delimits a URL.
_USERINFO_BAD = re.compile(r"[\x00-\x20\x7f-\U0010ffff/?#\\@\[\]%]")
_SCHEME_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*)://(.*)\Z", re.DOTALL)
MAX_URL = 4096


def _printable_ascii(text: str) -> bool:
    return text.isascii() and all(0x20 < ord(c) < 0x7F for c in text)


# --- repo ids ---------------------------------------------------------------------------------


def _valid_port(port: str) -> bool:
    return bool(_PORT_RE.fullmatch(port)) and int(port) <= 65535


def _is_repo_id(text: str) -> bool:
    if len(text) > MAX_URL or not _printable_ascii(text):
        return False
    host, slash, path = text.partition("/")
    if not slash:
        return False
    name, colon, port = host.partition(":")
    if not _HOST_RE.fullmatch(name):
        return False
    if colon and (not _valid_port(port) or port == "443"):
        return False  # 443 is canonical-less: normalization drops it, so no id carries it
    segments = path.split("/")
    return all(_SEGMENT_RE.fullmatch(s) and s not in (".", "..") for s in segments)


def validate_repo_id(text: str) -> str:
    """The id unchanged if it matches the grammar of docs/interface.md (Identifiers), else exit 2."""
    if not _is_repo_id(text):
        shown = text if len(text) <= 100 else text[:100] + "..."
        raise UsageError(
            f"invalid repo id {shown!r}: lowercase host[:port]/path with segments of "
            "[A-Za-z0-9._-] (no '.' or '..' segment)"
        )
    return text


def repo_host(repo_id: str) -> str:
    """``host`` or ``host:port`` of a valid repo id."""
    return repo_id.partition("/")[0]


def repo_path(repo_id: str) -> str:
    """The project path of a valid repo id."""
    return repo_id.partition("/")[2]


def host_dirname(host: str) -> str:
    """The host as a directory name in a path template (``:`` becomes ``_``)."""
    return host.replace(":", "_")


# --- normalization -----------------------------------------------------------------------------


def _clean_path(path: str) -> str | None:
    """A URL path (without its leading slash) as a project path, or None."""
    if path.endswith("/"):
        path = path[:-1]
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        return None
    segments = path.split("/")
    if all(_SEGMENT_RE.fullmatch(s) and s not in (".", "..") for s in segments):
        return path
    return None


def _split_authority(rest: str) -> tuple[str, str] | None:
    authority, slash, path = rest.partition("/")
    if not slash or not authority:
        return None
    return authority, path


def _host_port(hostport: str) -> tuple[str, str | None] | None:
    host, colon, port = hostport.partition(":")
    if not _HOST_RE.fullmatch(host):
        return None
    if colon:
        if not _valid_port(port):
            return None
        return host, port
    return host, None


def _split_userinfo(authority: str) -> tuple[str | None, str] | None:
    """``(userinfo, hostport)``. The userinfo may not contain ``@``: ``a@b@host`` is ambiguous
    between parsers, so it is refused instead of guessed."""
    if "@" not in authority:
        return None, authority
    userinfo, _, hostport = authority.rpartition("@")
    if not userinfo or _USERINFO_BAD.search(userinfo) or userinfo.startswith("-"):
        return None
    return userinfo, hostport


class _Parsed:
    __slots__ = ("host", "path", "port", "scheme", "userinfo")

    def __init__(
        self, scheme: str, userinfo: str | None, host: str, port: str | None, path: str
    ) -> None:
        self.scheme = scheme  # "https" or "ssh"
        self.userinfo = userinfo
        self.host = host
        self.port = port
        self.path = path  # project path, ".git" and trailing "/" already dropped

    @property
    def hostport(self) -> str:
        """The repo-id host: HTTPS keeps a non-default port, SSH ignores its port."""
        if self.scheme == "https" and self.port not in (None, "443"):
            return f"{self.host}:{self.port}"
        return self.host


def _parse_scp(url: str) -> _Parsed | None:
    head, colon, path = url.partition(":")
    if not colon or "/" in head:
        return None
    user: str | None = None
    host = head
    if "@" in head:
        user, _, host = head.rpartition("@")
        if not _USER_RE.fullmatch(user) or user.startswith("-"):
            return None
    if not _HOST_RE.fullmatch(host):
        return None
    clean = _clean_path(path)
    if clean is None:
        return None  # includes a leading "/" (an empty first segment) and "::" (ext::)
    return _Parsed("ssh", user, host, None, clean)


def _parse(url: str) -> _Parsed | None:
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        return None
    if not _printable_ascii(url) or url.startswith("-"):
        return None
    m = _SCHEME_RE.fullmatch(url)
    if m is None:
        return _parse_scp(url)
    scheme = m.group(1).lower()
    if scheme not in ("https", "ssh"):
        return None
    split = _split_authority(m.group(2))
    if split is None:
        return None
    authority, path = split
    ui = _split_userinfo(authority)
    if ui is None:
        return None
    userinfo, hostport = ui
    hp = _host_port(hostport)
    if hp is None:
        return None
    clean = _clean_path(path)
    if clean is None:
        return None
    return _Parsed(scheme, userinfo, hp[0], hp[1], clean)


def normalize_url(url: str) -> str | None:
    """``host[:port]/path`` for an HTTPS, SSH or scp-like URL, else None.

    The host is lowercased (it must already be plain ASCII), userinfo, one trailing ``.git`` and
    one trailing ``/`` are dropped, an HTTPS port 443 is dropped and an SSH port is ignored. A
    ``file://`` URL or plain path never normalizes here; see :func:`matches_repo`.
    """
    # Hosts are matched case-insensitively; paths keep their case.
    parsed = _parse(_lower_host(url))
    if parsed is None:
        return None
    return f"{parsed.hostport}/{parsed.path}"


def _lower_host(url: str) -> str:
    """Lowercase the scheme and host part only (path case is significant)."""
    if not isinstance(url, str) or len(url) > MAX_URL or not url.isascii():
        return url
    m = _SCHEME_RE.fullmatch(url)
    if m is not None:
        authority, slash, path = m.group(2).partition("/")
        userinfo, at, hostport = authority.rpartition("@")
        return f"{m.group(1).lower()}://{userinfo}{at}{hostport.lower()}{slash}{path}"
    head, colon, path = url.partition(":")
    if not colon or "/" in head:
        return url
    user, at, host = head.rpartition("@")
    return f"{user}{at}{host.lower()}{colon}{path}"


def url_scheme(url: str) -> str | None:
    """``https``, ``ssh`` (also scp-like) or ``file`` (a plain absolute path or ``file://``)."""
    parsed = _parse(_lower_host(url))
    if parsed is not None:
        return parsed.scheme
    if local_path(url) is not None:
        return "file"
    return None


def url_host(url: str) -> str | None:
    """The host of an HTTPS or SSH URL (``name``, plus ``:port`` for a non-default HTTPS port).

    This is the key of ``hosts.<host>`` in the config. None for local paths and anything that
    does not parse.
    """
    parsed = _parse(_lower_host(url))
    return None if parsed is None else parsed.hostport


def strip_userinfo(url: str) -> str:
    """The URL without its userinfo (user and password); text that is not a parsed URL is
    returned unchanged. Credentials are never kept in a URL wtctl records or uses."""
    lowered = _lower_host(url)
    parsed = _parse(lowered)
    if parsed is None or parsed.userinfo is None:
        return url
    m = _SCHEME_RE.fullmatch(url)
    if m is not None:
        authority, slash, path = m.group(2).partition("/")
        return f"{m.group(1)}://{authority.rpartition('@')[2]}{slash}{path}"
    return url.partition(":")[0].rpartition("@")[2] + ":" + url.partition(":")[2]


# --- local repositories (tests and mirrors) -----------------------------------------------------


def local_path(url: str) -> str | None:
    """The normalized absolute path of a plain path or ``file:///path`` URL, else None.

    Refuses relative paths, a host in a ``file://`` URL, percent-encoding, control characters,
    NUL, a leading ``-`` and any ``.`` or ``..`` component (no lexical collapsing across
    symlinks).
    """
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        return None
    path = url
    if url[:7].lower() == "file://":
        path = url[7:]
        if not path.startswith("/"):
            return None  # file://host/..., file://localhost/...
    if not path.startswith("/") or "%" in path:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path) or "\\" in path:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    return "/" + "/".join(parts)


def matches_repo(url: str, repo_id: str, *, clone_url: str | None = None) -> bool:
    """Whether a clone's remote URL identifies ``repo_id``.

    It does when it normalizes to the repo id. A configured ``repos.<id>.clone_url`` also
    identifies the repo: a local path or ``file://`` URL matches only when it points at the same
    path (tests and mirrors), and a forge-side mirror URL matches when both normalize equally.
    """
    norm = normalize_url(url)
    if norm is not None and norm == repo_id:
        return True
    if clone_url is None:
        return False
    want_path = local_path(clone_url)
    if want_path is not None:
        return local_path(url) == want_path
    want_norm = normalize_url(clone_url)
    return want_norm is not None and norm is not None and norm == want_norm


def render_clone_url(repo_id: str, *, template: str) -> str:
    """``hosts.<host>.clone_url`` rendered for ``repo_id``; it must normalize back to the id."""
    url = template.replace("{host}", repo_host(repo_id)).replace("{path}", repo_path(repo_id))
    if normalize_url(url) != repo_id:
        raise WtctlError(
            "remote_mismatch",
            f"the clone URL template {template!r} does not give a URL for {repo_id}",
        )
    return url

