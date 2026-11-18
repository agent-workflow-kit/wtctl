"""The curses driver of ``browse``: terminal checks, key mapping, drawing.

Everything it draws comes from ``Browser.render`` (already sanitized); this file only moves
pixels. Python's ``curses`` is in the standard library on Linux and macOS (not on Windows, which
is not a target), so ``browse`` adds no dependency (docs/design.md, D64).
"""

from __future__ import annotations

import codecs
import locale
import os
import signal
import sys
import threading
from collections.abc import Mapping
from typing import Any

from .browse_model import Browser, Line
from .errors import UsageError

POLL_MS = 100


def unsupported_reason(
    *, stdin_isatty: bool, stdout_isatty: bool, term: str | None, has_curses: bool
) -> str | None:
    """Why ``browse`` cannot run here, or None."""
    if not has_curses:
        return "the curses module is not available in this Python"
    if not stdin_isatty or not stdout_isatty:
        return "stdin and stdout must be a terminal"
    if not term or term.lower() == "dumb":
        return f"TERM is {'unset' if not term else repr(term)}, which has no cursor control"
    return None


def have_curses() -> bool:
    try:
        import curses  # noqa: F401
    except ImportError:
        return False
    return True


def isatty(stream: Any) -> bool:
    try:
        return stream is not None and stream.isatty()
    except ValueError:
        return False


def check_interactive(*, stdin_isatty: bool, env: Mapping[str, str] | None = None) -> None:
    """Raise ``UsageError`` (exit 2) unless a full-screen view can run."""
    reason = unsupported_reason(
        stdin_isatty=stdin_isatty,
        stdout_isatty=isatty(sys.stdout),
        term=(os.environ if env is None else env).get("TERM"),
        has_curses=have_curses(),
    )
    if reason is not None:
        raise UsageError(f"`wtctl browse` is interactive only: {reason}")
    term = (os.environ if env is None else env).get("TERM") or ""
    _check_terminfo(term)


def _check_terminfo(term: str) -> None:
    """An unknown TERM is a usage error (exit 2), not an internal error from ``initscr``."""
    import curses

    try:
        fd = sys.stdout.fileno()
    except (OSError, ValueError, AttributeError):
        fd = 1
    try:
        curses.setupterm(term, fd)
    except curses.error as exc:
        raise UsageError(
            f"`wtctl browse` is interactive only: TERM is {term!r}, which the terminal "
            f"database does not know ({exc})"
        ) from None


def _output_encoding() -> str:
    """The encoding ncurses writes in (the C library's, after ``setlocale``), else ascii."""
    try:
        name = locale.nl_langinfo(locale.CODESET)
        codecs.lookup(name)
    except (AttributeError, LookupError, ValueError):
        return "ascii"
    return name or "ascii"


class _Started:
    drawn = False


def _raise_exit(signum: int, _frame: Any) -> None:
    raise SystemExit(128 + signum)  # unwinds through curses.wrapper and Browser.close


def run(browser: Browser) -> None:
    import curses

    try:
        locale.setlocale(locale.LC_CTYPE, "")
    except locale.Error:  # an uninstalled locale: carry on with the C locale
        pass
    os.environ.setdefault("ESCDELAY", "100")  # slow escape sequences must not split into ESC
    started = _Started()
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGHUP, signal.SIGTERM):
            previous[sig] = signal.signal(sig, _raise_exit)
    try:
        curses.wrapper(_guarded, browser, started)
    except KeyboardInterrupt:  # ^C during terminal setup is a quit too, not a traceback
        return
    except curses.error as exc:
        if started.drawn:
            raise
        term = os.environ.get("TERM", "")
        raise UsageError(
            f"`wtctl browse` is interactive only: cannot initialize the terminal "
            f"(TERM={term!r}): {exc}"
        ) from None
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        browser.close()


def _key_name(curses: Any, key: Any) -> str | None:
    if isinstance(key, str):
        if key in ("\n", "\r"):
            return "enter"
        if key == "\x1b":
            return "esc"
        if key in ("\x7f", "\b"):
            return "backspace"
        return key if key.isprintable() else None
    names = {
        curses.KEY_UP: "up",
        curses.KEY_DOWN: "down",
        curses.KEY_LEFT: "left",
        curses.KEY_RIGHT: "right",
        curses.KEY_PPAGE: "pgup",
        curses.KEY_NPAGE: "pgdn",
        curses.KEY_HOME: "home",
        curses.KEY_END: "end",
        curses.KEY_BACKSPACE: "backspace",
        curses.KEY_ENTER: "enter",
        curses.KEY_RESIZE: "resize",
    }
    return names.get(key)


def _attrs(curses: Any) -> dict[str, int]:
    attrs = {
        "normal": 0,
        "header": curses.A_BOLD,
        "selected": curses.A_REVERSE,
        "dim": curses.A_DIM,
        "title": curses.A_BOLD,
        "error": curses.A_BOLD,
    }
    if curses.has_colors():
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_RED, -1)
            attrs["error"] = curses.color_pair(1) | curses.A_BOLD
        except curses.error:
            pass
    return attrs


def _draw(curses: Any, scr: Any, lines: list[Line], attrs: dict[str, int], enc: str) -> None:
    scr.erase()
    height, width = scr.getmaxyx()
    for y, line in enumerate(lines[:height]):
        text = line.text.encode(enc, "backslashreplace").decode(enc)
        try:
            scr.addnstr(y, 0, text, max(width - 1, 0), attrs.get(line.style, 0))
        except curses.error:
            pass
    scr.refresh()


def _guarded(scr: Any, browser: Browser, started: _Started) -> None:
    try:
        _loop(scr, browser, started)
    except KeyboardInterrupt:  # ^C is a quit, not a traceback
        return


def _loop(scr: Any, browser: Browser, started: _Started) -> None:
    import curses

    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    scr.timeout(POLL_MS)
    attrs = _attrs(curses)
    enc = _output_encoding()
    dirty = True
    while True:
        if dirty:
            height, width = scr.getmaxyx()
            _draw(curses, scr, browser.render(height, width), attrs, enc)
            dirty = False
            started.drawn = True
        try:
            key = scr.get_wch()
        except curses.error:  # timeout: look for a finished status run
            dirty = browser.tick()
            continue
        except KeyboardInterrupt:
            return
        if key == "\x03":  # a raw ^C (when the terminal does not turn it into SIGINT)
            return
        name = _key_name(curses, key)
        if name is None:
            continue
        if not browser.handle_key(name):
            return
        dirty = True
