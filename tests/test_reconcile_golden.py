"""Golden ``--json`` documents of ``reconcile``: a normal run, a dry run and each report kind.

Ids, paths, shas and timestamps are normalized; everything else is pinned. Regenerate with
``UPDATE_GOLDEN=1`` after an intended change.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.reclib import RC
from tests.schemacheck import validate
from tests.test_reconcile_creating import crash_after_add
from tests.test_throwaway_golden import check_golden


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> RC:
    return RC(tmp_path, private_home).build()


def normalized(w: RC, data: dict[str, Any], *ids: str) -> str:
    validate("wtctl/cli/reconcile.json", data)
    text = json.dumps(data, indent=1)
    for i, rid in enumerate(ids):
        text = text.replace(rid, "<id>" if len(ids) == 1 else f"<id{i + 1}>")
    text = text.replace(str(w.tmp.resolve()), "<tmp>").replace(str(w.tmp), "<tmp>")
    text = re.sub(r"\b[0-9a-f]{40}\b", "<sha>", text)
    return re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+Z", "<time>", text) + "\n"


def golden(w: RC, name: str, data: dict[str, Any], *ids: str) -> None:
    check_golden(f"reconcile_{name}.json", normalized(w, data, *ids))


def test_golden_normal_run(w: RC) -> None:
    rec = w.wt()
    w.vanish(rec)
    golden(w, "normal", w.reconciled(), rec["id"])


def test_golden_dry_run(w: RC) -> None:
    rec = w.wt()
    w.vanish(rec)
    golden(w, "dry_run", w.reconciled("--dry-run"), rec["id"])


def test_golden_nothing_to_do(w: RC) -> None:
    golden(w, "nothing", w.reconciled())


def test_golden_gone_with_an_absent_entry(w: RC) -> None:
    rec = w.wt()
    w.g(["worktree", "remove", "--force", rec["path"]])
    w.reconciled()
    w.clock.advance(2 * 3600)
    golden(w, "gone", w.reconciled(), rec["id"])


def test_golden_gone_with_unpushed(w: RC) -> None:
    rec = w.wt()
    w.push_branch(Path(rec["path"]), rec["branch"])
    w.vanish(rec)
    new = w.commit("elsewhere", w.clone)
    w.g(["update-ref", f"refs/heads/{rec['branch']}", new])
    w.reconciled()
    w.clock.advance(2 * 3600)
    golden(w, "gone_with_unpushed", w.reconciled(), rec["id"])


def test_golden_kept_unpushed(w: RC) -> None:
    rec = w.wt()
    w.commit("local", Path(rec["path"]))
    w.vanish(rec)
    w.reconciled()
    w.clock.advance(2 * 3600)
    golden(w, "kept_unpushed", w.reconciled(), rec["id"])


def test_golden_adopted_gone(w: RC) -> None:
    other = w.tmp / "adopted"
    w.g(["worktree", "add", "-q", "-b", "adb", str(other)])
    rid = w.run("adopt", str(other), "--repo", "example.test/org/repo").json()["record"]["id"]
    shutil.rmtree(other)
    w.reconciled()
    w.clock.advance(2 * 3600)
    golden(w, "adopted_gone", w.reconciled(), rid)


def test_golden_adopted_after_crash(w: RC) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    golden(w, "adopted_after_crash", w.reconciled(), rec["id"])


def test_golden_abandoned(w: RC) -> None:
    rec = w.wt()
    crash_after_add(w, rec)
    (Path(rec["path"]) / ".git").write_text("gitdir: /somewhere/else\n")
    golden(w, "abandoned", w.reconciled(), rec["id"])


def test_golden_stray(w: RC) -> None:
    (w.troot / "wt-orphan1").mkdir(parents=True)
    golden(w, "stray", w.reconciled())


def test_golden_unlinked(w: RC) -> None:
    rec = w.wt()
    path = Path(rec["path"])
    path.rename(path.with_name("moved-away"))
    path.mkdir()
    golden(w, "unlinked", w.reconciled(), rec["id"])


def test_golden_clone_missing(w: RC) -> None:
    rec = w.wt()
    w.clone.rename(w.tmp / "clone-moved")
    golden(w, "clone_missing", w.reconciled(), rec["id"])


def test_golden_moved_clone(w: RC) -> None:
    rec = w.wt()
    new = w.tmp / "clone-moved"
    w.clone.rename(new)
    w.repo_cfg["clone"] = str(new)
    golden(w, "moved_clone", w.reconciled(), rec["id"])


def test_golden_fetch_failed(w: RC) -> None:
    rec = w.wt()
    w.commit("local", Path(rec["path"]))
    w.vanish(rec)
    w.reconciled()
    w.clock.advance(2 * 3600)
    shutil.rmtree(w.remote)
    data = w.reconciled()
    for r in data["reports"]:
        if r["kind"] == "fetch_failed":
            r["message"] = "<git's words>"  # git's wording differs between versions
    golden(w, "fetch_failed", data, rec["id"])
