from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.schemacheck import SCHEMAS, validate
from wtctl.errors import EXIT_CODES, WtctlError

# Copied from docs/interface.md, "Errors" (code -> exit); deliberately not derived from the code.
DOCUMENTED = {
    "usage": 2,
    "config_invalid": 2,
    "not_found": 10,
    "vetoed": 11,
    "git_too_old": 12,
    "remote_mismatch": 12,
    "clone_failed": 12,
    "auth_required": 12,
    "fetch_failed": 12,
    "ref_not_found": 12,
    "create_failed": 12,
    "unsafe_repo_config": 12,
    "bootstrap_failed": 12,
    "extract_invalid": 12,
    "busy": 12,
    "confirmation_required": 13,
    "escalated": 13,
    "refused": 20,
    "ambiguous": 21,
    "internal": 1,
    "partial": 1,
    "io_error": 1,
}


def test_mapping_matches_the_documented_table() -> None:
    assert dict(EXIT_CODES) == DOCUMENTED


def test_mapping_covers_exactly_the_schema_error_codes() -> None:
    defs = json.loads((Path(SCHEMAS) / "defs.json").read_text())
    assert set(defs["$defs"]["error_code"]["enum"]) == set(EXIT_CODES)


# Fields the schema requires for a code (handlers supply them; the skeleton only carries them).
REQUIRED_FIELDS: dict[str, dict[str, object] | None] = {
    "refused": {"reason": "dirty"},
    "ambiguous": {
        "candidates": [
            {"id": "wt-aaaaaaaa", "path": "/x/a"},
            {"id": "wt-bbbbbbbb", "path": "/x/b"},
        ]
    },
    "vetoed": {"hook": "check.sh", "hook_stderr": "no"},
    "escalated": {
        "hook": "check.sh",
        "hook_stderr": "ask",
        "escalated_hooks": ["check.sh"],
    },
    "partial": {"details": {"remaining_path": "/x/a"}},
    "io_error": {"details": {"path": "a", "written": [], "deleted": []}},
    "unsafe_repo_config": {"details": {"keys": ["core.fsmonitor"]}},
    "extract_invalid": {"details": {"paths": [{"path": "a", "error": "invalid_path"}]}},
    # bootstrap_failed needs a full record object; its golden test belongs to the bootstrap issue.
    "bootstrap_failed": None,
}


@pytest.mark.parametrize(("code", "exit_code"), sorted(DOCUMENTED.items()))
def test_exit_code_and_golden_object(code: str, exit_code: int) -> None:
    extra = REQUIRED_FIELDS.get(code, {})
    err = WtctlError(code, "something happened", **(extra or {}))
    assert err.exit_code == exit_code
    obj = err.to_object()
    assert obj["error"]["code"] == code
    if extra is not None:
        validate("wtctl/cli/error.json", obj)


def test_unknown_code_is_rejected() -> None:
    with pytest.raises(ValueError):
        WtctlError("nope", "x")


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValueError):
        WtctlError("usage", "x", bogus=1)


def test_refused_requires_a_reason() -> None:
    with pytest.raises(ValueError):
        WtctlError("refused", "x")


def test_optional_fields_are_serialized() -> None:
    err = WtctlError(
        "ambiguous",
        "two match",
        candidates=[{"id": "wt-aaaaaaaa", "path": "/x/a"}, {"id": None, "path": "/x/b"}],
    )
    validate("wtctl/cli/error.json", err.to_object())
