"""``baseline accept`` gaps found in review: schema-valid refusals, an escaped prompt, one
transaction for the record, anchor and clone row, usage order, the FUSE check before the work."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.createlib import CW
from tests.schemacheck import validate
from tests.test_baseline_cmd import (
    accept,
    check,
    err_of,
    good_accept,
    good_check,
    made,
    row,
    set_col,
)
from wtctl import baselinesvc, repobaseline
from wtctl.commands import clone
from wtctl.state import State


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


# --- F8: no empty items / changed_classes ----------------------------------------------------


def test_refusal_never_has_empty_lists(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    cur = repobaseline.Baseline([], "0" * 64)
    record = {"id": rec["id"], "baseline_digest": "1" * 64}
    err = baselinesvc.refusal(record, cur, [], confirming=True)
    assert err.code == "busy"
    anchors = [
        {"anchor": "common_dir", "path": "/x", "recorded": "1:2", "current": "3:4",
         "changed": True, "fs_type": "ext4", "mount_source": "s", "mount_point": "/"}
    ]  # fmt: skip
    err = baselinesvc.refusal(
        {"id": rec["id"], "baseline_digest": "1" * 64}, cur, [], anchors=anchors
    )
    assert "items" not in err.fields and "changed_classes" not in err.fields["details"]
    validate("wtctl/cli/error.json", err.to_object())


def test_expect_digest_mismatch_with_only_a_moved_anchor(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    set_col(w, rec["id"], dir_id="1:2")
    e = err_of(accept(w, capsys, rec["id"], "--yes", "--expect-digest", "f" * 64), 20,
               "repo_config_changed")  # fmt: skip
    assert "items" not in e and "changed_classes" not in e["details"]
    assert [a["anchor"] for a in e["details"]["anchors"]] == ["worktree_dir"]
    assert row(w, rec["id"])["dir_id"] == "1:2"


def test_verify_unchanged_digest_only_mismatch_is_a_valid_refusal(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    set_col(w, rec["id"], baseline_digest="f" * 64)
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert "items" not in e and "changed_classes" not in e["details"]
    assert e["details"]["recorded_digest"] == "f" * 64
    assert e["details"]["digest"] == rec["baseline_digest"]


def test_bootstrap_rerun_precheck_digest_only_mismatch_is_a_valid_refusal(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    set_col(w, rec["id"], baseline_digest="f" * 64)
    e = err_of(w.cli(capsys, "bootstrap", rec["id"], human=True), 20, "repo_config_changed")
    assert "items" not in e and e["details"]["recorded_digest"] == "f" * 64


def test_refusal_without_confirming_is_digest_only_and_with_it_busy() -> None:
    cur = repobaseline.Baseline([], "0" * 64)
    record = {"id": "x", "baseline_digest": "1" * 64}
    err = baselinesvc.refusal(record, cur, [])
    assert err.code == "refused" and "items" not in err.fields
    validate("wtctl/cli/error.json", err.to_object())
    assert baselinesvc.refusal(record, cur, [], confirming=True).code == "busy"


def test_a_change_reverted_while_confirming_is_busy_not_an_empty_refusal(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    w.g(["config", "core.fsmonitor", "/bin/true"])

    def reverting(question: str, *, tty: bool) -> bool:
        w.g(["config", "--unset", "core.fsmonitor"])
        return True

    monkeypatch.setattr(clone, "confirm", reverting)
    r = w.cli(capsys, "baseline", "accept", rec["id"], human=True)
    err_of(r, 12)
    assert row(w, rec["id"])["baseline_digest"] == rec["baseline_digest"]


def test_identities_swapped_while_confirming_refuses_with_a_valid_payload(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    set_col(w, rec["id"], dir_id="1:2")
    p = Path(rec["path"])

    def swapping(question: str, *, tty: bool) -> bool:
        saved = p.with_name("saved-wt")
        pointer = (p / ".git").read_text()
        p.rename(saved)
        p.mkdir()
        (p / ".git").write_text(pointer)
        return True

    monkeypatch.setattr(clone, "confirm", swapping)
    r = w.cli(capsys, "baseline", "accept", rec["id"], human=True)
    e = err_of(r, 20, "repo_config_changed")
    assert "items" not in e and e["details"]["anchors"]
    assert row(w, rec["id"])["dir_id"] == "1:2"


# --- F9: the prompt is escaped ---------------------------------------------------------------


def test_the_prompt_escapes_untrusted_item_names(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    Path(rec["path"], "CLAUDE\x1b[2J.md").write_text("x")
    asked: list[str] = []
    monkeypatch.setattr(clone, "confirm", lambda q, *, tty: asked.append(q) or False)
    w.cli(capsys, "baseline", "accept", rec["id"], human=True)
    assert len(asked) == 1
    assert "\x1b" not in asked[0] and "\\x1b[2J" in asked[0]


def test_the_prompt_escapes_an_escape_in_a_config_section_name(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    with open(w.clone / ".git" / "config", "a") as fh:
        fh.write('[evil "a\x1b[2Jb"]\n\tkey = 1\n')
    asked: list[str] = []
    monkeypatch.setattr(clone, "confirm", lambda q, *, tty: asked.append(q) or False)
    w.cli(capsys, "baseline", "accept", rec["id"], human=True)
    assert len(asked) == 1 and "\x1b" not in asked[0]


def test_the_prompt_escapes_mount_facts(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    set_col(w, rec["id"], dir_id="1:2")
    real = baselinesvc.describe
    monkeypatch.setattr(
        baselinesvc, "describe",
        lambda path: {**real(path), "mount_source": "s\x1b[2J", "mount_point": "/m\x1b[H"},
    )  # fmt: skip
    asked: list[str] = []
    monkeypatch.setattr(clone, "confirm", lambda q, *, tty: asked.append(q) or False)
    w.cli(capsys, "baseline", "accept", rec["id"], human=True)
    assert len(asked) == 1 and "\x1b" not in asked[0] and "\\x1b[2J" in asked[0]


# --- F10: one transaction, the right clone row, healing --------------------------------------


def clone_row(w: CW) -> dict:
    return dict(w.rows("SELECT * FROM clones")[0])


def test_a_failure_writing_the_clone_row_rolls_the_whole_accept_back(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    set_col(w, rec["id"], common_id="3:4")
    conn = sqlite3.connect(w.db)
    conn.execute("UPDATE anchors SET dev_ino = '1:2' WHERE path = ?", (rec["base_dir"],))
    conn.commit()
    conn.close()
    w.g(["config", "core.fsmonitor", "/bin/true"])

    def boom(self, c, repo, common_dir, common_id):  # noqa: ANN001, ANN202
        raise sqlite3.OperationalError("injected")

    real = State._rebind_clone
    monkeypatch.setattr(State, "_rebind_clone", boom)
    r = w.cli(capsys, "baseline", "accept", rec["id"], "--yes", human=True)
    assert r.code == 1
    now = row(w, rec["id"])
    assert now["baseline_digest"] == rec["baseline_digest"] and now["common_id"] == "3:4"
    assert w.rows("SELECT dev_ino FROM anchors WHERE path = ?", rec["base_dir"])[0][0] == "1:2"
    monkeypatch.setattr(State, "_rebind_clone", real)
    good_accept(w.cli(capsys, "baseline", "accept", rec["id"], "--yes", human=True))
    assert row(w, rec["id"])["common_id"] == rec["common_id"]


def test_accept_leaves_another_clones_row_alone(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    set_col(w, rec["id"], common_id="3:4")
    conn = sqlite3.connect(w.db)
    conn.execute("UPDATE clones SET common_dir = '/somewhere/else/.git', common_id = '8:9'")
    conn.commit()
    conn.close()
    before = clone_row(w)
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    assert clone_row(w) == before
    assert row(w, rec["id"])["common_id"] == rec["common_id"]


def test_a_stale_clone_row_counts_as_moved_and_is_healed(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    conn = sqlite3.connect(w.db)
    conn.execute("UPDATE clones SET common_id = '8:9'")  # the crash window's leftover
    conn.commit()
    conn.close()
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    assert clone_row(w)["common_id"] == rec["common_id"]
    state = w.rows("SELECT baseline_digest FROM records")[0][0]
    assert state == rec["baseline_digest"]


# --- F11: a changed hooksPath is a new anchor, not a moved one --------------------------------


def test_a_changed_hooks_path_is_not_a_moved_anchor(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    a, b = w.tmp / "hooks-a", w.tmp / "hooks-b"
    a.mkdir()
    b.mkdir()
    w.g(["config", "core.hooksPath", str(a)])
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    w.g(["config", "core.hooksPath", str(b)])
    real = baselinesvc.describe
    monkeypatch.setattr(baselinesvc, "describe", lambda p: {**real(p), "fs_type": "fuse.sshfs"})
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))  # no fuse_anchor refusal
    assert not [x for x in data["anchors"] if x["anchor"] == "hooks_path"]
    monkeypatch.setattr(baselinesvc, "describe", real)
    good_check(check(w, capsys, rec["id"]))


def test_check_reports_a_changed_hooks_path_as_added_not_as_a_mount_point(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    a, b = w.tmp / "hooks-a", w.tmp / "hooks-b"
    a.mkdir()
    b.mkdir()
    w.g(["config", "core.hooksPath", str(a)])
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    w.g(["config", "core.hooksPath", str(b)])
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert not any(i.get("detail") == "mount_point" for i in e["items"])


# --- F12: usage errors win over the actor gate -----------------------------------------------


def test_a_malformed_expect_digest_is_exit_2_even_for_a_non_human(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    for who in (False, True):
        err_of(accept(w, capsys, rec["id"], "--yes", "--expect-digest", "XYZ", human=who), 2)


# --- F13: FUSE before the computation --------------------------------------------------------


def test_a_fuse_anchor_is_refused_before_anything_is_computed(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    set_col(w, rec["id"], dir_id="1:2")
    real = baselinesvc.describe
    monkeypatch.setattr(baselinesvc, "describe", lambda p: {**real(p), "fs_type": "fuse.sshfs"})

    def not_called(spec):  # noqa: ANN001, ANN202
        raise AssertionError("computed before the FUSE check")

    monkeypatch.setattr(baselinesvc, "compute", not_called)
    e = err_of(accept(w, capsys, rec["id"], "--yes"), 20, "unlinked")
    assert e["details"]["cause"] == "fuse_anchor"


# --- F19: nothing changed means nothing happens ----------------------------------------------


def test_accept_with_nothing_changed_fires_no_hook_and_writes_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    from tests.createlib import script

    fired = w.tmp / "fired"
    w.top["hooks"] = {"post-baseline-accept": [{"cmd": script(w.tmp / "h.sh", f"touch {fired}")}]}
    rec = made(w, capsys)

    def snapshot() -> dict:
        conn = sqlite3.connect(w.db)
        try:
            return {
                t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
                for t in ("records", "baseline_items", "anchors", "clones")
            }
        finally:
            conn.close()

    before = snapshot()
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))
    assert data["items"] == [] and not fired.exists()
    assert snapshot() == before


# --- F6: a bootstrap re-run whose new baseline is incomplete --------------------------------


def test_bootstrap_rerun_with_an_incomplete_new_baseline_refuses_and_keeps_the_old_one(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    import sys

    code = (
        "import os; os.makedirs('.claude/bulk', exist_ok=True)\n"
        "for i in range(400): open(f'.claude/bulk/f{i}', 'w').close()\n"
    )
    w.repo_cfg["bootstrap"] = {"steps": [{"argv": [sys.executable, "-c", code]}], "auto": False}
    rec = made(w, capsys)
    w.top["remove"] = {"nested_scan_limit": 200}  # fits the baseline now, not after the step
    good_check(check(w, capsys, rec["id"]))
    r = w.cli(capsys, "bootstrap", rec["id"], human=True)
    e = err_of(r, 20, "repo_config_changed")
    assert e["items"] and all(i.get("detail") == "incomplete" for i in e["items"])
    assert all(i["before"] != "absent" or i["after"] != "absent" for i in e["items"])
    assert row(w, rec["id"])["baseline_digest"] == rec["baseline_digest"]
