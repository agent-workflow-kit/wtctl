"""``create`` re-checks the path overlap inside the record-insert transaction.

The per-clone lease serializes creates in one clone only; two clones race on the shared
``state.db``. Each process below passes every pre-check before either inserts (a barrier in front
of the insert), so only the guard inside the insert transaction can refuse the loser.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.adoptlib import REPO2, inproc_baseline, second_clone, world
from tests.createlib import CW, REPO
from tests.test_create import refused

SUB = "{root}/nest/{name}/sub"  # repo2's worktree_path: lies inside repo1's {root}/nest/{name}


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    inproc_baseline(monkeypatch)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return world(tmp_path, private_home)


def two_clone_config(w: CW) -> tuple[Path, str]:
    """repo2's worktree root is the very path repo1 creates ``outer`` at, so repo2's target lies
    inside repo1's."""
    import json

    clone2, cfg = second_clone(w)
    r1 = w.tmp.resolve() / "r1"
    outer = r1 / "example.test" / "org" / "repo" / "outer"
    outer.parent.mkdir(parents=True)
    data = json.loads(Path(cfg).read_text())
    data["repos"][REPO] = {**w.repo_cfg, "worktree_root": str(r1)}
    data["repos"][REPO2]["worktree_root"] = str(outer)
    Path(cfg).write_text(json.dumps(data))
    return clone2, cfg


BARRIER = """
import os, sys, time
from wtctl import baselinesvc, repobaseline
baselinesvc.RUNNER = repobaseline.inproc
from wtctl.state import State

mine, theirs = sys.argv[1], sys.argv[2]
first = os.environ.get("BARRIER_WAIT_FIRST")  # start only once this file exists
after = os.environ.get("BARRIER_WAIT_AFTER")  # insert only once this file exists
done = os.environ.get("BARRIER_DONE")  # written once the insert returned


def wait_for(path):
    deadline = time.time() + 30
    while not os.path.exists(path) and time.time() < deadline:
        time.sleep(0.02)


def wrap(orig):
    def hooked(self, *a, **k):
        # both commands have passed their checks before either inserts
        open(mine, "w").close()
        wait_for(theirs)
        if after:
            wait_for(after)
        try:
            return orig(self, *a, **k)
        finally:
            if done:
                open(done, "w").close()
    return hooked


State.insert_record = wrap(State.insert_record)
State.insert_record_leased = wrap(State.insert_record_leased)
from wtctl.cli import main

if first:
    wait_for(first)
sys.exit(main(sys.argv[3:]))
"""


def race(
    w: CW, tmp: Path, cfg: str, cmds: list[list[str]], extra: list[dict[str, str]] | None = None
) -> list[tuple[int, str, str]]:
    script = tmp / "barrier.py"
    script.write_text(BARRIER)
    procs: list[subprocess.Popen[str]] = []
    out = []
    try:
        for i, argv in enumerate(cmds):
            env = {**os.environ, "HOME": str(w.home), **((extra or [{}, {}])[i])}
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(script),
                        str(tmp / f"b{i}"),
                        str(tmp / f"b{1 - i}"),
                        "--config",
                        cfg,
                        "--json",
                        "--actor",
                        "auto:t",
                        *argv,
                    ],  # fmt: skip
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
            )
        for p in procs:
            stdout, stderr = p.communicate(timeout=120)
            out.append((p.returncode, stdout, stderr))
    finally:
        for p in procs:  # only the PIDs started here, and only if still running
            if p.poll() is None:
                p.kill()
                p.wait()
    return out


def live_paths(w: CW) -> list[str]:
    rows = w.rows("SELECT path FROM records WHERE state NOT IN ('removed', 'forgotten')")
    return [r[0] for r in rows]


def test_creates_of_nested_paths_in_two_clones_cannot_both_win(w: CW, tmp_path: Path) -> None:
    _, cfg = two_clone_config(w)
    results = race(
        w, tmp_path, cfg,
        [["create", "--repo", REPO, "--name", "outer"],
         ["create", "--repo", REPO2, "--name", "inner"]],
        extra=[
            {"BARRIER_WAIT_AFTER": str(tmp_path / "second_inserted")},
            {"BARRIER_WAIT_FIRST": str(tmp_path / "b0"),
             "BARRIER_DONE": str(tmp_path / "second_inserted")},
        ],
    )  # fmt: skip
    # repo2 commits first (its target lies inside repo1's); repo1's guard sees that record
    assert [r[0] for r in results] == [2, 0], results
    assert "existing worktree" in results[0][1], results
    assert len(live_paths(w)) == 1
    assert w.admin_entries() == []  # repo1 added nothing to its clone
    assert not (w.tmp / "r1" / "example.test" / "org" / "repo" / "outer" / ".git").exists()


def test_create_racing_an_adopt_of_a_path_around_its_target_cannot_both_win(
    w: CW, tmp_path: Path
) -> None:
    """The second clone is not yet known to ``state.db``, so create's pre-check cannot see its
    worktrees; the adopt records one that contains create's target, and commits before create
    inserts."""
    import json

    clone2, cfg = second_clone(w)
    cand = w.tmp.resolve() / "around"  # a worktree of the second clone
    w.g(["worktree", "add", "-q", "-b", "around", str(cand)], clone2)
    data = json.loads(Path(cfg).read_text())
    data["repos"][REPO] = {**w.repo_cfg, "worktree_root": str(cand)}
    Path(cfg).write_text(json.dumps(data))
    results = race(
        w, tmp_path, cfg,
        [["create", "--repo", REPO, "--name", "inside"],
         ["adopt", str(cand), "--repo", REPO2]],
        extra=[
            {"BARRIER_WAIT_AFTER": str(tmp_path / "adopted")},
            {"BARRIER_WAIT_FIRST": str(tmp_path / "b0"), "BARRIER_DONE": str(tmp_path / "adopted")},
        ],
    )  # fmt: skip
    assert [r[0] for r in results] == [2, 0], results
    assert live_paths(w) == [str(cand)]
    assert w.admin_entries() == []  # create added nothing to its clone


def plant(w: CW, path: str, *, kind: str = "worktree", repo: str = REPO2, state: str = "active"):
    from wtctl.gateway import Gateway
    from wtctl.state import State

    st = State(w.db, Gateway())
    rid = st.allocate_id()
    st.insert_record(
        {
            "id": rid,
            "object_type": kind,
            "state": state,
            "repo": repo,
            "path": path,
            "name": Path(path).name,
            "owned": True,
            "clone_path": str(w.clone),
        }  # fmt: skip
    )
    st.close()
    return rid


@pytest.fixture
def planted(w: CW, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Plant a record after create's pre-checks, as a racing command in another clone would."""
    from wtctl.commands.create import Creator

    todo: list[tuple[str, dict]] = []
    real = Creator._pre_create

    def hooked(self, plan):  # noqa: ANN001, ANN202
        for suffix, kw in todo:
            plant(w, suffix, **kw)
        todo.clear()
        return real(self, plan)

    monkeypatch.setattr(Creator, "_pre_create", hooked)
    return todo


def _target(w: CW) -> str:
    return str(w.root.resolve() / "worktrees" / "example.test" / "org" / "repo" / "t1")


@pytest.mark.parametrize(
    ("where", "kw"),
    [
        ("inside", {}),  # the target lies inside a live record's path
        ("around", {}),  # the target contains a live record's path
        ("inside", {"kind": "throwaway"}),
        ("around", {"kind": "throwaway"}),
        ("inside", {"state": "creating"}),
        ("around", {"state": "partial"}),
        ("inside", {"repo": REPO}),  # same repo: the clone lease is no help to a non-create
    ],
)
def test_a_record_planted_after_the_pre_check_refuses_with_exit_2_and_leaks_nothing(
    w: CW,
    capsys,
    planted,
    where: str,
    kw: dict,  # noqa: ANN001
) -> None:
    target = _target(w)
    other = str(Path(target).parent) if where == "inside" else target + "/deeper"
    planted.append((other, kw))
    before = w.snapshot()
    r = w.create(capsys, "--name", "t1")
    err = refused(r, 2)
    assert "existing worktree" in err["message"]
    after = w.snapshot()
    assert after["refs"] == before["refs"]
    assert after["admin"] == [] and after["worktrees"] == before["worktrees"]
    assert not Path(target).exists()
    assert [x for x in live_paths(w) if x == target] == []
    assert w.count("records") == 1  # only the planted one
    assert w.count("leases") == 0
    assert w.count("ids") == 2  # the planted id and the burned create id


def test_tombstone_paths_do_not_block(w: CW, capsys, planted) -> None:  # noqa: ANN001
    planted.append((str(Path(_target(w)).parent), {"state": "removed"}))
    planted.append((_target(w) + "/deeper", {"state": "forgotten"}))
    r = w.create(capsys, "--name", "t1")
    assert r.code == 0, (r.out, r.err)


def test_an_equal_path_is_the_unique_constraint_not_the_overlap(w: CW, capsys, planted) -> None:  # noqa: ANN001
    planted.append((_target(w), {}))
    refused(w.create(capsys, "--name", "t1"), 20, "path_exists")


def test_sibling_names_sharing_a_string_prefix_do_not_block(w: CW, capsys, planted) -> None:  # noqa: ANN001
    # `<parent>/t1x` and `<parent>/t` share a string prefix with the target `<parent>/t1` (one way
    # or the other) but are neither inside nor around it; a plain startswith would refuse
    parent = str(Path(_target(w)).parent)
    planted.append((parent + "/t1x", {}))
    planted.append((parent + "/t", {}))
    r = w.create(capsys, "--name", "t1")
    assert r.code == 0, (r.out, r.err)
