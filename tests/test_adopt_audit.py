"""``adopt`` writes one ``adopt`` audit record per recorded state change (docs/interface.md, Audit
and log; schemas/audit.json): a new adoption, and an already recorded path that gains a holder.
Never for a dry run, a no-op or a refusal; an unusable audit file refuses before the state changes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.adoptlib import adopt, err_of, foreign, inproc_baseline, ok_adopt, world
from tests.createlib import CW
from tests.schemacheck import validate
from wtctl import audit


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    inproc_baseline(monkeypatch)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return world(tmp_path, private_home)


def audit_path(w: CW) -> Path:
    return w.home / ".local" / "state" / "wtctl" / "audit.jsonl"


def audits(w: CW) -> list[dict[str, Any]]:
    p = audit_path(w)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_a_new_adoption_writes_one_adopt_record(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    data = ok_adopt(adopt(w, capsys, path, "--holder", "agent#1", human=True))
    (a,) = audits(w)
    validate("wtctl/audit.json", a)
    rec = data["record"]
    assert a["action"] == "adopt" and a["result"] == "ok" and a["actor"] == "human:tester"
    assert a["target"] == {"id": rec["id"], "path": rec["path"]}
    assert a["inputs"] == {
        "path": rec["path"],
        "baseline_digest": rec["baseline_digest"],
        "holder": "agent#1",
    }


def test_an_agent_adoption_is_audited_with_its_actor_and_a_null_holder(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    ok_adopt(adopt(w, capsys, foreign(w)))
    (a,) = audits(w)
    validate("wtctl/audit.json", a)
    assert a["actor"] == "auto:tester" and a["inputs"]["holder"] is None


def test_an_existing_path_that_gains_a_holder_is_audited(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    first = ok_adopt(adopt(w, capsys, path, "--holder", "a#1"))["record"]
    second = ok_adopt(adopt(w, capsys, path, "--holder", "a#2"))
    assert second["existing"] is True
    recs = audits(w)
    assert len(recs) == 2
    validate("wtctl/audit.json", recs[1])
    assert recs[1]["target"] == {"id": first["id"], "path": first["path"]}
    assert recs[1]["inputs"] == {
        "path": first["path"],
        "baseline_digest": first["baseline_digest"],
        "holder": "a#2",
    }


def test_an_owned_record_gaining_a_holder_is_audited(w: CW, capsys) -> None:  # noqa: ANN001
    created = json.loads(w.create(capsys).out)["record"]
    ok_adopt(adopt(w, capsys, created["path"], "--holder", "me"))
    (a,) = audits(w)
    validate("wtctl/audit.json", a)
    assert a["target"]["id"] == created["id"] and a["inputs"]["holder"] == "me"


def test_nothing_is_audited_for_a_no_op_dry_run_or_refusal(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    ok_adopt(adopt(w, capsys, path, "--dry-run", "--holder", "x"))  # new, dry
    assert audits(w) == []
    ok_adopt(adopt(w, capsys, path, "--holder", "x"))
    assert len(audits(w)) == 1
    ok_adopt(adopt(w, capsys, path, "--holder", "x"))  # holder already there: no-op
    ok_adopt(adopt(w, capsys, path))  # no holder: no-op
    ok_adopt(adopt(w, capsys, path, "--dry-run", "--holder", "y"))  # existing, dry
    assert len(audits(w)) == 1
    err_of(adopt(w, capsys, w.tmp / "nope"), 10)  # refusal
    sub = path / "sub"
    sub.mkdir()
    err_of(adopt(w, capsys, sub), 2)
    assert len(audits(w)) == 1


def test_an_unusable_audit_file_refuses_a_new_adoption_before_the_state_changes(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    path = foreign(w)
    target = w.tmp / "elsewhere"
    target.write_text("")
    audit_path(w).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, audit_path(w))
    r = adopt(w, capsys, path)
    assert r.code == 1, (r.out, r.err)
    assert w.count("records") == 0
    assert target.read_text() == ""


def test_an_unusable_audit_file_refuses_a_holder_addition_before_the_state_changes(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    path = foreign(w)
    ok_adopt(adopt(w, capsys, path, "--holder", "a#1"))
    audit_path(w).unlink()
    target = w.tmp / "elsewhere"
    target.write_text("")
    os.symlink(target, audit_path(w))
    r = adopt(w, capsys, path, "--holder", "a#2")
    assert r.code == 1, (r.out, r.err)
    assert w.rows("SELECT holder FROM holders ORDER BY holder") != []
    assert [x["holder"] for x in w.rows("SELECT holder FROM holders")] == ["a#1"]
    # a no-op needs no audit write, so the unusable file does not matter
    assert adopt(w, capsys, path, "--holder", "a#1").code == 0


def test_an_oversized_record_refuses_before_the_state_changes(
    w: CW,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = foreign(w)
    monkeypatch.setattr(audit, "MAX_LINE_BYTES", 200)
    r = adopt(w, capsys, path)
    assert r.code == 1, (r.out, r.err)
    assert w.count("records") == 0 and audits(w) == []


def test_hostile_strings_cannot_inject_audit_lines(w: CW, capsys) -> None:  # noqa: ANN001
    name = "a\u2028b\x01c\x7fd"
    path = foreign(w, name=name, branch="plain")
    ok_adopt(adopt(w, capsys, path, "--holder", "h#x"))
    raw = audit_path(w).read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert raw.isascii() and b"\x01" not in raw
    (a,) = audits(w)
    validate("wtctl/audit.json", a)
    assert a["inputs"]["path"].endswith(name) and a["inputs"]["holder"] == "h#x"
