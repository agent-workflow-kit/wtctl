"""Concurrent ``adopt`` and ``forget``: the lease and the compare-and-set keep one record and
exact holder sets."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.adoptlib import (
    REPO2,
    adopt,
    err_of,
    foreign,
    inproc_baseline,
    ok_adopt,
    second_clone,
    world,
)
from tests.createlib import CW, REPO


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    inproc_baseline(monkeypatch)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return world(tmp_path, private_home)


def spawn(w: CW, *argv: str) -> subprocess.Popen[str]:
    env = {**os.environ, "HOME": str(w.home)}
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "wtctl",
            "--config",
            w.config(),
            "--json",
            "--actor",
            "auto:t",
            *argv,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def patient(w: CW) -> None:
    """The world's 1s ``lease_wait`` is for tests that expect ``busy``. Racing processes queue on
    the clone lease, and on a loaded host each one's start-up alone can outlast a second, so a
    queued loser would exit 12; a long wait makes the outcome independent of host speed."""
    w.top["lease_wait"] = "300s"


def finish(procs: list[subprocess.Popen[str]]) -> list[tuple[int, str, str]]:
    out = []
    try:
        for p in procs:
            stdout, stderr = p.communicate(timeout=120)
            out.append((p.returncode, stdout, stderr))
    except subprocess.TimeoutExpired:
        for p in procs:  # never leave the spawned processes running
            p.kill()
        for p in procs:
            p.wait()
        raise
    return out


def test_parallel_adopts_of_one_path_make_one_record_and_keep_every_holder(w: CW) -> None:
    patient(w)
    path = foreign(w)
    procs = [spawn(w, "adopt", str(path), "--repo", REPO, "--holder", f"h{i}") for i in range(4)]
    results = finish(procs)
    assert [r[0] for r in results] == [0] * 4, results
    existing = sorted(json.loads(r[1])["existing"] for r in results)
    assert existing == [False, True, True, True]
    assert w.count("records") == 1
    assert sorted(x[0] for x in w.rows("SELECT holder FROM holders")) == ["h0", "h1", "h2", "h3"]


def test_parallel_forgets_of_the_holders_forget_the_record_exactly_once(w: CW, capsys) -> None:  # noqa: ANN001
    patient(w)
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
    patient(w)
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
    if state == "forgotten":
        # the forget won. The adopt either saw the live record first (and then lost it, exit 10),
        # or ran after the tombstone and made a second record carrying only "new".
        live = w.rows("SELECT id FROM records WHERE state = 'active'")
        assert len(live) <= 1
        if live:
            assert results[1][0] == 0, results
            assert [
                x[0] for x in w.rows("SELECT holder FROM holders WHERE record_id = ?", live[0][0])
            ] == ["new"]
        else:
            assert results[1][0] == 10, results
    else:
        assert results[1][0] == 0, results
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


BARRIER = """
import os, sys, time
from wtctl import baselinesvc, repobaseline
baselinesvc.RUNNER = repobaseline.inproc
from wtctl.state import State

orig = State.insert_record
mine, theirs = sys.argv[1], sys.argv[2]


def hooked(self, *a, **k):
    # both adopts have passed their checks before either inserts
    open(mine, "w").close()
    deadline = time.time() + 20
    while not os.path.exists(theirs) and time.time() < deadline:
        time.sleep(0.02)
    return orig(self, *a, **k)


State.insert_record = hooked
from wtctl.cli import main

sys.exit(main(sys.argv[3:]))
"""


def test_adopts_of_nested_paths_in_two_clones_cannot_both_win(w: CW, tmp_path: Path) -> None:
    clone2, cfg = second_clone(w)
    outer = foreign(w, "outer")  # a worktree of the first clone
    inner = outer / "inner"  # a worktree of the second clone, inside it
    w.g(["worktree", "add", "-q", "-b", "inner", str(inner)], clone2)
    script = tmp_path / "barrier_adopt.py"
    script.write_text(BARRIER)
    env = {**os.environ, "HOME": str(w.home)}

    def start(mine: str, theirs: str, path: Path, repo: str) -> subprocess.Popen[str]:
        argv = ["--config", cfg, "--json", "--actor", "auto:t", "adopt", str(path), "--repo", repo]
        return subprocess.Popen(
            [sys.executable, str(script), str(tmp_path / mine), str(tmp_path / theirs), *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    results = finish([start("a", "b", outer, REPO), start("b", "a", inner, REPO2)])
    assert sorted(r[0] for r in results) == [0, 2], results
    assert w.count("records") == 1


def test_a_create_committed_between_the_pre_check_and_the_insert_is_refused(
    w: CW,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reverse race: a record a create committed after adopt's pre-check, inside the
    candidate's path, is caught by the guard inside adopt's insert transaction."""
    from wtctl import baselinesvc
    from wtctl.gateway import Gateway
    from wtctl.state import State

    outer = foreign(w, "outer")
    real = baselinesvc.record_current

    def hooked(*a, **k):  # noqa: ANN002, ANN003, ANN202
        st = State(w.db, Gateway())
        st.insert_record(
            {"id": st.allocate_id(), "object_type": "worktree", "state": "active",
             "repo": REPO2, "path": str(outer / "deeper"), "name": "deeper", "owned": True,
             "clone_path": str(w.clone)}
        )  # fmt: skip
        st.close()
        return real(*a, **k)

    monkeypatch.setattr(baselinesvc, "record_current", hooked)
    err = err_of(adopt(w, capsys, outer), 2)
    assert "contains the path of the live record" in err["message"]
    assert w.count("records") == 1  # only the planted one
