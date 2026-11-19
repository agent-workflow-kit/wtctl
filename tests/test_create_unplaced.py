"""A mount placed between create's mount-point check and the ``dir_id`` open (issue #83;
docs/design.md D62, docs/interface.md Create order step 13, Reconcile, Remove order).

The record never stays ``creating`` without a way out: create settles it ``partial`` with the admin
entry and the creation sha, reconcile does the same for a record an older version left ``creating``,
``remove`` clears only the admin entry and the branch (never the directory), ``forget`` drops the
record. The mount itself is simulated by seams here; ``test_create_unplaced_userns`` uses real tmpfs
mounts.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from tests.createlib import REPO
from tests.schemacheck import validate
from tests.throwlib import TW
from wtctl import baselinesvc, linkcheck, reconcile_probe, repobaseline, state
from wtctl.commands.create import Creator
from wtctl.errors import WtctlError


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> TW:
    return TW(tmp_path, private_home).build()


def db_set(w: TW, rid: str, **cols: object) -> None:
    conn = sqlite3.connect(w.db)
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE records SET {sets} WHERE id = ?", [*cols.values(), rid])
    conn.commit()
    conn.close()


def covered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The planned path is a mount point for everything that opens it by its recorded location."""

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise WtctlError("refused", "not the recorded worktree", reason="unlinked")

    monkeypatch.setattr(linkcheck, "verify_links", boom)
    monkeypatch.setattr(reconcile_probe, "planted", lambda st, rec: ("mount", None))


def mount_during_create(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Creator, "_on_another_mount", staticmethod(lambda p, c: True))


def made(w: TW, monkeypatch: pytest.MonkeyPatch) -> dict:
    with monkeypatch.context() as m:
        mount_during_create(m)
        r = w.run("create", "--repo", REPO, "--name", "one", "--no-bootstrap")
    assert r.code == 12, (r.out, r.err)
    return r.json()["error"]


def branches(w: TW) -> list[str]:
    return [k for k in w.refs() if k.startswith("refs/heads/wt/")]


def test_create_settles_the_record_partial_and_names_what_is_left(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    err = made(w, monkeypatch)
    validate("wtctl/cli/error.json", {"error": err})
    [rec] = w.records()
    assert err["details"]["cause"] == "anchor_changed" and err["details"]["record"] == rec["id"]
    assert rec["state"] == "partial" and rec["dir_id"] is None
    assert rec["admin_dir"] and rec["head_at_create"] == w.shas["c3"]
    admin, ref, path = err["details"]["remaining"]
    assert admin == rec["admin_dir"] and ref == branches(w)[0] and path == rec["path"]
    assert "wtctl remove" in err["message"]
    assert w.admin_entries() and branches(w) and (Path(rec["path"]) / ".git").is_file()


def test_create_without_an_admin_entry_keeps_the_record_creating(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("wtctl.worktrees.admin_for_path", lambda c, p: None)
    err = made(w, monkeypatch)
    assert "remaining" not in err["details"]
    assert w.records()[0]["state"] == "creating"


def test_a_failing_settle_keeps_the_record_creating_and_says_so(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(self, *a, **k):  # noqa: ANN001, ANN002, ANN003, ANN202
        raise WtctlError("busy", "boom")

    monkeypatch.setattr(state.State, "transition", fail)
    mount_during_create(monkeypatch)
    r = w.run("create", "--repo", REPO, "--name", "one", "--no-bootstrap")
    assert r.code == 12
    assert w.records()[0]["state"] == "creating"
    assert "could not be made `partial`" in r.err or "could not be made `partial`" in r.out


def test_remove_clears_the_admin_entry_and_the_branch_and_leaves_the_mount(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    (Path(rec["path"]) / "precious").write_text("x")
    covered(monkeypatch)
    r = w.run("remove", rec["id"], "--yes", human=True)
    assert r.code != 0
    err = r.json()["error"]
    validate("wtctl/cli/error.json", r.json())
    assert err["code"] == "partial" and err["details"]["remaining"] == [rec["path"]]
    assert w.admin_entries() == [] and branches(w) == []
    assert (Path(rec["path"]) / "precious").read_text() == "x"
    again = w.records()[0]
    assert again["state"] == "partial" and "wtctl forget" in again["last_error"]


def test_remove_by_an_agent_is_refused_as_for_any_partial_record(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    covered(monkeypatch)
    r = w.run("remove", rec["id"], "--yes")
    assert r.code != 0 and w.admin_entries() and branches(w)


def test_remove_is_idempotent_and_forget_clears_the_record(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    covered(monkeypatch)
    assert w.run("remove", rec["id"], "--yes", human=True).code != 0
    assert w.run("remove", rec["id"], "--yes", human=True).code != 0  # nothing left to clear
    f = w.run("forget", rec["id"], human=True)
    assert f.code == 0, (f.out, f.err)
    assert w.records()[0]["state"] == "forgotten"
    assert Path(rec["path"]).is_dir()


def test_remove_finishes_the_record_when_the_directory_is_gone(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    shutil.rmtree(rec["path"])  # the files lived in the mount and vanished with it
    r = w.run("remove", rec["id"], "--yes", human=True)
    assert r.code == 0, (r.out, r.err)
    validate("wtctl/cli/remove.json", r.json())
    assert w.records()[0]["state"] == "removed"
    assert w.admin_entries() == [] and branches(w) == []
    assert r.json()["branch_deleted"] is True


def test_remove_after_the_mount_is_gone_goes_on_as_a_normal_remove(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    r = w.run("remove", rec["id"], "--yes", human=True)  # the directory is visible and verifiable
    assert r.code == 0, (r.out, r.err)
    row = w.records()[0]
    assert row["state"] == "removed" and row["dir_id"] is not None
    assert not Path(rec["path"]).exists() and w.admin_entries() == []
    validate("wtctl/cli/show.json", w.run("show", rec["id"]).json())  # null baseline_digest
    for a in w.audit_records():
        validate("wtctl/audit.json", a)


def test_the_branch_is_kept_when_it_moved(w: TW, monkeypatch: pytest.MonkeyPatch) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    ref = branches(w)[0]
    w.g(["update-ref", ref, w.shas["c2"]])
    covered(monkeypatch)
    assert w.run("remove", rec["id"], "--yes", human=True).code != 0
    assert branches(w) == [ref] and w.admin_entries() == []


def test_a_branch_this_create_did_not_make_is_kept(w: TW, monkeypatch: pytest.MonkeyPatch) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    db_set(w, rec["id"], created_branch=0)
    covered(monkeypatch)
    assert w.run("remove", rec["id"], "--yes", human=True).code != 0
    assert branches(w) and w.admin_entries() == []


def finish_gone(w: TW, rec: sqlite3.Row, *argv: str) -> dict:
    """The files lived in the mount and vanished with it: remove finishes the record."""
    shutil.rmtree(rec["path"])
    r = w.run("remove", rec["id"], "--yes", *argv, human=True)
    assert r.code == 0, (r.out, r.err)
    return r.json()


def test_the_branch_is_kept_with_keep_branch(w: TW, monkeypatch: pytest.MonkeyPatch) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    out = finish_gone(w, rec, "--keep-branch")
    assert out["branch_deleted"] is False and out["branch_kept_reason"] == "keep_branch"
    assert branches(w)


def test_the_branch_is_kept_when_deleting_branches_is_disabled(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    w.top["remove"] = {"delete_branch": False}
    out = finish_gone(w, rec)
    assert out["branch_deleted"] is False and out["branch_kept_reason"] == "disabled"
    assert branches(w)


def test_the_branch_is_kept_when_another_worktree_has_it(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    w.g(["worktree", "add", "--force", str(w.tmp / "other"), rec["branch"]])
    shutil.rmtree(rec["path"])
    r = w.run("remove", rec["id"], "--yes", human=True)
    assert r.code == 0, (r.out, r.err)
    assert r.json()["branch_kept_reason"] == "checked_out_elsewhere" and branches(w)


def test_a_reflog_without_its_creation_entry_never_lets_remove_delete_the_branch(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = old_creating(w, monkeypatch)
    log = Path(rec["common_dir"]) / "worktrees"
    [entry] = [p for p in log.iterdir() if (p / "logs" / "HEAD").exists()]
    f = entry / "logs" / "HEAD"
    lines = f.read_text().splitlines(keepends=True)
    f.write_text("".join(lines[1:]) or f"{'1' * 40} {w.shas['c3']} x <x> 0 +0000\tcommit: more\n")
    covered(monkeypatch)
    w.run("reconcile")
    row = w.records()[0]
    assert row["state"] == "partial" and not row["created_branch"]
    w.run("remove", rec["id"], "--yes", human=True)
    assert branches(w)


def test_the_audit_line_of_an_unplaced_remove_is_valid(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    finish_gone(w, rec)
    lines = w.audit_records()
    assert lines
    for a in lines:
        validate("wtctl/audit.json", a)


def test_status_says_an_unplaced_record_would_be_removed(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    covered(monkeypatch)
    out = w.run("remove", rec["id"], "--dry-run", human=True).json()
    assert out["would_remove"] is True, out


def test_a_locked_admin_entry_is_never_cleared(w: TW, monkeypatch: pytest.MonkeyPatch) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    (Path(rec["admin_dir"]) / "locked").write_text("keep\n")
    covered(monkeypatch)
    r = w.run("remove", rec["id"], "--yes", human=True)
    assert r.code != 0 and w.admin_entries() and branches(w)


def test_an_admin_entry_naming_another_path_is_left(w: TW, monkeypatch: pytest.MonkeyPatch) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    (Path(rec["admin_dir"]) / "gitdir").write_text("/elsewhere/.git\n")
    covered(monkeypatch)
    w.run("remove", rec["id"], "--yes", human=True)
    assert w.admin_entries()  # foreign: never deleted


def test_remove_without_confirmation_changes_nothing(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    covered(monkeypatch)
    r = w.run("remove", rec["id"], human=True)  # no --yes, no terminal
    assert r.code != 0 and w.admin_entries() and branches(w)


def test_a_scanner_that_cannot_tell_refuses_and_changes_nothing(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    covered(monkeypatch)
    w.scanner.fail = True
    r = w.run("remove", rec["id"], "--yes", human=True)
    assert r.code != 0 and w.admin_entries() and branches(w)


def test_a_failing_admin_deletion_stops_before_the_branch(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import removal_delete

    made(w, monkeypatch)
    [rec] = w.records()
    covered(monkeypatch)
    failed = ("failed", [rec["admin_dir"]])
    monkeypatch.setattr(removal_delete, "delete_admin", lambda *a, **k: failed)
    r = w.run("remove", rec["id"], "--yes", human=True)
    assert r.json()["error"]["code"] == "partial"
    assert branches(w) and w.records()[0]["state"] == "partial"


def test_a_branch_that_moves_after_the_plan_is_kept(w: TW, monkeypatch: pytest.MonkeyPatch) -> None:
    from wtctl import worktrees

    made(w, monkeypatch)
    [rec] = w.records()
    covered(monkeypatch)
    ref = branches(w)[0]
    real = worktrees.delete_created_branch

    def moved(git, common, branch, sha):  # noqa: ANN001, ANN202
        w.g(["update-ref", ref, w.shas["c2"]])
        return real(git, common, branch, sha)

    monkeypatch.setattr(worktrees, "delete_created_branch", moved)
    w.run("remove", rec["id"], "--yes", human=True)
    assert branches(w) == [ref]  # the compare-and-swap lost


def test_status_list_and_show_work_on_an_unplaced_record(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    made(w, monkeypatch)
    [rec] = w.records()
    covered(monkeypatch)
    for argv in (("status", rec["id"]), ("remove", rec["id"], "--dry-run"), ("show", rec["id"])):
        r = w.run(*argv, human=True)
        assert r.code == 0, (argv, r.out, r.err)
    assert w.run("list", "--all").code == 0
    assert w.admin_entries() and branches(w)  # read-only


# -- reconcile --------------------------------------------------------------------------------


def old_creating(w: TW, monkeypatch: pytest.MonkeyPatch) -> dict:
    """What an older version left: ``creating``, no dir_id, the admin entry and the branch."""
    made(w, monkeypatch)
    [rec] = w.records()
    db_set(w, rec["id"], state="creating", admin_dir=None, head_at_create=None)
    return dict(w.records()[0])


def test_reconcile_settles_a_creating_record_under_a_mount_as_partial(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = old_creating(w, monkeypatch)
    covered(monkeypatch)
    out = w.run("reconcile").json()
    validate("wtctl/cli/reconcile.json", out)
    assert [(c["id"], c["to"], c["action"]) for c in out["changes"]] == [
        (rec["id"], "partial", "abandon_unplaced")
    ]
    row = w.records()[0]
    assert row["state"] == "partial" and row["dir_id"] is None
    assert row["admin_dir"] and row["head_at_create"] == w.shas["c3"]
    assert w.admin_entries() and branches(w)  # reconcile deletes nothing
    assert Path(rec["path"], ".git").is_file()


def test_reconcile_dry_run_settles_nothing(w: TW, monkeypatch: pytest.MonkeyPatch) -> None:
    old_creating(w, monkeypatch)
    covered(monkeypatch)
    out = w.run("reconcile", "--dry-run").json()
    assert out["changes"] and w.records()[0]["state"] == "creating"


def test_reconcile_then_remove_finishes_the_record(w: TW, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = old_creating(w, monkeypatch)
    with monkeypatch.context() as m:
        covered(m)
        w.run("reconcile")
    shutil.rmtree(rec["path"])
    assert w.run("remove", rec["id"], "--yes", human=True).code == 0
    assert w.admin_entries() == [] and branches(w) == []


def test_reconcile_without_the_admin_entry_leaves_the_record_as_it_was(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_creating(w, monkeypatch)
    covered(monkeypatch)
    shutil.rmtree(w.clone / ".git" / "worktrees")
    out = w.run("reconcile").json()
    assert out["changes"] == [] and w.records()[0]["state"] == "creating"
    assert [r["kind"] for r in out["reports"]] == ["abandoned"]


def test_reconcile_without_the_creation_sha_never_lets_remove_delete_the_branch(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = old_creating(w, monkeypatch)
    covered(monkeypatch)
    monkeypatch.setattr("wtctl.worktrees.reflog_first", lambda admin: None)
    w.run("reconcile")
    row = w.records()[0]
    assert row["state"] == "partial" and not row["created_branch"]
    w.run("remove", rec["id"], "--yes", human=True)
    assert branches(w)


def test_a_partial_record_whose_planted_path_is_not_a_mount_is_not_settled_by_reconcile(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle is only for a mount: another directory at the path is a human's (`forget`)."""
    old_creating(w, monkeypatch)
    monkeypatch.setattr(reconcile_probe, "planted", lambda st, rec: ("other", None))
    out = w.run("reconcile").json()
    assert out["changes"] == [] and w.records()[0]["state"] == "creating"


# -- real mounts under a user namespace ---------------------------------------------------------


def _userns(tmp_path: Path, script: str, *args: str) -> dict:
    import json
    import os
    import subprocess
    import sys

    unshare = shutil.which("unshare")
    if unshare is None:
        pytest.skip("no unshare")
    probe = tmp_path / "probe"
    probe.mkdir()
    ok = subprocess.run(
        [unshare, "-Urm", "mount", "-t", "tmpfs", "none", str(probe)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if ok.returncode != 0:
        pytest.skip("unprivileged user+mount namespaces are unavailable here")
    work = tmp_path / "ns"
    work.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(work)}
    r = subprocess.run(
        [unshare, "-Urm", sys.executable, str(Path(__file__).with_name(script)), str(work), *args],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("variant", ["after", "before"])
def test_a_real_mount_between_the_check_and_the_dir_id_open(tmp_path: Path, variant: str) -> None:
    out = _userns(tmp_path, "unplaced_userns_scenario.py", variant)
    err = out["create"]["error"]
    assert out["create"]["code"] == 12 and err["details"]["cause"] == "anchor_changed"
    assert len(err["details"]["remaining"]) == 3
    # create: settled, nothing deleted
    c = out["after_create"]
    assert c["state"] == "partial" and c["dir_id"] is None
    assert len(c["admin"]) == 1 and len(c["branches"]) == 1
    # reconcile finds nothing more to settle and deletes nothing
    assert out["reconcile"]["changes"] == [] and out["after_reconcile"] == c
    # remove: the admin entry and the branch go, the mount's content is untouched
    assert out["remove"]["code"] == 1 and out["remove"]["error"]["code"] == "partial"
    r = out["after_remove"]
    assert r["state"] == "partial" and r["admin"] == [] and r["branches"] == []
    assert r["mount_entries"] == c["mount_entries"]
    if variant == "before":
        assert ".git" in r["mount_entries"]  # files wtctl made in a foreign mount stay
    # repeating changes nothing; forget clears the record and still touches nothing on disk
    assert out["remove_again"]["code"] == 1 and out["after_remove_again"] == r
    assert out["forget"]["code"] == 0 and out["after_forget"]["state"] == "forgotten"
    assert out["after_forget"]["mount_entries"] == c["mount_entries"]


def test_a_real_mount_that_is_gone_before_remove_lets_it_finish_normally(tmp_path: Path) -> None:
    out = _userns(tmp_path, "unplaced_userns_unmount_scenario.py")
    assert out["create"] == 12 and out["state_before"] == "partial"
    assert out["remove"] == 0 and out["state"] == "removed"
    assert out["admin"] == [] and out["branches"] == [] and out["dir_exists"] is False
