"""``wtctl export``: print the records of audit.jsonl (default) or log.jsonl, oldest first."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable

from ..audit import parse_ts, read_records
from ..context import Context, Result
from ..errors import UsageError
from ..output import render_json
from ..state import state_path

_DURATION = re.compile(r"([1-9][0-9]{0,9})([smhdw])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(text: str, now: dt.datetime) -> dt.datetime:
    """``--since``: a DURATION back from ``now``, or an RFC 3339 time with an offset."""
    m = _DURATION.fullmatch(text)
    if m:
        try:
            return now - dt.timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2)])
        except OverflowError:
            raise UsageError(f"--since {text[:40]!r} is too far back") from None
    when = parse_ts(text)
    if when is None:
        raise UsageError(
            f"invalid --since {text[:80]!r}: expected an RFC 3339 time with an offset "
            "(2026-01-31T12:00:00Z) or a DURATION such as 36h"
        )
    return when


def run(ctx: Context) -> Result:
    args = ctx.args
    since = parse_since(args.since, ctx.gateway.clock.now()) if args.since is not None else None
    name = "log.jsonl" if args.log else "audit.jsonl"
    reader = read_records(state_path().parent / name, since=since)  # refuses unsafe files here

    def stream(as_json: bool, write: Callable[[str], None]) -> str:
        try:
            if as_json:
                write("[")
                for n, rec in enumerate(reader):
                    write(("," if n else "") + render_json(rec))
                write("]\n")
            else:
                for rec in reader:
                    write(render_json(rec) + "\n")
        finally:
            reader.close()
            # Set here too: a mid-stream failure must not lose the count (see cli._emit_result).
            result.stderr = note()
        return result.stderr

    def note() -> str:
        return f"skipped {reader.skipped} unreadable line(s) in {name}" if reader.skipped else ""

    result = Result(data=None, stream=stream)
    return result
