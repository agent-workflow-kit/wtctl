"""copy_env source walk and reads run in a child process under the walk deadline (#55)."""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.bootlib import Repo
from wtctl import bootstrapper as b
from wtctl import fsafe, fsafe_child

BLOCK = [sys.executable, "-c", "import sys, time; sys.stdin.read(); time.sleep(600)"]


@pytest.fixture
def r(tmp_path: Path, private_home: Path) -> Repo:
    return Repo(tmp_path, private_home)


def copy(r: Repo, patterns: list[str], **kw: Any) -> tuple[list[b.Copied], list[str]]:
    warns: list[str] = []
    with r.open() as wt:
        tracked = b.tracked_paths(r.git, wt)
        got = b.copy_env_files(
            wt, source_root=str(r.main), patterns=patterns, tracked=tracked, warn=warns.append, **kw
        )
    return got, warns


class Spy:
    """Wraps ``fsafe_child.run_op``: records the ops and pids, and can stall chosen ops."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, stall: set[int] | None = None) -> None:
        self.ops: list[str] = []
        self.pids: list[int] = []
        self.stall = stall or set()  # 0-based indexes of the calls to replace by a stalled worker
        real = fsafe_child.run_op

        def run_op(op: str, args: dict[str, object], **kw: Any) -> Any:
            n = len(self.ops)
            self.ops.append(op)
            kw["on_start"] = self.pids.append
            if n in self.stall:
                kw["argv"] = BLOCK
                kw["timeout"] = 0.5
            return real(op, args, **kw)

        monkeypatch.setattr(fsafe_child, "run_op", run_op)

    def assert_all_dead(self) -> None:
        assert self.pids
        for pid in self.pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)


def test_the_walk_and_the_reads_run_in_children(r: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch)
    r.write(".env", "A=1\n", root=r.main)
    got, warns = copy(r, [".env"])
    assert [c.path for c in got] == [".env"] and warns == []
    assert spy.ops == ["env_match", "env_read"]
    spy.assert_all_dead()  # each child has exited


def test_a_hung_walk_is_killed_by_pid_and_nothing_is_copied(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = Spy(monkeypatch, stall={0})
    r.write(".env", "A=1\n", root=r.main)
    got, warns = copy(r, [".env"])
    assert got == [] and not (r.wt / ".env").exists()
    assert spy.ops == ["env_match"]
    assert any("deadline" in w and "nothing copied" in w for w in warns)
    spy.assert_all_dead()


def test_hung_reads_are_killed_and_nothing_is_copied(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = Spy(monkeypatch, stall={1})
    r.write(".env", "A=1\n", root=r.main)
    r.write(".env.two", "B=2\n", root=r.main)
    got, warns = copy(r, [".env*"])
    assert got == [] and not (r.wt / ".env").exists() and not (r.wt / ".env.two").exists()
    assert any("deadline" in w for w in warns)
    spy.assert_all_dead()


def test_a_later_hung_batch_keeps_the_verified_earlier_files_only(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "ENV_BATCH_BYTES", 4)  # one file per batch
    spy = Spy(monkeypatch, stall={2})  # match, read of .env.a, read of .env.b hangs
    for n in "abc":
        r.write(f".env.{n}", f"{n}=1\n", root=r.main)
    got, warns = copy(r, [".env.*"])
    assert [c.path for c in got] == [".env.a"]
    assert (r.wt / ".env.a").exists() and not (r.wt / ".env.b").exists()
    assert not (r.wt / ".env.c").exists()
    assert spy.ops == ["env_match", "env_read", "env_read"]  # no third read after the deadline
    assert any("deadline" in w for w in warns)
    spy.assert_all_dead()


def test_many_files_over_the_batch_size_are_all_copied(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "ENV_BATCH_BYTES", 10)
    spy = Spy(monkeypatch)
    for i in range(7):
        r.write(f".env.{i}", f"value-{i}\n", root=r.main)
    got, warns = copy(r, [".env.*"])
    assert sorted(c.path for c in got) == [f".env.{i}" for i in range(7)] and warns == []
    assert spy.ops.count("env_read") > 1
    assert (r.wt / ".env.3").read_text() == "value-3\n"


def test_an_interrupt_kills_the_child_and_copies_nothing(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids: list[int] = []
    real = fsafe_child.run_op

    def run_op(op: str, args: dict[str, object], **kw: Any) -> Any:
        def boom(pid: int) -> None:
            pids.append(pid)
            raise KeyboardInterrupt

        return real(op, args, **{**kw, "on_start": boom})

    monkeypatch.setattr(fsafe_child, "run_op", run_op)
    r.write(".env", "A=1\n", root=r.main)
    with pytest.raises(KeyboardInterrupt):
        copy(r, [".env"])
    assert not (r.wt / ".env").exists()
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_the_lease_is_renewed_before_each_child(r: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch)
    ticks: list[int] = []
    r.write(".env", "A=1\n", root=r.main)
    got, _ = copy(r, [".env"], on_tick=lambda: ticks.append(len(spy.ops)))
    assert [c.path for c in got] == [".env"]
    assert ticks == [0, 1]  # before the walk and before the read


def test_a_lost_lease_stops_the_copy(r: Repo) -> None:
    r.write(".env", "A=1\n", root=r.main)
    got, warns = copy(r, [".env"], on_tick=lambda: True)
    assert got == [] and not (r.wt / ".env").exists()
    assert any("lease" in w for w in warns)


def test_the_hostile_name_round_trips_through_the_child(r: Repo) -> None:
    name = "e\nvé ‮.env"
    r.write(name, "x", root=r.main)
    got, warns = copy(r, ["e*"])
    assert [c.path for c in got] == [name] and warns == []
    assert (r.wt / name).read_text() == "x"


def test_a_name_that_is_not_utf8_never_reaches_the_read(r: Repo) -> None:
    bad = os.fsencode(str(r.main)) + b"/.env-\xff"
    with open(bad, "wb") as f:
        f.write(b"x")
    got, warns = copy(r, [".env-*"])
    assert got == [] and any("UTF-8" in w for w in warns)


# -- the operations themselves ----------------------------------------------------------------


def anchor_args(p: Path) -> dict[str, Any]:
    return {"anchor": {"path": str(p), "identity": None}, "common_id": None}


def test_env_match_op_reports_the_root_identity_and_sorted_matches(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / ".env").write_text("1")
    (tmp_path / ".env").write_text("2")
    st = os.stat(tmp_path)
    res = fsafe_child.run_op(
        "env_match", {**anchor_args(tmp_path), "plans": [[".env"], ["**", ".env"]], "limit": 1000}
    )
    assert res.ok and res.value is not None
    assert res.value["identity"] == [st.st_dev, st.st_ino]
    assert res.value["matches"] == [[".env"], ["a", ".env"]]
    assert res.value["refuse"] is None


def test_env_match_op_refuses_a_foreign_git_identity(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    res = fsafe_child.run_op(
        "env_match",
        {**anchor_args(tmp_path), "plans": [[".env"]], "limit": 10, "common_id": "1:2"},
    )
    assert res.ok and res.value is not None and res.value["refuse"]
    assert res.value["matches"] == []


def test_env_match_op_caps_its_warnings(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("permission bits do not stop root")
    dirs = []
    for i in range(b.ENV_WARN_CAP + 20):
        d = tmp_path / f"d{i}"
        d.mkdir()
        d.chmod(0)
        dirs.append(d)
    try:
        res = fsafe_child.run_op(
            "env_match", {**anchor_args(tmp_path), "plans": [["**", "x"]], "limit": 10_000}
        )
    finally:
        for d in dirs:
            d.chmod(0o700)
    assert res.ok and res.value is not None
    assert len(res.value["warnings"]) == b.ENV_WARN_CAP + 1
    assert "more warnings" in res.value["warnings"][-1]


def test_env_read_op_returns_base64_and_per_file_errors(tmp_path: Path) -> None:
    (tmp_path / "ok").write_bytes(b"\x00\xffdata")
    (tmp_path / "big").write_bytes(b"123456789")
    os.symlink(tmp_path / "ok", tmp_path / "ln")
    st = os.stat(tmp_path)
    res = fsafe_child.run_op(
        "env_read",
        {
            "anchor": {"path": str(tmp_path), "identity": [st.st_dev, st.st_ino]},
            "paths": [["ok"], ["big"], ["ln"], ["gone"]],
            "max_bytes": 8,
            "batch_bytes": 1000,
        },
    )
    assert res.ok and res.value is not None and res.value["next"] == 4
    f = res.value["files"]
    assert base64.b64decode(f[0]["data"]) == b"\x00\xffdata"
    assert f[1]["kind"] == "limit" and f[2]["kind"] == "symlink" and f[3]["kind"] == "missing"


def test_env_read_op_refuses_a_swapped_root(tmp_path: Path) -> None:
    (tmp_path / "ok").write_text("x")
    res = fsafe_child.run_op(
        "env_read",
        {
            "anchor": {"path": str(tmp_path), "identity": [1, 2]},
            "paths": [["ok"]],
            "max_bytes": 8,
            "batch_bytes": 100,
        },
    )
    assert not res.ok and res.error is not None
    assert isinstance(res.error, fsafe.FsafeError)


def test_env_read_op_stops_a_batch_at_the_byte_cap(tmp_path: Path) -> None:
    for n in "abc":
        (tmp_path / n).write_bytes(b"12345")
    st = os.stat(tmp_path)
    res = fsafe_child.run_op(
        "env_read",
        {
            "anchor": {"path": str(tmp_path), "identity": [st.st_dev, st.st_ino]},
            "paths": [["a"], ["b"], ["c"]],
            "max_bytes": 100,
            "batch_bytes": 6,
        },
    )
    assert res.ok and res.value is not None
    assert res.value["next"] == 2 and len(res.value["files"]) == 2  # always progresses
