"""Sparse checkouts of a worktree (docs/interface.md, Sparse checkouts).

A sparse worktree is a cone: the files directly in the top directory, everything below each listed
directory, and the files directly in the directories that lead to them. The cone is the only
shape wtctl creates and the only one it understands, so it can compute for itself which tracked
paths a sparse checkout leaves out, and never has to trust the skip-worktree bits or the pattern
file that sit in the agent-writable admin area.
"""

from __future__ import annotations

import os

from .errors import UsageError

MAX_DIRS = 256
MAX_DIR_LENGTH = 1024
MAX_COMPONENT = 255
# What a gitignore-style pattern line would read as syntax, plus backslash.
_FORBIDDEN = frozenset('*?[]\\!#"')


def _bad(raw: str, why: str) -> UsageError:
    return UsageError(f"--sparse {raw[:80]!r}: {why}")


def normalize_dir(raw: str) -> str:
    """One validated cone directory: a relative path of plain components, ``/`` separated, with
    at most one trailing ``/`` (dropped). Anything git could read as an option, a pattern, a
    parent reference or a second line is refused."""
    if not raw:
        raise _bad(raw, "empty")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        raise _bad(raw, "not valid UTF-8") from None
    if len(raw.encode("utf-8")) > MAX_DIR_LENGTH:
        raise _bad(raw, f"longer than {MAX_DIR_LENGTH} bytes")
    if raw.startswith("-"):
        raise _bad(raw, "starts with '-'")
    if raw.startswith("/"):
        raise _bad(raw, "is absolute")
    text = raw[:-1] if raw.endswith("/") else raw
    if not text:
        raise _bad(raw, "empty")
    for ch in text:
        if ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _FORBIDDEN:
            raise _bad(raw, f"contains the character {ch!r}")
    parts = text.split("/")
    for p in parts:
        if p == "":
            raise _bad(raw, "has an empty component")
        if p in (".", ".."):
            raise _bad(raw, f"has the component {p!r}")
        if p.casefold() == ".git":
            raise _bad(raw, "names a .git directory")
        if p != p.strip() or p.endswith("."):
            raise _bad(raw, "a component starts or ends with whitespace or ends with '.'")
        if len(p.encode("utf-8")) > MAX_COMPONENT:
            raise _bad(raw, "a component is too long")
    return "/".join(parts)


def normalize(dirs: list[str]) -> list[str]:
    """The sorted, de-duplicated cone directories, without any that lies below another one (it
    adds nothing and git writes no pattern for it). At least one, at most ``MAX_DIRS``."""
    if not dirs:
        raise UsageError("--sparse needs at least one directory")
    if len(dirs) > MAX_DIRS:
        raise UsageError(f"--sparse is given {len(dirs)} times; at most {MAX_DIRS}")
    seen = sorted({normalize_dir(d) for d in dirs})
    kept: list[str] = []
    for d in seen:
        if not any(d.startswith(k + "/") for k in kept):
            kept.append(d)
    return kept


def pattern_file(dirs: list[str]) -> bytes:
    """``info/sparse-checkout`` for ``dirs`` exactly as ``git sparse-checkout set`` writes a cone:
    the top directory's files, then each parent directory's files, then each directory whole."""
    lines = ["/*", "!/*/"]
    parents: set[str] = set()
    for d in dirs:
        parts = d.split("/")
        for i in range(1, len(parts)):
            parents.add("/".join(parts[:i]))
    for p in sorted(parents):
        lines += [f"/{p}/", f"!/{p}/*/"]
    lines += [f"/{d}/" for d in dirs]
    return ("\n".join(lines) + "\n").encode("utf-8")


class Cone:
    """Which tracked paths a cone sparse checkout of ``dirs`` includes."""

    def __init__(self, dirs: list[str]) -> None:
        self.dirs = list(dirs)
        self._parents = {
            "/".join(d.split("/")[:i]) for d in dirs for i in range(1, d.count("/") + 1)
        }

    def includes(self, path: str) -> bool:
        parent = os.path.dirname(path)
        if parent == "" or parent in self._parents:
            return True
        return any(path.startswith(d + "/") for d in self.dirs)
