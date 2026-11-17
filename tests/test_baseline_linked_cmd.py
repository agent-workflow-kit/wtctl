"""Linked worktrees beneath a recorded main worktree: check, status, accept and the migration of
format 1 and 2 records (docs/interface.md, Baseline, Linked worktrees and Format; issue #71)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from tests.adoptlib import adopt, ok_adopt
from tests.createlib import CW
from tests.schemacheck import validate
from tests.test_baseline_cmd import accept, check, err_of, good_accept, good_check, row
from wtctl import baselinelinked, baselinesvc, repobaseline
from wtctl.commands import baseline_accept, baseline_check, clone  # noqa: F401

KEY = baselinelinked.LINKED_KEY


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def linked(w: CW, name: str = "feat") -> Path:
    path = w.clone / ".claude" / "worktrees" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    w.g(["worktree", "add", "-q", "-b", f"b-{name}", str(path)])
    return path


def main_record(w: CW, capsys) -> dict:  # noqa: ANN001
    return ok_adopt(adopt(w, capsys, w.clone))["record"]


def fmt(w: CW, rid: str) -> int | None:
    got = w.rows("SELECT format FROM baseline_meta WHERE record_id = ?", rid)
    return got[0]["format"] if got else None


def rerecord_as(w: CW, rid: str, fmt_: int) -> None:
    """Store the baseline exactly as the collector of that format recorded it."""
    rec = row(w, rid)
    spec = repobaseline.Spec(
        common_dir=rec["common_dir"],
        common_id=rec["common_id"],
        worktree=rec["path"],
        dir_id=rec["dir_id"],
        admin_dir=rec["admin_dir"],
        home=baselinesvc.home_dir(),
        links=fmt_ >= 2,
        linked=False,
    )
    b = repobaseline.compute(spec, runner=repobaseline.inproc)
    assert not b.has_detail()
    conn = sqlite3.connect(w.db)
    conn.execute("UPDATE records SET baseline_digest = ? WHERE id = ?", (b.digest, rid))
    conn.execute("DELETE FROM baseline_items WHERE record_id = ?", (rid,))
    conn.executemany(
        'INSERT INTO baseline_items(record_id, path, "key", kind, class, digest, anchor_id) '
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(rid, r["path"], r["key"], r["type"], r["cls"], r["sha256"], r["anchor_id"])
         for r in baselinesvc.item_rows(b)],
    )  # fmt: skip
    conn.execute("DELETE FROM baseline_meta WHERE record_id = ?", (rid,))
    if fmt_ > 1:
        conn.execute("INSERT INTO baseline_meta(record_id, format) VALUES (?, ?)", (rid, fmt_))
    conn.commit()
    conn.close()


def edit_inside(wt: Path) -> None:
    (wt / "CLAUDE.md").write_text("edited\n")
    (wt / ".claude").mkdir(exist_ok=True)
    (wt / ".claude" / "settings.json").write_text('{"hooks": {}}\n')
    (wt / "README.md").write_text("changed\n")


# --- format 3 ---------------------------------------------------------------------------------


def test_edits_in_a_linked_worktree_do_not_flip_the_main_record(w: CW, capsys) -> None:  # noqa: ANN001
    wt = linked(w)
    rec = main_record(w, capsys)
    assert fmt(w, rec["id"]) == 3
    assert any(i["key"] == KEY for i in w.rows(
        'SELECT "key" FROM baseline_items WHERE record_id = ?', rec["id"]
    ))  # fmt: skip
    good_check(check(w, capsys, rec["id"]))
    edit_inside(wt)
    data = good_check(check(w, capsys, rec["id"]))
    assert data["changed"] is False and "format" not in data


def test_a_new_linked_worktree_is_a_reported_change_cleared_by_accept(w: CW, capsys) -> None:  # noqa: ANN001
    rec = main_record(w, capsys)
    good_check(check(w, capsys, rec["id"]))
    linked(w, "later")
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    hits = [i for i in e["items"] if i["key"] == KEY]
    assert [(i["path"], i["before"], i["after"]["type"]) for i in hits] == [
        (".claude/worktrees/later", "absent", "linked_worktree")
    ]
    assert e["details"]["changed_classes"] == ["claude"]
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    good_check(check(w, capsys, rec["id"]))


def test_a_removed_linked_worktree_is_a_reported_change(w: CW, capsys) -> None:  # noqa: ANN001
    wt = linked(w)
    rec = main_record(w, capsys)
    w.g(["worktree", "remove", "--force", str(wt)])
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert [i["after"] for i in e["items"] if i["key"] == KEY] == ["absent"]
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    good_check(check(w, capsys, rec["id"]))


def test_a_planted_directory_with_a_forged_git_file_is_still_detected(w: CW, capsys) -> None:  # noqa: ANN001
    linked(w)
    rec = main_record(w, capsys)
    fake = w.clone / ".claude" / "worktrees" / "fake"
    fake.mkdir()
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    (fake / ".git").write_text(f"gitdir: {w.clone}/.git/worktrees/feat\n")
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    good_check(check(w, capsys, rec["id"]))
    (fake / "CLAUDE.md").write_text("planted\n")
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert e["details"]["changed_classes"] == ["claude"]
    assert "planted" not in str(e)


def test_a_verified_worktree_made_unverifiable_is_detected(w: CW, capsys) -> None:  # noqa: ANN001
    wt = linked(w)
    rec = main_record(w, capsys)
    os.remove(wt / ".git")
    (wt / ".git").mkdir()
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert [i["after"] for i in e["items"] if i["key"] == KEY] == ["absent"]


def test_status_explains_the_linked_worktree_change(w: CW, capsys) -> None:  # noqa: ANN001
    rec = main_record(w, capsys)
    linked(w, "later")
    r = w.cli(capsys, "status", rec["id"])
    assert r.code == 0, (r.out, r.err)
    validate("wtctl/cli/status.json", r.json())
    b = r.json()["checks"]["baseline"]
    assert b["status"] == "warn"
    assert [d["key"] for d in b["details"] if d["key"]] == [KEY]


# --- migration --------------------------------------------------------------------------------


@pytest.mark.parametrize("old", [1, 2])
def test_an_old_record_compares_by_its_own_collector_and_nothing_newly_fails(
    w: CW,
    capsys,
    old: int,  # noqa: ANN001
) -> None:
    wt = linked(w)
    rec = main_record(w, capsys)
    rerecord_as(w, rec["id"], old)
    assert fmt(w, rec["id"]) == (None if old == 1 else 2)
    data = good_check(check(w, capsys, rec["id"]))  # includes the linked content: unchanged
    assert data["format"] == old
    assert KEY in {i["key"] for i in data["new_coverage"]}  # what accept would change is shown
    s = w.cli(capsys, "status", rec["id"])
    assert s.json()["checks"]["baseline"]["status"] == "pass"
    # no false "unchanged": an edit inside the linked worktree is a change for its own format
    edit_inside(wt)
    err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")


@pytest.mark.parametrize("old", [1, 2])
def test_accept_upgrades_an_old_record_to_format_3(w: CW, capsys, old: int) -> None:  # noqa: ANN001
    wt = linked(w)
    rec = main_record(w, capsys)
    rerecord_as(w, rec["id"], old)
    edit_inside(wt)
    err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    assert fmt(w, rec["id"]) == 3
    good_check(check(w, capsys, rec["id"]))
    (wt / "CLAUDE.md").write_text("again\n")
    good_check(check(w, capsys, rec["id"]))


def test_a_record_without_linked_worktrees_keeps_its_old_digest_across_formats(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    rec = main_record(w, capsys)
    now = row(w, rec["id"])["baseline_digest"]
    rerecord_as(w, rec["id"], 2)
    assert row(w, rec["id"])["baseline_digest"] == now
    data = good_check(check(w, capsys, rec["id"]))
    assert data["format"] == 2 and "new_coverage" not in data


def test_the_listing_failing_never_hides_anything(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl import worktrees
    from wtctl.errors import WtctlError

    linked(w)
    rec = main_record(w, capsys)

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise WtctlError("internal", "forged")

    monkeypatch.setattr(worktrees, "worktree_entries", boom)
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")  # walked: reported
    assert [i["after"] for i in e["items"] if i["key"] == KEY] == ["absent"]
