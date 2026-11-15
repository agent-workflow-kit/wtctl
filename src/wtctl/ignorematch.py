"""Disposable-path patterns in gitignore syntax (docs/interface.md, Content checks, ``ignored``).

A trailing ``/`` matches directories only; a pattern without a ``/`` (apart from a trailing one)
matches at any depth, one with a ``/`` is anchored at the worktree root; ``*`` and ``?`` do not
cross ``/``; ``**`` as a whole component crosses any number of directories; ``[...]`` is a
character class (an unclosed one, or one with a POSIX class, matches nothing). A path is
disposable when it, or any directory above it, matches (git does not
look inside an excluded directory). A pattern this module does not understand (``!`` negation,
comments) matches nothing: a path it fails to exempt stays dirty, which is the safe direction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern[str]
    dir_only: bool
    anchored: bool


class _NeverMatches(Exception):
    """The pattern can match nothing: git aborts on it (an unclosed ``[``) or this module does not
    parse it (a POSIX class ``[:alpha:]``); not matching is the safe direction."""


def _bracket(glob: str, start: int) -> tuple[str, int]:
    """The regex of the character class opening at ``glob[start]`` and the index after its ``]``.
    An unclosed class or one with a POSIX class (``[:`` inside) raises ``_NeverMatches``
    (git's wildmatch aborts on the first and this module does not implement the second). As in
    git's wildmatch with WM_PATHNAME: a backslash quotes the next character
    (so ``\\-`` is a literal ``-``, not a range), ``]`` first in the class is a literal, and a
    class never matches ``/`` (not even a negated one, or a range such as ``+-0`` that spans it)."""
    n = len(glob)
    j = start + 1
    neg = j < n and glob[j] in "!^"
    if neg:
        j += 1
    first = j
    items: list[tuple[str, bool]] = []  # (character, quoted)
    while True:
        if j >= n:
            raise _NeverMatches
        c = glob[j]
        if c == "]" and j > first:
            break
        if c == "[" and glob.startswith("[:", j):
            raise _NeverMatches
        if c == "\\":
            if j + 1 >= n:
                raise _NeverMatches
            items.append((glob[j + 1], True))
            j += 2
        else:
            items.append((c, False))
            j += 1
    parts: list[str] = []
    k = 0
    while k < len(items):
        ch = items[k][0]
        if k + 2 < len(items) and items[k + 1] == ("-", False):
            hi = items[k + 2][0]
            if ch <= hi:  # git matches nothing for a reversed range
                parts.append(f"{re.escape(ch)}-{re.escape(hi)}")
            k += 3
        else:
            parts.append(re.escape(ch))
            k += 1
    if not parts:
        body = "(?!)" if not neg else "[^/]"
        return body, j + 1
    return f"(?!/)[{'^' if neg else ''}{''.join(parts)}]", j + 1


def _translate(glob: str) -> str:
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "\\" and i + 1 < n:
            out.append(re.escape(glob[i + 1]))
            i += 2
        elif c == "*":
            if glob.startswith("**", i):
                before = i == 0 or glob[i - 1] == "/"
                after = i + 2 == n or glob[i + 2] == "/"
                if before and after:
                    if i + 2 == n:  # trailing "/**": everything below
                        out.append(".*")
                        i += 2
                    else:  # "**/": zero or more directories
                        out.append("(?:.*/)?")
                        i += 3
                    continue
                i += 2  # "**" inside a component acts as "*"
                out.append("[^/]*")
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            body, i = _bracket(glob, i)
            out.append(body)
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _quoted(p: str, idx: int) -> bool:
    """Whether the character at ``idx`` is preceded by an odd number of backslashes."""
    k = idx
    while k > 0 and p[k - 1] == "\\":
        k -= 1
    return (idx - k) % 2 == 1


def _dangling_backslash(p: str) -> bool:
    return p.endswith("\\") and not _quoted(p, len(p) - 1)


def _compile(raw: str) -> _Pattern | None:
    if not raw or raw.startswith(("!", "#")):
        return None
    p = raw
    while p.endswith(" ") and not _quoted(p, len(p) - 1):  # git drops unquoted trailing spaces
        p = p[:-1]
    if _dangling_backslash(p):
        return None  # git never matches a pattern that ends in a lone backslash
    dir_only = p.endswith("/")
    if dir_only:
        p = p[:-1]  # exactly one: git keeps the rest, so "a//" is "a/" and matches nothing
    if not p:
        return None
    anchored = "/" in p
    p = p.lstrip("/")
    if not p:
        return None
    try:
        return _Pattern(re.compile(_translate(p), re.DOTALL), dir_only, anchored)
    except (re.error, _NeverMatches):
        return None


class Disposable:
    """The compiled ``remove.disposable`` patterns of one repo."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self._patterns = [p for p in map(_compile, patterns) if p is not None]

    def matches(self, rel: str, *, is_dir: bool) -> bool:
        """Whether ``rel`` (relative to the worktree root, no trailing slash) is disposable."""
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            candidate_dir = is_dir or i < len(parts)
            for pat in self._patterns:
                if pat.dir_only and not candidate_dir:
                    continue
                target = "/".join(parts[:i]) if pat.anchored else parts[i - 1]
                if pat.regex.fullmatch(target):
                    return True
        return False
