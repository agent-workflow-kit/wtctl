"""``baseline check`` and ``baseline accept`` (docs/interface.md), against real git."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.createlib import CW, REPO, script
from tests.schemacheck import validate
from tests.test_create import ok
from wtctl import repobaseline
from wtctl.commands import baseline_accept, baseline_check, clone  # noqa: F401
from wtctl.gateway import Gateway
from wtctl.state import State, record_lease


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the collectors in this process (the real child path has its own tests)."""
    from wtctl import baselinesvc

    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def made(w: CW, capsys, *argv: str) -> dict:  # noqa: ANN001
    return ok(w.create(capsys, *argv))["record"]


def check(w: CW, capsys, rid: str, human: bool = False):  # noqa: ANN001, ANN201
    return w.cli(capsys, "baseline", "check", rid, human=human)


def accept(w: CW, capsys, rid: str, *argv: str, human: bool = True):  # noqa: ANN001, ANN201
    return w.cli(capsys, "baseline", "accept", rid, *argv, human=human)


def err_of(r, code: int, reason: str | None = None) -> dict:  # noqa: ANN001
    assert r.code == code, (r.out, r.err)
    validate("wtctl/cli/error.json", r.json())
    e = r.json()["error"]
    if reason is not None:
        assert e["reason"] == reason
    return e


def good_check(r) -> dict:  # noqa: ANN001
    assert r.code == 0, (r.out, r.err)
    validate("wtctl/cli/baseline-check.json", r.json())
    return r.json()


def good_accept(r) -> dict:  # noqa: ANN001
    assert r.code == 0, (r.out, r.err)
    validate("wtctl/cli/baseline-accept.json", r.json())
    return r.json()


def set_col(w: CW, rid: str, **cols: object) -> None:
    conn = sqlite3.connect(w.db)
    for k, v in cols.items():
        conn.execute(f"UPDATE records SET {k} = ? WHERE id = ?", (v, rid))
    conn.commit()
    conn.close()


def row(w: CW, rid: str) -> sqlite3.Row:
    return w.rows("SELECT * FROM records WHERE id = ?", rid)[0]


# --- check ---------------------------------------------------------------------------------


def test_create_records_the_baseline_and_check_agrees(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    assert rec["baseline_digest"] and len(rec["baseline_digest"]) == 64
    assert w.count("baseline_items") > 0
    data = good_check(check(w, capsys, rec["id"]))
    assert data == {"id": rec["id"], "changed": False, "digest": rec["baseline_digest"],
                    "recorded_digest": rec["baseline_digest"]}  # fmt: skip


def test_check_needs_no_human_and_takes_no_lease(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    st = State(w.db, Gateway())
    lease = st.lease(record_lease(rec["id"]), wait=1)
    try:
        good_check(check(w, capsys, rec["id"]))
    finally:
        lease.release()
        st.close()


def test_git_class_change_is_reported_with_items_and_details(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/tmp/evil-hooks"])
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert e["details"]["changed_classes"] == ["git"]
    assert e["details"]["recorded_digest"] == rec["baseline_digest"]
    assert e["details"]["digest"] != rec["baseline_digest"]
    hit = [i for i in e["items"] if i["key"] == "core.hookspath"]
    assert hit and hit[0]["before"] == "absent" and hit[0]["class"] == "git"
    assert "/tmp/evil-hooks" not in json.dumps(e)  # contents are never reported


def test_claude_class_change_refuses_check_too(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    Path(rec["path"], "CLAUDE.md").write_text("do evil")
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert e["details"]["changed_classes"] == ["claude"]
    assert [i["path"] for i in e["items"]] == ["CLAUDE.md"]


def test_both_classes(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    Path(rec["path"], ".mcp.json").write_text("{}")
    w.g(["config", "core.fsmonitor", "true"])
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert e["details"]["changed_classes"] == ["claude", "git"]


def test_routine_commands_keep_check_green(w: CW, capsys) -> None:  # noqa: ANN001
    a = made(w, capsys, "--name", "one")
    b = made(w, capsys, "--name", "two")  # a sibling worktree create
    wt = Path(a["path"])
    w.g(["push", "-q", "-u", "origin", "wt/one"], wt)
    w.g(["branch", "--set-upstream-to=origin/main", "wt/one"], wt)
    (wt / "x.txt").write_text("x")
    w.g(["add", "."], wt)
    w.g(["commit", "-q", "-m", "x"], wt)
    w.g(["worktree", "remove", "--force", b["path"]], w.clone)
    good_check(check(w, capsys, a["id"]))


def test_irrelevant_edits_keep_check_green(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    cfg = w.clone / ".git" / "config"
    cfg.write_text("# a comment\n\n" + cfg.read_text().replace("\t", "   "))
    good_check(check(w, capsys, rec["id"]))


def test_check_errors(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    err_of(check(w, capsys, "wt-zzzzzzzz"), 10)
    st = State(w.db, Gateway())
    tid = st.allocate_id()
    st.insert_record({"id": tid, "object_type": "throwaway", "state": "active", "repo": REPO,
                      "path": str(w.tmp / "t"), "name": "t"})  # fmt: skip
    st.close()
    err_of(check(w, capsys, tid), 2)
    set_col(w, rec["id"], state="creating")
    err_of(check(w, capsys, rec["id"]), 20, "unlinked")
    for state in ("missing", "clone_missing", "unlinked", "partial"):
        set_col(w, rec["id"], state=state)
        err_of(check(w, capsys, rec["id"]), 20, "unlinked")
    set_col(w, rec["id"], state="gone", removed_at="2026-01-01T00:00:00.000Z")
    err_of(check(w, capsys, rec["id"]), 10)


def test_check_on_a_moved_worktree_is_unlinked(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    p = Path(rec["path"])
    p.rename(p.with_name("moved-away"))
    err_of(check(w, capsys, rec["id"]), 20, "unlinked")


def test_check_with_an_unrecorded_baseline_reports_everything(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    set_col(w, rec["id"], baseline_digest=None)
    conn = sqlite3.connect(w.db)
    conn.execute("DELETE FROM baseline_items")
    conn.commit()
    conn.close()
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert e["items"] and all(i["before"] == "absent" for i in e["items"])
    assert e["details"]["recorded_digest"] == "0" * 64


def test_incomplete_computation_is_a_defined_refusal(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.top["remove"] = {"nested_scan_limit": 5}
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert any(i.get("detail") == "incomplete" for i in e["items"])


def test_ignore_config_keys_comes_from_the_config(w: CW, capsys) -> None:  # noqa: ANN001
    w.top["baseline"] = {"ignore_config_keys": ["foo.bar"]}
    rec = made(w, capsys)
    w.g(["config", "foo.bar", "1"])
    good_check(check(w, capsys, rec["id"]))
    w.g(["config", "foo.baz", "1"])
    err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")


def test_the_real_child_process_path(w: CW, capsys, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    from wtctl import baselinesvc, fsafe_child

    monkeypatch.setattr(baselinesvc, "RUNNER", fsafe_child.run_op)
    rec = made(w, capsys)
    good_check(check(w, capsys, rec["id"]))
    w.g(["config", "core.hooksPath", "/x"])
    err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")


# --- accept --------------------------------------------------------------------------------


def test_agents_cannot_accept_and_the_gate_precedes_resolution(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    err_of(accept(w, capsys, rec["id"], "--yes", human=False), 13)
    err_of(accept(w, capsys, "wt-zzzzzzzz", "--yes", human=False), 13)
    err_of(accept(w, capsys, "wt-zzzzzzzz", "--yes"), 10)


def test_accept_rerecords_after_a_change(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    Path(rec["path"], "CLAUDE.md").write_text("hi")
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))
    assert data["dry_run"] is False and data["old_digest"] == rec["baseline_digest"]
    assert data["new_digest"] != data["old_digest"]
    assert {i["class"] for i in data["items"]} == {"git", "claude"}
    assert data["record"]["baseline_digest"] == data["new_digest"]
    assert row(w, rec["id"])["baseline_digest"] == data["new_digest"]
    good_check(check(w, capsys, rec["id"]))
    assert {a["anchor"] for a in data["anchors"]} >= {"common_dir", "worktree_dir"}
    assert not any(a["changed"] for a in data["anchors"])


def test_accept_with_nothing_changed(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))
    assert data["items"] == [] and data["old_digest"] == data["new_digest"]


def test_expect_digest_accepts_only_that_state(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    shown = err_of(check(w, capsys, rec["id"]), 20)["details"]["digest"]
    w.g(["config", "core.fsmonitor", "true"])  # the state moved on after the human looked
    err_of(
        accept(w, capsys, rec["id"], "--yes", "--expect-digest", shown), 20, "repo_config_changed"
    )
    assert row(w, rec["id"])["baseline_digest"] == rec["baseline_digest"]
    now = err_of(check(w, capsys, rec["id"]), 20)["details"]["digest"]
    good_accept(accept(w, capsys, rec["id"], "--yes", "--expect-digest", now))
    good_check(check(w, capsys, rec["id"]))


def test_expect_digest_must_be_a_digest(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    for bad in ("", "abc", "G" * 64, "A" * 64, "a" * 63, "a" * 65):
        err_of(accept(w, capsys, rec["id"], "--yes", "--expect-digest", bad), 2)


def test_expect_digest_with_nothing_changed_is_a_no_op(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    good_accept(accept(w, capsys, rec["id"], "--yes", "--expect-digest", rec["baseline_digest"]))
    # nothing would be recorded, so there is nothing for the expectation to protect
    assert (
        good_accept(accept(w, capsys, rec["id"], "--yes", "--expect-digest", "0" * 64))["items"]
        == []
    )


def test_accept_recomputes_before_writing(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])

    def racing_confirm(question: str, *, tty: bool) -> bool:
        w.g(["config", "core.fsmonitor", "/bin/true"])  # an agent acts while the human decides
        return True

    monkeypatch.setattr(clone, "confirm", racing_confirm)
    r = w.cli(capsys, "baseline", "accept", rec["id"], human=True)
    err_of(r, 20, "repo_config_changed")
    assert row(w, rec["id"])["baseline_digest"] == rec["baseline_digest"]
    assert w.count("baseline_items") > 0


def test_accept_asks_and_a_no_changes_nothing(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    asked: list[str] = []
    monkeypatch.setattr(clone, "confirm", lambda q, *, tty: asked.append(q) or False)
    err_of(w.cli(capsys, "baseline", "accept", rec["id"], human=True), 13)
    assert len(asked) == 1 and "core.hookspath" in asked[0].lower()
    assert row(w, rec["id"])["baseline_digest"] == rec["baseline_digest"]
    monkeypatch.setattr(clone, "confirm", lambda q, *, tty: True)
    good_accept(w.cli(capsys, "baseline", "accept", rec["id"], human=True))


def test_accept_dry_run_changes_nothing_and_asks_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    data = good_accept(w.cli(capsys, "--dry-run", "baseline", "accept", rec["id"], human=True))
    assert data["dry_run"] is True and data["items"] and data["new_digest"] != data["old_digest"]
    assert row(w, rec["id"])["baseline_digest"] == rec["baseline_digest"]
    err_of(check(w, capsys, rec["id"]), 20)


def test_accept_refuses_to_record_an_incomplete_baseline(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.top["remove"] = {"nested_scan_limit": 5}
    e = err_of(accept(w, capsys, rec["id"], "--yes"), 20, "repo_config_changed")
    assert any(i.get("detail") == "incomplete" for i in e["items"])
    assert row(w, rec["id"])["baseline_digest"] == rec["baseline_digest"]


def test_accept_takes_the_record_lease(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    st = State(w.db, Gateway())
    lease = st.lease(record_lease(rec["id"]), wait=1)
    try:
        err_of(accept(w, capsys, rec["id"], "--yes"), 12)
    finally:
        lease.release()
        st.close()


def test_accept_refuses_records_that_are_not_active(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    for state in ("creating", "missing", "clone_missing", "unlinked", "partial"):
        set_col(w, rec["id"], state=state)
        err_of(accept(w, capsys, rec["id"], "--yes"), 20, "unlinked")


def test_accept_rerecords_a_changed_directory_identity_after_showing_it(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    set_col(w, rec["id"], dir_id="1:2", common_id="3:4")
    err_of(check(w, capsys, rec["id"]), 20, "unlinked")
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))
    by = {a["anchor"]: a for a in data["anchors"]}
    assert by["worktree_dir"]["changed"] and by["worktree_dir"]["recorded"] == "1:2"
    assert by["common_dir"]["changed"] and by["common_dir"]["recorded"] == "3:4"
    for a in by.values():
        assert a["fs_type"] and a["mount_point"]
    now = row(w, rec["id"])
    assert now["dir_id"] == rec["dir_id"] and now["common_id"] == rec["common_id"]
    assert w.rows("SELECT common_id FROM clones")[0]["common_id"] == rec["common_id"]
    good_check(check(w, capsys, rec["id"]))


def test_accept_rerecords_the_root_anchor(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    conn = sqlite3.connect(w.db)
    conn.execute("UPDATE anchors SET dev_ino = '1:2' WHERE path = ?", (rec["base_dir"],))
    conn.commit()
    conn.close()
    err_of(check(w, capsys, rec["id"]), 20, "unlinked")
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))
    root = [a for a in data["anchors"] if a["anchor"] == "worktree_root"][0]
    assert root["changed"] and root["recorded"] == "1:2" and root["path"] == rec["base_dir"]
    good_check(check(w, capsys, rec["id"]))


def test_a_fuse_anchor_is_refused_without_override(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl import baselinesvc

    rec = made(w, capsys)
    set_col(w, rec["id"], dir_id="1:2")
    real = baselinesvc.describe

    def fake(path: str) -> dict[str, str]:
        return {**real(path), "fs_type": "fuse.sshfs"}

    monkeypatch.setattr(baselinesvc, "describe", fake)
    e = err_of(accept(w, capsys, rec["id"], "--yes"), 20, "unlinked")
    assert e["details"]["cause"] == "fuse_anchor" and e["details"]["anchors"]
    assert row(w, rec["id"])["dir_id"] == "1:2"


def test_a_pointer_mismatch_is_not_re_accepted(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    (Path(rec["admin_dir"]) / "gitdir").write_text("/x/.git\n")
    err_of(accept(w, capsys, rec["id"], "--yes"), 20, "unlinked")


def test_accept_runs_the_post_hook_with_the_items(w: CW, capsys) -> None:  # noqa: ANN001
    out = w.tmp / "payload.json"
    hook = script(w.tmp / "hook.sh", f"cat > {out}")
    w.top["hooks"] = {"post-baseline-accept": [{"cmd": hook}]}
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    payload = json.loads(out.read_text())
    validate("wtctl/hooks/post-baseline-accept.json", payload)
    assert payload["actor"] == "human:tester" and payload["items"]


def test_external_hooks_path_identity_is_shown_and_rerecorded(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    hp = w.tmp / "userhooks"
    hp.mkdir()
    w.g(["config", "core.hooksPath", str(hp)])
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    good_check(check(w, capsys, rec["id"]))
    rows = [r for r in w.rows("SELECT * FROM baseline_items") if r["anchor_id"]]
    assert len(rows) == 1 and rows[0]["path"] == str(hp)
    # the directory is replaced by another one (a new identity): a changed item, then re-accepted
    hp.rename(w.tmp / "userhooks-old")  # (a removed directory's inode number may be reused)
    hp.mkdir()
    err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))
    hooks_anchor = [a for a in data["anchors"] if a["anchor"] == "hooks_path"]
    assert hooks_anchor and hooks_anchor[0]["changed"] and hooks_anchor[0]["path"] == str(hp)
    good_check(check(w, capsys, rec["id"]))


# --- create and bootstrap wiring -----------------------------------------------------------


def test_the_baseline_includes_what_bootstrap_made(w: CW, capsys) -> None:  # noqa: ANN001
    code = "import os; os.makedirs('.claude', exist_ok=True); open('.claude/settings.json','w')"
    w.repo_cfg["bootstrap"] = {"steps": [{"argv": [sys.executable, "-c", code]}]}
    rec = made(w, capsys)
    paths = [r["path"] for r in w.rows("SELECT path FROM baseline_items WHERE class = 'claude'")]
    assert paths == [".claude"]
    good_check(check(w, capsys, rec["id"]))


def test_a_create_whose_baseline_cannot_be_recorded_unwinds_and_leaves_no_record(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    w.top["remove"] = {"nested_scan_limit": 5}
    r = w.create(capsys)
    assert r.code == 12, (r.out, r.err)
    e = r.json()["error"]
    d = e["details"]
    assert e["code"] == "create_failed" and d["cause"] == "baseline_incomplete" and d["unwound"]
    assert "nested_scan_limit" in e["message"]
    assert w.records() == [] and w.admin_entries() == []
    del w.top["remove"]
    assert w.create(capsys).code == 0


def test_bootstrap_rerun_refuses_on_a_changed_baseline_of_either_class(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    marker = w.tmp / "ran"
    w.repo_cfg["bootstrap"] = {
        "steps": [{"argv": [sys.executable, "-c", f"open({str(marker)!r}, 'a')"]}],
        "auto": False,
    }
    rec = made(w, capsys)
    for change in ("git", "claude"):
        if change == "git":
            w.g(["config", "core.hooksPath", "/x"])
        else:
            Path(rec["path"], "CLAUDE.md").write_text("x")
        r = w.cli(capsys, "bootstrap", rec["id"], human=True)
        e = err_of(r, 20, "repo_config_changed")
        assert change in e["details"]["changed_classes"]
        assert not marker.exists()


def test_bootstrap_rerun_state_refusals_come_before_the_baseline_one(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    set_col(w, rec["id"], state="unlinked")
    err_of(w.cli(capsys, "bootstrap", rec["id"], human=True), 20, "unlinked")
    set_col(w, rec["id"], state="active", owned=0)
    err_of(w.cli(capsys, "bootstrap", rec["id"], human=True), 20, "not_owned")


def test_bootstrap_rerun_rerecords_the_baseline_after_success(w: CW, capsys) -> None:  # noqa: ANN001
    code = "open('CLAUDE.md', 'w').write('from bootstrap')"
    w.repo_cfg["bootstrap"] = {"steps": [{"argv": [sys.executable, "-c", code]}], "auto": False}
    rec = made(w, capsys)
    r = w.cli(capsys, "bootstrap", rec["id"], human=True)
    assert r.code == 0, (r.out, r.err)
    now = row(w, rec["id"])["baseline_digest"]
    assert now != rec["baseline_digest"]
    good_check(check(w, capsys, rec["id"]))


def test_a_failed_rerun_keeps_the_old_baseline(w: CW, capsys) -> None:  # noqa: ANN001
    code = "open('CLAUDE.md', 'w'); raise SystemExit(3)"
    w.repo_cfg["bootstrap"] = {"steps": [{"argv": [sys.executable, "-c", code]}], "auto": False}
    rec = made(w, capsys)
    r = w.cli(capsys, "bootstrap", rec["id"], human=True)
    assert r.code == 12
    assert row(w, rec["id"])["baseline_digest"] == rec["baseline_digest"]
    err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")


def test_bootstrap_dry_run_also_checks_the_baseline(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.g(["config", "core.hooksPath", "/x"])
    err_of(
        w.cli(capsys, "--dry-run", "bootstrap", rec["id"], human=True), 20, "repo_config_changed"
    )


def test_git_on_the_path_is_not_used_for_collection(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    """The collectors read files; `git config --list` is never run on repo-controlled files."""
    rec = made(w, capsys)
    calls: list[list[str]] = []
    real = subprocess.run
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: calls.append(list(a[0])) or real(*a, **k)
    )
    good_check(check(w, capsys, rec["id"]))
    assert not [c for c in calls if "config" in c]


def test_a_non_utf8_file_name_is_reported_and_accepted(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    with open(os.fsencode(rec["path"]) + b"/CLAUDE\xff.md", "wb") as fh:
        fh.write(b"x")
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert e["items"][0]["path"].startswith("␀hex:")
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    good_check(check(w, capsys, rec["id"]))
