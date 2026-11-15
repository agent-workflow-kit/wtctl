"""``remove``: hooks, audit, the step-9 re-check, branches, adopted and partial records."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import script
from tests.removelib import RR
from tests.schemacheck import validate
from wtctl.gateway import ProcessInfo

GIT_ID = "-c user.name=t -c user.email=t@example.test"


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> RR:  # noqa: ANN001
    return RR(tmp_path, private_home)


def hook(w: RR, name: str, body: str) -> str:
    return script(w.tmp / f"{name}.sh", body)


# -- hooks -----------------------------------------------------------------------------------


def test_pre_remove_payload_is_schema_valid_and_runs_before_deletion(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "h")
    out = w.tmp / "payload.json"
    seen = w.tmp / "seen"
    w.top["hooks"] = {"pre-remove": [{"cmd": hook(w, "p", f"cat > {out}\nls {w.wt} > {seen}")}]}
    (w.wt / "dirty.txt").write_text("x")
    w.removed("--force", "--holder", "h", "--merged-head", "a" * 40, human=True)
    payload = json.loads(out.read_text())
    validate("wtctl/hooks/pre-remove.json", payload)
    assert payload["object_id"] == w.rid and payload["actor"] == "human:tester"
    assert payload["overridden"] == ["untracked"] and payload["merged_head"] == "a" * 40
    assert payload["record"]["state"] == "active"
    assert "dirty.txt" in seen.read_text()  # the worktree was still there


def test_pre_remove_veto_leaves_everything(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.top["hooks"] = {"pre-remove": [{"cmd": "echo no >&2; exit 3"}]}
    err = w.refused(code=11)
    assert err["code"] == "vetoed" and err["hook_stderr"].strip() == "no"
    assert w.intact() and "work" in w.branches() and w.audit_records() == []


def test_pre_remove_escalation_and_override(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.top["hooks"] = {"pre-remove": [{"cmd": "exit 13"}]}
    assert w.refused(code=13)["code"] == "escalated"
    assert w.intact()
    assert w.refused("--override-escalation", code=2)["code"] == "usage"
    d = w.removed("--override-escalation", human=True)
    assert d["record"]["state"] == "removed"


def test_hook_flag_is_added_after_the_configured_ones(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    err = w.refused("--hook", "pre-remove=exit 4", code=11)
    assert err["code"] == "vetoed" and w.intact()


def test_decision_ids_reach_the_audit_record(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.top["hooks"] = {"pre-remove": [{"cmd": 'echo \'{"decision_id": "from-hook"}\''}]}
    w.removed("--decision-id", "mine")
    rec = [a for a in w.audit_records() if a["action"] == "remove"][0]
    assert rec["decision_id"] == ["mine", "from-hook"]


def test_post_remove_hook_gets_the_outcome_and_failure_only_warns(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    out = w.tmp / "post.json"
    w.top["hooks"] = {"post-remove": [{"cmd": hook(w, "q", f"cat > {out}")}, {"cmd": "exit 5"}]}
    d = w.removed()
    payload = json.loads(out.read_text())
    validate("wtctl/hooks/post-remove.json", payload)
    assert payload["record"]["state"] == "removed" and payload["branch_deleted"] is True
    assert any("post-remove" in x for x in d["warnings"]) and not w.wt.exists()


# -- audit -------------------------------------------------------------------------------------


def test_audit_records_for_remove_and_branch_delete(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    head = w.g(["rev-parse", "HEAD"], w.wt)
    w.removed("--decision-id", "d1")
    recs = w.audit_records()
    assert [r["action"] for r in recs] == ["remove", "branch_delete"]
    for r in recs:
        validate("wtctl/audit.json", r)
        assert r["target"]["id"] == w.rid and r["result"] == "ok" and r["decision_id"] == "d1"
    assert recs[0]["inputs"]["head"] == head and recs[0]["inputs"]["overridden"] == []
    assert recs[1]["inputs"] == {"branch": "work", "sha": head}


def test_an_audit_record_that_cannot_be_made_refuses_before_any_change(
    w: RR,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl.audit import Journal
    from wtctl.errors import WtctlError

    w.start(capsys)

    def boom(self: Journal, *a: Any, **k: Any) -> Any:
        raise WtctlError("internal", "audit record too large")

    monkeypatch.setattr(Journal, "prepare_audit", boom)
    assert w.refused(code=1)["code"] == "internal"
    assert w.intact() and "work" in w.branches()


def test_an_unusable_audit_file_refuses_before_any_change(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    state_dir = w.home / ".local" / "state" / "wtctl"
    other = w.tmp / "elsewhere.jsonl"
    other.write_text("")
    (state_dir / "audit.jsonl").symlink_to(other)
    assert w.refused(code=1)["code"] == "internal"
    assert w.intact() and "work" in w.branches() and other.read_text() == ""


def test_the_log_records_the_removal(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    w.removed()
    p = w.home / ".local" / "state" / "wtctl" / "log.jsonl"
    events = [json.loads(x) for x in p.read_text().splitlines()]
    mine = [e for e in events if e["event"] == "remove"]
    assert mine and mine[-1]["id"] == w.rid and mine[-1]["level"] == "info"
    for e in events:
        validate("wtctl/log.json", e)


# -- the step-9 re-check -----------------------------------------------------------------------


def mutate(w: RR, body: str) -> None:
    w.top["hooks"] = {"pre-remove": [{"cmd": hook(w, "m", body)}]}


def test_a_new_file_after_the_confirmation_refuses_even_forced(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    (w.wt / "old.txt").write_text("x")  # reported and overridden
    mutate(w, f"echo n > {w.wt}/new.txt")
    err = w.refused("--force", human=True)
    assert err["reason"] == "dirty"
    assert err["checks"]["untracked"]["details"] == [{"path": "new.txt"}]
    assert w.intact() and (w.wt / "old.txt").exists() and (w.wt / "new.txt").exists()
    assert w.audit_records() == []


def test_an_edit_to_a_tracked_file_after_the_confirmation_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    mutate(w, f"echo edit >> {w.wt}/c1.txt")
    err = w.refused("--force", human=True)
    assert err["reason"] == "dirty" and err["checks"]["tracked"]["status"] == "fail"
    assert w.intact()


def test_head_moved_refuses_unpushed_even_forced(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    mutate(w, f"git -C {w.wt} {GIT_ID} commit -q --allow-empty -m late")
    err = w.refused("--force", human=True)
    new = w.g(["rev-parse", "HEAD"], w.wt)
    assert err["reason"] == "unpushed"
    assert err["checks"]["unpushed_head"]["details"] == [{"sha": new, "detail": "head_moved"}]
    assert w.intact() and "work" in w.branches()


def test_a_process_that_appeared_refuses_in_use_even_forced(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    calls = {"n": 0}
    orig = w.scanner.scan

    def scan(target: str | None = None) -> list[ProcessInfo]:
        calls["n"] += 1
        if calls["n"] >= 2:
            w.scanner.procs = [ProcessInfo(77, "vim", str(w.wt), ())]
        return orig(target)

    w.scanner.scan = scan  # type: ignore[method-assign]
    err = w.refused("--force", human=True)
    assert err["reason"] == "in_use" and err["processes"][0]["pid"] == 77
    assert w.intact()


def test_a_new_holder_refuses_even_forced(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys, "--holder", "first")
    py = w.tmp / "addholder.py"
    py.write_text(
        "import sqlite3, sys\n"
        "c = sqlite3.connect(sys.argv[1])\n"
        "c.execute(\"INSERT INTO holders VALUES (?, 'late')\", (sys.argv[2],))\n"
        "c.commit()\n"
    )
    mutate(w, f"{sys.executable} {py} {w.db} {w.rid}")
    err = w.refused("--force", human=True)
    assert err["reason"] == "held" and err["details"]["holders"] == ["first", "late"]
    assert w.intact()


def test_baseline_change_after_the_confirmation_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    mutate(w, f"git -C {w.clone} config alias.zz status")
    assert w.refused("--force", human=True)["reason"] == "repo_config_changed"
    assert w.intact()


def test_a_lock_after_the_confirmation_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    mutate(w, f"git -C {w.clone} worktree lock {w.wt}")
    assert w.refused("--force", human=True)["reason"] == "locked"
    assert w.intact()


def test_forged_pointer_after_the_confirmation_refuses(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    mutate(w, f"echo /x/.git > {w.admin}/gitdir")
    assert w.refused("--force", human=True)["reason"] == "unlinked"
    assert w.wt.is_dir() and w.admin.is_dir()


def test_directory_swapped_for_a_symlink_after_the_confirmation_deletes_nothing(
    w: RR,
    capsys,
    tmp_path: Path,  # noqa: ANN001
) -> None:
    w.start(capsys)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("p")
    moved = Path(str(w.wt) + ".moved")
    mutate(w, f"mv {w.wt} {moved} && ln -s {outside} {w.wt}")
    err = w.refused("--force", human=True)
    assert err["reason"] == "unlinked"
    assert (outside / "precious").read_text() == "p" and moved.is_dir() and w.admin.is_dir()
    assert w.state_of() == "active"


def test_directory_replaced_by_another_directory_deletes_nothing(w: RR, capsys) -> None:  # noqa: ANN001
    w.start(capsys)
    moved = Path(str(w.wt) + ".moved")
    mutate(w, f"mv {w.wt} {moved} && mkdir {w.wt} && cp {moved}/.git {w.wt}/.git")
    err = w.refused("--force", human=True)
    assert err["reason"] == "unlinked"
    assert (moved / "c1.txt").exists() and w.admin.is_dir()
    assert w.state_of() == "active"
