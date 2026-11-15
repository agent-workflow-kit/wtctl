"""Label grammar and multiset rules (docs/interface.md, Labels).

A record's labels are a set of ``(key, value)`` pairs: a key may carry several values and adding
an identical pair is a no-op. Every violation is a usage error (exit 2).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .errors import UsageError

MAX_LABELS = 32

_KEY = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_VALUE = re.compile(r"[A-Za-z0-9._:/#@+=,-]{0,256}")

Pair = tuple[str, str]


def parse_label(text: str) -> Pair:
    """Split ``key=value`` at the first ``=`` and validate both parts."""
    key, sep, value = text.partition("=")
    if not sep:
        raise UsageError(f"invalid label {text!r}: expected key=value")
    # fullmatch: `$` would accept a trailing newline.
    if not _KEY.fullmatch(key):
        raise UsageError(f"invalid label key {key!r}: expected ^[a-z0-9][a-z0-9_.-]{{0,63}}$")
    if not _VALUE.fullmatch(value):
        raise UsageError(
            f"invalid label value for key {key!r}: 0 to 256 characters from [A-Za-z0-9._:/#@+=,-]"
        )
    return key, value


def render(pairs: Iterable[Pair]) -> list[str]:
    """Sorted, unique ``key=value`` strings (the ``--json`` form)."""
    return sorted({f"{k}={v}" for k, v in pairs})


def merge_labels(current: Iterable[Pair], additions: Iterable[str]) -> set[Pair]:
    """``current`` plus the parsed ``additions``; raises (changing nothing) if any is bad."""
    result = set(current)
    result.update(parse_label(a) for a in additions)
    if len(result) > MAX_LABELS:
        raise UsageError(f"a record holds at most {MAX_LABELS} label pairs")
    return result


def remove_labels(current: Iterable[Pair], removals: Iterable[str]) -> set[Pair]:
    """``current`` without the given pairs; removing an absent pair is a no-op."""
    result = set(current)
    result.difference_update(parse_label(r) for r in removals)
    return result
