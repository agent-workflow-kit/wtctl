"""A failing bootstrap reports the run's warnings in ``error.details.warnings`` (#55)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.createlib import CW
from tests.schemacheck import validate
from tests.test_bootstrap_cmd import err_of, made, rerun
from tests.test_create import ok
from wtctl import errors
from wtctl.errors import WARNING_MAX_CHARS, WARNINGS_MAX, WtctlError, bounded_warnings

PY = sys.executable
FAIL = {"argv": [PY, "-c", "raise SystemExit(3)"]}


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def failed_error(r) -> dict:  # noqa: ANN001
    assert r.code == 12, (r.out, r.err)
    doc = r.json()
    validate("wtctl/cli/error.json", doc)
    return doc["error"]


def test_a_failing_create_reports_the_copy_env_warnings(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg["copy_env"] = ["../escape", ".env"]
    (w.clone / ".env").write_text("A=1\n")
    w.repo_cfg["bootstrap"] = {"steps": [FAIL]}
    err = failed_error(w.create(capsys, "--copy-env"))
    warns = err["details"]["warnings"]
    assert any("copy_env" in m and "../escape" in m for m in warns)
    assert err["details"]["step"]["exit"] == 3


def test_a_failing_create_without_warnings_has_no_warnings_member(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg["bootstrap"] = {"steps": [FAIL]}
    err = failed_error(w.create(capsys))
    assert "warnings" not in err["details"]


def test_a_failing_rerun_reports_the_copy_env_warnings(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    w.repo_cfg["copy_env"] = ["/abs"]
    w.repo_cfg["bootstrap"] = {"steps": [FAIL]}
    err = err_of(rerun(w, capsys, rec["id"], "--copy-env"), 12)
    validate("wtctl/cli/error.json", {"error": err})
    assert any("copy_env" in m and "/abs" in m for m in err["details"]["warnings"])


def test_hostile_pattern_names_are_escaped_and_the_list_is_bounded(w: CW, capsys) -> None:  # noqa: ANN001
    hostile = [f"../x\nnew line\x1b[31m{i}" for i in range(WARNINGS_MAX + 30)]
    w.repo_cfg["copy_env"] = hostile
    w.repo_cfg["bootstrap"] = {"steps": [FAIL]}
    err = failed_error(w.create(capsys, "--copy-env"))
    warns = err["details"]["warnings"]
    assert len(warns) == WARNINGS_MAX
    assert all("\n" not in m and "\x1b" not in m for m in warns)
    assert any("\\x0a" in m for m in warns)


def test_a_successful_create_still_reports_them_in_warnings(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg["copy_env"] = ["../escape"]
    data = ok(w.create(capsys, "--copy-env"))
    assert any("copy_env" in m for m in data["warnings"])


# -- the helper and the error rules ------------------------------------------------------------


def test_bounded_warnings_caps_count_length_and_escapes() -> None:
    long = "a" * (WARNING_MAX_CHARS * 3)
    out = bounded_warnings([long, "line1\nline2", "nul\0byte", *["x"] * (WARNINGS_MAX + 10)])
    assert out is not None
    assert len(out) == WARNINGS_MAX
    assert len(out[0]) <= WARNING_MAX_CHARS and out[0].endswith("...")
    assert out[1] == "line1\\x0aline2" and out[2] == "nul\\x00byte"


def test_bounded_warnings_keeps_surrogates_json_safe() -> None:
    out = bounded_warnings(["bad \udcff name"])
    assert out is not None
    out[0].encode("utf-8")  # must not raise


def test_empty_warnings_are_none() -> None:
    assert bounded_warnings([]) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"details": {"record": {}, "step": {}, "warnings": "x"}},
        {"details": {"record": {}, "step": {}, "warnings": [1]}},
        {"details": {"record": {}, "step": {}, "warnings": ["x"] * (WARNINGS_MAX + 1)}},
        {"details": {"record": {}, "step": {}, "warnings": ["x" * (WARNING_MAX_CHARS + 1)]}},
    ],
)
def test_the_error_rejects_malformed_warnings(bad: dict) -> None:
    with pytest.raises(ValueError):
        WtctlError("bootstrap_failed", "x", **bad)


def test_warnings_belong_to_bootstrap_failed_only() -> None:
    with pytest.raises(ValueError):
        WtctlError("create_failed", "x", details={"warnings": ["x"]})


def test_the_limits_match_the_schema() -> None:
    import json

    schema = json.loads(
        Path(errors.__file__).parents[2].joinpath("schemas/cli/error.json").read_text()
    )
    prop = schema["properties"]["error"]["properties"]["details"]["properties"]["warnings"]
    assert prop["maxItems"] == WARNINGS_MAX and prop["items"]["maxLength"] == WARNING_MAX_CHARS


def test_the_copy_env_limits_are_documented() -> None:
    from wtctl import bootstrapper as b

    docs = Path(errors.__file__).parents[2].joinpath("docs/interface.md").read_text()
    assert f"{b.ENV_WARN_CAP} warnings" in docs
    assert f"{b.ENV_MATCH_RETURN_MAX} matches" in docs
    assert f"{b.ENV_MATCH_RETURN_BYTES // (1024 * 1024)} MiB of paths" in docs
    assert f"{b.ENV_BATCH_BYTES // (1024 * 1024)} MiB" in docs


# -- overflow, escapes and truncation (R2, R7) ---------------------------------------------------


def test_overflow_keeps_the_last_slot_for_an_omitted_count() -> None:
    out = bounded_warnings([f"w{i}" for i in range(WARNINGS_MAX + 10)])
    assert out is not None and len(out) == WARNINGS_MAX
    assert out[: WARNINGS_MAX - 1] == [f"w{i}" for i in range(WARNINGS_MAX - 1)]
    assert out[-1] == "11 more warnings omitted"


def test_exactly_the_maximum_is_kept_whole() -> None:
    out = bounded_warnings([f"w{i}" for i in range(WARNINGS_MAX)])
    assert out == [f"w{i}" for i in range(WARNINGS_MAX)]


@pytest.mark.parametrize(
    ("char", "expected"),
    [
        ("\x80", "\\x80"),
        ("\x9f", "\\x9f"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
        ("\u202a", "\\u202a"),
        ("\u202e", "\\u202e"),
        ("\u2066", "\\u2066"),
        ("\u2069", "\\u2069"),
        ("\udcff", "\\udcff"),
    ],
)
def test_invisible_and_bidi_characters_are_escaped(char: str, expected: str) -> None:
    assert bounded_warnings([f"a{char}b"]) == [f"a{expected}b"]


def test_ordinary_text_stays_as_is() -> None:
    assert bounded_warnings(["café 中"]) == ["café 中"]


@pytest.mark.parametrize("char", ["\x01", "\u202e", "\udcff"])
def test_truncation_never_cuts_an_escape_in_half(char: str) -> None:
    for pad in range(WARNING_MAX_CHARS - 12, WARNING_MAX_CHARS):
        (out,) = bounded_warnings(["a" * pad + char * 5]) or [""]
        assert len(out) <= WARNING_MAX_CHARS
        body = out[:-3] if out.endswith("...") else out
        assert body.startswith("a" * min(pad, len(body)))
        tail = body[pad:]
        # whatever remains of the run is whole escape sequences
        assert tail == "" or all(
            part in ("\\x01", "\\u202e", "\\udcff") for part in tail.replace("\\", " \\").split()
        )
