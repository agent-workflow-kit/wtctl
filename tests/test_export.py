"""``wtctl export``: filters, schema-valid output, tolerance of damaged files."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from tests.schemacheck import validate
from tests.test_audit import ID, FixedClock, audit_remove
from wtctl.audit import Journal
from wtctl.cli import main
from wtctl.context import Runtime
from wtctl.gateway import Gateway

NOW = dt.datetime(2026, 3, 10, 12, 0, 0, tzinfo=dt.UTC)


def state_dir() -> Path:
    return Path(os.environ["XDG_STATE_HOME"]) / "wtctl"


def at(when: dt.datetime) -> Journal:
    return Journal(state_dir(), clock=FixedClock(when))


def export(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    rt = Runtime(stdin_isatty=False, gateway=Gateway(clock=FixedClock(NOW)))
    code = main(["export", *argv], runtime=rt)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def seed() -> None:
    old = at(NOW - dt.timedelta(days=3))
    new = at(NOW - dt.timedelta(hours=2))
    old.log("create", actor="human:alice", id=ID, details={"n": 1})
    new.log("remove", actor="auto:cron", id=ID, details={"n": 2})
    audit_remove(old, actor="human:alice", decision_ids=["d1"])
    audit_remove(new, actor="auto:cron", decision_ids=["d1", "d2"])


def test_default_is_audit_as_jsonl_oldest_first(capsys: pytest.CaptureFixture[str]) -> None:
    seed()
    code, out, _ = export(capsys)
    assert code == 0
    recs = [json.loads(x) for x in out.splitlines()]
    assert [r["actor"] for r in recs] == ["human:alice", "auto:cron"]
    assert recs[1]["decision_id"] == ["d1", "d2"]
    for r in recs:
        validate("wtctl/audit.json", r)


def test_log_flag_selects_log_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    seed()
    code, out, _ = export(capsys, "--log")
    assert code == 0
    recs = [json.loads(x) for x in out.splitlines()]
    assert [r["details"]["n"] for r in recs] == [1, 2]
    for r in recs:
        validate("wtctl/log.json", r)


def test_json_output_is_an_array_matching_the_cli_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed()
    for extra in ([], ["--log"]):
        code, out, _ = export(capsys, "--json", *extra)
        assert code == 0
        data = json.loads(out)
        assert len(data) == 2
        validate("wtctl/cli/export.json", data)


def test_empty_or_missing_journal(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = export(capsys, "--json")
    assert (code, json.loads(out)) == (0, [])
    code, out, _ = export(capsys)
    assert (code, out) == (0, "")
    assert not state_dir().exists()  # export is read-only


@pytest.mark.parametrize(
    "since,expected",
    [
        ("1d", [2]),
        ("4d", [1, 2]),
        ("90m", []),
        ("3h", [2]),
        ("2026-03-10T09:00:00Z", [2]),
        ("2026-03-10T14:00:00+05:00", [2]),  # 09:00 UTC
        ("2026-03-10T09:00:00-05:00", []),  # 14:00 UTC
        ("2026-03-01T00:00:00.123456789Z", [1, 2]),
    ],
)
def test_since_filters(capsys: pytest.CaptureFixture[str], since: str, expected: list[int]) -> None:
    seed()
    code, out, _ = export(capsys, "--log", "--since", since)
    assert code == 0
    assert [json.loads(x)["details"]["n"] for x in out.splitlines()] == expected


def test_since_is_inclusive(capsys: pytest.CaptureFixture[str]) -> None:
    at(NOW).log("fetch", actor=None)
    code, out, _ = export(capsys, "--log", "--since", "2026-03-10T12:00:00Z")
    assert code == 0 and len(out.splitlines()) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "yesterday",
        "0d",
        "-1d",
        "5",
        "10y",
        "2026-03-10",
        "2026-03-10T12:00:00",
        "99999999999999w",
        "9" * 5000 + "d",
        "12345678901d",
    ],
)
def test_invalid_since_is_a_usage_error(capsys: pytest.CaptureFixture[str], bad: str) -> None:
    code, out, err = export(capsys, "--since", bad)
    assert code == 2
    assert "since" in err


def test_audit_and_log_are_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = export(capsys, "--audit", "--log")
    assert code == 2


def test_damaged_lines_are_skipped_and_reported(capsys: pytest.CaptureFixture[str]) -> None:
    j = at(NOW)
    j.log("create", actor=None, details={"n": 1})
    with open(j.log_path, "ab") as f:
        f.write(b"\n\n")  # blank lines
        f.write(b"not json\n")
        f.write(b"[1,2]\n")  # not an object
        f.write(b'{"ts":"2026-03-10T12:00:00.000Z","x":NaN}\n')
        f.write(b'{"ts":"\xff\xfe"}\n')  # invalid UTF-8
        f.write(b'{"a":"' + b"z" * 2_000_000 + b'"}\n')  # oversized
        f.write(b'{"ts":"2026-03-10T12:00:00.000Z","lev')  # torn, no newline
    j.log("fetch", actor=None, details={"n": 2})
    code, out, err = export(capsys, "--log")
    assert code == 0
    assert [json.loads(x)["event"] for x in out.splitlines()] == ["create", "fetch"]
    assert "skipped 6 unreadable line" in err


def test_foreign_control_characters_never_reach_the_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    j = at(NOW)
    j.directory.mkdir(parents=True, mode=0o700)
    j.log_path.write_bytes(
        b'{"ts":"2026-03-10T12:00:00.000Z","details":{"s":"a\\u001b[2Jb\xe2\x80\xaec"}}\n'
        b'{"ts":"2026-03-10T12:00:00.000Z","details":{"s":"raw \x1b[2J escape"}}\n'
    )
    code, out, err = export(capsys, "--log")
    assert code == 0
    assert out.isascii() and "\x1b" not in out
    assert len(out.splitlines()) == 1  # the line with a raw ESC is not valid JSON: skipped
    assert json.loads(out)["details"]["s"] == "a\x1b[2Jb\u202ec"
    assert "skipped 1" in err


def test_symlinked_file_is_refused(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    j = at(NOW)
    j.directory.mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text('{"ts":"2026-03-10T12:00:00.000Z"}\n')
    j.audit_path.symlink_to(target)
    code, out, err = export(capsys)
    assert code == 1
    assert out == ""
    assert "symlink" in err


def test_symlinked_directory_is_refused(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "audit.jsonl").write_text('{"ts":"2026-03-10T12:00:00.000Z"}\n')
    state_dir().parent.mkdir(parents=True)
    state_dir().symlink_to(real)
    code, _, _ = export(capsys)
    assert code == 1


def test_export_does_not_need_a_human_actor(capsys: pytest.CaptureFixture[str]) -> None:
    seed()
    rt = Runtime(stdin_isatty=False, gateway=Gateway(clock=FixedClock(NOW)))
    assert main(["export", "--actor", "auto:report"], runtime=rt) == 0


def test_blank_lines_not_counted_but_missing_ts_with_since_is(
    capsys: pytest.CaptureFixture[str],
) -> None:
    j = at(NOW)
    j.directory.mkdir(parents=True, mode=0o700)
    j.log_path.write_bytes(b'\n\n{"n":1}\n{"ts":"2026-03-10T11:00:00.000Z","n":2}\n')
    code, out, err = export(capsys, "--log")
    assert (code, err) == (0, "")
    assert len(out.splitlines()) == 2
    code, out, err = export(capsys, "--log", "--since", "1d")
    assert [json.loads(x)["n"] for x in out.splitlines()] == [2]
    assert "skipped 1 unreadable line" in err


def test_non_finite_numbers_are_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    j = at(NOW)
    j.directory.mkdir(parents=True, mode=0o700)
    j.log_path.write_bytes(b'{"n":1e999}\n{"n":2}\n')
    code, out, err = export(capsys, "--log")
    assert [json.loads(x)["n"] for x in out.splitlines()] == [2]
    assert "skipped 1" in err


def test_export_streams_without_holding_all_records(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    j = at(NOW)
    for n in range(50):
        j.log("fetch", actor=None, details={"n": n})
    live = 0
    peak = 0
    real_loads = json.loads
    import wtctl.audit as audit_mod
    import wtctl.commands.export as export_mod

    real_render = export_mod.render_json

    def counting(*a: object, **kw: object) -> object:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        return real_loads(*a, **kw)  # type: ignore[arg-type]

    def render(data: object) -> str:
        nonlocal live
        live -= 1  # a record is done once rendered
        return real_render(data)

    monkeypatch.setattr(audit_mod.json, "loads", counting)
    monkeypatch.setattr(export_mod, "render_json", render)
    for extra in ([], ["--json"]):
        live = peak = 0
        code, out, _ = export(capsys, "--log", *extra)
        assert code == 0
        assert peak <= 2
    assert len(real_loads(out)) == 50


def test_jsonl_and_json_output_bytes(capsys: pytest.CaptureFixture[str]) -> None:
    at(NOW).log("fetch", actor=None, details={"n": 1})
    at(NOW).log("fetch", actor=None, details={"n": 2})
    _, out, _ = export(capsys, "--log")
    lines = out.splitlines()
    assert len(lines) == 2 and out == "\n".join(lines) + "\n"
    _, jout, _ = export(capsys, "--log", "--json")
    assert jout == "[" + ",".join(lines) + "]\n"


def test_skipped_count_is_reported_when_the_stream_fails_midway(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl.commands import export as export_cmd

    class Reader:
        skipped = 3

        def __iter__(self):  # type: ignore[no-untyped-def]
            yield {"a": 1}
            raise OSError("disk went away")

        def close(self) -> None:
            pass

    at(NOW).log("create", actor=None, details={"n": 1})
    monkeypatch.setattr(export_cmd, "read_records", lambda *a, **k: Reader())
    code, _, err = export(capsys, "--log")
    assert code == 1
    assert "skipped 3 unreadable line" in err
    assert "disk went away" in err
