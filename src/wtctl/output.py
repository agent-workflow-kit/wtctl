"""Human tables and ``--json`` rendering."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence
from typing import Any

from .errors import WtctlError


def _escape_char(ch: str) -> str:
    cat = unicodedata.category(ch)
    if cat == "Cc":
        return f"\\x{ord(ch):02x}"
    # Line/paragraph separators, bidi and other format characters, lone surrogates.
    if cat in ("Zl", "Zp", "Cf", "Cs"):
        return f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}"
    return ch


def escape_control(text: str) -> str:
    """Escape control and terminal-spoofing characters of untrusted text.

    Control characters (Cc) become ``\\xNN``; line/paragraph separators, format characters
    (bidi overrides) and lone surrogates (Zl, Zp, Cf, Cs) become ``\\uNNNN``, or ``\\UNNNNNNNN``
    above U+FFFF. A literal backslash is not doubled, so the output is not injective (a literal
    ``\\x1b`` reads like an escaped ESC); it is for display, never for parsing back.
    """
    return "".join(_escape_char(ch) for ch in text)


def escape_text_lines(text: str) -> str:
    """Like :func:`escape_control` but keeps ``\\n`` as the line break (multi-line text).

    Only ``\\n`` is kept: tabs and carriage returns are escaped (``\\x09``, ``\\x0d``) on purpose,
    so handlers must not rely on them in human or stderr text.
    """
    return "\n".join(escape_control(line) for line in text.split("\n"))


def render_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """A left-aligned text table; every cell is escaped, so a cell never adds lines."""
    cells = [[escape_control(str(c)) for c in row] for row in [headers, *rows]]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    return "\n".join(lines)


def render_json(data: Any) -> str:
    """One line of JSON (ASCII only, so no raw control or surrogate ever reaches the terminal)."""
    return json.dumps(data, separators=(",", ":"), sort_keys=False, allow_nan=False)


def render_error(err: WtctlError, *, as_json: bool) -> tuple[str, str]:
    """Return ``(stdout, stderr)`` text for an error: the object on stdout only with ``--json``."""
    message = escape_control(err.message)
    reason = err.fields.get("reason")
    if reason:
        message = f"{message} ({escape_control(str(reason))})"
    stderr = f"wtctl: error: {message}\n"
    return (render_json(err.to_object()), stderr) if as_json else ("", stderr)
