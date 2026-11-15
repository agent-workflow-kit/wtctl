"""``create`` step 14: bootstrap during create, through the CLI with real git."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tests.createlib import CW, REPO, script
from tests.schemacheck import validate
from tests.test_create import ok, refused
from wtctl import bootstrapper

PY = sys.executable


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def failed(r) -> dict:  # noqa: ANN001
    """A bootstrap_failed error, validated against the schema (TODO(#17): the baseline digest
    that only #17 records is stood in for)."""
    assert r.code == 12, (r.out, r.err)
    doc = r.json()
    rec = dict(doc["error"]["details"]["record"])
    rec["baseline_digest"] = rec["baseline_digest"] or "0" * 64
    shown = {"error": {**doc["error"], "details": {**doc["error"]["details"], "record": rec}}}
    validate("wtctl/cli/error.json", shown)
    return doc["error"]


def pystep(code: str, **kw) -> dict:  # noqa: ANN003
    return {"argv": [PY, "-c", code], **kw}


def paths(w: CW, rid: str) -> dict[str, tuple[str, str | None]]:
    rows = w.rows("SELECT path, kind, digest FROM bootstrap_paths WHERE record_id = ?", rid)
    return {r["path"]: (r["kind"], r["digest"]) for r in rows}


def test_steps_run_and_the_created_set_is_recorded(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg["bootstrap"] = {
        "steps": [
            pystep("import os; os.makedirs('build'); open('build/o.bin','w').write('1')"),
            pystep("open('gen.txt','w').write('g'); print('hello')"),
        ]
    }
    data = ok(w.create(capsys))
    boot = data["bootstrap"]
    assert boot["state"] == "ok" and boot["tracked_changes"] == []
    assert [s["exit"] for s in boot["steps"]] == [0, 0]
    assert boot["steps"][1]["output_tail"] == "hello\n"
    assert boot["created_paths_count"] == 2
    assert data["record"]["bootstrap"] == {
        k: boot[k] for k in ("state", "steps", "created_paths_count")
    }
    rid = data["record"]["id"]
    assert paths(w, rid) == {
        "build/o.bin": ("untracked", hashlib.sha256(b"1").hexdigest()),
        "gen.txt": ("untracked", hashlib.sha256(b"g").hexdigest()),
    }
    assert data["record"]["state"] == "active"


def test_ignored_directories_are_recorded_by_prefix(w: CW, capsys) -> None:  # noqa: ANN001
    (w.work / ".gitignore").write_text("node_modules/\n")
    w.g(["add", ".gitignore"], w.work)
    w.g(["commit", "-q", "-m", "ignore"], w.work)
    w.g(["push", "-q", str(w.remote), "main"], w.work)
    w.repo_cfg["bootstrap"] = {
        "steps": [pystep("import os; os.makedirs('node_modules/p'); open('node_modules/p/i','w')")]
    }
    data = ok(w.create(capsys))
    assert paths(w, data["record"]["id"]) == {"node_modules": ("ignored_dir", None)}


def test_steps_do_not_see_token_variables_but_see_pass_env(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    monkeypatch.setenv("WT_TEST_TOKEN", "sekrit")
    monkeypatch.setenv("WT_TEST_PASS", "passed")
    monkeypatch.setenv("WT_TEST_PLAIN", "plain")
    w.top["hosts"] = {"example.test": {"token_env": "WT_TEST_TOKEN"}}
    out = w.tmp / "env.json"
    code = f"import os,json; json.dump(dict(os.environ), open({str(out)!r},'w'))"
    w.repo_cfg["bootstrap"] = {
        "steps": [pystep(code)],
        "env": {"FROM_CFG": "1"},
    }
    ok(w.create(capsys))
    env = json.loads(out.read_text())
    assert "WT_TEST_TOKEN" not in env and "sekrit" not in json.dumps(env)
    assert env["WT_TEST_PLAIN"] == "plain" and env["FROM_CFG"] == "1"
    assert env["WTCTL_REPO"] == REPO and env["WTCTL_ID"].startswith("wt-")
    assert env["WTCTL_PATH"] == os.getcwd() or Path(env["WTCTL_PATH"]).is_dir()
    w.top["hosts"] = {"example.test": {"token_env": "WT_TEST_TOKEN"}}
    w.repo_cfg["bootstrap"]["pass_env"] = ["WT_TEST_TOKEN"]
    out.unlink()
    ok(w.create(capsys))
    assert json.loads(out.read_text())["WT_TEST_TOKEN"] == "sekrit"


def test_a_failing_step_keeps_the_worktree_and_exits_12_after_hooks(w: CW, capsys) -> None:  # noqa: ANN001
    marker = w.tmp / "post.json"
    w.top["hooks"] = {"post-create": [{"cmd": script(w.tmp / "post.sh", f"cat > {marker}")}]}
    w.repo_cfg["bootstrap"] = {
        "steps": [
            pystep("open('one.txt','w'); print('ok1')"),
            pystep("print('boom'); raise SystemExit(7)"),
            pystep("open('never.txt','w')"),
        ]
    }
    r = w.create(capsys)
    err = failed(r)
    assert err["code"] == "bootstrap_failed"
    rec, step = err["details"]["record"], err["details"]["step"]
    assert step["exit"] == 7 and step["output_tail"] == "boom\n"
    assert rec["state"] == "active" and rec["bootstrap"]["state"] == "failed"
    assert [s["exit"] for s in rec["bootstrap"]["steps"]] == [0, 7]
    wt = Path(rec["path"])
    assert wt.is_dir() and (wt / "one.txt").exists() and not (wt / "never.txt").exists()
    assert rec["path"] in w.worktree_paths()
    assert w.records()[0]["state"] == "active"
    assert paths(w, rec["id"]) == {"one.txt": ("untracked", hashlib.sha256(b"").hexdigest())}
    payload = json.loads(marker.read_text())  # post-create fired, with the failed bootstrap
    assert payload["bootstrap"]["state"] == "failed"


def test_human_output_after_bootstrap_failed_is_still_the_path(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg["bootstrap"] = {"steps": [pystep("raise SystemExit(1)")]}
    r = w.create(capsys, json_out=False)
    assert r.code == 12
    path = r.out.strip()
    assert Path(path).is_dir() and "bootstrap" in r.err


def test_a_step_timeout_is_a_failure_with_no_exit_status(w: CW, capsys, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(bootstrapper, "TERM_GRACE", 1.0)
    w.repo_cfg["bootstrap"] = {"steps": [pystep("import time; time.sleep(60)", timeout=1)]}
    err = failed(w.create(capsys))
    assert err["details"]["step"]["exit"] is None


def test_no_bootstrap_and_auto_false_run_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    marker = w.tmp / "ran"
    w.repo_cfg["bootstrap"] = {"steps": [pystep(f"open({str(marker)!r},'w')")]}
    data = ok(w.create(capsys, "--no-bootstrap"))
    assert data["bootstrap"]["state"] == "none" and not marker.exists()
    w.repo_cfg["bootstrap"] = {**w.repo_cfg["bootstrap"], "auto": False}
    data = ok(w.create(capsys))
    assert data["bootstrap"]["state"] == "none" and not marker.exists()


def test_copy_env_without_steps_copies_and_records_the_file(w: CW, capsys) -> None:  # noqa: ANN001
    (w.clone / ".env").write_text("A=1\n")
    (w.clone / ".env").chmod(0o666)
    w.repo_cfg["copy_env"] = [".env"]
    data = ok(w.create(capsys, "--copy-env", "--no-bootstrap"))
    dst = Path(data["record"]["path"]) / ".env"
    assert dst.read_text() == "A=1\n" and stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert data["bootstrap"]["state"] == "ok" and data["bootstrap"]["steps"] == []
    assert paths(w, data["record"]["id"]) == {
        ".env": ("untracked", hashlib.sha256(b"A=1\n").hexdigest())
    }


def test_env_files_are_copied_before_the_steps_run(w: CW, capsys) -> None:  # noqa: ANN001
    (w.clone / ".env").write_text("A=1\n")
    w.repo_cfg["copy_env"] = [".env"]
    w.repo_cfg["bootstrap"] = {"steps": [pystep("print(open('.env').read().strip())")]}
    data = ok(w.create(capsys, "--copy-env"))
    assert data["bootstrap"]["steps"][0]["output_tail"] == "A=1\n"


def test_env_files_are_not_copied_without_the_flag(w: CW, capsys) -> None:  # noqa: ANN001
    (w.clone / ".env").write_text("A=1\n")
    w.repo_cfg["copy_env"] = [".env"]
    data = ok(w.create(capsys))
    assert not (Path(data["record"]["path"]) / ".env").exists()


def test_copy_env_with_empty_config_warns(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--copy-env"))
    assert any("copy_env" in m for m in data["warnings"])


def test_a_symlinked_env_source_is_skipped_with_a_warning(w: CW, capsys) -> None:  # noqa: ANN001
    secret = w.tmp / "secret"
    secret.write_text("TOP")
    os.symlink(secret, w.clone / ".env")
    w.repo_cfg["copy_env"] = [".env"]
    data = ok(w.create(capsys, "--copy-env"))
    assert not (Path(data["record"]["path"]) / ".env").exists()
    assert any("symlink" in m for m in data["warnings"])


def test_an_internal_failure_keeps_the_creating_record_as_interrupted(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    def boom(**kw):  # noqa: ANN003, ANN202
        raise RuntimeError("git status exploded")

    monkeypatch.setattr(bootstrapper, "run_bootstrap", boom)
    w.repo_cfg["bootstrap"] = {"steps": [pystep("pass")]}
    err = refused(w.create(capsys), 12)
    assert err["code"] == "create_failed" and err["details"]["record"].startswith("wt-")
    row = w.records()[0]
    assert row["state"] == "creating" and row["bootstrap_state"] == "interrupted"
    assert Path(err["details"]["path"]).is_dir()


def test_hostile_repo_files_are_not_executed_or_sourced(w: CW, capsys) -> None:  # noqa: ANN001
    marker = w.tmp / "hit"
    for name in (".envrc", ".env", "Makefile", "setup.sh", ".tool-versions", "package.json"):
        (w.work / name).write_text(f"#!/bin/sh\ntouch {marker}\n")
        (w.work / name).chmod(0o755)
    w.g(["add", "."], w.work)
    w.g(["commit", "-q", "-m", "hostile"], w.work)
    w.g(["push", "-q", str(w.remote), "main"], w.work)
    w.repo_cfg["bootstrap"] = {"steps": [pystep("print(1)")]}
    data = ok(w.create(capsys))
    assert (Path(data["record"]["path"]) / "Makefile").exists()  # the files are really there
    assert not marker.exists()


def test_reused_and_dry_run_outputs_still_validate(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg["bootstrap"] = {"steps": [pystep("print(1)")]}
    first = ok(w.create(capsys, "--holder", "h1"))
    again = ok(w.create(capsys, "--holder", "h1", "--branch", first["record"]["branch"]))
    assert again["reused"] is True
    assert again["record"]["bootstrap"]["state"] == "ok"
    dry = ok(w.create(capsys, "--dry-run"))
    assert dry["dry_run"] is True and dry["bootstrap"]["state"] == "none"
