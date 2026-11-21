"""``baseline accept`` writes one ``baseline_accept`` audit record (docs/interface.md, Audit and
log; schemas/audit.json), after the state change and never for a dry run, a no-op or a refusal."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import CW
from tests.reclib import RC
from tests.schemacheck import validate
from tests.test_baseline_cmd import accept, check, err_of, good_accept, made, row, set_col
from wtctl import audit, baselinesvc, repobaseline
from wtctl.errors import WtctlError
from wtctl.state import State


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def audit_path(home: Path) -> Path:
    return home / ".local" / "state" / "wtctl" / "audit.jsonl"


def audits(home: Path) -> list[dict[str, Any]]:
    p = audit_path(home)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_accept_writes_one_baseline_accept_record(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    Path(rec["path"], "CLAUDE.md").write_text("hi")
    data = good_accept(accept(w, capsys, rec["id"], "--yes", "--actor", "human:tester"))
    recs = audits(w.home)
    assert [a["action"] for a in recs] == ["baseline_accept"]
    a = recs[0]
    validate("wtctl/audit.json", a)
    assert a["actor"] == "human:tester" and a["result"] == "ok"
    assert a["target"] == {"id": rec["id"], "path": rec["path"]}
    assert a["inputs"]["old_digest"] == rec["baseline_digest"]
    assert a["inputs"]["new_digest"] == data["new_digest"] == row(w, rec["id"])["baseline_digest"]
    assert a["inputs"]["items"] == data["items"] and a["inputs"]["items"]
    assert a["inputs"]["anchors"] == data["anchors"]
    assert set(a["inputs"]) == {"old_digest", "new_digest", "items", "anchors"}


def test_accept_with_only_a_moved_anchor_is_audited(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    set_col(w, rec["id"], dir_id="1:2")
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))
    (a,) = audits(w.home)
    validate("wtctl/audit.json", a)
    assert a["inputs"]["items"] == [] and a["inputs"]["anchors"] == data["anchors"]
    assert any(x["anchor"] == "worktree_dir" and x["changed"] for x in a["inputs"]["anchors"])


def test_nothing_is_audited_for_a_no_op_dry_run_or_refusal(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    good_accept(accept(w, capsys, rec["id"], "--yes"))  # nothing changed
    assert audits(w.home) == []
    w.g(["config", "core.hooksPath", "/x"])
    good_accept(w.cli(capsys, "--dry-run", "baseline", "accept", rec["id"], human=True))
    err_of(accept(w, capsys, rec["id"], "--yes", human=False), 13)
    err_of(accept(w, capsys, rec["id"], "--yes", "--expect-digest", "f" * 64), 20)
    assert audits(w.home) == []
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    assert len(audits(w.home)) == 1
    good_accept(accept(w, capsys, rec["id"], "--yes"))  # now unchanged again
    assert len(audits(w.home)) == 1
    assert check(w, capsys, rec["id"]).code == 0


def test_an_unusable_audit_file_refuses_before_the_state_changes(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    target = w.tmp / "elsewhere"
    target.write_text("")
    audit_path(w.home).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, audit_path(w.home))
    r = accept(w, capsys, rec["id"], "--yes")
    assert r.code == 1, (r.out, r.err)
    assert row(w, rec["id"])["baseline_digest"] == rec["baseline_digest"]
    assert target.read_text() == ""


def test_reattaching_a_moved_clone_is_audited(tmp_path: Path, private_home: Path) -> None:
    w = RC(tmp_path, private_home).build()
    rec = w.wt()
    new = w.tmp / "clone-moved"
    w.clone.rename(new)
    w.repo_cfg["clone"] = str(new)
    w.reconciled()
    assert w.run("baseline", "accept", rec["id"], "--yes", "--dry-run", human=True).code == 0
    assert audits(w.home) == []
    r = w.run("baseline", "accept", rec["id"], "--yes", "--actor", "human:tester", human=True)
    assert r.code == 0, (r.out, r.err)
    (a,) = [x for x in audits(w.home) if x["action"] == "baseline_accept"]
    validate("wtctl/audit.json", a)
    assert a["actor"] == "human:tester" and a["target"]["id"] == rec["id"]
    assert "common_dir" in [x["anchor"] for x in a["inputs"]["anchors"]]
    assert a["inputs"]["new_digest"] == w.row(rec["id"])["baseline_digest"]


def test_a_large_change_records_the_longest_prefix_that_fits(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    for i in range(400):
        Path(rec["path"], f"CLAUDE-{i:04d}-{'x' * 40}.md").write_text("hi")
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))
    assert len(data["items"]) > 300
    assert row(w, rec["id"])["baseline_digest"] == data["new_digest"]
    (a,) = audits(w.home)
    validate("wtctl/audit.json", a)
    items = a["inputs"]["items"]
    assert 0 < len(items) < len(data["items"])
    assert items == data["items"][: len(items)]
    assert a["inputs"]["new_digest"] == data["new_digest"]
    assert a["inputs"]["old_digest"] == rec["baseline_digest"]
    assert a["inputs"]["anchors"] == data["anchors"]


def test_an_invalid_record_is_not_degraded_to_a_prefix(
    w: CW,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    monkeypatch.setattr(audit.Journal, "prepare_audit", _refusing)
    r = accept(w, capsys, rec["id"], "--yes")
    assert r.code == 1, (r.out, r.err)
    assert row(w, rec["id"])["baseline_digest"] == rec["baseline_digest"]
    assert audits(w.home) == []


def _refusing(self, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
    raise WtctlError("internal", "invalid field")


def test_no_audit_line_when_the_state_transition_fails(
    w: CW,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise WtctlError("internal", "transition failed")

    monkeypatch.setattr(State, "transition", boom)
    r = accept(w, capsys, rec["id"], "--yes")
    assert r.code == 1, (r.out, r.err)
    assert audits(w.home) == []


def moved_world(tmp_path: Path, private_home: Path) -> tuple[RC, dict[str, Any]]:
    w = RC(tmp_path, private_home).build()
    rec = w.wt()
    new = w.tmp / "clone-moved"
    w.clone.rename(new)
    w.repo_cfg["clone"] = str(new)
    w.reconciled()
    return w, rec


def pointers(w: RC, rec: dict[str, Any]) -> tuple[str, str, str]:
    dotgit = Path(rec["path"], ".git").read_text()
    gitdirs = sorted(
        (p / "gitdir").read_text() for p in w.tmp.glob("**/worktrees/*") if (p / "gitdir").is_file()
    )
    r = w.row(rec["id"])
    return dotgit, "|".join(gitdirs), f"{r['state']} {r['clone_path']}"


def test_reattach_records_the_old_digest(tmp_path: Path, private_home: Path) -> None:
    w, rec = moved_world(tmp_path, private_home)
    before = w.row(rec["id"])["baseline_digest"]
    assert w.run("baseline", "accept", rec["id"], "--yes", human=True).code == 0
    (a,) = [x for x in audits(w.home) if x["action"] == "baseline_accept"]
    assert before and a["inputs"]["old_digest"] == before


def test_reattach_with_an_unusable_audit_file_changes_nothing(
    tmp_path: Path, private_home: Path
) -> None:
    w, rec = moved_world(tmp_path, private_home)
    before = pointers(w, rec)
    target = w.tmp / "elsewhere"
    target.write_text("")
    audit_path(w.home).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, audit_path(w.home))
    r = w.run("baseline", "accept", rec["id"], "--yes", human=True)
    assert r.code == 1, (r.out, r.err)
    assert pointers(w, rec) == before and before[2].startswith("clone_missing")
    assert target.read_text() == ""


def test_reattach_refused_record_changes_nothing(
    tmp_path: Path, private_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, rec = moved_world(tmp_path, private_home)
    before = pointers(w, rec)
    monkeypatch.setattr(audit.Journal, "prepare_audit", _refusing)
    r = w.run("baseline", "accept", rec["id"], "--yes", human=True)
    assert r.code == 1, (r.out, r.err)
    assert pointers(w, rec) == before and before[2].startswith("clone_missing")
    assert audits(w.home) == []


def test_reattach_no_audit_line_when_the_transition_fails(
    tmp_path: Path, private_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, rec = moved_world(tmp_path, private_home)

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise WtctlError("internal", "transition failed")

    monkeypatch.setattr(State, "transition", boom)
    r = w.run("baseline", "accept", rec["id"], "--yes", human=True)
    assert r.code == 1, (r.out, r.err)
    assert audits(w.home) == []
