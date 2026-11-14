"""`label add` / `label remove` through the CLI."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from tests.listhelpers import cli, seed
from tests.schemacheck import validate
from tests.statehelpers import FakeClock, FakeProcs, fake_gateway, make_state
from wtctl import label_edit
from wtctl.state import State, record_lease

A, B = "wt-aaaaaaaa", "wt-bbbbbbbb"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def st(tmp_path: Path, clock: FakeClock) -> State:
    return make_state(tmp_path, clock=clock)


def labels_of(st: State, id_: str) -> list[str]:
    rec = st.get_record(id_)
    assert rec is not None
    return rec["labels"]


def test_add_golden_validates_and_sorts(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, labels=["z=1"])
    r = cli(capsys, st, "--json", "label", "add", A, "b=2", "a=1", "a=0")
    assert r.code == 0
    obj = r.json()
    validate("wtctl/cli/label-add.json", obj)
    assert obj == {"id": A, "labels": ["a=0", "a=1", "b=2", "z=1"], "dry_run": False}
    assert labels_of(st, A) == obj["labels"]


def test_remove_golden_validates(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, labels=["a=1", "b=2", "a=3"])
    r = cli(capsys, st, "--json", "label", "remove", A, "a=1", "nope=x")
    assert r.code == 0
    obj = r.json()
    validate("wtctl/cli/label-remove.json", obj)
    assert obj == {"id": A, "labels": ["a=3", "b=2"], "dry_run": False}


def test_multiset_semantics_a_key_may_hold_several_values(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    cli(capsys, st, "label", "add", A, "k=1")
    cli(capsys, st, "label", "add", A, "k=2", "k=1")
    assert labels_of(st, A) == ["k=1", "k=2"]
    cli(capsys, st, "label", "remove", A, "k=1")
    assert labels_of(st, A) == ["k=2"]  # removing one value keeps the key's other values


def test_noop_leaves_updated_unchanged(capsys, st, clock) -> None:  # noqa: ANN001
    seed(st, A, labels=["k=1"])
    before = st.get_record(A)["updated"]  # type: ignore[index]
    clock.advance(10)
    assert cli(capsys, st, "label", "add", A, "k=1").code == 0
    assert cli(capsys, st, "label", "remove", A, "x=y").code == 0
    assert st.get_record(A)["updated"] == before  # type: ignore[index]
    cli(capsys, st, "label", "add", A, "k=2")
    assert st.get_record(A)["updated"] != before  # type: ignore[index]


def test_empty_value_is_allowed(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    assert cli(capsys, st, "--json", "label", "add", A, "flag=").json()["labels"] == ["flag="]


@pytest.mark.parametrize(
    "bad", ["nokey", "K=v", "=v", "k=v v", "k=v\n", "k=é", "a" * 65 + "=v", "k=" + "v" * 257]
)
@pytest.mark.parametrize("verb", ["add", "remove"])
def test_bad_label_is_usage_and_changes_nothing(capsys, st, verb: str, bad: str) -> None:  # noqa: ANN001
    seed(st, A, labels=["keep=1"])
    r = cli(capsys, st, "--json", "label", verb, A, "ok=1", bad)
    assert r.code == 2
    assert r.json()["error"]["code"] == "usage"
    assert labels_of(st, A) == ["keep=1"]


def test_bad_label_beats_unknown_record(capsys, st) -> None:  # noqa: ANN001
    assert cli(capsys, st, "--json", "label", "add", "wt-zzzzzzzz", "BAD").code == 2


def test_limit_of_32_pairs_all_or_nothing(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, labels=[f"k=v{i}" for i in range(30)])
    r = cli(capsys, st, "--json", "label", "add", A, "n=1", "n=2", "n=3")
    assert r.code == 2
    assert len(labels_of(st, A)) == 30
    ok = cli(capsys, st, "--json", "label", "add", A, "n=1", "n=2", "k=v0")
    assert ok.code == 0
    assert len(ok.json()["labels"]) == 32
    assert cli(capsys, st, "--json", "label", "add", A, "n=3").code == 2
    # at the limit, removing then adding works
    assert cli(capsys, st, "label", "remove", A, "n=2").code == 0
    assert cli(capsys, st, "label", "add", A, "n=3").code == 0


def test_a_repeated_pair_in_one_call_counts_once(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, labels=[f"k=v{i}" for i in range(31)])
    assert cli(capsys, st, "label", "add", A, "n=1", "n=1", "n=1").code == 0
    assert len(labels_of(st, A)) == 32


def test_requires_at_least_one_label(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    assert cli(capsys, st, "label", "add", A).code == 2


def test_selectors_prefix_path_ambiguous_missing(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, path="/p/a")
    seed(st, "wt-aaaabbbb", path="/p/b")
    assert cli(capsys, st, "--json", "label", "add", "/p/a", "k=1").json()["id"] == A
    r = cli(capsys, st, "--json", "label", "add", "aaaa", "k=1")
    assert r.code == 21
    assert len(r.json()["error"]["candidates"]) == 2
    assert cli(capsys, st, "--json", "label", "add", "aaa", "k=1").code == 2
    assert cli(capsys, st, "--json", "label", "add", "wt-qqqqqqqq", "k=1").code == 10
    assert cli(capsys, st, "--json", "label", "add", "rel/path", "k=1").code == 2
    assert cli(capsys, st, "--json", "label", "add", "aaaaa", "k=2").json()["id"] == A


@pytest.mark.parametrize("verb", ["add", "remove"])
def test_tombstone_exits_10_with_details_even_by_full_id(capsys, st, verb: str) -> None:  # noqa: ANN001
    for i, state in enumerate(("gone", "removed", "forgotten")):
        id_ = f"wt-{state[0] * 8}"
        seed(st, id_, state=state, path=f"/t{i}", labels=["k=1"])
        r = cli(capsys, st, "--json", "label", verb, id_, "k=1")
        assert r.code == 10
        err = r.json()["error"]
        validate("wtctl/cli/error.json", r.json())
        assert err["details"]["state"] == state
        assert labels_of(st, id_) == ["k=1"]
    # a prefix never reaches a tombstone
    assert cli(capsys, st, "--json", "label", verb, "gggg", "k=1").code == 10


@pytest.mark.parametrize(
    "state",
    ["creating", "active", "unlinked", "missing", "clone_missing", "partial"],
)
def test_every_live_state_accepts_labels(capsys, st, state: str) -> None:  # noqa: ANN001
    seed(
        st,
        A,
        state=state,
        missing_since="2026-01-01T00:00:00.000Z" if state in ("missing", "clone_missing") else None,
    )
    assert cli(capsys, st, "--json", "label", "add", A, "k=1").code == 0
    assert labels_of(st, A) == ["k=1"]


def test_throwaway_takes_labels(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, object_type="throwaway")
    assert cli(capsys, st, "--json", "label", "add", A, "keep=1").json()["labels"] == ["keep=1"]


def test_dry_run_changes_nothing_and_reports(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, labels=["a=1"])
    before = st.get_record(A)
    r = cli(capsys, st, "--json", "--dry-run", "label", "add", A, "b=2")
    assert r.code == 0
    validate("wtctl/cli/label-add.json", r.json())
    assert r.json() == {"id": A, "labels": ["a=1", "b=2"], "dry_run": True}
    r = cli(capsys, st, "--json", "label", "remove", A, "a=1", "--dry-run")
    assert r.json() == {"id": A, "labels": [], "dry_run": True}
    validate("wtctl/cli/label-remove.json", r.json())
    assert st.get_record(A) == before


def test_dry_run_still_runs_every_check(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, labels=[f"k=v{i}" for i in range(32)])
    seed(st, B, state="removed", path="/b")
    assert cli(capsys, st, "--dry-run", "label", "add", A, "n=1").code == 2
    assert cli(capsys, st, "--dry-run", "label", "add", A, "BAD").code == 2
    assert cli(capsys, st, "--dry-run", "label", "add", B, "n=1").code == 10
    assert cli(capsys, st, "--dry-run", "label", "add", "wt-qqqqqqqq", "n=1").code == 10


def test_dry_run_takes_no_lease(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    other = State(st.path, st.gateway, pid=200)
    st.gateway.procs.table[200] = "s200"  # type: ignore[attr-defined]
    with other.lease(record_lease(A), wait=0):
        assert cli(capsys, st, "--dry-run", "label", "add", A, "k=1").code == 0


def test_busy_when_another_process_holds_the_record_lease(
    capsys,
    st,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    seed(st, A, state="creating")
    st.gateway.procs.table[200] = "s200"  # type: ignore[attr-defined]
    other = State(st.path, st.gateway, pid=200)
    monkeypatch.setattr(label_edit, "lease_wait", lambda ctx: 0.0)
    with other.lease(record_lease(A), wait=0):
        r = cli(capsys, st, "--json", "label", "add", A, "k=1")
        assert r.code == 12
        assert r.json()["error"]["code"] == "busy"
        assert labels_of(st, A) == []
    # the creator died: the lease is taken over and the label applied
    st.gateway.procs.table.pop(200)  # type: ignore[attr-defined]
    leaked = State(st.path, st.gateway, pid=200)
    st.gateway.procs.table[200] = "s200"  # type: ignore[attr-defined]
    with leaked.lease(record_lease(A), wait=0):
        pass
    assert cli(capsys, st, "label", "add", A, "k=1").code == 0


def test_dead_lease_holder_is_taken_over(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    st.gateway.procs.table[200] = "s200"  # type: ignore[attr-defined]
    dead = State(st.path, st.gateway, pid=200)
    lease = dead.lease(record_lease(A), wait=0)
    del st.gateway.procs.table[200]  # type: ignore[attr-defined]
    assert cli(capsys, st, "label", "add", A, "k=1").code == 0
    del lease  # never released: its holder is gone


def test_lease_is_released_after_the_command(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    cli(capsys, st, "label", "add", A, "k=1")
    assert st.try_lease(record_lease(A)) is not None


def test_record_removed_between_resolve_and_write_exits_10(
    capsys,
    st,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    seed(st, A, labels=["k=1"])
    real = State.resolve

    def resolve_then_lose_race(self: State, selector: str, **kw):  # noqa: ANN003, ANN202
        rec = real(self, selector, **kw)
        self.transition(rec["id"], "active", "removed")
        return rec

    monkeypatch.setattr(State, "resolve", resolve_then_lose_race)
    r = cli(capsys, st, "--json", "label", "add", A, "k=2")
    assert r.code == 10
    assert r.json()["error"]["details"]["state"] == "removed"
    assert labels_of(st, A) == ["k=1"]


def test_concurrent_adds_from_separate_processes_all_land(tmp_path: Path) -> None:
    """Real SQLite contention: each worker has its own connection and adds its own pair."""
    path = tmp_path / "shared" / "state.db"
    first = make_state(tmp_path, name="x.db")
    first.close()
    clock = FakeClock()
    boot = State(path, fake_gateway(clock, FakeProcs({100: "s100"})), pid=100)
    seed(boot, A)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            procs = FakeProcs({100 + n: f"s{100 + n}"})
            s = State(path, fake_gateway(clock, procs), pid=100 + n)
            for j in range(5):
                s.add_labels(A, [f"w{n}=v{j}"])
            s.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    assert len(labels_of(boot, A)) == 20


def test_human_output_is_the_labels_and_summary_on_stderr(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    r = cli(capsys, st, "label", "add", A, "b=2", "a=1")
    assert r.out == "a=1\nb=2\n"
    assert A in r.err
    assert cli(capsys, st, "--quiet", "label", "add", A, "c=3").err == ""


def test_label_injection_shaped_values_stay_data(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    # SQL-ish text fails the grammar (space, quote); grammar-valid lookalikes are stored verbatim
    assert cli(capsys, st, "label", "add", A, "k=1';DROP TABLE records;--").code == 2
    assert cli(capsys, st, "label", "add", A, "k=a,b=c").code == 0
    assert labels_of(st, A) == ["k=a,b=c"]
    assert sqlite3.connect(st.path).execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
