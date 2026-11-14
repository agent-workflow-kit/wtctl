"""``log.jsonl`` and ``audit.jsonl``: the append-only, public records of what wtctl did.

Callers (the command handlers, hooks, cron) use one :class:`Journal`, from :func:`journal_for`:

* ``journal.log(event, level=..., id=..., repo=..., actor=..., decision_ids=..., details=...)``
  appends a ``schemas/log.json`` record. ``event`` is one of :data:`LOG_EVENTS`.
* ``journal.audit(action, actor=..., decision_ids=..., target_id=..., target_path=...,
  inputs=..., result=...)`` appends a ``schemas/audit.json`` record. ``action`` is one of
  :data:`AUDIT_ACTIONS`; ``inputs`` must have exactly the keys :data:`AUDIT_INPUTS` lists for it.

Both are written by the caller *after* the state change they describe succeeded (the state module
writes neither). A failed write of an audit record raises :class:`JournalIOError`
(``internal``, exit 1): the caller must not report success for an unaudited action.
:meth:`Journal.log_best_effort` returns a warning text instead of raising.

Guarantees:

* One record is one line of ASCII JSON, written with a single ``write`` on an ``O_APPEND``
  descriptor, so concurrent processes never interleave inside a line. A line is at most
  :data:`MAX_LINE_BYTES`; log records that would be longer are shrunk (long strings, long lists
  and deep nesting are cut and marked), audit records fail loudly instead of losing data.
* The directory is 0700 and the files 0600. A symlinked directory or file, a hard-linked or
  non-regular file, or one owned by someone else is refused (no-follow opens relative to the
  directory descriptor).
* If the file does not end in a newline (a crash or a full disk cut the last line), the next
  record starts on a fresh line, so it is never glued to the torn one. The check can race
  with another writer and then adds a blank line. Readers skip torn and blank lines.
* Secrets never reach a record: URL userinfo is removed, token-shaped strings and
  ``key: value``/``key=value`` pairs for secret-looking keys are replaced by ``[redacted]``,
  values of secret-named keys are dropped, and any value in ``secrets`` is masked.
* There is no rotation: the files are append-only contracts that other tools read (design guide
  section 6).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .actor import ACTOR_RE
from .errors import WtctlError

if TYPE_CHECKING:
    from .context import Context
    from .gateway import Clock

LOG_EVENTS = (
    "create",
    "throwaway_create",
    "bootstrap",
    "remove",
    "throwaway_remove",
    "refused",
    "extract",
    "baseline_accept",
    "prune",
    "reconcile",
    "clone",
    "discovered",
    "adopt",
    "forget",
    "label",
    "cron",
    "fetch",
    "hook",
)
LOG_LEVELS = ("info", "warn", "error")
AUDIT_ACTIONS = (
    "remove",
    "branch_delete",
    "baseline_accept",
    "adopt",
    "clone_accept",
    "admin_entry_delete",
)
AUDIT_RESULTS = ("ok", "error")
# Exactly the keys of `inputs` per action (schemas/audit.json: additionalProperties false).
AUDIT_INPUTS: Mapping[str, frozenset[str]] = {
    "remove": frozenset({"head", "merged_head", "baseline_digest", "overridden"}),
    "branch_delete": frozenset({"branch", "sha"}),
    "baseline_accept": frozenset({"old_digest", "new_digest", "items", "anchors"}),
    "adopt": frozenset({"path", "baseline_digest", "holder"}),
    "clone_accept": frozenset({"repo", "clone_path", "anchors"}),
    "admin_entry_delete": frozenset({"admin_entry", "head", "branch", "tip"}),
}

MAX_LINE_BYTES = 32 * 1024  # including the newline
MAX_DECISION_IDS = 32
_PRECUT = 16 * 1024  # strings longer than this are cut before the regexes run
_MIN_SECRET = 6  # shorter configured secrets are ignored: masking them would shred records
# (max string chars, max items per list/dict, max depth), tried in order for log records.
_LIMITS = ((4096, 1000, 8), (512, 100, 6), (128, 20, 4))
REDACTED = "[redacted]"

_ID_RE = re.compile(r"wt-[a-z2-7]{8}")
_REPO_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+"
)
_DECISION_RE = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")

_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@?#]*@")
_TOKEN_SHAPES = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{10,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}"
)
_KEY_VALUE = re.compile(
    r"(?i)([A-Za-z0-9_.-]{0,40}(?:authorization|private-token|token|password|passwd|secret|api[_-]?key))"
    r"(\s*[:=]\s*)(?:(?:basic|bearer|token)\s+)?(?:'[^']*'|\"[^\"]*\"|[^\s'\"&,;]+)"
)
_SECRET_KEY = re.compile(
    r"(?i)token|secret|passw(?:or)?d|authorization|api[_-]?key|cookie|credential"
    r"|private[_-]?key|headers?"
)

# Seams for tests (disk full, EIO).
_os_write = os.write
_os_fsync = os.fsync


def _internal(message: str) -> WtctlError:
    return WtctlError("internal", message)


class JournalIOError(WtctlError):
    """A log or audit file could not be used or written (exit 1, ``internal``)."""

    def __init__(self, message: str) -> None:
        super().__init__("internal", message)


def _io(message: str) -> JournalIOError:
    return JournalIOError(message)


def _fmt_ts(when: dt.datetime) -> str:
    """UTC, fixed width (``YYYY-MM-DDTHH:MM:SS.mmmZ``), the same form state.db uses."""
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def normalize_decision_ids(ids: Iterable[Any]) -> tuple[list[str], list[str]]:
    """Split ids into (valid, deduplicated, at most 32) and warning texts for the dropped ones.

    For ids returned by hooks, which are dropped with a warning when invalid or in excess
    (docs/interface.md). :meth:`Journal.log` and :meth:`Journal.audit` themselves are strict.
    """
    out: list[str] = []
    warnings: list[str] = []
    for raw in ids:
        if not isinstance(raw, str) or not _DECISION_RE.fullmatch(raw):
            warnings.append(f"dropped an invalid decision id: {str(raw)[:40]!r}")
        elif raw in out:
            continue
        elif len(out) >= MAX_DECISION_IDS:
            warnings.append(f"dropped decision id {raw!r}: at most {MAX_DECISION_IDS} are kept")
        else:
            out.append(raw)
    return out, warnings


class _Sanitizer:
    """Turn arbitrary values into bounded, redacted, JSON-safe ones."""

    def __init__(self, secrets: Sequence[str], max_str: int, max_items: int, max_depth: int):
        self.secrets = [s for s in secrets if len(s) >= _MIN_SECRET]
        self.max_str = max_str
        self.max_items = max_items
        self.max_depth = max_depth
        self.truncated = False

    def text(self, s: str) -> str:
        if len(s) > _PRECUT:
            self.truncated = True
            cut = s[:_PRECUT]
            # Do not leave half a token at the edge: back up to the last whitespace.
            ws = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
            s = cut[: ws if ws > _PRECUT - 256 else _PRECUT]
        s = _URL_USERINFO.sub(r"\1", s)
        s = _TOKEN_SHAPES.sub(REDACTED, s)
        s = _KEY_VALUE.sub(rf"\1\2{REDACTED}", s)
        for secret in self.secrets:
            s = s.replace(secret, REDACTED)
        if len(s) > self.max_str:
            self.truncated = True
            s = f"{s[: self.max_str]}...[truncated {len(s)} chars]"
        return s

    def value(self, v: Any, depth: int = 0) -> Any:
        if v is None or isinstance(v, bool | int):
            return v
        if isinstance(v, float):
            return v if v == v and v not in (float("inf"), float("-inf")) else str(v)
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, bytes | bytearray):
            return self.text(bytes(v).decode("utf-8", "replace"))
        if depth >= self.max_depth:
            self.truncated = True
            return "...[truncated depth]"
        if isinstance(v, Mapping):
            out: dict[str, Any] = {}
            for n, (k, item) in enumerate(v.items()):
                if n >= self.max_items:
                    self.truncated = True
                    break
                key = self.text(k if isinstance(k, str) else str(k))
                secret_key = isinstance(k, str) and bool(_SECRET_KEY.search(k))
                if REDACTED in key and key != k:
                    secret_key = True
                    key = REDACTED
                base, i = key, 1
                while key in out:
                    i += 1
                    key = f"{base}#{i}"
                out[key] = REDACTED if secret_key else self.value(item, depth + 1)
            return out
        if isinstance(v, list | tuple):
            if len(v) > self.max_items:
                self.truncated = True
            return [self.value(x, depth + 1) for x in list(v)[: self.max_items]]
        return self.text(str(v))


def _dump(record: Mapping[str, Any]) -> bytes:
    return json.dumps(record, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode(
        "ascii"
    )


def _check_actor(actor: Any, *, nullable: bool) -> None:
    if actor is None and nullable:
        return
    if not isinstance(actor, str) or not ACTOR_RE.fullmatch(actor):
        raise _internal(f"invalid actor {actor!r}")


def _check_decision_ids(ids: Any) -> list[str]:
    if isinstance(ids, str) or not isinstance(ids, Iterable):
        raise _internal("decision_ids must be a list of strings")
    out = list(ids)
    if len(out) > MAX_DECISION_IDS:
        raise _internal(f"at most {MAX_DECISION_IDS} decision ids")
    for d in out:
        if not isinstance(d, str) or not _DECISION_RE.fullmatch(d):
            raise _internal(f"invalid decision id {str(d)[:40]!r}")
    return out


def _check_pattern(value: Any, pattern: re.Pattern[str], what: str, *, nullable: bool) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _internal(f"invalid {what} {str(value)[:80]!r}")


class Journal:
    """Appends records to ``log.jsonl`` and ``audit.jsonl`` in ``directory``."""

    def __init__(self, directory: Path, *, clock: Clock, secrets: Iterable[str] = ()) -> None:
        self.directory = Path(directory)
        self.clock = clock
        self.secrets = [s for s in secrets if isinstance(s, str)]

    @property
    def log_path(self) -> Path:
        return self.directory / "log.jsonl"

    @property
    def audit_path(self) -> Path:
        return self.directory / "audit.jsonl"

    # --- public API ---------------------------------------------------------------------------

    def log(
        self,
        event: str,
        *,
        actor: str | None,
        level: str = "info",
        id: str | None = None,  # noqa: A002 - the record's field name
        repo: str | None = None,
        decision_ids: Iterable[str] = (),
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a log record and return it. Raises ``internal`` for an invalid field."""
        if event not in LOG_EVENTS:
            raise _internal(f"unknown log event {event!r}")
        if level not in LOG_LEVELS:
            raise _internal(f"unknown log level {level!r}")
        _check_pattern(id, _ID_RE, "record id", nullable=True)
        _check_pattern(repo, _REPO_RE, "repo id", nullable=True)
        _check_actor(actor, nullable=True)
        dids = _check_decision_ids(decision_ids)
        if details is None:
            details = {}
        if not isinstance(details, Mapping):
            raise _internal("log details must be an object")
        base = {
            "ts": _fmt_ts(self.clock.now()),
            "level": level,
            "event": event,
            "id": id,
            "repo": repo,
            "actor": actor,
            "decision_ids": dids,
        }
        for max_str, max_items, max_depth in _LIMITS:
            s = _Sanitizer(self.secrets, max_str, max_items, max_depth)
            record = {**base, "details": s.value(details)}
            line = _dump(record)
            if len(line) < MAX_LINE_BYTES:
                break
        else:
            record = {**base, "details": {"truncated": True}}
            line = _dump(record)
        self._append(self.log_path, line, durable=False)
        return record

    def log_best_effort(self, event: str, **kw: Any) -> str | None:
        """Like :meth:`log`, but a write failure returns a warning text instead of raising."""
        try:
            self.log(event, **kw)
        except JournalIOError as exc:
            return f"could not write log.jsonl: {exc.message}"
        return None

    def audit(
        self,
        action: str,
        *,
        actor: str | None,
        decision_ids: Iterable[str],
        target_id: str | None,
        target_path: str,
        inputs: Mapping[str, Any],
        result: str = "ok",
    ) -> dict[str, Any]:
        """Append an audit record (fsynced) and return it.

        ``decision_id`` is null with no ids, a string with one, a list with several. Raises
        ``internal`` for an invalid field or an unrepresentable ``inputs`` (too large: nothing is
        cut), ``JournalIOError`` when the write fails.
        """
        if action not in AUDIT_ACTIONS:
            raise _internal(f"unknown audit action {action!r}")
        if result not in AUDIT_RESULTS:
            raise _internal(f"unknown audit result {result!r}")
        _check_actor(actor, nullable=False)
        dids = _check_decision_ids(decision_ids)
        _check_pattern(target_id, _ID_RE, "target id", nullable=True)
        if (
            not isinstance(target_path, str)
            or not target_path.startswith("/")
            or "\n" in target_path
        ):
            raise _internal(f"invalid target path {str(target_path)[:80]!r}")
        if not isinstance(inputs, Mapping) or set(inputs) != AUDIT_INPUTS[action]:
            raise _internal(
                f"audit inputs for {action} must be exactly {sorted(AUDIT_INPUTS[action])}"
            )
        s = _Sanitizer(self.secrets, 4096, 1000, 8)
        record = {
            "ts": _fmt_ts(self.clock.now()),
            "actor": actor,
            "decision_id": None if not dids else dids[0] if len(dids) == 1 else dids,
            "target": {"id": target_id, "path": s.text(target_path)},
            "action": action,
            "inputs": s.value(inputs),
            "result": result,
        }
        line = _dump(record)
        if s.truncated or len(line) >= MAX_LINE_BYTES:
            raise _internal(
                f"audit record for {action} is too large to record whole "
                f"(limit {MAX_LINE_BYTES} bytes)"
            )
        self._append(self.audit_path, line, durable=True)
        return record

    # --- file handling ------------------------------------------------------------------------

    def _open_dir(self) -> int:
        d = self.directory
        try:
            try:
                os.lstat(d)
            except FileNotFoundError:
                d.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.mkdir(d, 0o700)
                except FileExistsError:
                    pass
                else:
                    os.chmod(d, 0o700)  # not subject to the umask
            fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise _io(f"cannot use {d} (a symlink is refused): {exc.strerror}") from exc
        try:
            st = os.fstat(fd)
            if st.st_uid != os.geteuid():
                raise _io(f"{d} is not owned by the current user")
            if stat.S_IMODE(st.st_mode) != 0o700:
                os.fchmod(fd, 0o700)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _append(self, path: Path, line: bytes, *, durable: bool) -> None:
        try:
            dfd = self._open_dir()
        except WtctlError:
            raise
        except OSError as exc:  # fchmod
            raise _io(f"cannot use {self.directory}: {exc.strerror}") from exc
        try:
            try:
                fd = os.open(
                    path.name,
                    os.O_RDWR
                    | os.O_APPEND
                    | os.O_CREAT
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK
                    | os.O_CLOEXEC,
                    0o600,
                    dir_fd=dfd,
                )
            except OSError as exc:
                raise _io(f"cannot open {path} (a symlink is refused): {exc.strerror}") from exc
            try:
                self._write_line(fd, path, line, durable)
            finally:
                os.close(fd)
        finally:
            os.close(dfd)

    def _write_line(self, fd: int, path: Path, line: bytes, durable: bool) -> None:
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
                raise _io(f"{path} is not a plain regular file owned by the current user")
            if stat.S_IMODE(st.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            data = line + b"\n"
            if st.st_size > 0 and os.pread(fd, 1, st.st_size - 1) != b"\n":
                data = b"\n" + data  # a torn last line: start on a fresh one
            view = memoryview(data)
            while view:
                n = _os_write(fd, bytes(view))
                if n <= 0:
                    raise OSError(0, "write made no progress")
                view = view[n:]
            if durable:
                _os_fsync(fd)
        except OSError as exc:
            raise _io(f"cannot write {path}: {exc.strerror or exc}") from exc


MAX_READ_LINE = 1024 * 1024  # a longer line cannot be ours; readers skip it


def _parse_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


def read_records(
    path: Path, *, since: dt.datetime | None = None
) -> tuple[list[dict[str, Any]], int]:
    """The JSON-object lines of a journal file, oldest (file order) first, and the number skipped.

    Read-only: nothing is created. A missing file or directory is an empty journal. Blank lines
    are ignored silently; torn, oversized, non-UTF-8, non-object and non-finite-number lines are
    skipped and counted. With ``since``, only records whose ``ts`` is at or after it are kept (a
    record with no parseable ``ts`` is dropped). A symlinked file or directory is refused.
    """
    try:
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return [], 0
    except OSError as exc:
        raise _io(f"cannot read {path.parent} (a symlink is refused): {exc.strerror}") from exc
    try:
        try:
            fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=dfd,
            )
        except FileNotFoundError:
            return [], 0
        except OSError as exc:
            raise _io(f"cannot read {path} (a symlink is refused): {exc.strerror}") from exc
    finally:
        os.close(dfd)
    out: list[dict[str, Any]] = []
    skipped = 0
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise _io(f"{path} is not a regular file")
        while True:
            raw = f.readline(MAX_READ_LINE + 1)
            if not raw:
                break
            if len(raw) > MAX_READ_LINE:
                skipped += 1
                while raw and not raw.endswith(b"\n"):  # drop the rest of the long line
                    raw = f.readline(MAX_READ_LINE)
                continue
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8"), parse_constant=_parse_constant)
            except (ValueError, RecursionError):
                skipped += 1
                continue
            if not isinstance(rec, dict):
                skipped += 1
                continue
            if since is not None:
                when = parse_ts(rec.get("ts"))
                if when is None or when < since:
                    continue
            out.append(rec)
    return out, skipped


_RFC3339 = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:[Zz]|[+-]\d{2}:\d{2})"
)


def parse_ts(value: Any) -> dt.datetime | None:
    """An RFC 3339 time with an explicit offset as an aware datetime, else None."""
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        return None
    text = value.upper()
    if "." in text:  # fromisoformat takes at most 6 fractional digits
        head, rest = text.split(".", 1)
        digits = re.match(r"\d+", rest)
        assert digits is not None
        text = f"{head}.{digits.group()[:6]}{rest[digits.end() :]}"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def journal_for(ctx: Context) -> Journal:
    """The journal for this invocation: the state directory and the gateway's clock."""
    from .state import state_path

    # TODO(#3): pass the values of the configured ``hosts.<host>.token_env`` variables as
    # ``secrets=`` so they are masked even when they do not look like a token.
    return Journal(state_path().parent, clock=ctx.gateway.clock)
