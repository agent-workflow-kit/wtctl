"""``state.db``: schema, migrations, leases, ids, compare-and-set transitions and labels.

The database is private to wtctl (design guide section 6). Everything that changes it runs in one
``BEGIN IMMEDIATE`` transaction, so concurrent processes serialise on SQLite's write lock.
Process liveness (for lease takeover) and the clock come from the gateway.

Plugs in as ``open_state(ctx)`` (see ``context.py``); the handle is a ``State``.
"""

from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import labels as labelmod
from .errors import UsageError, WtctlError
from .gateway import Gateway, ProcessUnreadable

if TYPE_CHECKING:
    from .context import Context

Record = dict[str, Any]

LIVE_STATES = ("creating", "active", "unlinked", "missing", "clone_missing", "partial")
TOMBSTONE_STATES = ("gone", "removed", "forgotten")
STATES = LIVE_STATES + TOMBSTONE_STATES
OBJECT_TYPES = ("worktree", "throwaway")

GLOBAL_LEASE = "global"
LEASE_TTL = 600.0  # 10 minutes, renewed while held (docs/design.md, Concurrency)
_POLL = 0.1
_BUSY_TIMEOUT = 30.0  # seconds SQLite waits on a locked database (tests shorten it)

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
_ID_BODY = "[a-z2-7]"

# --- schema ---------------------------------------------------------------------------------

_TOMB = "('gone','removed','forgotten')"

# Additive only: a later version adds tables, columns (ALTER TABLE ... ADD COLUMN) and indexes and
# never drops, renames or rewrites anything, so an older binary's reads keep working and it
# refuses (rather than damages) a database whose version it does not know.
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            "CREATE TABLE schema_version (version INTEGER NOT NULL)",
            # Every id ever allocated, so an id is never reused even after its tombstone is
            # deleted or when a vetoed create never wrote a record.
            "CREATE TABLE ids (id TEXT PRIMARY KEY, allocated TEXT NOT NULL)",
            f"""CREATE TABLE records (
                id TEXT PRIMARY KEY CHECK (length(id) = 11 AND id GLOB 'wt-{_ID_BODY * 8}'),
                object_type TEXT NOT NULL CHECK (object_type IN ('worktree','throwaway')),
                owned INTEGER CHECK (owned IN (0,1)),
                state TEXT NOT NULL CHECK (state IN
                    ('creating','active','unlinked','missing','clone_missing','partial',
                     'gone','removed','forgotten')),
                repo TEXT NOT NULL,
                clone_path TEXT, common_dir TEXT, common_id TEXT, admin_dir TEXT,
                base_dir TEXT, root_id TEXT,
                path TEXT NOT NULL,
                dir_id TEXT, name TEXT, branch TEXT,
                created_branch INTEGER CHECK (created_branch IN (0,1)),
                base TEXT, base_sha TEXT, head_at_create TEXT, ref TEXT, source TEXT,
                depth INTEGER, fetch_url TEXT, remote TEXT,
                reflog_start_ts TEXT, reflog_start_sha TEXT, baseline_digest TEXT,
                bootstrap_state TEXT NOT NULL DEFAULT 'none'
                    CHECK (bootstrap_state IN ('none','ok','failed','interrupted')),
                missing_since TEXT, missing_seen TEXT,
                created TEXT NOT NULL, updated TEXT NOT NULL, removed_at TEXT, last_error TEXT
            )""",
            f"""CREATE UNIQUE INDEX records_live_name ON records(repo, name)
                WHERE object_type = 'worktree' AND name IS NOT NULL AND state NOT IN {_TOMB}""",
            f"CREATE UNIQUE INDEX records_live_path ON records(path) WHERE state NOT IN {_TOMB}",
            "CREATE INDEX records_state ON records(state)",
            """CREATE TABLE holders (
                record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                holder TEXT NOT NULL, PRIMARY KEY (record_id, holder))""",
            """CREATE TABLE labels (
                record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                "key" TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY (record_id, "key", value))""",
            """CREATE TABLE stash_ids (
                record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                oid TEXT NOT NULL, PRIMARY KEY (record_id, oid))""",
            """CREATE TABLE baseline_items (
                record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                path TEXT NOT NULL, "key" TEXT, kind TEXT NOT NULL, class TEXT NOT NULL,
                digest TEXT NOT NULL, anchor_id TEXT)""",
            "CREATE INDEX baseline_items_record ON baseline_items(record_id)",
            """CREATE TABLE bootstrap_paths (
                record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                path TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT,
                PRIMARY KEY (record_id, path))""",
            """CREATE TABLE bootstrap_nested (
                record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                path TEXT NOT NULL, PRIMARY KEY (record_id, path))""",
            """CREATE TABLE bootstrap_runs (
                record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                run INTEGER NOT NULL, step INTEGER NOT NULL, argv_json TEXT NOT NULL,
                "exit" INTEGER, duration_ms INTEGER, output_tail TEXT, started TEXT,
                PRIMARY KEY (record_id, run, step))""",
            """CREATE TABLE clones (
                repo TEXT PRIMARY KEY, path TEXT NOT NULL, common_dir TEXT, common_id TEXT,
                source TEXT, fetch_url TEXT, mirror_path TEXT, verified_at TEXT,
                default_branch TEXT)""",
            """CREATE TABLE anchors (
                path TEXT PRIMARY KEY, dev_ino TEXT NOT NULL, recorded_at TEXT NOT NULL)""",
            """CREATE TABLE leases (
                name TEXT PRIMARY KEY, pid INTEGER NOT NULL, start_time TEXT NOT NULL,
                expires REAL NOT NULL)""",
        ),
    ),
)

COLUMNS = (
    "id object_type owned state repo clone_path common_dir common_id admin_dir base_dir root_id "
    "path dir_id name branch created_branch base base_sha head_at_create ref source depth "
    "fetch_url remote reflog_start_ts reflog_start_sha baseline_digest bootstrap_state "
    "missing_since missing_seen created updated removed_at last_error"
).split()
_BOOL_COLUMNS = ("owned", "created_branch")
# What `transition` may set besides the state: everything except identity and bookkeeping.
_UPDATABLE = frozenset(COLUMNS) - {"id", "object_type", "state", "created", "updated", "removed_at"}

_HOLDER_GRAMMAR = labelmod._VALUE  # same characters; 1 to 64 checked below


# --- paths and ids --------------------------------------------------------------------------


def state_path() -> Path:
    """``${XDG_STATE_HOME:-~/.local/state}/wtctl/state.db`` (a relative XDG value is ignored)."""
    base = os.environ.get("XDG_STATE_HOME", "")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".local" / "state"
    return root / "wtctl" / "state.db"


def _random_id() -> str:
    """``wt-`` + 8 base32 characters (40 random bits)."""
    return "wt-" + base64.b32encode(secrets.token_bytes(5)).decode("ascii").lower()


def record_lease(record_id: str) -> str:
    return f"record:{record_id}"


def repo_lease(repo: str) -> str:
    return f"repo:{repo}"


def clone_lease(clone_path: str) -> str:
    return f"clone:{clone_path}"


class Conflict(Exception):
    """An insert violated a uniqueness rule; ``column`` is ``id``, ``path`` or ``name``."""

    def __init__(self, column: str, message: str) -> None:
        super().__init__(message)
        self.column = column


def _internal(message: str) -> WtctlError:
    return WtctlError("internal", message)


def _integrity_error(exc: sqlite3.IntegrityError) -> Exception:
    """Conflict for a uniqueness violation on path/name/id, else ValueError."""
    msg = str(exc)
    for column in ("path", "name", "id"):
        if f"records.{column}" in msg:
            return Conflict(column, f"record {column} already in use: {msg}")
    return ValueError(f"invalid record: {msg}")


def _not_found(record_id: str, row: sqlite3.Row | tuple[Any, ...] | None) -> WtctlError:
    """exit 10; for a tombstone the error carries ``details: {state, removed_at}``."""
    if row is not None and row[0] in TOMBSTONE_STATES:
        return WtctlError(
            "not_found",
            f"record {record_id} was already {row[0]}",
            details={"state": row[0], "removed_at": row[1]},
        )
    return WtctlError("not_found", f"no record {record_id}")


# --- opening --------------------------------------------------------------------------------


def _prepare_dir(d: Path) -> None:
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            pass  # a concurrent first open created it: fall through to the checks below
        else:
            os.chmod(d, 0o700)  # not subject to the umask
            return
        st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode):
        raise _internal(f"refusing to use {d}: it is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise _internal(f"{d} is not a directory")
    if st.st_uid != os.geteuid():
        raise _internal(f"{d} is not owned by the current user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)


def _prepare_file(f: Path) -> None:
    try:
        fd = os.open(f, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise _internal(f"cannot open {f} (a symlink is refused): {exc.strerror}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise _internal(f"{f} is not a regular file owned by the current user")
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return 0
        raise
    return int(row[0] or 0)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=_BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class State:
    """An open ``state.db``. One instance per process and thread; leases heartbeat on their own."""

    def __init__(
        self,
        path: Path,
        gateway: Gateway,
        *,
        pid: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        heartbeat: bool = False,
    ) -> None:
        self.path = Path(path)
        self.gateway = gateway
        self.pid = os.getpid() if pid is None else pid
        self._sleep = sleep
        self._heartbeat = heartbeat
        self._start: str | None = None
        _prepare_dir(self.path.parent)
        _prepare_file(self.path)
        self._conn = _connect(self.path)
        try:
            self.schema_version = self._migrate()
        except BaseException:
            self._conn.close()
            raise

    def close(self) -> None:
        self._conn.close()

    # -- migrations -----------------------------------------------------------------------

    def _migrate(self) -> int:
        migrations = MIGRATIONS  # looked up late so tests can substitute
        latest = migrations[-1][0]
        conn = self._conn
        try:
            current = _current_version(conn)
            if current > latest:
                raise self._newer(current, latest)
            conn.execute("PRAGMA journal_mode = WAL")
            if current == latest:
                return current
            with self._txn():
                current = _current_version(conn)  # re-read under the write lock
                if current > latest:
                    raise self._newer(current, latest)
                for version, statements in migrations:
                    if version <= current:
                        continue
                    for sql in statements:
                        conn.execute(sql)
                    conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
                    current = version
            return current
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise WtctlError("busy", "state.db is locked by another process") from exc
            raise _internal(f"cannot use state database {self.path}: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise _internal(f"cannot use state database {self.path}: {exc}") from exc

    def _newer(self, found: int, supported: int) -> WtctlError:
        return _internal(
            f"state database {self.path} has schema version {found}, newer than the "
            f"{supported} this wtctl supports; upgrade wtctl (the database was not changed)"
        )

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction: commit on success, roll back on any exception."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise WtctlError("busy", "state.db is locked by another process") from exc
            raise
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- time -----------------------------------------------------------------------------

    def _now(self) -> str:
        """UTC, fixed width (``YYYY-MM-DDTHH:MM:SS.mmmZ``) so the text sorts like the time."""
        return self.gateway.clock.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    # -- ids ------------------------------------------------------------------------------

    def allocate_id(self) -> str:
        """A fresh record id, burned for good (a vetoed create keeps its id)."""
        for _ in range(100):
            candidate = _random_id()
            try:
                self.reserve_id(candidate)
            except Conflict:
                continue
            return candidate
        raise _internal("could not allocate a unique record id")

    def reserve_id(self, record_id: str) -> None:
        """Burn a specific id (``allocate_id`` is the normal entry); Conflict if already used."""
        _check_id(record_id)
        with self._txn() as c:
            try:
                c.execute("INSERT INTO ids(id, allocated) VALUES (?, ?)", (record_id, self._now()))
            except sqlite3.IntegrityError as exc:
                raise Conflict("id", f"id {record_id} was already allocated") from exc

    def id_allocated(self, record_id: str) -> bool:
        return (
            self._conn.execute("SELECT 1 FROM ids WHERE id = ?", (record_id,)).fetchone()
            is not None
        )

    # -- records --------------------------------------------------------------------------

    def _build(self, row: sqlite3.Row) -> Record:
        rec: Record = {k: row[k] for k in row.keys()}  # noqa: SIM118 - sqlite3.Row
        for col in _BOOL_COLUMNS:
            if rec[col] is not None:
                rec[col] = bool(rec[col])
        rid = rec["id"]
        rec["labels"] = labelmod.render(self.label_rows(rid))
        rec["holders"] = sorted(
            r[0]
            for r in self._conn.execute("SELECT holder FROM holders WHERE record_id = ?", (rid,))
        )
        return rec

    def label_rows(self, record_id: str) -> list[labelmod.Pair]:
        return [
            (r[0], r[1])
            for r in self._conn.execute(
                'SELECT "key", value FROM labels WHERE record_id = ?', (record_id,)
            )
        ]

    def get_record(self, record_id: str) -> Record | None:
        row = self._conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        return None if row is None else self._build(row)

    def records(
        self, *, states: Collection[str] | None = None, include_tombstones: bool = False
    ) -> list[Record]:
        """Records ordered by creation time then id. Live ones unless ``states`` or
        ``include_tombstones`` says otherwise. TODO(#7): label and repo filters for `list`."""
        if states is not None:
            wanted = list(states)
            for s in wanted:
                _check_state(s)
        elif include_tombstones:
            wanted = list(STATES)
        else:
            wanted = list(LIVE_STATES)
        marks = ",".join("?" * len(wanted))
        rows = self._conn.execute(
            f"SELECT * FROM records WHERE state IN ({marks}) ORDER BY created, id", wanted
        ).fetchall()
        return [self._build(r) for r in rows]

    def insert_record(
        self,
        fields: dict[str, Any],
        *,
        labels: Iterable[str] = (),
        holders: Iterable[str] = (),
    ) -> Record:
        with self._txn() as c:
            self._insert(c, fields, labels, holders)
        got = self.get_record(fields["id"])
        assert got is not None
        return got

    def insert_record_leased(
        self,
        fields: dict[str, Any],
        *,
        labels: Iterable[str] = (),
        holders: Iterable[str] = (),
        ttl: float = LEASE_TTL,
    ) -> tuple[Record, Lease]:
        """Insert a record and take its per-record lease in the same transaction (create)."""
        name = record_lease(fields["id"])
        with self._txn() as c:
            self._insert(c, fields, labels, holders)
            lease = self._insert_lease(c, name, ttl)
        lease._start_heartbeat()
        got = self.get_record(fields["id"])
        assert got is not None
        return got, lease

    def _insert(
        self,
        c: sqlite3.Connection,
        fields: dict[str, Any],
        labels: Iterable[str],
        holders: Iterable[str],
    ) -> None:
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise ValueError(f"unknown record fields: {sorted(unknown)}")
        for required in ("id", "object_type", "state", "repo", "path"):
            if fields.get(required) is None:
                raise ValueError(f"record field {required!r} is required")
        _check_id(fields["id"])
        _check_state(fields["state"])
        if fields["object_type"] not in OBJECT_TYPES:
            raise ValueError(f"unknown object_type {fields['object_type']!r}")
        if not self.id_allocated(fields["id"]):
            raise ValueError(f"id {fields['id']} was not allocated (use allocate_id first)")
        pairs = labelmod.merge_labels((), labels)
        hs = set(holders)
        if hs and fields["object_type"] == "throwaway":
            raise ValueError("a throwaway has no holders")
        for h in hs:
            _check_holder(h)
        now = self._now()
        row = {"bootstrap_state": "none", **fields}
        row.setdefault("created", now)
        row.setdefault("updated", row["created"])
        if row["state"] in TOMBSTONE_STATES:
            row.setdefault("removed_at", now)
        for col in _BOOL_COLUMNS:
            if row.get(col) is not None:
                row[col] = int(bool(row[col]))
        cols = list(row)
        try:
            c.execute(
                f"INSERT INTO records({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [row[k] for k in cols],
            )
        except sqlite3.IntegrityError as exc:
            raise _integrity_error(exc) from exc
        c.executemany(
            'INSERT INTO labels(record_id, "key", value) VALUES (?, ?, ?)',
            [(row["id"], k, v) for k, v in pairs],
        )
        c.executemany(
            "INSERT INTO holders(record_id, holder) VALUES (?, ?)", [(row["id"], h) for h in hs]
        )

    def transition(
        self,
        record_id: str,
        expected: str | Collection[str],
        new: str,
        **updates: Any,
    ) -> Record:
        """Compare-and-set: move a live record from one of ``expected`` states to ``new``.

        ``updates`` sets further columns in the same statement. A lost race raises ``busy``
        (exit 12), or ``not_found`` (exit 10, with ``details``) when the record became a
        tombstone or does not exist; nothing is changed then.
        """
        exp = [expected] if isinstance(expected, str) else list(expected)
        for s in exp:
            _check_state(s)
            if s in TOMBSTONE_STATES:
                raise ValueError("tombstones are final: they cannot be an expected state")
        _check_state(new)
        bad = set(updates) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update record fields: {sorted(bad)}")
        now = self._now()
        sets = {"state": new, "updated": now, **updates}
        if new in TOMBSTONE_STATES:
            sets["removed_at"] = now
        for col in _BOOL_COLUMNS:
            if sets.get(col) is not None:
                sets[col] = int(bool(sets[col]))
        assign = ", ".join(f"{k} = ?" for k in sets)
        marks = ",".join("?" * len(exp))
        with self._txn() as c:
            try:
                cur = c.execute(
                    f"UPDATE records SET {assign} WHERE id = ? AND state IN ({marks})",
                    [*sets.values(), record_id, *exp],
                )
            except sqlite3.IntegrityError as exc:
                raise _integrity_error(exc) from exc
            if cur.rowcount == 0:
                row = c.execute(
                    "SELECT state, removed_at FROM records WHERE id = ?", (record_id,)
                ).fetchone()
                if row is None or row[0] in TOMBSTONE_STATES:
                    raise _not_found(record_id, row)
                raise WtctlError(
                    "busy",
                    f"record {record_id} is {row[0]}, not {'/'.join(exp)}: "
                    "another command changed it first",
                )
        got = self.get_record(record_id)
        assert got is not None
        return got

    # -- addressing -----------------------------------------------------------------------

    def resolve(self, selector: str, *, allow_tombstone: bool = False) -> Record:
        """The record named by an id, an id prefix (4 to 8 characters after ``wt-``) or an
        absolute path (docs/interface.md, Addressing a record).

        Usage error (2) for a short prefix, a relative path or anything else malformed; not found
        (10) for no match; ambiguous (21) with ``candidates`` for several. Prefixes and paths
        never match tombstones; a full id does, but only returns it with ``allow_tombstone``
        (otherwise exit 10 with ``details: {state, removed_at}``).

        A relative selector made only of letters and digits is read as an id or prefix, so one
        that cannot be an id (outside ``a-z2-7``) is not found (10), not a usage error. A relative
        path that happens to look like an id prefix is taken as the prefix.
        """
        if selector.startswith("/"):
            return self._resolve_path(selector)
        body = selector[3:] if selector.startswith("wt-") else selector
        if not (1 <= len(body) <= 8 and body.isascii() and body.isalnum()):
            raise UsageError(f"{selector!r} is neither a record id (prefix) nor an absolute path")
        if len(body) < 4:
            raise UsageError(f"an id prefix needs at least 4 characters after wt-: {selector!r}")
        if not all(ch in _ID_ALPHABET for ch in body):
            raise WtctlError(
                "not_found", f"no record matches {selector!r} (expected an id or an absolute path)"
            )
        if len(body) == 8:
            full = "wt-" + body
            row = self._conn.execute("SELECT * FROM records WHERE id = ?", (full,)).fetchone()
            if row is None:
                raise WtctlError("not_found", f"no record {full}")
            if row["state"] in TOMBSTONE_STATES and not allow_tombstone:
                raise _not_found(full, (row["state"], row["removed_at"]))
            return self._build(row)
        rows = self._conn.execute(
            "SELECT * FROM records WHERE substr(id, 4, ?) = ? AND state IN "
            f"({','.join('?' * len(LIVE_STATES))}) ORDER BY id",
            (len(body), body, *LIVE_STATES),
        ).fetchall()
        if not rows:
            raise WtctlError("not_found", f"no record matches {selector!r}")
        if len(rows) > 1:
            raise WtctlError(
                "ambiguous",
                f"{selector!r} matches {len(rows)} records",
                candidates=[{"id": r["id"], "path": r["path"]} for r in rows],
            )
        return self._build(rows[0])

    def _resolve_path(self, path: str) -> Record:
        try:
            real = os.path.realpath(path)
        except ValueError as exc:
            raise UsageError(f"invalid path: {exc}") from exc
        try:
            row = self._conn.execute(
                "SELECT * FROM records WHERE path = ? AND state IN "
                f"({','.join('?' * len(LIVE_STATES))})",
                (real, *LIVE_STATES),
            ).fetchone()
        except UnicodeEncodeError:  # not representable as text, so no record can hold it
            row = None
        if row is None:
            raise WtctlError("not_found", f"no record for path {path}")
        return self._build(row)

    # -- labels ---------------------------------------------------------------------------

    def _require_live(self, c: sqlite3.Connection, record_id: str) -> None:
        row = c.execute(
            "SELECT state, removed_at FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None or row[0] in TOMBSTONE_STATES:
            raise _not_found(record_id, row)

    def add_labels(self, record_id: str, additions: Iterable[str]) -> list[str]:
        """Add ``key=value`` pairs (identical pairs are a no-op; at most 32 in all). Returns the
        record's labels, sorted. All or nothing."""
        with self._txn() as c:
            self._require_live(c, record_id)
            current = self.label_rows(record_id)
            merged = labelmod.merge_labels(current, additions)
            added = merged - set(current)
            c.executemany(
                'INSERT INTO labels(record_id, "key", value) VALUES (?, ?, ?)',
                [(record_id, k, v) for k, v in added],
            )
            if added:
                c.execute("UPDATE records SET updated = ? WHERE id = ?", (self._now(), record_id))
            return labelmod.render(merged)

    def remove_labels(self, record_id: str, removals: Iterable[str]) -> list[str]:
        with self._txn() as c:
            self._require_live(c, record_id)
            current = set(self.label_rows(record_id))
            kept = labelmod.remove_labels(current, removals)
            gone = current - kept
            c.executemany(
                'DELETE FROM labels WHERE record_id = ? AND "key" = ? AND value = ?',
                [(record_id, k, v) for k, v in gone],
            )
            if gone:
                c.execute("UPDATE records SET updated = ? WHERE id = ?", (self._now(), record_id))
            return labelmod.render(kept)

    # -- leases ---------------------------------------------------------------------------

    def _own_start(self) -> str:
        if self._start is None:
            try:
                start = self.gateway.procs.start_time(self.pid)
            except ProcessUnreadable as exc:
                raise _internal(f"cannot determine this process's start time: {exc}") from exc
            if start is None:
                raise _internal(f"cannot determine this process's start time (pid {self.pid})")
            self._start = start
        return self._start

    def _holder_dead(self, pid: int, start: str, expires: float, now: float) -> bool:
        """The recorded holder is gone: no such PID, or the PID now belongs to another process.

        If liveness cannot be read the holder is assumed alive until its lease expires.
        """
        try:
            current = self.gateway.procs.start_time(pid)
        except ProcessUnreadable:
            return expires < now
        return current is None or current != start

    def _insert_lease(self, c: sqlite3.Connection, name: str, ttl: float) -> Lease:
        expires = self.gateway.clock.now().timestamp() + ttl
        c.execute(
            "INSERT INTO leases(name, pid, start_time, expires) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET pid = excluded.pid,"
            " start_time = excluded.start_time, expires = excluded.expires",
            (name, self.pid, self._own_start(), expires),
        )
        return Lease(self, name, self._own_start(), ttl)

    def _try_take(self, name: str, ttl: float) -> tuple[Lease | None, str]:
        """(lease, "") if taken, else (None, description of the live holder)."""
        with self._txn() as c:
            row = c.execute("SELECT * FROM leases WHERE name = ?", (name,)).fetchone()
            if row is not None:
                now = self.gateway.clock.now().timestamp()
                if row["pid"] == self.pid and row["start_time"] == self._own_start():
                    raise WtctlError(
                        "busy", f"lease {name} is already held by this process (pid {self.pid})"
                    )
                if not self._holder_dead(row["pid"], row["start_time"], row["expires"], now):
                    return None, f"pid {row['pid']}"
            lease = self._insert_lease(c, name, ttl)
        lease._start_heartbeat()
        return lease, ""

    def try_lease(self, name: str, *, ttl: float = LEASE_TTL) -> Lease | None:
        """Take the lease if it is free (or its holder is dead), else None; never waits."""
        lease, _ = self._try_take(name, ttl)
        return lease

    def lease(self, name: str, *, wait: float, ttl: float = LEASE_TTL) -> Lease:
        """Take lease ``name``, waiting up to ``wait`` seconds for a live holder, then ``busy``."""
        clock = self.gateway.clock
        deadline = clock.monotonic() + wait
        while True:
            lease, holder = self._try_take(name, ttl)
            if lease is not None:
                return lease
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                raise WtctlError("busy", f"lease {name} is held by {holder}; try again later")
            self._sleep(min(_POLL, remaining))

    def lease_expires(self, name: str) -> float:
        row = self._conn.execute("SELECT expires FROM leases WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        return float(row[0])


class Lease:
    """A held lease; use as a context manager. Renewed in the background when the state was
    opened with ``heartbeat=True``, else by ``renew()``."""

    def __init__(self, state: State, name: str, start: str, ttl: float) -> None:
        self.state = state
        self.name = name
        self._start = start
        self.ttl = ttl
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False

    def _renew(self, conn: sqlite3.Connection) -> bool:
        expires = self.state.gateway.clock.now().timestamp() + self.ttl
        cur = conn.execute(
            "UPDATE leases SET expires = ? WHERE name = ? AND pid = ? AND start_time = ?",
            (expires, self.name, self.state.pid, self._start),
        )
        return cur.rowcount == 1

    def renew(self) -> None:
        """Extend the expiry; ``busy`` if the lease was taken over meanwhile."""
        with self.state._txn() as c:
            ok = self._renew(c)
        if not ok:
            self.lost = True
            raise WtctlError("busy", f"lease {self.name} was lost to another process")

    def _start_heartbeat(self) -> None:
        if not self.state._heartbeat:
            return
        t = threading.Thread(target=self._beat, name=f"lease-{self.name}", daemon=True)
        self._thread = t
        t.start()

    def _beat(self) -> None:
        conn = _connect(self.state.path)
        try:
            while not self._stop.wait(self.ttl / 3):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        ok = self._renew(conn)
                    finally:
                        conn.execute("COMMIT")
                except sqlite3.Error:
                    continue  # transient (locked); the next beat retries
                if not ok:
                    self.lost = True
                    return
        finally:
            conn.close()

    def release(self) -> None:
        """Drop the lease if it is still ours (a stale release never touches a successor's)."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        with self.state._txn() as c:
            c.execute(
                "DELETE FROM leases WHERE name = ? AND pid = ? AND start_time = ?",
                (self.name, self.state.pid, self._start),
            )

    def __enter__(self) -> Lease:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# --- validation helpers ---------------------------------------------------------------------


def _check_id(record_id: str) -> None:
    ok = (
        isinstance(record_id, str)
        and len(record_id) == 11
        and record_id.startswith("wt-")
        and all(ch in _ID_ALPHABET for ch in record_id[3:])
    )
    if not ok:
        raise ValueError(f"invalid record id {record_id!r}")


def _check_state(state: str) -> None:
    if state not in STATES:
        raise ValueError(f"unknown record state {state!r}")


def _check_holder(holder: str) -> None:
    if not (1 <= len(holder) <= 64 and _HOLDER_GRAMMAR.fullmatch(holder)):
        raise UsageError(f"invalid holder {holder!r}: 1 to 64 of [A-Za-z0-9._:/#@+=,-]")


def open_state(ctx: Context) -> State:
    """The seam ``Context.state`` calls (lazily, once): ``state.db`` under the XDG state dir."""
    return State(state_path(), ctx.gateway, heartbeat=True)
