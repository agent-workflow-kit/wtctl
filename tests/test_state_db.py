"""state.db: open, schema, migrations, permissions, newer-schema refusal."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from tests.statehelpers import fake_gateway, make_state
from wtctl import state as state_mod
from wtctl.errors import WtctlError
from wtctl.state import MIGRATIONS, State, state_path


def tables(path: Path) -> set[str]:
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    finally:
        c.close()


def test_state_path_follows_xdg_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "x"))
    assert state_path() == tmp_path / "x" / "wtctl" / "state.db"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert state_path() == Path(os.environ["HOME"]) / ".local" / "state" / "wtctl" / "state.db"
    monkeypatch.setenv("XDG_STATE_HOME", "relative/dir")  # XDG: relative values are invalid
    assert state_path() == Path(os.environ["HOME"]) / ".local" / "state" / "wtctl" / "state.db"


def test_open_from_empty_creates_every_table(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    names = tables(st.path)
    assert {
        "records", "holders", "labels", "stash_ids", "baseline_items", "bootstrap_paths",
        "bootstrap_nested", "bootstrap_runs", "clones", "anchors", "leases", "schema_version",
        "ids",
    } <= names  # fmt: skip
    assert st.schema_version == len(MIGRATIONS)


def test_wal_mode(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    c = sqlite3.connect(st.path)
    assert c.execute("pragma journal_mode").fetchone()[0] == "wal"
    c.close()


def test_reopen_is_idempotent_and_keeps_data(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    new = st.allocate_id()
    st2 = make_state(tmp_path)
    assert st2.schema_version == len(MIGRATIONS)
    assert st2.id_allocated(new)


def test_permissions_dir_0700_and_files_0600(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    st.allocate_id()  # force WAL side files into existence
    assert stat.S_IMODE(st.path.parent.stat().st_mode) == 0o700
    for f in st.path.parent.iterdir():
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


def test_permissions_ignore_a_permissive_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        st = make_state(tmp_path)
        st.allocate_id()
    finally:
        os.umask(old)
    assert stat.S_IMODE(st.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(st.path.stat().st_mode) == 0o600


def test_loose_existing_dir_and_file_are_tightened(tmp_path: Path) -> None:
    d = tmp_path / "st"
    d.mkdir(mode=0o755)
    d.chmod(0o755)
    (d / "state.db").write_bytes(b"")
    (d / "state.db").chmod(0o644)
    st = make_state(tmp_path)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(st.path.stat().st_mode) == 0o600


def test_symlinked_state_dir_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "st").symlink_to(real)
    with pytest.raises(WtctlError) as e:
        make_state(tmp_path)
    assert e.value.code == "internal" and "symlink" in e.value.message
    assert not (real / "state.db").exists()


def test_symlinked_db_file_is_refused(tmp_path: Path) -> None:
    d = tmp_path / "st"
    d.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"")
    (d / "state.db").symlink_to(target)
    with pytest.raises(WtctlError) as e:
        make_state(tmp_path)
    assert e.value.code == "internal"
    assert target.read_bytes() == b""


def test_newer_schema_is_refused_and_left_untouched(tmp_path: Path) -> None:
    st = make_state(tmp_path)
    c = sqlite3.connect(st.path)
    c.execute("insert into schema_version(version) values (?)", (len(MIGRATIONS) + 1,))
    c.commit()
    c.close()
    st.close()

    def dump() -> list[str]:
        conn = sqlite3.connect(st.path)
        try:
            return list(conn.iterdump())
        finally:
            conn.close()

    before = dump()
    with pytest.raises(WtctlError) as e:
        make_state(tmp_path)
    assert e.value.code == "internal"
    assert "newer" in e.value.message and "upgrade" in e.value.message.lower()
    assert dump() == before


def test_foreign_non_sqlite_file_is_an_error_not_overwritten(tmp_path: Path) -> None:
    d = tmp_path / "st"
    d.mkdir(mode=0o700)
    (d / "state.db").write_bytes(b"this is not a database" * 10)
    with pytest.raises(WtctlError) as e:
        make_state(tmp_path)
    assert e.value.code == "internal"
    assert (d / "state.db").read_bytes().startswith(b"this is not")


def test_migrations_are_additive_only() -> None:
    import re

    for version, statements in MIGRATIONS:
        for s in statements:
            assert not re.match(r"\s*(drop|delete|update|replace)\b", s, re.I), (version, s)
            assert not re.search(r"alter\s+table\s+\w+\s+(drop|rename)", s, re.I), (version, s)
            assert not re.search(r"\bdrop\s+(table|column|index)\b", s, re.I), (version, s)


def test_migration_versions_are_consecutive_from_1() -> None:
    assert [v for v, _ in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))


def test_later_migration_applies_on_top_and_keeps_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st = make_state(tmp_path)
    ident = st.allocate_id()
    st.close()
    extra = (
        *MIGRATIONS,
        (len(MIGRATIONS) + 1, ("ALTER TABLE records ADD COLUMN extra TEXT", "CREATE TABLE t2(a)")),
    )
    monkeypatch.setattr(state_mod, "MIGRATIONS", extra)
    st2 = State(st.path, fake_gateway(), pid=1)
    assert st2.schema_version == len(extra)
    assert st2.id_allocated(ident)
    assert "t2" in tables(st.path)
    # An older binary (without the extra migration) now refuses the newer database.
    monkeypatch.setattr(state_mod, "MIGRATIONS", MIGRATIONS)
    with pytest.raises(WtctlError):
        State(st.path, fake_gateway(), pid=1)


def test_failed_migration_rolls_back_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = (*MIGRATIONS, (len(MIGRATIONS) + 1, ("CREATE TABLE ok1(a)", "THIS IS NOT SQL")))
    st = make_state(tmp_path)
    st.close()
    monkeypatch.setattr(state_mod, "MIGRATIONS", bad)
    with pytest.raises(WtctlError) as e:
        State(st.path, fake_gateway(), pid=1)
    assert e.value.code == "internal"
    assert "ok1" not in tables(st.path)
    c = sqlite3.connect(st.path)
    assert c.execute("select max(version) from schema_version").fetchone()[0] == len(MIGRATIONS)
    c.close()


def test_open_state_uses_ctx_gateway_and_xdg(tmp_path: Path) -> None:
    from types import SimpleNamespace

    ctx = SimpleNamespace(gateway=fake_gateway())
    st = state_mod.open_state(ctx)  # type: ignore[arg-type]
    assert st.path == state_path()
    assert st.path.exists()


def test_wiring_via_context_seam() -> None:
    import argparse

    from wtctl.actor import Actor
    from wtctl.context import Context, Runtime

    ctx = Context(args=argparse.Namespace(), actor=Actor("auto:x"), runtime=Runtime())
    assert isinstance(ctx.state, State)
    assert ctx.state is ctx.state  # cached


def test_first_open_race_on_mkdir_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = Path.mkdir
    target = tmp_path / "st"

    def racing(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False):
        if self == target:
            real_mkdir(self, mode=0o700)  # the peer wins the race
            raise FileExistsError(str(self))
        return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", racing)
    st = make_state(tmp_path)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o700
    st.close()


def test_race_winner_that_is_a_symlink_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkdir = Path.mkdir
    target = tmp_path / "st"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def racing(self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False):
        if self == target:
            target.symlink_to(elsewhere)
            raise FileExistsError(str(self))
        return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", racing)
    with pytest.raises(WtctlError, match="symlink"):
        make_state(tmp_path)


def test_lock_timeout_while_opening_is_busy_not_internal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "st"
    d.mkdir(mode=0o700)
    peer = sqlite3.connect(d / "state.db", isolation_level=None)
    peer.execute("BEGIN EXCLUSIVE")
    monkeypatch.setattr(state_mod, "_BUSY_TIMEOUT", 0.1)
    try:
        with pytest.raises(WtctlError) as e:
            make_state(tmp_path)
        assert e.value.code == "busy" and e.value.exit_code == 12
    finally:
        peer.execute("ROLLBACK")
        peer.close()
