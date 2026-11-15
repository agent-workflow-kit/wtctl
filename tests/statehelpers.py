"""Fakes and builders for the state tests."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from wtctl.gateway import Gateway
from wtctl.state import State


class FakeClock:
    def __init__(self) -> None:
        self.t = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        self.mono = 0.0

    def now(self) -> dt.datetime:
        return self.t

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.t += dt.timedelta(seconds=seconds)
        self.mono += seconds


class FakeProcs:
    """Process table: pid -> start time. Missing pid = not running."""

    def __init__(self, table: dict[int, str] | None = None) -> None:
        self.table = dict(table or {})
        self.unreadable: set[int] = set()

    def start_time(self, pid: int) -> str | None:
        from wtctl.gateway import ProcessUnreadable

        if pid in self.unreadable:
            raise ProcessUnreadable("denied")
        return self.table.get(pid)


def fake_gateway(clock: FakeClock | None = None, procs: FakeProcs | None = None) -> Gateway:
    return Gateway(clock=clock or FakeClock(), procs=procs or FakeProcs())


def make_state(
    tmp: Path,
    *,
    pid: int = 100,
    start: str = "s100",
    clock: FakeClock | None = None,
    procs: FakeProcs | None = None,
    name: str = "state.db",
) -> State:
    clock = clock or FakeClock()
    procs = procs or FakeProcs()
    procs.table.setdefault(pid, start)
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock.advance(s)

    st = State(tmp / "st" / name, fake_gateway(clock, procs), pid=pid, sleep=sleep)
    st.sleeps = sleeps  # type: ignore[attr-defined]
    return st


def rec(id_: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": id_,
        "object_type": "worktree",
        "state": "active",
        "repo": "example.com/o/r",
        "path": f"/w/{id_}",
        "name": id_[3:],
        "owned": True,
    }
    base.update(kw)
    return base
