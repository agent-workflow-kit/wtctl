"""Error codes, their exit codes, and the ``{"error": {...}}`` object.

The mapping is the "Errors" table in docs/interface.md; ``schemas/cli/error.json`` is the shape.
"""

from __future__ import annotations

from collections.abc import Mapping
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


class WtctlError(Exception):
    """An expected failure: carries the ``error.code``, a message and the optional fields."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        if code not in EXIT_CODES:
            raise ValueError(f"unknown error code: {code!r}")
        unknown = set(fields) - FIELDS
        if unknown:
            raise ValueError(f"unknown error fields: {sorted(unknown)}")
        if code == "refused" and "reason" not in fields:
            raise ValueError("a refusal (exit 20) needs a reason")
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields

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
