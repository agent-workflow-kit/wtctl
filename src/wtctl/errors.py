"""Error codes, their exit codes, and the ``{"error": {...}}`` object.

The mapping is the "Errors" table in docs/interface.md; ``schemas/cli/error.json`` is the shape.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

EXIT_CODES: Mapping[str, int] = MappingProxyType(
    {
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
)

# Optional members of the error object (schemas/cli/error.json), besides code and message.
FIELDS = frozenset(
    {
        "reason",
        "checks",
        "holder_path",
        "holder_id",
        "processes",
        "items",
        "hook",
        "hook_stderr",
        "escalated_hooks",
        "decision_ids",
        "candidates",
        "details",
    }
)


REFUSAL_REASONS = frozenset(
    {
        "dirty",
        "unpushed",
        "repo_config_changed",
        "branch_in_use",
        "branch_exists",
        "path_exists",
        "in_use",
        "held",
        "unlinked",
        "not_owned",
        "main_worktree",
        "locked",
    }
)

# `details.warnings` of bootstrap_failed (schemas/cli/error.json): bounded in count and length.
WARNINGS_MAX = 50
WARNING_MAX_CHARS = 1024
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def _escape_char(ch: str) -> str:
    """One character as it appears in a warning: control, C1, line/paragraph separator and bidi
    characters as ``\\xNN`` / ``\\uXXXX``, lone surrogates (names that are not UTF-8) as
    ``\\udcXX``; anything else unchanged."""
    code = ord(ch)
    if 0xD800 <= code <= 0xDFFF or code > 0xFF and _CONTROL.match(ch):
        return f"\\u{code:04x}"
    if _CONTROL.match(ch):
        return f"\\x{code:02x}"
    return ch


def _bounded_text(text: str) -> str:
    """Escape ``text`` and cut it at ``WARNING_MAX_CHARS`` on an escape boundary."""
    parts = [_escape_char(ch) for ch in text]
    if sum(map(len, parts)) <= WARNING_MAX_CHARS:
        return "".join(parts)
    out: list[str] = []
    used = 0
    for part in parts:
        if used + len(part) > WARNING_MAX_CHARS - 3:
            break
        out.append(part)
        used += len(part)
    return "".join(out) + "..."


def bounded_warnings(warnings: Sequence[str]) -> list[str] | None:
    """The run's warnings as ``details.warnings``: at most ``WARNINGS_MAX`` entries (when there
    are more, the first ``WARNINGS_MAX - 1`` and a last entry counting the rest) of at most
    ``WARNING_MAX_CHARS`` characters, control, C1, separator and bidi characters escaped and lone
    surrogates made printable, never cut inside an escape; None when there are none."""
    if len(warnings) > WARNINGS_MAX:
        kept = list(warnings[: WARNINGS_MAX - 1])
        kept.append(f"{len(warnings) - len(kept)} more warnings omitted")
    else:
        kept = list(warnings)
    out = [_bounded_text(text) for text in kept]
    return out or None


def bootstrap_failed_details(record: Any, step: Any, warnings: Sequence[str]) -> dict[str, Any]:
    """``details`` of ``bootstrap_failed``: the record, the failed step and, when the run warned
    (``copy_env`` skips, a stopped scan), the bounded warnings."""
    details: dict[str, Any] = {"record": record, "step": step}
    bounded = bounded_warnings(warnings)
    if bounded:
        details["warnings"] = bounded
    return details


# `details.cause` values the schema allows, per code (when the cause is present).
_CAUSES = {
    "remote_mismatch": {"no_remote"},
    "clone_failed": {"timeout"},
    "create_failed": {"object_format", "anchor_changed", "tree_too_large"},
    "fetch_failed": {"timeout", "prune_incomplete"},
}

# Required `details` members, per code and per refusal reason (schemas/cli/error.json allOf).
_CODE_DETAILS = {
    "partial": ("remaining_path",),
    "io_error": ("path", "written", "deleted"),
    "unsafe_repo_config": ("keys",),
    "extract_invalid": ("paths",),
    "bootstrap_failed": ("record", "step"),
}
_REASON_DETAILS = {
    "held": ("holders",),
    "repo_config_changed": ("digest", "recorded_digest"),
    "branch_exists": ("tip",),
    "locked": ("lock_reason",),
}
# Fields that belong to exactly one refusal reason (present if and only if it is the reason).
_REASON_FIELD = {
    "holder_path": "branch_in_use",
    "processes": "in_use",
}


def _validate(code: str, fields: Mapping[str, Any]) -> None:
    """The conditional rules of schemas/cli/error.json; a violation is a programmer error."""

    def fail(msg: str) -> None:
        raise ValueError(f"{code} error: {msg}")

    def details_need(keys: tuple[str, ...]) -> None:
        details = fields.get("details")
        if not isinstance(details, dict):
            fail(f"needs details with {list(keys)}")
        else:
            missing = [k for k in keys if k not in details]
            if missing:
                fail(f"details lacks {missing}")

    reason = fields.get("reason")
    if code == "refused":
        if reason not in REFUSAL_REASONS:
            fail(f"a refusal (exit 20) needs a reason from the refusal_reason enum, got {reason!r}")
    elif "reason" in fields:
        fail("reason is only for refused")
    if code != "refused" and "checks" in fields:
        fail("checks are only for refused")

    for name, owner in (("candidates", "ambiguous"), ("escalated_hooks", "escalated")):
        if (code == owner) != (name in fields):
            fail(f"{name} is required for {owner} and forbidden otherwise")
    if code == "ambiguous" and len(fields["candidates"]) < 2:
        fail("candidates needs at least 2 entries")
    if code == "escalated" and not fields["escalated_hooks"]:
        fail("escalated_hooks needs at least 1 entry")

    if code in ("vetoed", "escalated"):
        for name in ("hook", "hook_stderr"):
            if name not in fields:
                fail(f"{name} is required")
    else:
        for name in ("hook", "hook_stderr"):
            if name in fields:
                fail(f"{name} is only for vetoed and escalated")

    for name, owner in _REASON_FIELD.items():
        if (reason == owner) != (name in fields):
            fail(f"{name} is required for reason {owner} and forbidden otherwise")
    if "items" in fields and reason != "repo_config_changed":
        fail("items is only for reason repo_config_changed")
    if reason == "repo_config_changed":
        details = fields.get("details")
        if "items" in fields:
            if not fields["items"]:
                fail("items needs at least 1 entry")
            details_need(("changed_classes",))
        elif not (
            isinstance(details, dict)
            and (details.get("anchors") or ("digest" in details and "recorded_digest" in details))
        ):
            fail(
                "items is required for reason repo_config_changed "
                "(or details.anchors, or details.digest with details.recorded_digest)"
            )
    if reason == "branch_in_use" and "holder_id" not in fields:
        fail("holder_id is required for reason branch_in_use")
    if "holder_id" in fields and reason not in ("branch_in_use", "path_exists"):
        fail("holder_id is only for reasons branch_in_use and path_exists")

    if code in _CODE_DETAILS:
        details_need(_CODE_DETAILS[code])
    if reason in _REASON_DETAILS:
        details_need(_REASON_DETAILS[reason])

    details = fields.get("details")
    if isinstance(details, dict) and "warnings" in details:
        warns = details["warnings"]
        if code != "bootstrap_failed":
            fail("details.warnings is only for bootstrap_failed")
        if not (
            isinstance(warns, list)
            and len(warns) <= WARNINGS_MAX
            and all(isinstance(m, str) and len(m) <= WARNING_MAX_CHARS for m in warns)
        ):
            fail(f"details.warnings is at most {WARNINGS_MAX} strings of {WARNING_MAX_CHARS} chars")
    cause = details.get("cause") if isinstance(details, dict) else None
    if cause is not None and code in _CAUSES and cause not in _CAUSES[code]:
        fail(f"details.cause must be one of {sorted(_CAUSES[code])}")
    if code == "create_failed" and cause == "anchor_changed":
        details_need(("anchors",))
    if cause == "fuse_anchor":
        details_need(("anchors",))
        if reason != "unlinked":
            fail("details.cause fuse_anchor needs reason unlinked")
    if code == "not_found" and isinstance(details, dict) and "state" in details:
        details_need(("state", "removed_at"))
        if details["state"] not in ("gone", "removed", "forgotten"):
            fail("details.state must be gone, removed or forgotten")


class WtctlError(Exception):
    """An expected failure: carries the ``error.code``, a message and the optional fields."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        if code not in EXIT_CODES:
            raise ValueError(f"unknown error code: {code!r}")
        unknown = set(fields) - FIELDS
        if unknown:
            raise ValueError(f"unknown error fields: {sorted(unknown)}")
        _validate(code, fields)
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields
        # Text for stdout in human mode when the command still has output (create's path after
        # bootstrap_failed); never part of the error object.
        self.stdout_text = ""

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]

    def to_object(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.fields}}


class UsageError(WtctlError):
    """Bad command line, flag combination or human-only flag (exit 2)."""

    def __init__(self, message: str, *, usage: str | None = None) -> None:
        super().__init__("usage", message)
        self.usage = usage
