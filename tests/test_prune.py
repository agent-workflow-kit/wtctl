"""``prune --throwaways`` (docs/interface.md, prune): age, protection, skips, partial retries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import REPO
from tests.test_throwaway_remove import stuck
from tests.throwlib import TW, leaks
from wtctl.gateway import Gateway, ProcessInfo
from wtctl.state import GLOBAL_LEASE, State, record_lease


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> TW:
    return TW(tmp_path, private_home).build()


def mk(w: TW, days: float, *labels: str) -> dict[str, Any]:
    rec = w.made("--source", "local", *[x for lab in labels for x in ("--label", lab)])
    w.age(rec["id"], days)
    return rec


# -- age ---------------------------------------------------------------------------------------


def test_removes_throwaways_older_than_the_given_age_and_nothing_else(w: TW) -> None:
    old, mid, new = mk(w, 10), mk(w, 3), mk(w, 0)
    data = w.pruned("--older-than", "7d")
    assert data["removed"] == [old["id"]] and data["skipped"] == []
    assert data["already_running"] is False and data["dry_run"] is False
    assert not Path(old["path"]).exists()
    assert Path(mid["path"]).is_dir() and Path(new["path"]).is_dir()
    assert w.state_of(old["id"]) == "removed" and w.state_of(mid["id"]) == "active"
    assert w.pruned("--older-than", "2d")["removed"] == [mid["id"]]
    # `new` was just created: far inside a 1h age, whatever the machine's speed
    assert w.pruned("--older-than", "1h")["removed"] == []
    assert Path(new["path"]).is_dir() and w.state_of(new["id"]) == "active"


@pytest.mark.parametrize("unit", ["12h", "2d", "1w", "30m", "86400s"])
def test_durations(w: TW, unit: str) -> None:
    rec = mk(w, 100)
    assert w.pruned("--older-than", unit)["removed"] == [rec["id"]]


def test_the_default_age_is_the_configured_max_age(w: TW) -> None:
    w.top["throwaway"] = {"max_age": "7d"}
    old, new = mk(w, 8), mk(w, 1)
    assert w.pruned()["removed"] == [old["id"]]
    assert Path(new["path"]).is_dir()


def test_an_explicit_age_beats_the_configured_one(w: TW) -> None:
    w.top["throwaway"] = {"max_age": "100d"}
    rec = mk(w, 2)
    assert w.pruned()["removed"] == []
    assert w.pruned("--older-than", "1d")["removed"] == [rec["id"]]


def test_without_an_age_prune_is_a_usage_error(w: TW) -> None:
    mk(w, 100)
    w.failed(w.prune(), 2)
    assert len(leaks(w.troot)) > 0


def test_if_configured_without_an_age_only_retries_partial_throwaways(w: TW) -> None:
    old, part = mk(w, 100), mk(w, 0)
    w.set_state(part["id"], "partial")
    data = w.pruned("--if-configured")
    assert data["removed"] == [part["id"]]
    assert Path(old["path"]).is_dir() and w.state_of(old["id"]) == "active"


def test_if_configured_with_an_age_uses_it(w: TW) -> None:
    w.top["throwaway"] = {"max_age": "7d"}
    old = mk(w, 8)
    assert w.pruned("--if-configured")["removed"] == [old["id"]]


@pytest.mark.parametrize("bad", ["0d", "7", "d", "-1d", "7x", "1.5d", ""])
def test_a_bad_duration_is_a_usage_error(w: TW, bad: str) -> None:
    w.failed(w.prune("--older-than", bad), 2)


def test_a_duration_too_large_for_a_date_removes_nothing(w: TW) -> None:
    mk(w, 100)
    assert w.pruned("--older-than", "99999999999w")["removed"] == []


@pytest.mark.parametrize("age", ["57000w", "56000w", "60000w"])
def test_an_age_reaching_before_year_1000_removes_nothing(w: TW, age: str) -> None:
    # %Y is not zero padded below year 1000: a text comparison must still say "nothing is older"
    rec = mk(w, 0)
    assert w.pruned("--older-than", age)["removed"] == []
    assert w.state_of(rec["id"]) == "active"


def test_prune_requires_throwaways(w: TW) -> None:
    w.failed(w.run("prune", "--older-than", "1d"), 2)


def test_partial_ones_are_retried_whatever_their_age(w: TW) -> None:
    young_partial, old = mk(w, 0), mk(w, 30)
    w.set_state(young_partial["id"], "partial")
    data = w.pruned("--older-than", "7d")
    assert sorted(data["removed"]) == sorted([young_partial["id"], old["id"]])


def test_only_active_and_partial_throwaways_are_touched(w: TW) -> None:
    creating, unlinked, worktree = mk(w, 50), mk(w, 50), None
    w.set_state(creating["id"], "creating")
    w.set_state(unlinked["id"], "unlinked")
    worktree = w.run("create", "--repo", REPO, "--detach", "--no-bootstrap").json()["record"]
    w.age(worktree["id"], 50)
    gone = mk(w, 50)
    w.set_state(gone["id"], "gone")
    assert w.pruned("--older-than", "1d")["removed"] == []
    for r in (creating, unlinked, gone):
        assert Path(r["path"]).is_dir()
    assert Path(worktree["path"]).is_dir() and w.state_of(worktree["id"]) == "active"


def test_the_label_filter_narrows_the_set(w: TW) -> None:
    a, b = mk(w, 10, "run=1"), mk(w, 10, "run=2")
    data = w.pruned("--older-than", "1d", "--label", "run=1")
    assert data["removed"] == [a["id"]] and Path(b["path"]).is_dir()
    w.failed(w.prune("--older-than", "1d", "--label", "BAD"), 2)


# -- protection and skips ----------------------------------------------------------------------


@pytest.mark.parametrize("protect", [["keep"], ["keep=yes"], ["other", "keep=yes"]])
def test_protected_labels_are_skipped(w: TW, protect: list[str]) -> None:
    w.top["prune"] = {"protect_labels": protect}
    prot, free, wrong = mk(w, 10, "keep=yes"), mk(w, 10), mk(w, 10, "keep=no")
    data = w.pruned("--older-than", "1d")
    expect_wrong_removed = protect != ["keep"] and "keep=yes" in protect
    removed = {free["id"]} | ({wrong["id"]} if expect_wrong_removed else set())
    assert set(data["removed"]) == removed
    assert {"id": prot["id"], "reason": "protected", "processes": [], "paths": []} in data[
        "skipped"
    ]
    assert Path(prot["path"]).is_dir() and Path(prot["path"]).stat().st_mode


def test_protection_applies_to_partial_ones_and_to_a_dry_run(w: TW) -> None:
    w.top["prune"] = {"protect_labels": ["keep"]}
    prot = mk(w, 0, "keep=1")
    w.set_state(prot["id"], "partial")
    assert w.pruned("--if-configured")["skipped"][0]["reason"] == "protected"
    assert w.pruned("--if-configured", "--dry-run")["skipped"][0]["reason"] == "protected"
    assert Path(prot["path"]).is_dir()


def test_in_use_throwaways_are_skipped_with_their_processes(w: TW) -> None:
    busy, free = mk(w, 10), mk(w, 10)
    w.scanner.procs = [ProcessInfo(4242, "vim", busy["path"], ())]
    data = w.pruned("--older-than", "1d")
    assert data["removed"] == [free["id"]]
    (skip,) = data["skipped"]
    assert skip["id"] == busy["id"] and skip["reason"] == "in_use" and skip["paths"] == []
    assert skip["processes"][0]["pid"] == 4242
    assert Path(busy["path"]).is_dir() and w.state_of(busy["id"]) == "active"


def test_an_unavailable_scanner_skips_everything_as_failed(w: TW) -> None:
    rec = mk(w, 10)
    w.scanner.fail = True
    data = w.pruned("--older-than", "1d")
    assert data["removed"] == []
    assert data["skipped"] == [{"id": rec["id"], "reason": "failed", "processes": [], "paths": []}]
    assert Path(rec["path"]).is_dir() and w.state_of(rec["id"]) == "active"
    assert w.pruned("--older-than", "1d", "--dry-run")["skipped"][0]["reason"] == "failed"


def test_hooks_veto_and_escalate_per_record(w: TW, tmp_path: Path) -> None:
    a, b = mk(w, 10), mk(w, 10)
    w.top["hooks"] = {
        "pre-remove": [{"cmd": f"if grep -q {a['id']} /dev/stdin; then exit 3; else exit 13; fi"}]
    }
    data = w.pruned("--older-than", "1d")
    assert data["removed"] == []
    reasons = {s["id"]: s["reason"] for s in data["skipped"]}
    assert reasons == {a["id"]: "vetoed", b["id"]: "escalated"}
    assert Path(a["path"]).is_dir() and Path(b["path"]).is_dir()


def test_a_held_record_lease_is_busy_and_the_run_goes_on(w: TW) -> None:
    held, free = mk(w, 10), mk(w, 10)
    st = State(w.db, Gateway())
    lease = st.try_lease(record_lease(held["id"]))
    assert lease is not None
    try:
        data = w.pruned("--older-than", "1d")
    finally:
        lease.release()
        st.close()
    assert data["removed"] == [free["id"]], data
    assert data["skipped"] == [{"id": held["id"], "reason": "busy", "processes": [], "paths": []}]


def test_a_failed_removal_is_reported_and_retried_by_the_next_run(
    w: TW, monkeypatch: pytest.MonkeyPatch, inproc: None
) -> None:
    rec, ok = mk(w, 10), mk(w, 10)
    p = Path(rec["path"])
    (p / "stuck.txt").write_text("s")
    flag = stuck(monkeypatch, "stuck.txt")
    r = w.prune("--older-than", "1d")
    assert r.code == 0, r.err  # a failed removal still exits 0
    data = r.json()
    (skip,) = data["skipped"]
    assert skip["id"] == rec["id"] and skip["reason"] == "failed" and skip["processes"] == []
    assert f"{p}/stuck.txt" in skip["paths"] and len(skip["paths"]) <= 100
    assert data["removed"] == [ok["id"]] and w.state_of(rec["id"]) == "partial"
    flag["on"] = False
    again = w.pruned("--if-configured")  # no age: only the partial one is retried
    assert again["removed"] == [rec["id"]] and not p.exists()


def test_a_failure_in_one_record_does_not_stop_the_others(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import throwaway_remove

    a, b = mk(w, 10), mk(w, 10)
    real = throwaway_remove.ThrowawayRemover.remove

    def remove(self: Any, lease: Any) -> Any:
        if self.rid == a["id"]:
            raise RuntimeError("boom")
        return real(self, lease)

    monkeypatch.setattr(throwaway_remove.ThrowawayRemover, "remove", remove)
    data = w.pruned("--older-than", "1d")
    assert data["removed"] == [b["id"]]
    assert data["skipped"][0]["id"] == a["id"] and data["skipped"][0]["reason"] == "failed"


# -- dry run -----------------------------------------------------------------------------------


def test_dry_run_changes_nothing_and_runs_no_hooks(w: TW, tmp_path: Path) -> None:
    hit = tmp_path / "hook-ran"
    w.top["hooks"] = {
        "pre-remove": [{"cmd": f"touch {hit}"}],
        "post-remove": [{"cmd": f"touch {hit}"}],
    }
    old, busy = mk(w, 10), mk(w, 10)
    w.scanner.procs = [ProcessInfo(9, "x", busy["path"], ())]
    data = w.pruned("--older-than", "1d", "--dry-run")
    assert data["dry_run"] is True and data["removed"] == [old["id"]]
    assert [s["reason"] for s in data["skipped"]] == ["in_use"]
    assert Path(old["path"]).is_dir() and w.state_of(old["id"]) == "active"
    assert not hit.exists() and w.leases() == []
    assert not [e for e in w.log_events() if e["event"] in ("prune", "throwaway_remove")]


# -- global lease and cron ---------------------------------------------------------------------


def test_a_held_global_lease_is_already_running(w: TW) -> None:
    rec = mk(w, 10)
    st = State(w.db, Gateway())
    lease = st.try_lease(GLOBAL_LEASE)
    assert lease is not None
    try:
        data = w.pruned("--older-than", "1d")
    finally:
        lease.release()
        st.close()
    assert data == {"removed": [], "skipped": [], "already_running": True, "dry_run": False}
    assert Path(rec["path"]).is_dir()


def test_cron_style_run_needs_no_tty_and_prints_one_json_document(w: TW) -> None:
    mk(w, 100)
    r = w.run("prune", "--throwaways", "--if-configured", "--actor", "auto:cron", "--quiet")
    assert r.code == 0, r.err
    assert json.loads(r.out)["removed"] == []
    w.top["throwaway"] = {"max_age": "1d"}
    r = w.run("prune", "--throwaways", "--if-configured", "--actor", "auto:cron")
    assert r.code == 0 and len(json.loads(r.out)["removed"]) == 1


def test_human_output_lists_what_happened(w: TW) -> None:
    import contextlib
    import io

    from tests.helpers import Run
    from wtctl.cli import main
    from wtctl.context import Runtime

    rec = mk(w, 10)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["--config", w.config(), "--actor", "auto:t", "prune", "--throwaways",
                     "--older-than", "1d"], runtime=Runtime(stdin_isatty=False, me_name="a",
                     gateway=Gateway(scanner=w.scanner)))  # fmt: skip
    assert code == 0 and f"removed {rec['id']}" in out.getvalue()
    _ = Run


def test_the_run_is_logged_with_a_warning_when_something_was_skipped(w: TW) -> None:
    mk(w, 10)
    w.pruned("--older-than", "1d")
    events = [e for e in w.log_events() if e["event"] == "prune"]
    assert events[-1]["level"] == "info" and events[-1]["details"]["removed"] == 1
    w.top["prune"] = {"protect_labels": ["keep"]}
    mk(w, 10, "keep=1")
    w.pruned("--older-than", "1d")
    assert [e for e in w.log_events() if e["event"] == "prune"][-1]["level"] == "warn"
    assert w.audit_records() == []  # removals by prune are logged, not audited


def test_concurrent_prunes_remove_each_throwaway_once(w: TW) -> None:
    recs = [mk(w, 10) for _ in range(4)]
    w.config()
    cmd = [sys.executable, "-m", "wtctl", "--config", str(w.tmp / "config.yaml"), "--json",
           "--actor", "auto:cron", "prune", "--throwaways", "--older-than", "1d"]  # fmt: skip
    procs = [
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(3)
    ]
    results = [(p.communicate(timeout=120), p.returncode) for p in procs]
    assert all(code == 0 for _, code in results), results
    removed = [i for (o, _), _ in results for i in json.loads(o)["removed"]]
    assert sorted(removed) == sorted(r["id"] for r in recs)  # none twice, none missed
    assert leaks(w.troot) == []


def test_prune_does_not_remove_a_throwaway_that_is_being_created(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.createlib import Spy

    # a cutoff in the far future: every record is "older" by age, so only the in-creation
    # protection can keep the new one (no dependence on how long creation takes)
    monkeypatch.setattr(State, "timestamp_before", lambda self, seconds: "2999-01-01T00:00:00.000Z")

    seen: dict[str, Any] = {}
    old = mk(w, 10)

    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "fetch" and "r" not in seen:
            seen["r"] = w.prune(
                "--throwaways", "--older-than", "1d", gateway=Gateway(scanner=w.scanner)
            )
        return None

    w.git_runner = Spy(before=before)
    rec = w.made("--source", "remote")
    pr = seen["r"]
    assert pr.code == 0, pr.err
    out = json.loads(pr.out)
    assert rec["id"] not in out["removed"] and out["removed"] == [old["id"]]
    assert Path(rec["path"]).is_dir() and w.state_of(rec["id"]) == "active"
    _ = os


# -- the record is re-read under the lease -------------------------------------------------------


def changed_before_lease(mp: pytest.MonkeyPatch, w: TW, rid: str, sql: str) -> None:
    """Run ``sql`` against the database right before prune takes the lease on ``rid``."""
    import sqlite3

    real = State.try_lease
    done: list[bool] = []

    def patched(self: State, name: str, *a: Any, **kw: Any) -> Any:
        if name == record_lease(rid) and not done:
            done.append(True)
            conn = sqlite3.connect(w.db)
            conn.execute(sql, (rid,))
            conn.commit()
            conn.close()
        return real(self, name, *a, **kw)

    mp.setattr(State, "try_lease", patched)


def test_a_record_removed_between_selection_and_lease_is_skipped_silently(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, other = mk(w, 10), mk(w, 10)
    changed_before_lease(
        monkeypatch, w, gone["id"], "UPDATE records SET state = 'removed' WHERE id = ?"
    )
    r = w.prune("--older-than", "1d")
    data = json.loads(r.out)
    assert data["removed"] == [other["id"]] and data["skipped"] == []
    assert "warning" not in r.err.lower() and gone["id"] not in r.err


def test_a_record_that_is_no_longer_old_enough_is_left_alone(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = mk(w, 10)
    changed_before_lease(
        monkeypatch, w, rec["id"],
        "UPDATE records SET created = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
    )  # fmt: skip
    data = w.pruned("--older-than", "1d")
    assert data["removed"] == [] and data["skipped"] == []
    assert w.state_of(rec["id"]) == "active" and Path(rec["path"]).is_dir()


def test_a_record_that_became_protected_is_skipped_under_the_lease(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.top["prune"] = {"protect_labels": ["keep"]}
    rec = mk(w, 10)
    changed_before_lease(
        monkeypatch, w, rec["id"],
        "INSERT INTO labels(record_id, \"key\", value) VALUES (?, 'keep', 'yes')",
    )  # fmt: skip
    data = w.pruned("--older-than", "1d")
    assert data["removed"] == []
    assert [s["reason"] for s in data["skipped"]] == ["protected"]
    assert w.state_of(rec["id"]) == "active"
