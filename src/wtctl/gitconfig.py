"""wtctl's own git-config parser (docs/interface.md, Baseline: "never by running git").

The grammar is git's (``config.c`` ``git_parse_source``), ported byte for byte for the cases a
repository's config uses, and **stricter** wherever git's behaviour is odd: a NUL byte, a lone
carriage return, an empty section name and a key before any section header are all
``ConfigSyntaxError``. A file that does not parse is recorded by its content hash instead of by
keys, so being stricter than git can only make a change more visible, never hide one.

A key is git's canonical ``section[.subsection].name`` string: section and name lower-cased, the
subsection kept as written (the deprecated ``[section.sub]`` form lower-cases it, as git does).
Includes are not resolved here; ``include_kind`` only says whether a key names one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

MAX_ENTRIES = 1_000_000
_SPACE = frozenset(b" \t\n")  # git's isspace; a lone CR is refused before it matters
_KEYCHAR = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
_ALPHA = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_BOM = b"\xef\xbb\xbf"


class ConfigSyntaxError(ValueError):
    """The file is not a config file wtctl is willing to read key by key."""


class ConfigLimitError(ConfigSyntaxError):
    """More entries than the caller's bound (a bound, not a syntax problem)."""


@dataclass(frozen=True)
class Entry:
    key: str  # canonical, see the module docstring
    value: bytes | None  # None: the key has no ``=`` (git's boolean true)


class _Scan:
    """A byte cursor; past the end it yields one newline (as git does) and sets ``over``."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    @property
    def over(self) -> bool:
        return self.pos > len(self.data)

    def get(self) -> int:
        c = self.data[self.pos] if self.pos < len(self.data) else 10
        self.pos += 1
        return c


def _lower(c: int) -> int:
    return c | 0x20 if 65 <= c <= 90 else c


def parse(data: bytes, *, max_entries: int = MAX_ENTRIES) -> list[Entry]:
    """The entries of one config file in file order (``ConfigSyntaxError`` if it is refused)."""
    if b"\0" in data:
        raise ConfigSyntaxError("NUL byte")
    if data.startswith(_BOM):
        data = data[3:]
    # git reads CRLF as LF; every other carriage return is refused
    data = data.replace(b"\r\n", b"\n")
    if b"\r" in data:
        raise ConfigSyntaxError("carriage return outside a line ending")
    sc = _Scan(data)
    entries: list[Entry] = []
    base = ""  # "section." or "section.sub."
    comment = False
    while True:
        c = sc.get()
        if c == 10:
            if sc.over:
                return entries
            comment = False
            continue
        if comment or c in _SPACE:
            continue
        if c in (0x23, 0x3B):  # '#' ';'
            comment = True
            continue
        if c == 0x5B:  # '['
            base = _section_header(sc)
            continue
        if c not in _ALPHA:
            raise ConfigSyntaxError("bad config line")
        if not base:
            raise ConfigSyntaxError("key before any section header")
        name = bytearray([_lower(c)])
        c = sc.get()
        while c in _KEYCHAR:
            name.append(_lower(c))
            c = sc.get()
        while c in (0x20, 0x09):
            c = sc.get()
        value: bytes | None
        if c == 10:
            value = None
            sc.pos -= 1  # the main loop sees the newline (and the end of the file)
        elif c == 0x3D:  # '='
            value = _value(sc)
        else:
            raise ConfigSyntaxError("bad config line")
        entries.append(Entry(base + name.decode("ascii"), value))
        if len(entries) > max_entries:
            raise ConfigLimitError(f"more than {max_entries} entries")


def _section_header(sc: _Scan) -> str:
    """Parse after '[' up to and including ']'; returns the ``section[.sub].`` stem."""
    name = bytearray()
    while True:
        c = sc.get()
        if sc.over:
            raise ConfigSyntaxError("unterminated section header")
        if c == 0x5D:  # ']'
            break
        if c in _SPACE:
            return _extended(name, c, sc)
        if c not in _KEYCHAR and c != 0x2E:
            raise ConfigSyntaxError("bad section name")
        name.append(_lower(c))
    if not name:
        raise ConfigSyntaxError("empty section name")
    return name.decode("ascii") + "."


def _extended(name: bytearray, c: int, sc: _Scan) -> str:
    """``[section "sub"]`` after the first whitespace byte ``c``."""
    if not name:
        raise ConfigSyntaxError("empty section name")
    while True:
        if c == 10:
            raise ConfigSyntaxError("incomplete section header")
        c = sc.get()
        if sc.over:
            raise ConfigSyntaxError("unterminated section header")
        if c not in _SPACE:
            break
    if c != 0x22:
        raise ConfigSyntaxError("bad subsection")
    sub = bytearray()
    while True:
        c = sc.get()
        if sc.over or c == 10:
            raise ConfigSyntaxError("incomplete section header")
        if c == 0x22:
            break
        if c == 0x5C:  # backslash: the next byte is literal
            c = sc.get()
            if sc.over or c == 10:
                raise ConfigSyntaxError("incomplete section header")
        sub.append(c)
    if sc.get() != 0x5D:
        raise ConfigSyntaxError("bad subsection terminator")
    return name.decode("ascii") + "." + sub.decode("utf-8", "surrogateescape") + "."


_ESCAPES = {ord("t"): 0x09, ord("b"): 0x08, ord("n"): 0x0A, 0x5C: 0x5C, 0x22: 0x22}


def _value(sc: _Scan) -> bytes:
    """Parse a value after ``=`` up to (not including) its terminating newline."""
    out = bytearray()
    quote = False
    comment = False
    space = 0
    while True:
        c = sc.get()
        if c == 10:
            if quote:
                raise ConfigSyntaxError("unterminated quote")
            sc.pos -= 1
            return bytes(out)
        if comment:
            continue
        if c in _SPACE and not quote:
            if out:
                space += 1
            continue
        if not quote and c in (0x23, 0x3B):
            comment = True
            continue
        if space:
            out.extend(b" " * space)
            space = 0
        if c == 0x5C:
            c = sc.get()
            if c == 10:
                if sc.over:
                    sc.pos -= 1  # a backslash at the very end: the value ends here
                continue  # a backslash-newline joins the lines
            if c not in _ESCAPES:
                raise ConfigSyntaxError("bad escape")
            out.append(_ESCAPES[c])
            continue
        if c == 0x22:
            quote = not quote
            continue
        out.append(c)


# --- keys ---------------------------------------------------------------------------------


def split_key(key: str) -> tuple[str, str | None, str]:
    """``(section, subsection or None, name)`` of a canonical key."""
    section, _, rest = key.partition(".")
    if "." not in rest:
        return section, None, rest
    sub, _, name = rest.rpartition(".")
    return section, sub, name


def include_kind(key: str) -> str | None:
    """``include`` for ``include.path``, ``includeif`` for ``includeif.<cond>.path``."""
    if key == "include.path":
        return "include"
    if key.startswith("includeif.") and key.endswith(".path") and len(key) > len("includeif..path"):
        return "includeif"
    return None


_BENIGN_BRANCH = frozenset({"remote", "merge", "rebase", "pushremote", "description"})
_BENIGN_REMOTE = frozenset({"gh-resolved", "fetch", "prune", "tagopt"})


def is_benign(key: str) -> bool:
    """The keys routine git and forge commands write (docs/interface.md, Baseline)."""
    section, sub, name = split_key(key)
    if sub is None:
        return False
    if section == "branch":
        return name in _BENIGN_BRANCH or name == "vscode-merge-base"
    if section == "remote":
        return name in _BENIGN_REMOTE
    return False


Pattern = tuple[str, str | None, str]  # section, subsection (None: any or none), name


def compile_ignores(patterns: Iterable[str]) -> list[Pattern]:
    """``section.key``, ``section.*.key`` or ``section.<subsection>.key`` (global config only)."""
    out: list[Pattern] = []
    for p in patterns:
        section, _, rest = p.partition(".")
        sub, dot, name = rest.rpartition(".")
        if not dot:  # section.key
            out.append((section.lower(), None, rest.lower()))
        else:
            out.append((section.lower(), sub, name.lower()))
    return out


def is_ignored(key: str, patterns: Iterable[Pattern]) -> bool:
    section, sub, name = split_key(key)
    for psec, psub, pname in patterns:
        if psec != section or pname != name:
            continue
        if psub is None:
            if sub is None:
                return True
        elif psub == "*":
            if sub is not None:
                return True
        elif psub == sub:
            return True
    return False


_PATTERN_OK = re.compile(r"[A-Za-z][A-Za-z0-9-]*(\.[^\n]+)?\.[A-Za-z][A-Za-z0-9-]*\Z")


def valid_pattern(p: str) -> bool:
    return _PATTERN_OK.fullmatch(p) is not None
