"""log.jsonl and audit.jsonl writers: atomicity, permissions, hostile content, redaction."""

from __future__ import annotations

import datetime as dt
import errno
import json
import multiprocessing
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from tests.schemacheck import validate
from wtctl import audit
from wtctl.audit import Journal
from wtctl.errors import WtctlError

ID = "wt-abcdefgh"
REPO = "example.test/team/proj"
SHA = "a" * 40
DIGEST = "b" * 64


class FixedClock:
    def __init__(self, when: dt.datetime | None = None) -> None:
        self.when = when or dt.datetime(2026, 3, 4, 5, 6, 7, 891000, tzinfo=dt.UTC)

    def now(self) -> dt.datetime:
        return self.when

    def monotonic(self) -> float:
        return 0.0


def make(tmp_path: Path, **kw: Any) -> Journal:
    return Journal(tmp_path / "state" / "wtctl", clock=FixedClock(), **kw)


def lines(path: Path) -> list[str]:
    return [x.decode("ascii") for x in path.read_bytes().split(b"\n")[:-1] if x]


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in lines(path)]


def remove_inputs(**over: Any) -> dict[str, Any]:
    base = {"head": SHA, "merged_head": None, "baseline_digest": DIGEST, "overridden": []}
    base.update(over)
    return base


def audit_remove(j: Journal, **over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = {
        "actor": "human:alice",
        "decision_ids": [],
        "target_id": ID,
        "target_path": "/work/a",
        "inputs": remove_inputs(),
    }
    kw.update(over)
    return j.audit("remove", **kw)


# --- log ---------------------------------------------------------------------------------------


def test_log_record_is_schema_valid_and_timestamped(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.log(
        "create",
        id=ID,
        repo=REPO,
        actor="auto:cron",
        decision_ids=["d1"],
        details={"path": "/work/a"},
    )
    (rec,) = records(j.log_path)
    validate("wtctl/log.json", rec)
    assert rec["ts"] == "2026-03-04T05:06:07.891Z"
    assert rec["level"] == "info"
    assert rec["decision_ids"] == ["d1"]
    assert rec["details"] == {"path": "/work/a"}


def test_log_defaults_null_and_empty(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.log("fetch", actor=None)
    (rec,) = records(j.log_path)
    validate("wtctl/log.json", rec)
    assert (rec["id"], rec["repo"], rec["actor"], rec["decision_ids"], rec["details"]) == (
        None,
        None,
        None,
        [],
        {},
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"event": "nope"},
        {"event": "create", "level": "fatal"},
        {"event": "create", "id": "wt-BAD"},
        {"event": "create", "repo": "Not A Repo"},
        {"event": "create", "actor": "root"},
        {"event": "create", "actor": "human:a\n"},
        {"event": "create", "decision_ids": ["bad id"]},
        {"event": "create", "decision_ids": [f"d{i}" for i in range(33)]},
        {"event": "create", "details": []},
    ],
)
def test_log_rejects_invalid_fields(tmp_path: Path, kw: dict[str, Any]) -> None:
    j = make(tmp_path)
    kw = {"actor": None, **kw}
    with pytest.raises(WtctlError) as ei:
        j.log(**kw)
    assert ei.value.code == "internal"
    assert not j.log_path.exists() or j.log_path.read_bytes() == b""


def test_log_every_event_and_level_is_accepted(tmp_path: Path) -> None:
    j = make(tmp_path)
    schema = json.loads(Path("schemas/log.json").read_text())
    events = schema["properties"]["event"]["enum"]
    assert set(events) == set(audit.LOG_EVENTS)
    for ev in events:
        for lvl in ("info", "warn", "error"):
            j.log(ev, level=lvl, actor=None)
    for rec in records(j.log_path):
        validate("wtctl/log.json", rec)


# --- audit -------------------------------------------------------------------------------------


def test_audit_remove_human_with_single_decision_id(tmp_path: Path) -> None:
    j = make(tmp_path)
    audit_remove(j, decision_ids=["gate-1"])
    (rec,) = records(j.audit_path)
    validate("wtctl/audit.json", rec)
    assert rec["actor"] == "human:alice"
    assert rec["decision_id"] == "gate-1"
    assert rec["target"] == {"id": ID, "path": "/work/a"}
    assert rec["result"] == "ok"
    assert rec["action"] == "remove"


def test_audit_decision_id_none_string_list(tmp_path: Path) -> None:
    j = make(tmp_path)
    audit_remove(j, actor="auto:policy-x", decision_ids=[])
    audit_remove(j, decision_ids=["a", "b"])
    r1, r2 = records(j.audit_path)
    assert r1["decision_id"] is None
    assert r1["actor"] == "auto:policy-x"
    assert r2["decision_id"] == ["a", "b"]
    validate("wtctl/audit.json", r1)
    validate("wtctl/audit.json", r2)


def _inputs_by_action() -> dict[str, tuple[dict[str, Any], str | None]]:
    anchor = {
        "anchor": "common_dir",
        "path": "/c/.git",
        "recorded": "1:2",
        "current": "3:4",
        "changed": True,
        "fs_type": "ext4",
        "mount_source": "/dev/sda1",
        "mount_point": "/",
    }
    return {
        "remove": (remove_inputs(overridden=["tracked"]), ID),
        "branch_delete": ({"branch": "feat/x", "sha": SHA}, ID),
        "baseline_accept": (
            {"old_digest": DIGEST, "new_digest": "c" * 64, "items": [], "anchors": []},
            ID,
        ),
        "adopt": ({"path": "/work/a", "baseline_digest": DIGEST, "holder": None}, ID),
        "clone_accept": ({"repo": REPO, "clone_path": "/c", "anchors": [anchor]}, None),
        "admin_entry_delete": (
            {"admin_entry": "/c/.git/worktrees/a", "head": None, "branch": None, "tip": None},
            ID,
        ),
    }


@pytest.mark.parametrize("action", sorted(_inputs_by_action()))
def test_audit_every_action_writes_a_valid_record(tmp_path: Path, action: str) -> None:
    j = make(tmp_path)
    inputs, target_id = _inputs_by_action()[action]
    j.audit(
        action,
        actor="human:alice",
        decision_ids=[],
        target_id=target_id,
        target_path="/work/a",
        inputs=inputs,
        result="error",
    )
    (rec,) = records(j.audit_path)
    try:
        validate("wtctl/audit.json", rec)
    except ValidationError as exc:  # pragma: no cover - failure detail
        pytest.fail(str(exc))
    assert rec["result"] == "error"


def test_audit_actions_match_the_schema() -> None:
    schema = json.loads(Path("schemas/audit.json").read_text())
    assert set(schema["properties"]["action"]["enum"]) == set(audit.AUDIT_ACTIONS)


@pytest.mark.parametrize("action", sorted(_inputs_by_action()))
def test_audit_rejects_missing_and_extra_inputs(tmp_path: Path, action: str) -> None:
    j = make(tmp_path)
    inputs, target_id = _inputs_by_action()[action]
    common: dict[str, Any] = {
        "actor": "human:a",
        "decision_ids": [],
        "target_id": target_id,
        "target_path": "/p",
    }
    with pytest.raises(WtctlError):
        j.audit(action, inputs={**inputs, "extra": 1}, **common)
    with pytest.raises(WtctlError):
        j.audit(action, inputs={k: v for k, v in list(inputs.items())[1:]}, **common)
    assert not j.audit_path.exists()


@pytest.mark.parametrize(
    "over",
    [
        {"actor": None},
        {"actor": "bob"},
        {"result": "maybe"},
        {"target_path": "relative"},
        {"target_path": "/a\nb"},
        {"target_id": "bad"},
        {"decision_ids": ["x y"]},
    ],
)
def test_audit_rejects_invalid_envelope(tmp_path: Path, over: dict[str, Any]) -> None:
    j = make(tmp_path)
    with pytest.raises(WtctlError):
        audit_remove(j, **over)
    assert not j.audit_path.exists()


def test_audit_unknown_action_rejected(tmp_path: Path) -> None:
    j = make(tmp_path)
    with pytest.raises(WtctlError):
        j.audit(
            "push",
            actor="human:a",
            decision_ids=[],
            target_id=None,
            target_path="/p",
            inputs={},
        )


def test_audit_fails_loud_when_it_cannot_be_written(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.directory.parent.mkdir(parents=True)
    j.directory.write_text("a file, not a directory")
    with pytest.raises(WtctlError) as ei:
        audit_remove(j)
    assert ei.value.code == "internal"


def test_audit_too_large_inputs_fail_instead_of_corrupting(tmp_path: Path) -> None:
    j = make(tmp_path)
    inputs = {
        "old_digest": DIGEST,
        "new_digest": DIGEST,
        "items": [{"i": n} for n in range(200000)],
        "anchors": [],
    }
    with pytest.raises(WtctlError):
        j.audit(
            "baseline_accept",
            actor="human:a",
            decision_ids=[],
            target_id=ID,
            target_path="/p",
            inputs=inputs,
        )
    assert not j.audit_path.exists() or j.audit_path.read_bytes() == b""


# --- files, permissions, symlinks -------------------------------------------------------------


def test_creates_dir_0700_and_files_0600(tmp_path: Path) -> None:
    old = os.umask(0)  # a hostile umask must not widen anything
    try:
        j = make(tmp_path)
        j.log("fetch", actor=None)
        audit_remove(j)
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(j.directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(j.log_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(j.audit_path).st_mode) == 0o600


def test_tightens_loose_existing_permissions(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.directory.mkdir(parents=True)
    os.chmod(j.directory, 0o755)
    j.log_path.write_text("")
    os.chmod(j.log_path, 0o666)
    j.log("fetch", actor=None)
    assert stat.S_IMODE(os.stat(j.directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(j.log_path).st_mode) == 0o600


def test_refuses_symlinked_log_file(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.directory.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    j.audit_path.symlink_to(victim)
    with pytest.raises(WtctlError) as ei:
        audit_remove(j)
    assert ei.value.code == "internal"
    assert victim.read_text() == "keep\n"


def test_refuses_symlinked_directory(tmp_path: Path) -> None:
    j = make(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    j.directory.parent.mkdir(parents=True)
    j.directory.symlink_to(real)
    with pytest.raises(WtctlError) as ei:
        j.log("fetch", actor=None)
    assert ei.value.code == "internal"
    assert list(real.iterdir()) == []


def test_refuses_hardlinked_log_file(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.directory.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    os.link(victim, j.log_path)
    with pytest.raises(WtctlError):
        j.log("fetch", actor=None)
    assert victim.read_text() == "keep\n"


def test_refuses_non_regular_file(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.directory.mkdir(parents=True)
    os.mkfifo(j.log_path)
    with pytest.raises(WtctlError):
        j.log("fetch", actor=None)


# --- hostile content ---------------------------------------------------------------------------


def test_hostile_strings_stay_on_one_ascii_line(tmp_path: Path) -> None:
    j = make(tmp_path)
    nasty = "a\nb\r\nc\x00d\x1b[31m e‮f\udcff\U0001f600\x7f"
    j.log("hook", actor=None, details={nasty: nasty, "list": [nasty], "bytes": b"\xff\xfeok"})
    j.log("fetch", actor=None)
    raw = j.log_path.read_bytes()
    assert raw.isascii()
    assert raw.count(b"\n") == 2
    rec = json.loads(raw.split(b"\n")[0])
    validate("wtctl/log.json", rec)
    assert rec["details"][nasty] == nasty
    assert rec["details"]["bytes"].endswith("ok")


def test_huge_values_are_truncated_and_marked(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.log("hook", actor=None, details={"stderr": "x" * 5_000_000})
    (rec,) = records(j.log_path)
    val = rec["details"]["stderr"]
    assert len(val) < 5000
    assert "truncated" in val
    assert len(j.log_path.read_bytes()) <= audit.MAX_LINE_BYTES


def test_many_keys_and_deep_nesting_are_bounded(tmp_path: Path) -> None:
    j = make(tmp_path)
    deep: Any = "end"
    for _ in range(500):
        deep = {"k": deep}
    wide = {f"k{i}": "v" * 100 for i in range(100000)}
    j.log("hook", actor=None, details={"deep": deep, "wide": wide})
    j.log("hook", actor=None, details={"list": list(range(10**6))})
    data = j.log_path.read_bytes()
    for line in data.split(b"\n")[:-1]:
        assert len(line) < audit.MAX_LINE_BYTES
        validate("wtctl/log.json", json.loads(line))


def test_non_json_types_become_strings(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.log(
        "hook",
        actor=None,
        details={"p": Path("/a/b"), "f": float("nan"), "s": {1, 2}, "o": object, "n": None},
    )
    (rec,) = records(j.log_path)
    assert rec["details"]["p"] == "/a/b"
    assert rec["details"]["f"] == "nan"
    assert rec["details"]["n"] is None


# --- redaction ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,leaked",
    [
        ("https://user:hunter2@example.test/x.git", "hunter2"),
        ("https://tok3nvalue@example.test/x.git", "tok3nvalue"),
        ("ghp_" + "A1b2C3d4" * 5, "A1b2C3d4A1b2"),
        ("github_pat_" + "A1b2C3d4_" * 5, "A1b2C3d4_A1b2"),
        ("glpat-abcdefghij0123456789", "abcdefghij0123"),
        ("xoxb-1234567890-abcdefghij", "1234567890"),
        ("Authorization: Bearer abcdef0123456789", "abcdef0123456789"),
        ("http.extraheader=AUTHORIZATION: basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("GITHUB_TOKEN=s3cr3tvalue123", "s3cr3tvalue123"),
        ("password: 'hunter2'", "hunter2"),
        ("curl -H 'private-token: zzzzzzzz1234'", "zzzzzzzz1234"),
    ],
)
def test_secret_shapes_in_strings_are_redacted(tmp_path: Path, text: str, leaked: str) -> None:
    j = make(tmp_path)
    j.log("hook", actor=None, details={"stderr": f"fatal: {text} failed", "list": [text]})
    raw = j.log_path.read_text()
    assert leaked not in raw
    assert "[redacted]" in raw or "example.test/x.git" in raw


def test_url_userinfo_removed_but_host_kept(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.log("clone", actor=None, details={"url": "https://u:p@example.test:8443/a/b.git"})
    (rec,) = records(j.log_path)
    assert rec["details"]["url"] == "https://example.test:8443/a/b.git"


def test_scp_style_urls_and_plain_text_are_untouched(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.log("clone", actor=None, details={"a": "git@example.test:a/b.git", "b": "token count is 3"})
    (rec,) = records(j.log_path)
    assert rec["details"] == {"a": "git@example.test:a/b.git", "b": "token count is 3"}


@pytest.mark.parametrize(
    "key",
    ["token", "GH_TOKEN", "password", "api_key", "apiKey", "Authorization", "secret", "headers"],
)
def test_secret_named_keys_have_values_redacted(tmp_path: Path, key: str) -> None:
    j = make(tmp_path)
    j.log("hook", actor=None, details={key: {"nested": "plain-looking"}, "ok": "fine"})
    (rec,) = records(j.log_path)
    assert rec["details"][key] == "[redacted]"
    assert rec["details"]["ok"] == "fine"


def test_configured_secret_values_are_masked_everywhere(tmp_path: Path) -> None:
    j = make(tmp_path, secrets=["plainsecretvalue", "x"])
    j.log(
        "hook",
        actor=None,
        details={"out": "got plainsecretvalue here", "plainsecretvalue": "k", "x": "x marks"},
    )
    audit_remove(j, target_path="/work/plainsecretvalue")
    raw = j.log_path.read_text() + j.audit_path.read_text()
    assert "plainsecretvalue" not in raw
    # A one-character "secret" would shred every record, so very short values are ignored.
    assert "x marks" in raw


def test_audit_inputs_are_redacted_too(tmp_path: Path) -> None:
    j = make(tmp_path)
    audit_remove(j, inputs=remove_inputs(overridden=[]), target_path="/w/https://u:pw@h.test/")
    assert "pw@" not in j.audit_path.read_text()


# --- concurrency, torn lines, I/O failures -------------------------------------------------------


def _writer(directory: str, tag: int, count: int) -> None:
    j = Journal(Path(directory), clock=FixedClock())
    for n in range(count):
        j.log("hook", actor=None, details={"tag": tag, "n": n, "pad": "p" * (200 + 37 * tag)})
        audit_remove(j, target_path=f"/work/{tag}/{n}")


def test_concurrent_processes_never_tear_a_line(tmp_path: Path) -> None:
    directory = tmp_path / "state" / "wtctl"
    procs_n, per = 6, 150
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_writer, args=(str(directory), t, per)) for t in range(procs_n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0
    j = Journal(directory, clock=FixedClock())
    log = records(j.log_path)
    aud = records(j.audit_path)
    assert len(log) == len(aud) == procs_n * per
    assert {(r["details"]["tag"], r["details"]["n"]) for r in log} == {
        (t, n) for t in range(procs_n) for n in range(per)
    }
    for r in aud[:20]:
        validate("wtctl/audit.json", r)


def test_a_torn_last_line_does_not_corrupt_the_next_record(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.log("fetch", actor=None)
    with open(j.log_path, "ab") as f:
        f.write(b'{"ts": "2026-01-01T00:00:00.000Z", "lev')  # partial, no newline
    j.log("create", actor=None)
    raw = j.log_path.read_bytes().split(b"\n")
    good = [json.loads(x) for x in raw if x.startswith(b'{"ts":"')]
    assert [r["event"] for r in good] == ["fetch", "create"]


def test_disk_full_raises_io_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    j = make(tmp_path)
    j.log("fetch", actor=None)

    def full(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(audit, "_os_write", full)
    with pytest.raises(WtctlError) as ei:
        audit_remove(j)
    assert ei.value.code == "internal"
    assert "No space" in ei.value.message


def test_short_writes_are_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    j = make(tmp_path)
    real = os.write

    def two_bytes(fd: int, data: bytes) -> int:
        return real(fd, bytes(data[:7]))

    monkeypatch.setattr(audit, "_os_write", two_bytes)
    j.log("fetch", actor=None)
    monkeypatch.undo()
    assert records(j.log_path)[0]["event"] == "fetch"


def test_eio_on_fsync_of_audit_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    j = make(tmp_path)

    def bad(fd: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(audit, "_os_fsync", bad)
    with pytest.raises(WtctlError) as ei:
        audit_remove(j)
    assert ei.value.code == "internal"


def test_best_effort_log_returns_a_warning_instead_of_raising(tmp_path: Path) -> None:
    j = make(tmp_path)
    j.directory.parent.mkdir(parents=True)
    j.directory.write_text("x")
    warning = j.log_best_effort("fetch", actor=None)
    assert warning is not None and "log.jsonl" in warning
    good = make(tmp_path / "ok")
    assert good.log_best_effort("fetch", actor=None) is None


# --- context wiring -------------------------------------------------------------------------------


def test_journal_for_context_uses_state_dir_and_gateway_clock(tmp_path: Path) -> None:
    import argparse

    from wtctl.actor import Actor
    from wtctl.context import Context, Runtime
    from wtctl.gateway import Gateway

    ctx = Context(
        args=argparse.Namespace(),
        actor=Actor("human:alice"),
        runtime=Runtime(gateway=Gateway(clock=FixedClock()), me_name="alice"),
        decision_ids=["d1"],
    )
    j = audit.journal_for(ctx)
    assert j.directory == Path(os.environ["XDG_STATE_HOME"]) / "wtctl"
    j.log("fetch", actor=ctx.actor.value, decision_ids=ctx.decision_ids)
    assert records(j.log_path)[0]["ts"].startswith("2026-03-04")
