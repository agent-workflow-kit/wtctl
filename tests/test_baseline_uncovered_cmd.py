"""Cross-device symlink targets through the commands: ``create``, ``adopt``, ``baseline check`` and
``accept``, ``status`` and the ``remove`` class split (docs/interface.md, Baseline; issue #68)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.adoptlib import adopt, foreign, ok_adopt
from tests.createlib import CW
from tests.schemacheck import validate
from tests.test_baseline_cmd import accept, check, err_of, good_accept, good_check, made
from wtctl import baselinesvc, repobaseline

KEY = "symlink-target"
UNCOVERED = "symlink_uncovered"

pytestmark = pytest.mark.skipif(not os.path.isdir("/dev/shm"), reason="needs /dev/shm")


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    world = CW(tmp_path, private_home).build()
    if os.stat("/dev/shm").st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("/dev/shm shares the device")
    return world


@pytest.fixture
def shm() -> Iterator[Path]:
    d = Path("/dev/shm") / f"wtctl-uncmd-{os.getpid()}-{os.urandom(4).hex()}"
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def relink(link: Path, target: str | Path) -> None:
    if link.is_symlink():
        link.unlink()
    os.symlink(target, link)


def test_a_new_cross_device_link_is_a_change_until_accepted_and_then_a_notice(
    w: CW,
    capsys,
    shm: Path,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    (shm / "dots.md").write_text("one")
    os.symlink(shm / "dots.md", Path(rec["path"], "CLAUDE.md"))
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")  # a new item, as any link
    assert [i["after"]["type"] for i in e["items"] if i["key"] == KEY] == [UNCOVERED]
    assert not any(i.get("detail") for i in e["items"])  # not a mount_point refusal
    data = good_accept(accept(w, capsys, rec["id"], "--yes"))  # accept is not blocked
    assert [i["after"]["type"] for i in data["items"] if i["key"] == KEY] == [UNCOVERED]
    checked = good_check(check(w, capsys, rec["id"]))
    assert checked["uncovered"] == ["CLAUDE.md"]
    validate("wtctl/cli/baseline-check.json", checked)
    assert "CLAUDE.md" in check(w, capsys, rec["id"]).err
    # stored without a detail, as an ordinary item
    kinds = {
        r["kind"] for r in w.rows("SELECT kind FROM baseline_items WHERE record_id = ?", rec["id"])
    }
    assert UNCOVERED in kinds


def test_retargeting_the_link_is_detected_and_content_behind_it_is_not(
    w: CW,
    capsys,
    shm: Path,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    link = Path(rec["path"], "CLAUDE.md")
    (shm / "a.md").write_text("one")
    (shm / "b.md").write_text("one")
    os.symlink(shm / "a.md", link)
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    (shm / "a.md").write_text("tampered")
    good_check(check(w, capsys, rec["id"]))  # the documented gap
    relink(link, shm / "b.md")
    e = err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")
    assert [i["path"] for i in e["items"] if i["key"] == KEY] == ["CLAUDE.md"]
    assert "tampered" not in str(e)


def test_a_link_to_dev_null_is_covered_and_has_no_notice(w: CW, capsys) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    hook = w.clone / ".git" / "hooks" / "pre-commit"
    os.symlink("/dev/null", hook)
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    data = good_check(check(w, capsys, rec["id"]))
    assert "uncovered" not in data
    os.unlink(hook)
    (shm_file := w.tmp / "h").write_text("#!/bin/sh\n")
    os.symlink(shm_file, hook)
    err_of(check(w, capsys, rec["id"]), 20, "repo_config_changed")  # no longer disabled


def test_accept_names_the_uncovered_paths_in_the_prompt(
    w: CW,
    capsys,
    shm: Path,  # noqa: ANN001
) -> None:
    rec = made(w, capsys)
    os.symlink(shm / "x.md", Path(rec["path"], "CLAUDE.md"))
    r = accept(w, capsys, rec["id"], "--yes")
    good_accept(r)
    q = baselinesvc_question(w, rec)
    assert "not covered" in q and "CLAUDE.md" in q


def baselinesvc_question(w: CW, rec: dict) -> str:
    from wtctl.commands import baseline_accept

    row = {
        "class": "claude", "path": "CLAUDE.md", "key": KEY, "before": "absent",
        "after": {"type": UNCOVERED, "sha256": "0" * 64},
    }  # fmt: skip
    snap = baseline_accept.Snapshot(None, [], None, [row], False)  # type: ignore[arg-type]
    return baseline_accept._question({"name": rec["name"]}, snap)


def test_status_warns_and_the_baseline_check_passes(w: CW, capsys, shm: Path) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    os.symlink(shm / "x.md", Path(rec["path"], "CLAUDE.md"))
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    r = w.cli(capsys, "status", rec["id"])
    assert r.code == 0, (r.out, r.err)
    assert "CLAUDE.md" in r.out and "another device" in r.out
    assert r.json()["checks"]["baseline"]["status"] == "pass"


def test_remove_class_split_for_uncovered_links(w: CW, capsys, shm: Path) -> None:  # noqa: ANN001
    rec = made(w, capsys)
    claude = Path(rec["path"], "CLAUDE.md")
    hook = w.clone / ".git" / "hooks" / "pre-commit"
    (shm / "a").write_text("a")
    (shm / "b").write_text("b")
    os.symlink(shm / "a", claude)
    os.symlink(shm / "a", hook)
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    # content behind the targets changed: undetectable, the baseline still passes
    (shm / "a").write_text("changed")
    r = w.cli(capsys, "status", rec["id"])
    assert r.json()["checks"]["baseline"]["status"] == "pass"
    # a claude-class link retargeted: a warning
    relink(claude, shm / "b")
    r = w.cli(capsys, "status", rec["id"])
    assert r.json()["checks"]["baseline"]["status"] == "warn"
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    # a git-class link retargeted: a failure
    relink(hook, shm / "b")
    r = w.cli(capsys, "status", rec["id"])
    body = r.json().get("error", r.json())
    assert body["checks"]["baseline"]["status"] == "fail"


def test_adopt_records_a_cross_device_link_without_refusing(w: CW, capsys, shm: Path) -> None:  # noqa: ANN001
    path = foreign(w)
    os.symlink(shm / "x.md", path / "CLAUDE.md")
    rec = ok_adopt(adopt(w, capsys, path))["record"]
    assert good_check(check(w, capsys, rec["id"]))["uncovered"] == ["CLAUDE.md"]


def test_create_records_a_tracked_cross_device_link_in_the_baseline(
    w: CW,
    capsys,
    shm: Path,  # noqa: ANN001
) -> None:
    """A bootstrap re-run (which recomputes the baseline) keeps it recorded and unchanged."""
    from tests.test_bootstrap_cmd import good, rerun

    rec = made(w, capsys)
    os.symlink(shm / "x.md", Path(rec["path"], "CLAUDE.md"))
    good_accept(accept(w, capsys, rec["id"], "--yes"))
    good(rerun(w, capsys, rec["id"]))
    assert good_check(check(w, capsys, rec["id"]))["uncovered"] == ["CLAUDE.md"]


def test_routine_git_leaves_the_digest_with_uncovered_links(w: CW, capsys, shm: Path) -> None:  # noqa: ANN001
    a = made(w, capsys, "--name", "one")
    os.symlink(shm / "x.md", Path(a["path"], "CLAUDE.md"))
    good_accept(accept(w, capsys, a["id"], "--yes"))
    b = made(w, capsys, "--name", "two")
    wt = Path(a["path"])
    w.g(["push", "-q", "-u", "origin", "wt/one"], wt)
    w.g(["branch", "--set-upstream-to=origin/main", "wt/one"], wt)
    w.g(["worktree", "remove", "--force", b["path"]], w.clone)
    good_check(check(w, capsys, a["id"]))
