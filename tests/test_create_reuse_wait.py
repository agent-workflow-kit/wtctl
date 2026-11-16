"""A holder's retry that meets a record still being created (a held creator lease).

The creator is another process: leases are per process, so one held by this test process would
refuse at once instead of making the retry wait.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.createlib import CW
from tests.test_create import refused

CREATOR = """
import sqlite3, sys, time
from wtctl.gateway import Gateway
from wtctl.state import State, clone_lease, record_lease
db, rid, clone, finish = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
st = State(db, Gateway())
lease = st.lease(record_lease(rid), wait=1)
print("held", flush=True)
if finish < 0:
    sys.stdin.readline()  # hold until the test is done
    sys.exit(0)
time.sleep(finish)
try:
    st.lease(clone_lease(clone), wait=0).release()
    print("clone-free", flush=True)
except Exception as exc:
    print("clone-held", exc, flush=True)
conn = sqlite3.connect(db)
conn.execute("UPDATE records SET state = 'active' WHERE id = ?", (rid,))
conn.commit()
conn.close()
lease.release()
"""


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def creator(w: CW, rid: str, finish: float, home: Path) -> subprocess.Popen[str]:
    env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "PATH": "/usr/bin:/bin"}
    p = subprocess.Popen(
        [sys.executable, "-c", CREATOR, str(w.db), rid, str(w.clone.resolve()), str(finish)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert p.stdout is not None and p.stdout.readline().strip() == "held"
    return p


@pytest.fixture
def procs() -> Iterator[list[subprocess.Popen[str]]]:
    made: list[subprocess.Popen[str]] = []
    yield made
    for p in made:
        if p.stdin and not p.stdin.closed:
            p.stdin.close()
        p.wait(timeout=10)


def test_a_retry_waits_for_the_creator_and_returns_reused(
    w: CW,
    capsys,
    private_home: Path,
    procs,  # noqa: ANN001
) -> None:
    rid = w.plant_creating(branch=None, name="busy-one", holder="h1")
    p = creator(w, rid, 0.5, private_home)
    procs.append(p)
    r = w.create(capsys, "--name", "busy-one", "--holder", "h1")
    assert r.code == 0, (r.out, r.err)
    data = r.json()  # (the planted record is not schema-complete)
    assert data["reused"] is True and data["record"]["id"] == rid
    assert p.stdout is not None
    # the retry was not holding the per-clone lease while it waited
    assert p.stdout.readline().strip() == "clone-free"


def test_a_wait_that_times_out_ends_in_the_documented_refusal_not_busy(
    w: CW,
    capsys,
    private_home: Path,
    procs,  # noqa: ANN001
) -> None:
    rid = w.plant_creating(branch=None, name="busy-one", holder="h1")
    procs.append(creator(w, rid, -1, private_home))
    start = time.monotonic()
    err = refused(w.create(capsys, "--name", "busy-one", "--holder", "h1"), 20, "path_exists")
    assert time.monotonic() - start >= 0.9  # it did wait (lease_wait is 1s)
    assert err["holder_id"] == rid


def test_a_dry_run_does_not_take_the_creators_lease(
    w: CW,
    capsys,
    private_home: Path,
    procs,  # noqa: ANN001
) -> None:
    rid = w.plant_creating(branch=None, name="busy-one", holder="h1")
    procs.append(creator(w, rid, -1, private_home))
    start = time.monotonic()
    err = refused(
        w.create(capsys, "--dry-run", "--name", "busy-one", "--holder", "h1"), 20, "path_exists"
    )
    assert time.monotonic() - start < 0.9  # lease_wait is 1s: it did not wait
    assert err["holder_id"] == rid


def test_a_creator_that_crashed_leaves_the_refusal(w: CW, capsys) -> None:  # noqa: ANN001
    rid = w.plant_creating(branch=None, name="crashed", holder="h1")
    err = refused(w.create(capsys, "--name", "crashed", "--holder", "h1"), 20, "path_exists")
    assert err["holder_id"] == rid
