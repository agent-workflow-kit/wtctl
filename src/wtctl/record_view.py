"""The record object printed by ``show`` and ``list`` (``schemas/cli/record.json``)."""

from __future__ import annotations

from typing import Any

from .state import Record, State

# The object's members, in the order docs/interface.md lists them.
FIELDS = (
    "id object_type owned state repo clone_path common_dir common_id admin_dir dir_id base_dir "
    "root_id path name branch created_branch base base_sha head_at_create ref source depth "
    "fetch_url remote labels holders baseline_digest bootstrap created updated missing_since "
    "removed_at last_error"
).split()


def record_object(state: State, record: Record) -> dict[str, Any]:
    """``record`` (a row from ``State``) as the schema's object: ``bootstrap`` is assembled from
    the bootstrap tables and is null for a throwaway."""
    full = dict(record)
    full["bootstrap"] = (
        None if record["object_type"] == "throwaway" else state.bootstrap_info(record["id"])
    )
    return {k: full[k] for k in FIELDS}
