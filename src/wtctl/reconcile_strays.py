"""Strays: leftovers reconcile reports and never deletes (docs/interface.md, Reconcile).

A directory under the throwaway root with no live record, a ``*.wtctl-tmp-*`` directory left by an
interrupted clone, and an entry of ``mirrors/`` that no clone row names. Listing only: names are
read with ``lstat`` semantics and nothing is opened, followed or changed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

from . import mirror
from .fsafe import escape_for_details

TMP_NAME = re.compile(r".+\.wtctl-tmp-[A-Za-z0-9_-]+\Z")
MAX_ENTRIES = 20_000
MAX_DEPTH = 8


def _names(directory: str) -> Iterator[str]:
    """Entry names of ``directory`` (capped); nothing when it is missing or not a directory."""
    try:
        with os.scandir(directory) as it:
            for n, e in enumerate(it):
                if n >= MAX_ENTRIES:
                    return
                yield e.name
    except OSError:
        return


def _stray(path: str, kind_of: str) -> dict[str, Any]:
    return {"kind": "stray", "path": escape_for_details(path), "kind_of": kind_of}


def throwaway_strays(root: str, live_ids: set[str]) -> list[dict[str, Any]]:
    return [
        _stray(os.path.join(root, n), "throwaway")
        for n in sorted(_names(root))
        if n not in live_ids
    ]


def clone_tmp_strays(clone_root: str, clone_dirs: list[str]) -> list[dict[str, Any]]:
    """Leftover temporary clone directories below ``clone_root`` (a bounded walk that never
    follows a link and never enters a clone) and next to each configured clone."""
    found: set[str] = set()
    budget = [MAX_ENTRIES]

    def walk(directory: str, depth: int) -> None:
        for name in sorted(_names(directory)):
            budget[0] -= 1
            if budget[0] < 0:
                return
            full = os.path.join(directory, name)
            try:
                if not os.path.isdir(full) or os.path.islink(full):
                    continue
            except OSError:
                continue
            if TMP_NAME.fullmatch(name):
                found.add(full)
            elif depth < MAX_DEPTH and not os.path.lexists(os.path.join(full, ".git")):
                walk(full, depth + 1)

    walk(clone_root, 0)
    for clone in clone_dirs:
        parent, base = os.path.split(clone.rstrip("/"))
        for name in _names(parent):
            if name.startswith(base + ".wtctl-tmp-") and TMP_NAME.fullmatch(name):
                found.add(os.path.join(parent, name))
    return [_stray(p, "clone_tmp") for p in sorted(found)]


def mirror_strays(state: Any) -> list[dict[str, Any]]:
    root = str(mirror.mirrors_root())
    named = state.mirror_paths()
    return [
        _stray(os.path.join(root, n), "mirror")
        for n in sorted(_names(root))
        if os.path.join(root, n) not in named
    ]
