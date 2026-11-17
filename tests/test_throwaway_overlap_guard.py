"""``create --throwaway`` re-checks the path overlap inside the record-insert transaction.

The pre-check (``_check_location``) reads the records before the insert, so a worktree create or
adopt in another clone (the per-clone lease does not serialize those) can commit a record that
contains the throwaway root in between. The guard passed to ``insert_record_leased`` repeats the
decision on the records read inside the transaction.

The rule: the new throwaway's root may not be, or lie inside, the path of any live record (a
throwaway included, so throwaways never nest), and no live record may lie inside the new
throwaway's own path. An equal path is left to the unique constraint (``path_exists``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.adoptlib import REPO2, inproc_baseline, second_clone
from tests.createlib import REPO
from tests.test_create_concurrency import plant, race
from tests.throwlib import TW, leaks
from wtctl.throwaway import ThrowawayCreator


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    inproc_baseline(monkeypatch)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> TW:
    return TW(tmp_path, private_home).build()


@pytest.fixture
def planted(w: TW, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Plant records after the pre-check, as a racing command in another clone would."""
    todo: list[tuple[str, dict]] = []
    real = ThrowawayCreator._pre_create

    def hooked(self, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        for path, kw in todo:
            plant(w, path.format(path=self.path), **kw)
        todo.clear()
        return real(self, *a, **k)

    monkeypatch.setattr(ThrowawayCreator, "_pre_create", hooked)
    return todo


def the_root(w: TW) -> str:
    w.troot.mkdir(parents=True, mode=0o700, exist_ok=True)
    return str(w.troot)


def nothing_leaked(w: TW, planted_count: int = 1) -> None:
    assert leaks(w.troot) == []  # no directory
    assert len(w.records()) == planted_count  # only what was planted
    assert w.leases() == []
    assert w.count("ids") == planted_count + 1  # the planted id and the burned create id


# -- the race: two real processes, ordered by barrier files -----------------------------------


def test_an_adopt_of_a_worktree_around_the_root_racing_a_throwaway_create_cannot_both_win(
    w: TW, tmp_path: Path
) -> None:
    clone2, cfg = second_clone(w)
    cand = w.tmp.resolve() / "around"  # a worktree of the second clone, wtctl does not know it
    w.g(["worktree", "add", "-q", "-b", "around", str(cand)], clone2)
    data = json.loads(Path(cfg).read_text())
    data["throwaway_root"] = str(cand / "tr")
    Path(cfg).write_text(json.dumps(data))
    results = race(
        w, tmp_path, cfg,
        [["create", "--throwaway", "--repo", REPO, "--ref", w.shas["c3"]],
         ["adopt", str(cand), "--repo", REPO2]],
        extra=[
            {"BARRIER_WAIT_AFTER": str(tmp_path / "adopted")},
            {"BARRIER_WAIT_FIRST": str(tmp_path / "b0"), "BARRIER_DONE": str(tmp_path / "adopted")},
        ],
    )  # fmt: skip
    # the adopt commits first; the throwaway's guard sees it
    assert [r[0] for r in results] == [2, 0], results
    err = json.loads(results[0][1])["error"]
    assert "lies inside" in err["message"], err
    assert [r["path"] for r in w.records()] == [str(cand)]
    assert w.leases() == []
    assert leaks(cand / "tr") == []  # no throwaway directory


# -- records planted between the pre-check and the insert --------------------------------------


@pytest.mark.parametrize(
    "kw",
    [
        {},  # a worktree record
        {"kind": "throwaway"},  # throwaways never nest
        {"state": "creating"},
        {"state": "partial"},
        {"repo": REPO},
    ],
)
@pytest.mark.parametrize("where", ["equal-root", "above-root"])
def test_a_record_around_the_root_planted_after_the_pre_check_is_refused_with_exit_2(
    w: TW,
    planted,
    where: str,
    kw: dict,  # noqa: ANN001
) -> None:
    root = the_root(w)
    planted.append((root if where == "equal-root" else str(w.troot.parent), kw))
    err = w.failed(w.throw("--source", "remote"), 2)
    assert f"the throwaway root {root} lies inside" in err["message"]  # the pre-check's message
    nothing_leaked(w)


def test_a_record_inside_the_new_throwaway_path_is_refused(w: TW, planted) -> None:  # noqa: ANN001
    the_root(w)
    planted.append(("{path}/deeper", {}))
    err = w.failed(w.throw("--source", "remote"), 2)
    assert str(w.troot) in err["message"] and "contains" in err["message"]
    assert leaks(w.troot) == []
    assert w.leases() == []


def test_a_sibling_throwaway_beside_the_new_one_does_not_block(w: TW, planted) -> None:  # noqa: ANN001
    root = the_root(w)
    planted.append((f"{root}/zzzzzz", {"kind": "throwaway"}))
    assert w.throw("--source", "remote").code == 0


def test_tombstones_do_not_block(w: TW, planted) -> None:  # noqa: ANN001
    planted.append((str(w.troot.parent), {"state": "removed"}))
    planted.append((str(w.troot), {"state": "forgotten"}))
    assert w.throw("--source", "remote").code == 0


def test_sibling_names_sharing_a_string_prefix_do_not_block(
    w: TW,
    planted,
    tmp_path: Path,  # noqa: ANN001
) -> None:
    # root is `<base>/tr`; records at `<base>/t` and `<base>/trx` share a prefix with it, one way
    # or the other, yet neither contains it
    base = tmp_path / "pfx"
    base.mkdir()
    w.top["throwaway_root"] = str(base / "tr")
    planted.append((str(base / "t"), {}))
    planted.append((str(base / "trx"), {}))
    assert w.throw("--source", "remote").code == 0


def test_an_equal_path_is_the_unique_constraint_not_the_overlap(
    w: TW,
    planted,  # noqa: ANN001
) -> None:
    the_root(w)
    planted.append(("{path}", {}))
    err = w.failed(w.throw("--source", "remote"), 20)
    assert err["reason"] == "path_exists"
    assert leaks(w.troot) == []
    assert w.leases() == []
