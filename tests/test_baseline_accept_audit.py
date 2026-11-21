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
from wtctl import baselinesvc, repobaseline


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
