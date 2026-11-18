"""The ``browse`` view-model and renderer: pure functions of state, no curses, no terminal.

``Browser`` holds what the screen shows (records, filter, selection, the detail and status views)
and turns keys into state changes; ``Browser.render(height, width)`` returns the lines to draw.
``browse_ui`` is the thin curses driver around it. Nothing here writes anything: the records come
from a loader (the same code path as ``list``) and status results from a ``StatusHandle`` (a child
``wtctl status`` run by ``browse_status``).

Every string that reaches a ``Line`` has passed through :func:`sanitize`: record fields, labels,
paths, branch names and status output are agent-controlled text.
"""

from __future__ import annotations

import datetime as dt
import shlex
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .commands import show as show_cmd
from .commands.list import HEADERS, age
from .errors import WtctlError
from .output import _escape_char

MIN_HEIGHT = 5
MIN_WIDTH = 24
MAX_FILTER = 64
MAX_FIELD = 1024  # characters kept of any one value in the detail view
MAX_CELL = 40  # columns of any one table cell
MAX_DETAIL_LINES = 2000
MAX_STATUS_LINES = 500

Style = Literal["normal", "header", "selected", "dim", "title", "error"]


@dataclass(frozen=True)
class Line:
    text: str
    style: Style = "normal"


@dataclass(frozen=True)
class StatusOutcome:
    ok: bool
    lines: tuple[str, ...]


class StatusHandle(Protocol):
    def poll(self) -> StatusOutcome | None: ...

    def cancel(self) -> None: ...


StatusStarter = Callable[[str], StatusHandle]
Loader = Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]]]]


# --- sanitizing ---------------------------------------------------------------------------


def _escape(ch: str) -> str:
    out = _escape_char(ch)  # control, separators, format (bidi) characters, surrogates
    if out != ch:
        return out
    cat = unicodedata.category(ch)
    if cat in ("Co", "Cn", "Mn", "Me"):  # private/unassigned and zero-width marks (overpaint)
        return f"\\u{ord(ch):04x}" if ord(ch) <= 0xFFFF else f"\\U{ord(ch):08x}"
    return ch


def char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def clip(text: str, columns: int) -> str:
    """``text`` cut to at most ``columns`` display columns."""
    out: list[str] = []
    used = 0
    for ch in text:
        w = char_width(ch)
        if used + w > columns:
            break
        out.append(ch)
        used += w
    return "".join(out)


def sanitize(value: object, columns: int) -> str:
    """One display-safe line of at most ``columns`` columns.

    Control characters (ESC, CSI, OSC, CR, LF, tab, DEL, C1), bidi and other format characters,
    line separators, surrogates, private-use and unassigned code points, and zero-width marks are
    shown as visible ``\\xNN`` or ``\\uNNNN`` escapes, so text can neither inject terminal
    sequences nor repaint or reorder what is around it. Longer text ends in ``~``.
    """
    raw = str(value)
    limit = max(columns, 0)
    shown = [_escape(ch) for ch in raw[: limit + 1]]  # each character is at least one column
    if len(raw) <= limit and sum(display_width(x) for x in shown) <= limit:
        return "".join(shown)
    if limit == 0:
        return ""
    out: list[str] = []
    used = 0
    for piece in shown:
        w = display_width(piece)
        if used + w > limit - 1:
            break
        out.append(piece)
        used += w
    return "".join(out) + "~"


def _wrap(text: str, width: int, indent: str = "  ") -> list[str]:
    """Hard-wrap an already sanitized string to ``width`` columns (continuations indented)."""
    if width <= 0:
        return []
    out: list[str] = []
    cur = ""
    cur_w = 0
    prefix = ""
    for ch in text:
        w = char_width(ch)
        if cur_w + w > width:
            out.append(cur)
            prefix = indent[: max(width - 2, 0)]
            cur, cur_w = prefix, display_width(prefix)
        cur += ch
        cur_w += w
    out.append(cur)
    return out


# --- the browser --------------------------------------------------------------------------


@dataclass
class _Item:
    record: dict[str, Any]
    foreign: bool
    key: str
    cells: tuple[str, ...]
    haystack: str


@dataclass
class _StatusView:
    handle: StatusHandle | None = None
    outcome: StatusOutcome | None = None
    cancelled: bool = False


def _label_strings(record: dict[str, Any]) -> list[str]:
    labels = record.get("labels") or []
    out: list[str] = []
    for item in labels:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out.append(f"{item.get('key', '')}={item.get('value', '')}")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            out.append(f"{item[0]}={item[1]}")
    return out


def _cells(record: dict[str, Any], foreign: bool, now: dt.datetime) -> tuple[str, ...]:
    """The table row: the same columns as ``list`` (``HEADERS``)."""
    if foreign:
        return ("-", "foreign", "-", "-", "-", str(record.get("branch") or "-"), "-")
    is_throwaway = record.get("object_type") == "throwaway"
    ref = record.get("ref") if is_throwaway else record.get("name")
    name = str(ref or "-")
    return (
        str(record.get("id", "?")),
        str(record.get("object_type", "?")),
        str(record.get("state", "?")),
        str(record.get("repo") or "-"),
        name[:12] if is_throwaway else name,
        str(record.get("branch") or "-"),
        age(str(record.get("created", "")), now),
    )


def _make_item(record: dict[str, Any], foreign: bool, now: dt.datetime) -> _Item:
    cells = tuple(sanitize(c, MAX_FIELD) for c in _cells(record, foreign, now))
    extra = [str(record.get("path") or ""), *_label_strings(record)]
    haystack = " ".join([*cells, *(sanitize(x, MAX_FIELD) for x in extra)]).lower()
    key = "foreign:" + str(record.get("path", "")) if foreign else str(record.get("id", ""))
    return _Item(record, foreign, key, cells, haystack)


HELP = (
    "wtctl browse: a read-only view of the records `list`, `show` and `status` print.",
    "",
    "  j / down, k / up   move (or scroll the detail view)",
    "  pgdn, pgup         page; g / home, G / end: first and last",
    "  enter, l / right   open the record (the `show` view)",
    "  esc, h / left      back",
    "  /                  filter (substring of id, repo, name, branch, labels, path); esc clears",
    "  s                  in the record view: run `status` (may refresh the mirror); c cancels",
    "  r                  reload the records",
    "  ?                  this help",
    "  q                  quit",
    "",
    "browse never changes a record or a worktree. To act, run the command it shows",
    "(for example `wtctl remove <id>`) yourself, and answer that command's own prompts.",
)


class Browser:
    def __init__(
        self,
        loader: Loader,
        start_status: StatusStarter,
        *,
        now: Callable[[], dt.datetime],
    ) -> None:
        self._loader = loader
        self._start_status = start_status
        self._now = now
        self._items: list[_Item] = []
        self._view: list[_Item] = []
        self.filter_text = ""
        self._editing = False
        self._cursor = 0
        self._top = 0
        self._mode: Literal["list", "detail", "help"] = "list"
        self._back: Literal["list", "detail"] = "list"
        self._scroll = 0
        self._message = ""
        self._message_error = False
        self._status: dict[str, _StatusView] = {}
        self._load()  # errors of the first load propagate (bad filters are exit 2)

    # -- data -------------------------------------------------------------------------------

    def _load(self) -> None:
        records, foreign = self._loader()
        now = self._now()
        keep = self.selected_id()
        self._items = [
            *(_make_item(r, False, now) for r in records),
            *(_make_item(f, True, now) for f in foreign),
        ]
        self._apply_filter(keep)

    def _apply_filter(self, keep: str | None = None) -> None:
        needle = self.filter_text.lower()
        self._view = [i for i in self._items if needle in i.haystack]
        self._cursor = 0
        if keep is not None:
            for n, item in enumerate(self._view):
                if item.key == keep or item.record.get("id") == keep:
                    self._cursor = n
                    break

    def _current(self) -> _Item | None:
        return self._view[self._cursor] if 0 <= self._cursor < len(self._view) else None

    def selected_id(self) -> str | None:
        item = self._current()
        if item is None:
            return None
        return item.key if item.foreign else str(item.record.get("id"))

    def visible_ids(self) -> list[str]:
        return [i.key if i.foreign else str(i.record.get("id")) for i in self._view]

    # -- time -------------------------------------------------------------------------------

    def tick(self) -> bool:
        """Collect finished status runs; True when the screen changed."""
        changed = False
        for view in self._status.values():
            if view.handle is not None:
                outcome = view.handle.poll()
                if outcome is not None:
                    view.outcome, view.handle, changed = outcome, None, True
        return changed

    def close(self) -> None:
        for view in self._status.values():
            if view.handle is not None:
                view.handle.cancel()
                view.handle = None

    # -- keys -------------------------------------------------------------------------------

    def handle_key(self, key: str) -> bool:
        """Apply one key (a single character or a name such as ``down``); False means quit."""
        if self._editing:
            self._filter_key(key)
            return True
        if key in ("q", "Q"):
            return False
        if self._mode == "help":
            if key in ("esc", "?", "enter", "left", "h", "backspace"):
                self._mode = self._back
            return True
        if key == "?":
            self._back = "detail" if self._mode == "detail" else "list"
            self._mode = "help"
            return True
        if self._mode == "detail":
            self._detail_key(key)
        else:
            self._list_key(key)
        return True

    def _filter_key(self, key: str) -> None:
        if key == "enter":
            self._editing = False
        elif key == "esc":
            self._editing = False
            self.filter_text = ""
            self._apply_filter(self.selected_id())
        elif key == "backspace":
            self.filter_text = self.filter_text[:-1]
            self._apply_filter()
        elif len(key) == 1 and key.isprintable() and len(self.filter_text) < MAX_FILTER:
            self.filter_text += key
            self._apply_filter()

    def _list_key(self, key: str) -> None:
        self._message = ""
        last = len(self._view) - 1
        page = max(self._page(), 1)
        if key in ("j", "down"):
            self._cursor = min(self._cursor + 1, max(last, 0))
        elif key in ("k", "up"):
            self._cursor = max(self._cursor - 1, 0)
        elif key == "pgdn":
            self._cursor = min(self._cursor + page, max(last, 0))
        elif key == "pgup":
            self._cursor = max(self._cursor - page, 0)
        elif key in ("g", "home"):
            self._cursor = 0
        elif key in ("G", "end"):
            self._cursor = max(last, 0)
        elif key in ("enter", "l", "right"):
            if self._current() is not None:
                self._mode, self._scroll = "detail", 0
        elif key == "/":
            self._editing = True
        elif key == "r":
            self._reload()
        elif key == "esc" and self.filter_text:
            self.filter_text = ""
            self._apply_filter(self.selected_id())

    def _detail_key(self, key: str) -> None:
        self._message = ""
        if key in ("esc", "h", "left", "backspace"):
            self._mode = "list"
        elif key in ("j", "down"):
            self._scroll += 1
        elif key in ("k", "up"):
            self._scroll = max(self._scroll - 1, 0)
        elif key == "pgdn":
            self._scroll += max(self._page(), 1)
        elif key == "pgup":
            self._scroll = max(self._scroll - max(self._page(), 1), 0)
        elif key in ("g", "home"):
            self._scroll = 0
        elif key in ("G", "end"):
            self._scroll = 10**9  # clamped when rendering
        elif key == "s":
            self._start()
        elif key == "c":
            self._cancel()
        elif key == "r":
            self._reload()

    def _reload(self) -> None:
        try:
            self._load()
        except WtctlError as err:
            self._say(f"reload failed: {err.message}", error=True)
        else:
            self._say(f"reloaded {len(self._items)} entries")

    def _say(self, message: str, *, error: bool = False) -> None:
        self._message = sanitize(message, 300)
        self._message_error = error

    def _start(self) -> None:
        item = self._current()
        if item is None or item.foreign:
            self._say("status needs a record (a foreign entry has none)")
            return
        if any(v.handle is not None for v in self._status.values()):
            self._say("a status run is already in progress (c cancels it)")
            return
        rid = str(item.record["id"])
        try:
            handle = self._start_status(rid)
        except (OSError, WtctlError) as exc:
            self._status[rid] = _StatusView(outcome=StatusOutcome(False, (f"cannot start: {exc}",)))
            return
        self._status[rid] = _StatusView(handle=handle)

    def _cancel(self) -> None:
        item = self._current()
        view = self._status.get(str(item.record.get("id"))) if item else None
        if view is not None and view.handle is not None:
            view.handle.cancel()
            view.handle, view.cancelled, view.outcome = None, True, None

    def _page(self) -> int:
        return getattr(self, "_body_height", 10) - 1

    # -- rendering --------------------------------------------------------------------------

    def render(self, height: int, width: int) -> list[Line]:
        if height <= 0 or width <= 1:
            return []
        cols = width - 1  # never write the last cell
        if height < MIN_HEIGHT or width < MIN_WIDTH:
            note = f"terminal too small (need {MIN_WIDTH}x{MIN_HEIGHT})"
            return [Line(clip(note, cols), "error")][:height]
        body = height - 3
        self._body_height = body
        if self._mode == "help":
            lines = [Line(x) for x in HELP]
            title = "help"
            hints = "esc back  q quit"
            top = 0
        elif self._mode == "detail" and (item := self._current()) is not None:
            title = self._detail_title(item)
            all_lines = self._detail_lines(item, cols)
            self._scroll = min(self._scroll, max(len(all_lines) - body, 0))
            lines, top = all_lines, self._scroll
            hints = "esc back  j/k scroll  s status  c cancel  r reload  ? help  q quit"
        else:
            self._mode = "list"
            title, lines, top = self._list_screen(body, cols)
            hints = (
                "/ filter: " + sanitize(self.filter_text, 40) + "_  enter apply  esc clear"
                if self._editing
                else "j/k move  enter open  / filter  r reload  ? help  q quit"
            )
        visible = lines[top : top + body]
        out = [Line(clip(title, cols), "title")]
        out += [Line(clip(x.text, cols), x.style) for x in visible]
        out += [Line("") for _ in range(body - len(visible))]
        out.append(Line(clip(self._message, cols), "error" if self._message_error else "dim"))
        out.append(Line(clip(hints, cols), "dim"))
        return out[:height]

    def _list_screen(self, body: int, cols: int) -> tuple[str, list[Line], int]:
        flt = f"  filter: {sanitize(self.filter_text, 30)}" if self.filter_text else ""
        title = f"wtctl browse  {len(self._view)}/{len(self._items)} entries{flt}"
        widths = [
            min(max([len(h), *(display_width(i.cells[n]) for i in self._items)]), MAX_CELL)
            for n, h in enumerate(HEADERS)
        ]

        def row(cells: Sequence[str]) -> str:
            parts = [
                clip(c, widths[n]) + " " * (widths[n] - min(display_width(c), widths[n]))
                for n, c in enumerate(cells)
            ]
            return "  ".join(parts).rstrip()

        lines = [Line(row(HEADERS), "header")]
        rows_h = max(body - 1, 1)
        if not self._view:
            note = "no records match the filter" if self._items else "no records"
            return title, [*lines, Line(note, "dim")], 0
        self._cursor = min(self._cursor, len(self._view) - 1)
        if self._cursor < self._top:
            self._top = self._cursor
        if self._cursor >= self._top + rows_h:
            self._top = self._cursor - rows_h + 1
        self._top = max(min(self._top, len(self._view) - 1), 0)
        for n in range(self._top, min(self._top + rows_h, len(self._view))):
            lines.append(Line(row(self._view[n].cells), "selected" if n == self._cursor else "normal"))
        return title, lines, 0

    def _detail_title(self, item: _Item) -> str:
        if item.foreign:
            return "foreign worktree (no record)"
        return f"{sanitize(item.record.get('id'), 40)}  [{sanitize(item.record.get('state'), 20)}]"

    def _detail_lines(self, item: _Item, cols: int) -> list[Line]:
        rec = item.record
        lines: list[Line] = []

        def add(text: str, style: Style = "normal") -> None:
            for part in _wrap(text, cols):
                lines.append(Line(part, style))

        if item.foreign:
            add("type: foreign (git lists this worktree; wtctl has no record of it)")
            for k in ("path", "branch", "head", "clone_path"):
                add(f"{k}: {sanitize(rec.get(k) if rec.get(k) is not None else '-', MAX_FIELD)}")
            return lines[:MAX_DETAIL_LINES]
        try:
            text = show_cmd.render_human(rec)
        except (KeyError, TypeError, IndexError):  # a record not in the schema's shape
            text = "\n".join(f"{k}: {v}" for k, v in rec.items() if v is not None)
        for raw in text.split("\n"):
            add(sanitize(raw, MAX_FIELD))
        rid = shlex.quote(str(rec.get("id")))
        lines.append(Line(""))
        add(sanitize(f"to remove: wtctl remove {rid}", MAX_FIELD), "dim")
        add("  (run it yourself; browse never removes anything)", "dim")
        lines.append(Line(""))
        lines += self._status_lines(str(rec.get("id")), cols)
        return lines[:MAX_DETAIL_LINES]

    def _status_lines(self, rid: str, cols: int) -> list[Line]:
        view = self._status.get(rid)
        out: list[Line] = []

        def add(text: str, style: Style = "normal") -> None:
            for part in _wrap(sanitize(text, MAX_FIELD), cols):
                out.append(Line(part, style))

        if view is None:
            add("-- status: not run (press s; it may refresh the mirror from the network)", "dim")
        elif view.handle is not None:
            add("-- status: running... (c cancels)", "dim")
        elif view.cancelled:
            add("-- status: cancelled (press s to run again)", "dim")
        elif view.outcome is not None:
            ok = view.outcome.ok
            add("-- status" if ok else "-- status failed", "title" if ok else "error")
            for text in view.outcome.lines[:MAX_STATUS_LINES]:
                add(text, "normal" if ok else "error")
        return out

