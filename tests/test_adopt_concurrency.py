"""Concurrent ``adopt`` and ``forget``: the lease and the compare-and-set keep one record and
exact holder sets."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from tests.adoptlib import adopt, fast, foreign, ok_adopt, w  # noqa: F401
from tests.createlib import CW, REPO


def spawn(w: CW, *argv: str) -> subprocess.Popen[str]:
    env = {**os.environ, "HOME": str(w.home)}
    return subprocess.Popen(
        [sys.executable, "-m", "wtctl", "--config", w.config(), "--json", "--actor", "auto:t", *argv],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )  # fmt: skip


def finish(procs: list[subprocess.Popen[str]]) -> list[tuple[int, str, str]]:
    out = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=120)
        out.append((p.returncode, stdout, stderr))
    return out


def test_parallel_adopts_of_one_path_make_one_record_and_keep_every_holder(w: CW) -> None:
    path = foreign(w)
    procs = [spawn(w, "adopt", str(path), "--repo", REPO, "--holder", f"h{i}") for i in range(4)]
    results = finish(procs)
    assert [r[0] for r in results] == [0] * 4, results
    existing = sorted(json.loads(r[1])["existing"] for r in results)
    assert existing == [False, True, True, True]
    assert w.count("records") == 1
    assert sorted(x[0] for x in w.rows("SELECT holder FROM holders")) == ["h0", "h1", "h2", "h3"]


def test_parallel_forgets_of_the_holders_forget_the_record_exactly_once(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    rid = None
    for i in range(4):
        rid = ok_adopt(adopt(w, capsys, path, "--holder", f"h{i}"))["record"]["id"]
    assert rid is not None
    procs = [spawn(w, "forget", rid, "--holder", f"h{i}") for i in range(4)]
    results = finish(procs)
    assert [r[0] for r in results].count(0) >= 1, results
    # a loser that arrives after the tombstone exits 10; nobody sees any other failure
    assert {r[0] for r in results} <= {0, 10}, results
    row = w.rows("SELECT state FROM records WHERE id = ?", rid)[0]
    assert row[0] == "forgotten"
    assert w.rows("SELECT count(*) FROM holders WHERE record_id = ?", rid)[0][0] == 0


def test_forget_and_adopt_holder_race_never_loses_a_live_holder(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    rid = ok_adopt(adopt(w, capsys, path, "--holder", "old"))["record"]["id"]
    procs = [
        spawn(w, "forget", rid, "--holder", "old"),
        spawn(w, "adopt", str(path), "--repo", REPO, "--holder", "new"),
    ]
    results = finish(procs)
    assert all(r[0] in (0, 10, 12) for r in results), results
    state = w.rows("SELECT state FROM records WHERE id = ?", rid)[0][0]
    holders = sorted(x[0] for x in w.rows("SELECT holder FROM holders WHERE record_id = ?", rid))
    # either the adopt won the race (record live with "new"), or the forget did first and the
    # adopt created a second record; a live record with "new" must exist in both cases
    if state == "forgotten":
        live = w.rows("SELECT id FROM records WHERE state = 'active'")
        assert len(live) == 1
        assert [x[0] for x in w.rows("SELECT holder FROM holders WHERE record_id = ?", live[0][0])] == ["new"]
    else:
        assert holders == ["new"]


@pytest.mark.skipif(sys.platform == "win32", reason="posix only")
def test_a_second_adopt_of_the_same_new_path_waits_for_the_clone_lease(w: CW, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import Gateway
    from wtctl.state import State, clone_lease

    path = foreign(w)
    st = State(w.db, Gateway())
    lease = st.lease(clone_lease(str(w.clone.resolve())), wait=1)
    try:
        r = adopt(w, capsys, path)  # lease_wait is 1s in the test config
        assert r.code == 12 and r.json()["error"]["code"] == "busy"
    finally:
        lease.release()
        st.close()
    assert w.count("records") == 0
    ok_adopt(adopt(w, capsys, path))
