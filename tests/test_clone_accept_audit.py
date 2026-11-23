"""A human ``clone`` re-accepting a changed clone identity writes one ``clone_accept`` audit
record (docs/interface.md, Audit and log; schemas/audit.json); none for a dry run, an agent actor,
a refusal or a declined prompt. An unusable audit file refuses before the row is rewritten."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.schemacheck import validate
from tests.test_locate import REPO, World, replace_clone, row
from wtctl import audit
from wtctl import locate as locate_mod
from wtctl.errors import WtctlError
from wtctl.state import State


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> World:
    return World(tmp_path, private_home)


def audit_path(w: World) -> Path:
    return w.home / ".local" / "state" / "wtctl" / "audit.jsonl"


def audits(w: World) -> list[dict[str, Any]]:
    p = audit_path(w)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def changed(w: World, capsys, name: str = "mine") -> Path:  # noqa: ANN001
    clone = w.stub(w.tmp / name)
    w.repos[REPO] = {"clone": str(clone)}
    w.cli(capsys, "locate", REPO)
    replace_clone(w, clone)
    return clone


def test_a_human_reaccept_writes_one_clone_accept_record(w: World, capsys) -> None:  # noqa: ANN001
    clone = changed(w, capsys)
    r = w.cli(capsys, "--yes", "clone", REPO, human=True)
    assert r.code == 0, r.out
    data = r.json()
    (a,) = audits(w)
    validate("wtctl/audit.json", a)
    assert a["action"] == "clone_accept" and a["actor"] == "human:tester"
    assert a["result"] == "ok"
    assert a["target"] == {"id": None, "path": str(clone)}
    assert a["inputs"] == {"repo": REPO, "clone_path": str(clone), "anchors": data["anchors"]}
    assert len(a["inputs"]["anchors"]) == 1 and a["inputs"]["anchors"][0]["changed"]


def test_nothing_is_audited_for_a_dry_run_agent_decline_fuse_or_a_plain_clone(
    w: World,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed(w, capsys)
    before = row()
    assert w.cli(capsys, "--dry-run", "clone", REPO, human=True).code == 0
    assert w.cli(capsys, "--actor", "auto:bot", "--yes", "clone", REPO).code == 13
    assert w.cli(capsys, "clone", REPO, human=True).code == 13  # no tty, no --yes
    with monkeypatch.context() as m:
        m.setattr(
            locate_mod,
            "describe_mount",
            lambda path, st: {"fs_type": "fuse.sshfs", "mount_source": "x", "mount_point": "/"},
        )
        assert w.cli(capsys, "--yes", "clone", REPO, human=True).code == 20
    assert audits(w) == [] and row() == before
    assert w.cli(capsys, "--yes", "clone", REPO, human=True).code == 0
    assert len(audits(w)) == 1
    again = w.cli(capsys, "--yes", "clone", REPO, human=True)  # resolves unchanged now
    assert again.code == 0 and again.json()["anchors"] == []
    assert len(audits(w)) == 1


def test_an_unusable_audit_file_refuses_before_the_row_is_rewritten(
    w: World,
    capsys,  # noqa: ANN001
) -> None:
    changed(w, capsys)
    before = row()
    target = w.tmp / "elsewhere"
    target.write_text("")
    audit_path(w).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, audit_path(w))
    r = w.cli(capsys, "--yes", "clone", REPO, human=True)
    assert r.code == 1, (r.out, r.err)
    assert row() == before and target.read_text() == ""


def test_an_oversized_record_refuses_before_the_row_is_rewritten(
    w: World,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed(w, capsys)
    before = row()
    monkeypatch.setattr(audit, "MAX_LINE_BYTES", 200)
    r = w.cli(capsys, "--yes", "clone", REPO, human=True)
    assert r.code == 1, (r.out, r.err)
    assert row() == before and audits(w) == []


def test_hostile_mount_text_cannot_inject_audit_lines(
    w: World,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed(w, capsys)
    monkeypatch.setattr(
        locate_mod,
        "describe_mount",
        lambda path, st: {
            "fs_type": "ext4",
            "mount_source": 'src\n{"forged":1}\u2028\x00',
            "mount_point": "/mnt\u2028x\r",
        },
    )
    assert w.cli(capsys, "--yes", "clone", REPO, human=True).code == 0
    raw = audit_path(w).read_bytes()
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n") and raw.isascii()
    (a,) = audits(w)
    validate("wtctl/audit.json", a)
    assert set(a) == {"ts", "actor", "decision_id", "target", "action", "inputs", "result"}


def test_mount_text_with_newlines_keeps_the_audit_record_schema_valid(
    w: World,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed(w, capsys)
    monkeypatch.setattr(
        locate_mod,
        "describe_mount",
        lambda path, st: {
            "fs_type": "ext4",
            "mount_source": "s\nx",
            "mount_point": "/mnt/a\nb",
        },
    )
    r = w.cli(capsys, "--yes", "clone", REPO, human=True)
    assert r.code == 0, (r.out, r.err)
    assert audit_path(w).read_bytes().count(b"\n") == 1
    (a,) = audits(w)
    validate("wtctl/audit.json", a)


def test_a_failed_row_write_leaves_no_audit_record(
    w: World,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed(w, capsys)

    def boom(self, repo, **fields):  # noqa: ANN001, ANN202, ARG001
        raise WtctlError("internal", "forced put_clone failure")

    monkeypatch.setattr(State, "put_clone", boom)
    r = w.cli(capsys, "--yes", "clone", REPO, human=True)
    assert r.code == 1, (r.out, r.err)
    assert audits(w) == []
