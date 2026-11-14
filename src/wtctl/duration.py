"""DURATION values (docs/interface.md, Identifiers): ``^[1-9][0-9]*[smhdw]$``."""

from __future__ import annotations

import re

from .errors import UsageError

_DURATION = re.compile(r"[1-9][0-9]*[smhdw]")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str, *, flag: str = "duration") -> int:
    """Seconds in ``text``; anything outside the grammar is a usage error (exit 2)."""
    # fullmatch: `$` would accept a trailing newline.
    if not _DURATION.fullmatch(text):
        raise UsageError(
            f"invalid {flag} {text!r}: expected a positive integer and s, m, h, d or w"
        )
    try:
        count = int(text[:-1])
    except ValueError:  # beyond Python's digit limit: valid, just absurdly large
        count = 10**400
    return count * _UNITS[text[-1]]
