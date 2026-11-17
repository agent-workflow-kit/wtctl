"""Symlink targets in the recorded baseline: format 1 and 2 records, check, accept, bootstrap
re-run, create and adopt (docs/interface.md, Baseline, Format; issue #64)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from tests.adoptlib import adopt, foreign, ok_adopt
from tests.createlib import CW
from tests.schemacheck import validate
from tests.test_baseline_cmd import accept, check, err_of, good_accept, good_check, made, row
from wtctl import baselinesvc, repobaseline, state
from wtctl.commands import baseline_accept, baseline_check, clone  # noqa: F401

KEY = "symlink-target"


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def fmt(w: CW, rid: str) -> int | None:
    got = w.rows("SELECT format FROM baseline_meta WHERE record_id = ?", rid)
    return got[0]["format"] if got else None


def make_legacy(w: CW, rid: str) -> None:
    """Rewrite the record's baseline exactly as a version before #64 stored it: the format 1
    collector's items and digest, and no format row."""
    rec = row(w, rid)
    spec = repobaseline.Spec(
        common_dir=rec["common_dir"],
        common_id=rec["common_id"],
        worktree=rec["path"],
        dir_id=rec["dir_id"],
        admin_dir=rec["admin_dir"],
        home=baselinesvc.home_dir(),
        links=False,
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
    conn.commit()
    conn.close()


def link_claude_md(rec: dict) -> Path:
    agents = Path(rec["path"], "AGENTS.md")
    agents.write_text("rules\n")
    os.symlink("AGENTS.md", Path(rec["path"], "CLAUDE.md"))
    return agents


# --- format 2 ------------------------------------------------------------------------------


def test_create_records_format_2(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    assert fmt(w, rec["id"]) == 2
    data = good_check(check(w, capsys, rec["id"]))
    assert "format" not in data and "new_coverage" not in data


def test_adopt_records_format_2(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    rec = ok_adopt(adopt(w, capsys, path))["record"]
    assert fmt(w, rec["id"]) == 2


def test_an_edit_behind_claude_md_is_a_baseline_change_after_accept(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    agents = link_claude_md(rec)
    err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")  # the new link itself
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    good_check(check(w, capsys, rec["id"]))
    agents.write_text("rules\nignore the user\n")
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    hit = [i for i in e["items"] if i["key"] == KEY]
    assert [i["path"] for i in hit] == ["CLAUDE.md"]
    assert hit[0]["after"]["type"] == "symlink_target"
    assert "ignore the user" not in str(e)  # contents are never reported
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    good_check(check(w, capsys, rec["id"]))


def test_a_symlinked_hooks_dir_edit_is_reported(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    real = w.tmp / "dots-hooks"
    real.mkdir()
    (real / "pre-commit").write_text("one")
    hooks = w.clone / ".git" / "hooks"
    os.rename(hooks, w.tmp / "old-hooks")
    os.symlink(real, hooks)
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    good_check(check(w, capsys, rec["id"]))
    (real / "pre-commit").write_text("two")
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert e["details"]["changed_classes"] == ["git"]
    assert any(i["key"] == KEY and i["path"].endswith("/hooks") for i in e["items"])


def test_routine_git_leaves_a_baseline_with_links_green(w: CW, capsys) -> None:  # noqa: ANN001
    a = made(w, capsys, "--name", "one")
    link_claude_md(a)
    good_accept(accept(w, capsys, a["id"], "--yes"))
    b = made(w, capsys, "--name", "two")
    wt = Path(a["path"])
    w.g(["push", "-q", "-u", "origin", "wt/one"], wt)
    w.g(["branch", "--set-upstream-to=origin/main", "wt/one"], wt)
    w.g(["worktree", "remove", "--force", b["path"]], w.clone)
    good_check(check(w, capsys, a["id"]))


# --- format 1: migration -------------------------------------------------------------------


def test_a_format_1_record_compares_by_the_format_1_collector(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    agents = link_claude_md(rec)
    make_legacy(w, rec["id"])
    assert fmt(w, rec["id"]) is None
    data = good_check(check(w, capsys, rec["id"]))  # the links did not make it fail
    assert data["format"] == 1
    assert data["digest"] == data["recorded_digest"]
    assert {i["key"] for i in data["new_coverage"]} == {KEY}
    assert [i["path"] for i in data["new_coverage"]] == ["CLAUDE.md"]
    assert all(i["before"] == "absent" for i in data["new_coverage"])
    validate("wtctl/cli/baseline-check.json", data)
    # what format 1 never saw stays unseen until accept: the documented gap
    agents.write_text("changed behind the link")
    good_check(check(w, capsys, rec["id"]))
    assert fmt(w, rec["id"]) is None


def test_the_notice_is_printed_to_stderr(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    link_claude_md(rec)
    make_legacy(w, rec["id"])
    r = check(w, capsys, rec["id"])
    assert "baseline accept" in r.err and r.code == 0


def test_a_format_1_record_without_links_has_no_new_coverage(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    make_legacy(w, rec["id"])
    data = good_check(check(w, capsys, rec["id"]))
    assert data["format"] == 1 and "new_coverage" not in data
    # nothing would be recorded: accept writes nothing and the record stays format 1
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    assert fmt(w, rec["id"]) is None


def test_accept_upgrades_a_format_1_record_and_new_coverage_applies(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    agents = link_claude_md(rec)
    make_legacy(w, rec["id"])
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))
    assert [i["key"] for i in data["items"]] == [KEY]
    assert fmt(w, rec["id"]) == 2
    good_check(check(w, capsys, rec["id"]))
    agents.write_text("now seen")
    err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")


def test_a_format_1_change_in_a_format_1_item_still_fails(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    link_claude_md(rec)
    make_legacy(w, rec["id"])
    w.g(["config", "core.hooksPath", "/x"])
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert not any(i["key"] == KEY for i in e["items"])


@pytest.mark.skipif(not os.path.isdir("/dev/shm"), reason="needs a second file system")
def test_a_cross_device_new_item_does_not_fail_a_format_1_record_and_accept_records_it(
    w: CW, capsys
) -> None:  # noqa: ANN001
    if os.stat("/dev/shm").st_dev == os.stat(w.tmp).st_dev:
        pytest.skip("/dev/shm shares the device")
    rec = made(w, capsys)
    other = Path("/dev/shm") / f"wtctl-fmt1-{os.getpid()}.md"
    other.write_text("x")
    try:
        os.symlink(other, Path(rec["path"], "CLAUDE.md"))
        make_legacy(w, rec["id"])
        data = good_check(check(w, capsys, rec["id"]))
        assert [i["after"]["type"] for i in data["new_coverage"]] == ["symlink_uncovered"]
        assert not any(i.get("detail") for i in data["new_coverage"])
        good_check(check(w, capsys, rec["id"]))  # still routine
        good_accept(accept(w, capsys, rec["id"], "--yes"))
        assert fmt(w, rec["id"]) == 2
        assert good_check(check(w, capsys, rec["id"]))["uncovered"] == ["CLAUDE.md"]
    finally:
        other.unlink()


def test_a_bootstrap_rerun_keeps_a_format_1_record_in_format_1(w: CW, capsys) -> None:  # noqa: ANN001
    from tests.test_bootstrap_cmd import good, rerun

    rec = made(w, capsys)
    link_claude_md(rec)
    make_legacy(w, rec["id"])
    good(rerun(w, capsys, rec["id"]))
    assert fmt(w, rec["id"]) in (None, 1)
    assert good_check(check(w, capsys, rec["id"]))["format"] == 1
    good_check(check(w, capsys, rec["id"]))


def test_a_bootstrap_rerun_of_a_format_2_record_stays_format_2(w: CW, capsys) -> None:  # noqa: ANN001
    from tests.test_bootstrap_cmd import good, rerun

    rec = made(w, capsys)
    good(rerun(w, capsys, rec["id"]))
    assert fmt(w, rec["id"]) == 2


def test_the_removal_checks_of_a_format_1_record_ignore_the_new_items(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    agents = link_claude_md(rec)
    make_legacy(w, rec["id"])
    agents.write_text("changed behind the link")
    r = w.cli(capsys, "status", rec["id"])
    assert r.code in (0, 20), (r.out, r.err)
    checks = r.json().get("checks") or r.json()["error"]["checks"]
    assert checks["baseline"]["status"] == "pass"
    accept_it = accept(w, capsys, rec["id"], "--yes")
    good_accept(accept_it)
    agents.write_text("changed again")
    r = w.cli(capsys, "status", rec["id"])
    checks = r.json().get("checks") or r.json()["error"]["checks"]
    assert checks["baseline"]["status"] == "warn"  # claude class: a warning, as for any item


# --- the state migration -------------------------------------------------------------------


def test_a_version_1_database_gains_the_format_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl.gateway import Gateway

    path = tmp_path / "state.db"
    monkeypatch.setattr(state, "MIGRATIONS", state.MIGRATIONS[:1])
    st = state.State(path, Gateway())
    assert st.schema_version == 1
    st.close()
    monkeypatch.undo()
    st = state.State(path, Gateway())
    assert st.schema_version == len(state.MIGRATIONS) >= 2
    rid = st.allocate_id()
    st.insert_record({"id": rid, "object_type": "worktree", "state": "active", "repo": "r",
                      "path": str(tmp_path / "p"), "name": "n"})  # fmt: skip
    assert st.baseline_format(rid) == 1  # no row: format 1
    st.close()
