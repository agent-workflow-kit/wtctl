"""Anchored walks of a worktree or throwaway for the removal checks.

Every walk is an operation of ``fsafe_child`` (a child process under the walk deadline) opened
from a *recorded* identity: the root a worktree lives under (``base_dir``, kept per root in the
``anchors`` table) or the directory itself for an adopted record, and the throwaway root
(``root_id``). ``RUNNER`` is the seam tests replace with ``repobaseline.inproc`` (no child).
"""

from __future__ import annotations

import os
from typing import Any

from . import fsafe, fsafe_child
from .repobaseline import Runner

RUNNER: Runner | None = None


def identity_list(text: str | None) -> list[int] | None:
    """``"<dev>:<ino>"`` as the ``[dev, ino]`` of a child request; None when not recorded."""
    if not text:
        return None
    dev, _, ino = text.partition(":")
    if not (dev.isdigit() and ino.isdigit()):
        raise fsafe.FsafeError("bad_request", f"not an identity: {text!r}")
    return [int(dev), int(ino)]


def walk_args(state: Any, record: dict[str, Any], **extra: object) -> dict[str, Any]:
    """The anchor, chain and root identity of the record's directory.

    An owned worktree is reached from its recorded root, a throwaway from its recorded throwaway
    root, an adopted worktree from its own directory (verified by ``dir_id``)."""
    path = record["path"]
    base = record.get("base_dir")
    if base:
        identity = (
            record.get("root_id")
            if record["object_type"] == "throwaway"
            else state.anchor_identity(base)
        )
        rel = os.path.relpath(path, base)
        if rel in (".", "..") or rel.startswith("../") or os.path.isabs(rel):
            raise fsafe.FsafeError("identity", f"{path} is not below its root {base}")
        anchor = {"path": base, "identity": identity_list(identity)}
        chain = rel.split(os.sep)
    else:
        anchor = {"path": path, "identity": identity_list(record.get("dir_id"))}
        chain = []
    return {
        "anchor": anchor,
        "chain": chain,
        "root_identity": identity_list(record.get("dir_id")),
        **extra,
    }


def run(op: str, args: dict[str, Any], budget: fsafe_child.Budget | None = None) -> Any:
    """Run ``op`` under the walk deadline (``RUNNER`` replaces the child in tests)."""
    if RUNNER is not None:
        return RUNNER(op, args)
    return fsafe_child.run_op(op, args, budget=budget)
