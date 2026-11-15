"""Disposable-path patterns in gitignore syntax (docs/interface.md, Content checks, ``ignored``).

A trailing ``/`` matches directories only; a pattern without a ``/`` (apart from a trailing one)
matches at any depth, one with a ``/`` is anchored at the worktree root; ``*`` and ``?`` do not
cross ``/``; ``**`` as a whole component crosses any number of directories; ``[...]`` is a
character class. A path is disposable when it, or any directory above it, matches (git does not
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
            j = i + 1
            if j < n and glob[j] in "!^":
                j += 1
            if j < n and glob[j] == "]":
                j += 1
            while j < n and glob[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
                i += 1
                continue
            body = glob[i + 1 : j]
            neg = body[:1] in ("!", "^")
            if neg:
                body = body[1:]
            body = body.replace("\\", "\\\\").replace("[", "\\[")
            out.append(f"[{'^' if neg else ''}{body}]")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def _compile(raw: str) -> _Pattern | None:
    if not raw or raw.startswith(("!", "#")):
        return None
    p = raw
    dir_only = p.endswith("/")
    p = p.rstrip("/")
    if not p:
        return None
    anchored = "/" in p
    p = p.lstrip("/")
    if not p:
        return None
    try:
        return _Pattern(re.compile(_translate(p), re.DOTALL), dir_only, anchored)
    except re.error:
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
