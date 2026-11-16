"""Golden ``--json`` documents of ``create --throwaway``, a throwaway ``remove`` and ``prune``.

Ids, paths, timestamps and identities are normalized; everything else is pinned. Regenerate with
``UPDATE_GOLDEN=1`` after an intended change.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from tests.throwlib import TW


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> TW:
    return TW(tmp_path, private_home).build()


def normalized(w: TW, data: Any, *ids: str) -> str:
    text = json.dumps(data, indent=1)
    for key in ("created", "updated", "removed_at", "dir_id", "root_id"):
        for rec in _records(data):
            if rec.get(key):
                text = text.replace(str(rec[key]), f"<{key}>")
    for i, rid in enumerate(ids):
        text = text.replace(rid, "<id>" if len(ids) == 1 else f"<id{i + 1}>")
    for name, sha in w.shas.items():
        text = text.replace(sha, f"<{name}>")
    text = text.replace(str(w.tmp.resolve()), "<tmp>").replace(str(w.tmp), "<tmp>")
    return re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+Z", "<time>", text) + "\n"


def _records(data: Any) -> list[dict[str, Any]]:
    return (
        [data["record"]] if isinstance(data, dict) and isinstance(data.get("record"), dict) else []
    )


def check_golden(name: str, text: str) -> None:
    golden = Path(__file__).parent / "golden" / name
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(text)
    assert text == golden.read_text()


@pytest.mark.parametrize("source", ["remote", "local"])
def test_golden_json_of_a_throwaway_create(w: TW, source: str) -> None:
    r = w.throw("--source", source, "--label", "run=1")
    assert r.code == 0, r.err
    data = r.json()
    check_golden(f"throwaway_create_{source}.json", normalized(w, data, data["record"]["id"]))


def test_golden_json_of_a_throwaway_removal(w: TW) -> None:
    rec = w.made("--source", "local")
    data = w.removed(rec["id"])
    check_golden("throwaway_remove_ok.json", normalized(w, data, rec["id"]))


def test_golden_json_of_a_prune(w: TW) -> None:
    from wtctl.gateway import ProcessInfo

    old, busy, young = (w.made("--source", "local") for _ in range(3))
    w.age(old["id"], 10)
    w.age(busy["id"], 10)
    w.scanner.procs = [ProcessInfo(9, "vim", busy["path"], ())]
    data = w.pruned("--older-than", "1d")
    text = normalized(w, data, old["id"], busy["id"])
    assert young["id"] not in json.dumps(data)
    check_golden("throwaway_prune.json", text)
