"""Leases: take, wait, expiry, dead-holder takeover by PID + start time, real contention."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.statehelpers import FakeClock, FakeProcs, fake_gateway, make_state, rec
from wtctl.errors import WtctlError
from wtctl import state as state_mod
from wtctl.gateway import Gateway
from wtctl.state import MIGRATIONS, State, state_path


def two_holders(tmp: Path) -> tuple[State, State, FakeClock, FakeProcs]:
    clock, procs = FakeClock(), FakeProcs()
    a = make_state(tmp, pid=100, start="s100", clock=clock, procs=procs)
    b = make_state(tmp, pid=200, start="s200", clock=clock, procs=procs)
    return a, b, clock, procs


def test_take_release_retake(tmp_path: Path) -> None:
    a, b, *_ = two_holders(tmp_path)
    with a.lease("global", wait=0):
        with pytest.raises(WtctlError) as e:
            b.lease("global", wait=0)
        assert e.value.code == "busy" and e.value.exit_code == 12
    with b.lease("global", wait=0):
        pass


def test_distinct_names_do_not_conflict(tmp_path: Path) -> None:
    a, b, *_ = two_holders(tmp_path)
    with a.lease("record:wt-aaaaaaaa", wait=0), b.lease("record:wt-bbbbbbbb", wait=0):
        pass


def test_release_only_removes_own_row(tmp_path: Path) -> None:
    a, b, clock, procs = two_holders(tmp_path)
    la = a.lease("global", wait=0)
    procs.table.pop(100)  # a "dies"; b takes over
    lb = b.lease("global", wait=0)
    la.release()  # stale release by the old holder must not drop b's lease
    with pytest.raises(WtctlError):
        a.lease("global", wait=0)
    lb.release()


def test_dead_holder_is_taken_over_immediately(tmp_path: Path) -> None:
    a, b, clock, procs = two_holders(tmp_path)
    a.lease("clone:/c", wait=0)
    procs.table.pop(100)
    lb = b.lease("clone:/c", wait=0)
    assert b.sleeps == []  # type: ignore[attr-defined]
    lb.release()


def test_pid_reuse_with_different_start_time_is_dead(tmp_path: Path) -> None:
    a, b, clock, procs = two_holders(tmp_path)
    a.lease("global", wait=0)
    procs.table[100] = "REUSED"  # same pid, a different process
    b.lease("global", wait=0).release()


def test_same_pid_same_start_is_alive(tmp_path: Path) -> None:
    a, b, clock, procs = two_holders(tmp_path)
    a.lease("global", wait=0)
    clock.advance(10_000)  # far past the nominal expiry: a live holder is still waited for
    with pytest.raises(WtctlError) as e:
        b.lease("global", wait=0)
    assert e.value.code == "busy"


def test_unreadable_holder_is_alive_until_expiry(tmp_path: Path) -> None:
    a, b, clock, procs = two_holders(tmp_path)
    a.lease("global", wait=0, ttl=600)
    procs.unreadable.add(100)
    with pytest.raises(WtctlError):
        b.lease("global", wait=0)
    clock.advance(601)
    b.lease("global", wait=0).release()


def test_wait_polls_then_gives_up_after_the_limit(tmp_path: Path) -> None:
    a, b, clock, _ = two_holders(tmp_path)
    a.lease("global", wait=0)
    t0 = clock.mono
    with pytest.raises(WtctlError) as e:
        b.lease("global", wait=3)
    assert e.value.code == "busy"
    assert 2.99 <= clock.mono - t0 < 3.5
    assert len(b.sleeps) > 3  # type: ignore[attr-defined]


def test_wait_succeeds_when_holder_releases_during_the_wait(tmp_path: Path) -> None:
    a, b, clock, _ = two_holders(tmp_path)
    la = a.lease("global", wait=0)
    b_sleeps = b.sleeps  # type: ignore[attr-defined]
    real_sleep = b._sleep

    def sleep_and_release(s: float) -> None:
        real_sleep(s)
        if len(b_sleeps) == 2:
            la.release()

    b._sleep = sleep_and_release
    b.lease("global", wait=60).release()


def test_waiting_on_yourself_fails_fast(tmp_path: Path) -> None:
    a = make_state(tmp_path)
    a.lease("global", wait=0)
    with pytest.raises(WtctlError) as e:
        a.lease("global", wait=600)
    assert e.value.code == "busy" and "this process" in e.value.message
    assert a.sleeps == []  # type: ignore[attr-defined]


def test_busy_error_names_the_lease_and_holder(tmp_path: Path) -> None:
    a, b, *_ = two_holders(tmp_path)
    a.lease("record:wt-aaaaaaaa", wait=0)
    with pytest.raises(WtctlError) as e:
        b.lease("record:wt-aaaaaaaa", wait=0)
    assert "record:wt-aaaaaaaa" in e.value.message and "100" in e.value.message


def test_try_lease_returns_none_when_held(tmp_path: Path) -> None:
    a, b, *_ = two_holders(tmp_path)
    la = a.lease("global", wait=0)
    assert b.try_lease("global") is None
    la.release()
    lb = b.try_lease("global")
    assert lb is not None
    lb.release()


def test_renew_extends_expiry_and_detects_loss(tmp_path: Path) -> None:
    a, b, clock, procs = two_holders(tmp_path)
    la = a.lease("global", wait=0, ttl=600)
    clock.advance(500)
    la.renew()
    clock.advance(500)
    assert b.try_lease("global") is None
    procs.table.pop(100)
    lb = b.lease("global", wait=0)  # taken over: a's renew must now fail loudly
    with pytest.raises(WtctlError) as e:
        la.renew()
    assert e.value.code == "busy"
    lb.release()


def test_double_release_is_harmless(tmp_path: Path) -> None:
    a, b, *_ = two_holders(tmp_path)
    la = a.lease("global", wait=0)
    la.release()
    la.release()
    b.lease("global", wait=0).release()


def test_context_manager_releases_on_exception(tmp_path: Path) -> None:
    a, b, *_ = two_holders(tmp_path)
    with pytest.raises(RuntimeError):
        with a.lease("global", wait=0):
            raise RuntimeError("boom")
    b.lease("global", wait=0).release()


def test_insert_with_lease_is_one_transaction(tmp_path: Path) -> None:
    a, b, *_ = two_holders(tmp_path)
    i = a.allocate_id()
    r, lease = a.insert_record_leased(rec(i, state="creating"))
    assert r["state"] == "creating"
    assert b.try_lease(f"record:{i}") is None
    lease.release()
    assert b.try_lease(f"record:{i}") is not None


def test_failed_insert_leaves_no_lease(tmp_path: Path) -> None:
    a, b, *_ = two_holders(tmp_path)
    i = a.allocate_id()
    with pytest.raises(ValueError):
        a.insert_record_leased(rec(i, state="nonsense"))
    assert b.try_lease(f"record:{i}") is not None


def test_lease_name_helpers() -> None:
    from wtctl import state

    assert state.record_lease("wt-aaaaaaaa") == "record:wt-aaaaaaaa"
    assert state.repo_lease("example.com/o/r") == "repo:example.com/o/r"
    assert state.clone_lease("/c") == "clone:/c"
    assert state.GLOBAL_LEASE == "global"


# --- heartbeat (real time) -----------------------------------------------------------------


def test_heartbeat_keeps_a_held_lease_alive(tmp_path: Path) -> None:
    """The heartbeat extends the expiry on its own.

    Asserts on observed renewals (the stored expiry advancing), not on ``expiry > now`` after a
    fixed sleep: a process-wide stall longer than the ttl (a paused CI VM) makes the lease
    genuinely expire for a moment, and a wall-clock check right after the stall races the
    heartbeat thread's catch-up. The deadline is a generous upper bound, not a tuned wait.
    """
    st = State(tmp_path / "st" / "state.db", Gateway(), heartbeat=True)
    other_pid = os.getpid() + 1
    other = State(
        tmp_path / "st" / "state.db", Gateway(procs=FakeProcs({other_pid: "x"})), pid=other_pid
    )
    lease = st.lease("global", wait=0, ttl=0.6)
    try:
        seen = st.lease_expires("global")
        renewals = 0
        deadline = time.monotonic() + 60
        while renewals < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
            now_expires = st.lease_expires("global")
            if now_expires > seen:
                renewals += 1
                seen = now_expires
        assert renewals == 3, "the heartbeat did not renew the lease"
        assert not lease.lost
    finally:
        lease.release()
    assert other.try_lease("global") is not None


# --- real processes ----------------------------------------------------------------------------

WORKER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from wtctl.gateway import Gateway
    from wtctl.state import State, state_path

    mode, arg = sys.argv[1], sys.argv[2]
    st = State(state_path(), Gateway())
    if mode == "hold":
        st.lease("global", wait=0)
        print("ready", flush=True)
        time.sleep(120)
    elif mode == "counter":
        f = Path(arg)
        for _ in range(int(sys.argv[3])):
            with st.lease("global", wait=120):
                n = int(f.read_text() or "0")
                time.sleep(0.001)
                f.write_text(str(n + 1))
    elif mode == "cas":
        try:
            st.transition(arg, "active", "partial")
            print("won", flush=True)
        except Exception as e:
            print("lost", getattr(e, "code", type(e).__name__), flush=True)
    elif mode == "ids":
        ids = [st.allocate_id() for _ in range(int(arg))]
        print(" ".join(ids), flush=True)
    elif mode == "open":
        print("open", st.schema_version, flush=True)
    """
)


@pytest.fixture
def worker(tmp_path: Path) -> Path:
    p = tmp_path / "worker.py"
    p.write_text(WORKER)
    return p


def spawn(worker: Path, *args: str) -> subprocess.Popen[str]:
    # Inherits the private HOME/XDG that conftest put in os.environ.
    return subprocess.Popen(
        [sys.executable, str(worker), *args],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )


def finish(procs: list[subprocess.Popen[str]]) -> list[str]:
    outs = []
    for p in procs:
        out, _ = p.communicate(timeout=120)
        assert p.returncode == 0, out
        outs.append(out)
    return outs


def test_real_processes_are_mutually_exclusive_under_a_lease(tmp_path: Path, worker: Path) -> None:
    counter = tmp_path / "counter"
    counter.write_text("0")
    procs = [spawn(worker, "counter", str(counter), "25") for _ in range(4)]
    finish(procs)
    assert counter.read_text() == "100"  # no lost update: the lease serialised every increment


def test_real_dead_holder_is_taken_over_and_live_holder_is_busy(
    tmp_path: Path, worker: Path
) -> None:
    holder = spawn(worker, "hold", "-")
    pid = holder.pid  # captured at launch; the only process this test ever kills
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "ready"
        mine = State(state_path(), Gateway())
        with pytest.raises(WtctlError) as e:
            mine.lease("global", wait=0.3)
        assert e.value.code == "busy" and str(pid) in e.value.message
        holder.kill()  # exact Popen handle
        holder.wait(timeout=30)
        mine.lease("global", wait=0).release()  # dead (reaped) holder: takeover
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=30)
        if holder.stdout:
            holder.stdout.close()


def test_real_killed_but_unreaped_holder_counts_as_dead(tmp_path: Path, worker: Path) -> None:
    holder = spawn(worker, "hold", "-")
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "ready"
        holder.kill()
        time.sleep(0.2)  # zombie: killed, not yet waited for
        mine = State(state_path(), Gateway())
        mine.lease("global", wait=5).release()
    finally:
        holder.wait(timeout=30)
        if holder.stdout:
            holder.stdout.close()


def test_real_concurrent_cas_has_exactly_one_winner(tmp_path: Path, worker: Path) -> None:
    st = State(state_path(), Gateway())
    i = st.allocate_id()
    st.insert_record(rec(i))
    procs = [spawn(worker, "cas", i) for _ in range(8)]
    outs = [o.strip() for o in finish(procs)]
    assert outs.count("won") == 1
    assert outs.count("lost busy") == 7
    got = st.get_record(i)
    assert got is not None and got["state"] == "partial"


def test_real_concurrent_id_allocation_never_collides(tmp_path: Path, worker: Path) -> None:
    procs = [spawn(worker, "ids", "60") for _ in range(6)]
    allids = [i for o in finish(procs) for i in o.split()]
    assert len(allids) == 360 and len(set(allids)) == 360


def test_real_concurrent_first_open_migrates_once(tmp_path: Path, worker: Path) -> None:
    procs = [spawn(worker, "open", "-") for _ in range(8)]
    outs = finish(procs)
    assert all(o.startswith(f"open {len(MIGRATIONS)}") for o in outs)


def test_real_concurrent_upgrade_of_a_version_1_database(tmp_path: Path, worker: Path) -> None:
    import sqlite3

    path = state_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    for sql in MIGRATIONS[0][1]:
        conn.execute(sql)
    conn.execute("INSERT INTO schema_version(version) VALUES (1)")
    conn.close()
    procs = [spawn(worker, "open", "-") for _ in range(8)]
    outs = finish(procs)
    assert all(o.startswith(f"open {len(MIGRATIONS)}") for o in outs)
    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT max(version) FROM schema_version").fetchone()[0] == len(
            MIGRATIONS
        )
        check.execute("SELECT count(*) FROM baseline_meta")
        check.execute("SELECT count(*) FROM sparse_dirs")
    finally:
        check.close()


_ = (fake_gateway,)


OPENER = textwrap.dedent(
    """
    import os, sys, time
    from wtctl.gateway import Gateway
    from wtctl.state import State, state_path

    go = float(sys.argv[1])
    print("ready", flush=True)
    while time.time() < go:
        pass
    State(state_path(), Gateway()).close()
    print("opened")
    """
)


def test_real_concurrent_first_open_of_a_fresh_database_never_reports_busy(
    tmp_path: Path,
) -> None:
    script = tmp_path / "opener.py"
    script.write_text(OPENER)
    for rnd in range(4):
        # A fresh private state dir per round, so every round races the very first open.
        env = {
            **os.environ,
            "XDG_STATE_HOME": str(tmp_path / f"round{rnd}"),
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
        go = time.time() + 2.0
        procs = [
            subprocess.Popen(
                [sys.executable, str(script), str(go)], stdout=subprocess.PIPE, text=True, env=env
            )
            for _ in range(16)
        ]
        for p in procs:
            out, _ = p.communicate(timeout=120)
            assert p.returncode == 0 and "opened" in out, out


# --- heartbeat failure handling (deterministic: no thread, no sleeps) -----------------------


class _FlakyConn:
    """A real connection whose ``COMMIT`` raises, as SQLite does for BUSY/IOERR/FULL.

    The raise happens before the real COMMIT runs, so the real connection stays inside its
    transaction exactly as after a failed commit. ``fail_commits`` is how many COMMITs fail;
    ``fail_rollback`` makes ROLLBACK raise too.
    """

    def __init__(self, real, fail_commits: int, fail_rollback: bool = False) -> None:  # noqa: ANN001
        self.real = real
        self.fail_commits = fail_commits
        self.fail_rollback = fail_rollback
        self.closed = False

    @property
    def in_transaction(self) -> bool:
        return not self.closed and self.real.in_transaction

    def execute(self, sql, *args):  # noqa: ANN001, ANN002, ANN202
        if sql == "COMMIT" and self.fail_commits > 0:
            self.fail_commits -= 1
            raise sqlite3.OperationalError("database is locked")
        if sql == "ROLLBACK" and self.fail_rollback:
            raise sqlite3.OperationalError("disk I/O error")
        return self.real.execute(sql, *args)

    def close(self) -> None:
        self.closed = True
        self.real.close()


class _Ticks:
    """Stands in for the stop event: ``wait`` returns False (keep beating) ``n`` times, then
    True. ``on_tick`` runs before each beat so a test can move the monotonic clock."""

    def __init__(self, n: int, on_tick=None) -> None:  # noqa: ANN001
        self.n = n
        self.on_tick = on_tick
        self.waits: list[float] = []
        self.flag = False

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if self.n <= 0:
            return True
        self.n -= 1
        if self.on_tick:
            self.on_tick()
        return False

    def set(self) -> None:
        self.flag = True

    def is_set(self) -> bool:
        return self.flag


def _beating_lease(tmp: Path, monkeypatch: pytest.MonkeyPatch, flaky: dict, ttl: float = 30.0):  # noqa: ANN001, ANN202
    """A held lease (no thread) whose ``_beat`` opens ``_FlakyConn``s; ``flaky`` is filled in
    with every connection it opens."""
    st = make_state(tmp, pid=100, start="s100", clock=FakeClock(), procs=FakeProcs())
    lease = st.lease("global", wait=0, ttl=ttl)
    real_connect = state_mod._connect
    conns: list[_FlakyConn] = flaky.setdefault("conns", [])

    def connect(path):  # noqa: ANN001, ANN202
        c = _FlakyConn(
            real_connect(path), flaky.get("commits", 0), flaky.get("fail_rollback", False)
        )
        conns.append(c)
        return c

    monkeypatch.setattr(state_mod, "_connect", connect)
    return st, lease


def test_a_failed_commit_is_rolled_back_and_the_next_beat_renews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky: dict = {"commits": 1}
    st, lease = _beating_lease(tmp_path, monkeypatch, flaky)
    before = st.lease_expires("global")
    st.gateway.clock.advance(5)  # type: ignore[attr-defined]
    lease._stop = _Ticks(2)  # type: ignore[assignment]
    lease._beat()
    (conn,) = flaky["conns"]
    assert not conn.in_transaction
    assert st.lease_expires("global") > before, "the second beat did not renew"
    assert not lease.lost
    assert lease.last_error is None  # cleared by the successful beat
    lease.release()


def test_a_failed_commit_leaves_no_open_transaction_between_beats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky: dict = {"commits": 1}
    st, lease = _beating_lease(tmp_path, monkeypatch, flaky)
    seen: list[bool] = []
    ticks = _Ticks(2)
    orig = ticks.wait

    def wait(timeout: float) -> bool:
        if flaky["conns"]:
            seen.append(flaky["conns"][0].in_transaction)
        return orig(timeout)

    ticks.wait = wait  # type: ignore[method-assign]
    lease._stop = ticks  # type: ignore[assignment]
    lease._beat()
    assert seen == [False, False]
    lease.release()


def test_a_failed_commit_records_last_error_not_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky: dict = {"commits": 1}
    _, lease = _beating_lease(tmp_path, monkeypatch, flaky)
    lease._stop = _Ticks(1)  # type: ignore[assignment]
    lease._beat()
    assert isinstance(lease.last_error, sqlite3.OperationalError)
    assert "locked" in str(lease.last_error)
    lease.release()


def test_failed_rollback_does_not_stop_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection that cannot even roll back is replaced; the next beat renews."""
    flaky: dict = {"commits": 1, "fail_rollback": True}
    st, lease = _beating_lease(tmp_path, monkeypatch, flaky)
    before = st.lease_expires("global")
    st.gateway.clock.advance(5)  # type: ignore[attr-defined]
    flaky["fail_rollback"] = False
    flaky["commits"] = 0
    lease._stop = _Ticks(2)  # type: ignore[assignment]
    # The first connection is already built with fail_rollback; the replacement is clean.
    flaky["conns"].clear()
    lease._beat()
    assert st.lease_expires("global") > before
    assert not lease.lost
    lease.release()


def test_persistent_failure_marks_the_lease_lost_within_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky: dict = {"commits": 10**6}
    st, lease = _beating_lease(tmp_path, monkeypatch, flaky, ttl=30.0)
    clock = st.gateway.clock
    ticks = _Ticks(10**6, on_tick=lambda: clock.advance(4))  # type: ignore[attr-defined]
    lease._stop = ticks  # type: ignore[assignment]
    lease._beat()  # returns by itself once the lease is lost
    assert lease.lost
    assert isinstance(lease.last_error, sqlite3.OperationalError)
    # lost no later than the first beat at or after one ttl of failures (30 s, 4 s steps)
    assert len(ticks.waits) <= 9
    assert all(w <= lease.ttl / 3 for w in ticks.waits)
    assert not flaky["conns"][-1].in_transaction
    lease.release()  # release still works after failures


def test_retry_delay_is_shorter_after_a_failure_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky: dict = {"commits": 3}
    _, lease = _beating_lease(tmp_path, monkeypatch, flaky, ttl=30.0)
    lease._stop = _Ticks(5)  # type: ignore[assignment]
    ticks = lease._stop
    lease._beat()
    w = ticks.waits  # type: ignore[attr-defined]
    assert w[0] == 10.0  # first wait: the normal ttl/3
    assert w[1] < w[0] and w[1] < w[2] <= 10.0  # backing off after failures, never above ttl/3
    assert w[-2] == 10.0  # back to normal after a success
    lease.release()



def test_a_failed_connect_is_retried_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    st = make_state(tmp_path, pid=100, start="s100", clock=FakeClock(), procs=FakeProcs())
    lease = st.lease("global", wait=0, ttl=30.0)
    real_connect, calls = state_mod._connect, []

    def connect(path):  # noqa: ANN001, ANN202
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(path)

    monkeypatch.setattr(state_mod, "_connect", connect)
    before = st.lease_expires("global")
    st.gateway.clock.advance(5)  # type: ignore[attr-defined]
    lease._stop = _Ticks(2)  # type: ignore[assignment]
    lease._beat()
    assert st.lease_expires("global") > before
    assert not lease.lost
    lease.release()
