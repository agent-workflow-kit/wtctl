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
REQUIRED_FIELDS: dict[str, dict[str, object]] = {
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
    "bootstrap_failed": {"details": {"record": {}, "step": "steps"}},
}
# Codes whose object is built but not validated against the schema here (see above).
UNVALIDATED = {"bootstrap_failed"}


@pytest.mark.parametrize(("code", "exit_code"), sorted(DOCUMENTED.items()))
def test_exit_code_and_golden_object(code: str, exit_code: int) -> None:
    extra = REQUIRED_FIELDS.get(code, {})
    err = WtctlError(code, "something happened", **extra)
    assert err.exit_code == exit_code
    obj = err.to_object()
    assert obj["error"]["code"] == code
    if code not in UNVALIDATED:
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


# --- conditional rules of schemas/cli/error.json ------------------------------------------------

CAND = [{"id": "wt-aaaaaaaa", "path": "/x/a"}, {"id": None, "path": "/x/b"}]


@pytest.mark.parametrize(
    ("code", "fields"),
    [
        ("refused", {"reason": "not_a_reason"}),
        ("not_found", {"reason": "dirty"}),
        ("not_found", {"checks": {}}),
        ("ambiguous", {}),
        ("ambiguous", {"candidates": CAND[:1]}),
        ("not_found", {"candidates": CAND}),
        ("escalated", {"hook": "h", "hook_stderr": ""}),
        ("escalated", {"hook": "h", "hook_stderr": "", "escalated_hooks": []}),
        ("vetoed", {"hook": "h"}),
        ("vetoed", {"hook_stderr": "x"}),
        ("not_found", {"hook": "h"}),
        ("not_found", {"hook_stderr": "h"}),
        ("vetoed", {"hook": "h", "hook_stderr": "", "escalated_hooks": ["h"]}),
        ("refused", {"reason": "dirty", "holder_path": "/x"}),
        ("refused", {"reason": "dirty", "holder_id": None}),
        ("refused", {"reason": "dirty", "processes": [{"pid": 1}]}),
        ("refused", {"reason": "dirty", "items": [{}]}),
        ("refused", {"reason": "branch_in_use", "holder_id": None}),
        ("refused", {"reason": "branch_in_use", "holder_path": "/x"}),
        ("refused", {"reason": "in_use"}),
        ("refused", {"reason": "repo_config_changed", "items": [{}]}),
        ("refused", {"reason": "held", "details": {}}),
        ("refused", {"reason": "branch_exists", "details": {}}),
        ("refused", {"reason": "locked"}),
        ("partial", {}),
        ("partial", {"details": {}}),
        ("io_error", {"details": {"path": "a"}}),
        ("unsafe_repo_config", {"details": {}}),
        ("extract_invalid", {}),
        ("bootstrap_failed", {"details": {"record": {}}}),
        ("create_failed", {"details": {"cause": "timeout"}}),
        ("create_failed", {"details": {"cause": "anchor_changed"}}),
        ("not_found", {"details": {"state": "removed"}}),
        ("not_found", {"details": {"state": "weird", "removed_at": "t"}}),
        ("refused", {"reason": "dirty", "details": {"cause": "fuse_anchor", "anchors": []}}),
    ],
)
def test_schema_conditional_rules_are_enforced(code: str, fields: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        WtctlError(code, "x", **fields)


@pytest.mark.parametrize(
    ("code", "fields"),
    [
        ("refused", {"reason": "dirty", "checks": {}}),
        ("refused", {"reason": "branch_in_use", "holder_path": "/x", "holder_id": None}),
        ("refused", {"reason": "path_exists", "holder_id": "wt-aaaaaaaa"}),
        ("refused", {"reason": "path_exists"}),
        ("refused", {"reason": "in_use", "processes": [{"pid": 1}]}),
        (
            "refused",
            {
                "reason": "repo_config_changed",
                "items": [{}],
                "details": {"digest": "d", "recorded_digest": "r", "changed_classes": []},
            },
        ),
        ("refused", {"reason": "held", "details": {"holders": [{}]}}),
        ("refused", {"reason": "branch_exists", "details": {"tip": "a" * 40}}),
        ("refused", {"reason": "locked", "details": {"lock_reason": "x"}}),
        ("create_failed", {"details": {"cause": "object_format"}}),
        ("create_failed", {"details": {"cause": "anchor_changed", "anchors": [{}]}}),
        ("not_found", {"details": {"state": "removed", "removed_at": "t"}}),
        ("not_found", {}),
        ("vetoed", {"hook": "h", "hook_stderr": ""}),
        ("fetch_failed", {"details": {"cause": "prune_incomplete"}}),
        (
            "refused",
            {"reason": "unlinked", "details": {"cause": "fuse_anchor", "anchors": [{}]}},
        ),
    ],
)
def test_valid_combinations_construct(code: str, fields: dict[str, object]) -> None:
    WtctlError(code, "x", **fields)
