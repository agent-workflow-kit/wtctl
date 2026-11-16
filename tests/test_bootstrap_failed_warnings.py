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
