"""Names, branches and paths derived by ``create`` (docs/interface.md, Identifiers > Names, Locations).

All pure: nothing here touches git, state or the file system. A derived name always matches the
name grammar, so it is a single safe path component.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_NAME = 64
MAX_SUFFIX = 1000  # candidates tried before create gives up on finding a free name

_OUTSIDE = re.compile(r"[^A-Za-z0-9._-]")
_RUNS = re.compile(r"-{2,}")
_BRANCH_VARS = ("name", "id", "date")


def name_from_branch(branch: str) -> str:
    """The name derived from a branch: each ``/`` and each character outside the name grammar
    becomes ``-``, runs of ``-`` collapse, leading ``-`` and ``.`` are dropped, and the result is
    cut to 64 characters. Empty when nothing usable is left (the caller falls back to the id)."""
    text = _RUNS.sub("-", _OUTSIDE.sub("-", branch))
    text = text.lstrip("-.")
    return text[:MAX_NAME]


def name_from_id(record_id: str) -> str:
    """The id's 8 characters (``wt-`` dropped)."""
    return record_id[3:]


def suffixed(base: str, n: int) -> str:
    """``base-<n>`` with the base cut so the result fits 64 characters."""
    tail = f"-{n}"
    return base[: MAX_NAME - len(tail)] + tail


def candidates(base: str) -> Iterator[str]:
    """``base``, then ``base-2``, ``base-3`` ... (at most ``MAX_SUFFIX`` names)."""
    yield base
    for n in range(2, MAX_SUFFIX + 1):
        yield suffixed(base, n)


def _render(template: str, values: dict[str, str]) -> str:
    """Replace ``{key}`` for the known keys in one pass; anything else stays literal."""
    pattern = re.compile("|".join(re.escape("{" + k + "}") for k in values))
    return pattern.sub(lambda m: values[m.group(0)[1:-1]], template)


def render_branch(prefix: str, template: str, *, name: str, record_id: str, date: str) -> str:
    """``branch_prefix`` + ``branch_template`` with ``{name}``, ``{id}`` and ``{date}``."""
    return _render(prefix + template, {"name": name, "id": record_id, "date": date})


def render_path(
    template: str, *, root: str, host: str, path: str, name: str, record_id: str
) -> str:
    """The worktree path from ``worktree_path``; it must lie strictly below ``root`` after
    normalization (ValueError otherwise, and for a newline: git 2.34's porcelain is line-based)."""
    values = {
        "root": root,
        "host": host.replace(":", "_"),
        "path": path,
        "name": name,
        "id": record_id,
    }
    rendered = _render(template, values)
    if "\n" in rendered or "\x00" in rendered:
        raise ValueError("the rendered path contains a newline or NUL")
    norm = posixpath.normpath(rendered)
    base = posixpath.normpath(root)
    if not posixpath.isabs(norm) or not norm.startswith(base.rstrip("/") + "/"):
        raise ValueError(f"the rendered path {rendered!r} is not strictly below {root!r}")
    if norm != rendered.rstrip("/"):
        # a `..` or `//` that normalization would change is a template bug or an attack
        raise ValueError(f"the rendered path {rendered!r} is not normalized")
    return norm
