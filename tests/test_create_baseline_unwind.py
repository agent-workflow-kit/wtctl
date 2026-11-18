"""A create whose baseline cannot be recorded unwinds what it made (issue #77; docs/interface.md,
Create order step 15, Reconcile)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.createlib import CW
from tests.schemacheck import validate
from wtctl import baselinesvc, repobaseline

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
    d = Path("/dev/shm") / f"wtctl-unw-{os.getpid()}-{os.urandom(4).hex()}"
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def bad_hook(w: CW, shm: Path) -> Path:
    """A hook that is a symlink to another device: a refused position (`mount_point`)."""
    (shm / "h").write_text("#!/bin/sh\n")
    link = w.clone / ".git" / "hooks" / "post-commit"
    os.symlink(shm / "h", link)
    return link


def err(r, code: int = 12) -> dict:  # noqa: ANN001
    assert r.code == code, (r.out, r.err)
    validate("wtctl/cli/error.json", r.json())
    return r.json()["error"]


def test_create_unwinds_and_names_the_blocking_position(w: CW, capsys, shm: Path) -> None:  # noqa: ANN001
    before = w.snapshot()
    link = bad_hook(w, shm)
    before["refs"] = w.refs()
    e = err(w.create(capsys, "--name", "one"))
    assert e["code"] == "create_failed"
    assert e["details"]["cause"] == "baseline_incomplete"
    assert e["details"]["unwound"] is True
    assert e["details"]["positions"][0]["detail"] == "mount_point"
    assert "post-commit" in e["details"]["positions"][0]["path"]
    assert "post-commit" in e["message"] and "symlink" in e["message"]
    snap = w.snapshot()
    assert snap["admin"] == [] and snap["refs"] == before["refs"]
    assert snap["worktrees"] == before["worktrees"]
    assert not (w.root / "worktrees" / "example.test" / "org" / "repo" / "one").exists()
    assert w.records() == [] or all(r["state"] != "creating" for r in w.records())
    assert w.count("records") == 0
    assert w.count("ids") == 1  # the id is burned
    assert "branch.wt/one" not in snap["config"]
    link.unlink()
    again = w.create(capsys, "--name", "one")
    assert again.code == 0, (again.out, again.err)
    assert again.json()["record"]["state"] == "active"


def test_unwind_keeps_a_preexisting_branch_and_deletes_only_a_created_one(
    w: CW,
    capsys,
    shm: Path,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)
    tip = w.refs()["refs/heads/local-b"]
    e = err(w.create(capsys, "--branch", "local-b"))
    assert e["details"]["unwound"] is True
    assert w.refs()["refs/heads/local-b"] == tip  # not made by this create
    assert w.admin_entries() == []


def test_unwind_keeps_a_remote_tracking_branch_and_deletes_the_new_local_one(
    w: CW,
    capsys,
    shm: Path,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)
    before = w.refs()
    e = err(w.create(capsys, "--branch", "feature/remote-only"))
    assert e["details"]["unwound"] is True
    assert w.refs() == before  # the local branch this create made is gone, nothing else moved
    assert "feature/remote-only" not in w.g(["config", "--local", "--list"])


def test_unwind_deletes_an_empty_preexisting_directory_with_the_worktree(
    w: CW,
    capsys,
    shm: Path,  # noqa: ANN001
) -> None:
    bad_hook(w, shm)
    target = w.root / "worktrees" / "example.test" / "org" / "repo" / "one"
    target.mkdir(parents=True)
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["unwound"] is True
    assert not (target / ".git").exists() and w.admin_entries() == []


def test_a_process_in_the_worktree_keeps_it_and_the_record_becomes_partial(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl import inuse

    bad_hook(w, shm)
    real = inuse.check_in_use

    class Busy:
        status = "fail"
        processes = [{"pid": 1}]
        warnings: list[str] = []

    monkeypatch.setattr(inuse, "check_in_use", lambda *a, **k: Busy())
    e = err(w.create(capsys, "--name", "one"))
    monkeypatch.setattr(inuse, "check_in_use", real)
    assert e["details"]["unwound"] is False
    rec = w.records()[0]
    assert rec["state"] == "partial" and "baseline" in rec["last_error"]
    assert e["details"]["record"] == rec["id"]
    assert (Path(rec["path"]) / ".git").exists()


def test_a_directory_swapped_for_a_symlink_deletes_nothing_outside(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl import removal_delete

    bad_hook(w, shm)
    victim = w.tmp / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("precious")
    real = removal_delete.delete_worktree
    path = w.root / "worktrees" / "example.test" / "org" / "repo" / "one"

    def swap(state, record, budget):  # noqa: ANN001, ANN202
        shutil.move(str(path), str(w.tmp / "moved-away"))
        os.symlink(victim, path)
        return real(state, record, budget)

    monkeypatch.setattr(removal_delete, "delete_worktree", swap)
    e = err(w.create(capsys, "--name", "one"))
    assert (victim / "keep.txt").read_text() == "precious"
    assert e["details"]["unwound"] is False
    assert w.records()[0]["state"] == "partial"


def test_a_failure_before_the_worktree_exists_is_unchanged(w: CW, capsys) -> None:  # noqa: ANN001
    r = w.create(capsys, "--base", "no-such-branch")
    assert r.code == 12 and w.count("records") == 0 and w.admin_entries() == []


# --- the other commands --------------------------------------------------------------------


def test_adopt_refuses_with_the_positions_and_records_nothing(w: CW, capsys, shm: Path) -> None:  # noqa: ANN001
    from tests.adoptlib import adopt, err_of, foreign

    path = foreign(w)
    bad_hook(w, shm)
    e = err_of(adopt(w, capsys, path), 12)
    assert e["code"] == "create_failed" and e["details"]["cause"] == "baseline_incomplete"
    assert "post-commit" in e["details"]["positions"][0]["path"]
    assert w.count("records") == 0 and (path / ".git").exists()


def test_a_bootstrap_rerun_leaves_the_active_record_as_it_was(w: CW, capsys, shm: Path) -> None:  # noqa: ANN001
    from tests.test_bootstrap_cmd import err_of, rerun
    from tests.test_create import ok

    rec = ok(w.create(capsys, "--name", "one"))["record"]
    digest = w.records()[0]["baseline_digest"]
    bad_hook(w, shm)
    err_of(rerun(w, capsys, rec["id"]), 20)
    row = w.records()[0]
    assert row["state"] == "active" and row["baseline_digest"] == digest


def test_the_unwind_runs_for_a_same_device_link_through_the_mount_id_seam(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl import gateway

    real = gateway.fd_mount_id
    if real(os.open(w.tmp, os.O_RDONLY)) is None:
        pytest.skip("no mount ids on this platform")
    (w.tmp / "bound").mkdir()
    (w.tmp / "bound" / "h").write_text("#!/bin/sh\n")
    link = w.clone / ".git" / "hooks" / "post-commit"
    os.symlink(w.tmp / "bound" / "h", link)

    def fake(fd: int) -> int | None:
        base = real(fd)
        try:
            where = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return base
        return (base or 0) + 4242 if where.startswith(str(w.tmp / "bound")) else base

    monkeypatch.setattr(gateway, "fd_mount_id", fake)
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["positions"][0]["detail"] == "mount_point" and e["details"]["unwound"]
    assert w.count("records") == 0 and w.admin_entries() == []
    link.unlink()
    assert w.create(capsys, "--name", "one").code == 0


def test_a_partial_leftover_of_a_create_is_finished_by_remove(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl import inuse

    bad_hook(w, shm)
    real = inuse.check_in_use

    class Busy:
        status = "fail"
        processes: list = []
        warnings: list[str] = []

    monkeypatch.setattr(inuse, "check_in_use", lambda *a, **k: Busy())
    err(w.create(capsys, "--name", "one"))
    monkeypatch.setattr(inuse, "check_in_use", real)
    rid = w.records()[0]["id"]
    r = w.cli(capsys, "remove", rid, "--yes", human=True)
    assert r.code == 0, (r.out, r.err)
    assert w.admin_entries() == [] and w.records()[0]["state"] == "removed"


def test_a_real_tmpfs_under_a_user_namespace(tmp_path: Path) -> None:
    import json
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
    script = Path(__file__).with_name("unwind_userns_scenario.py")
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    r = subprocess.run(
        [unshare, "-Urm", sys.executable, str(script), str(work)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["code"] == 12 and out["error"]["details"]["cause"] == "baseline_incomplete"
    assert out["error"]["details"]["unwound"] is True
    assert out["records"] == 0 and out["admin"] == [] and out["dir_exists"] is False
    assert out["retry_code"] == 0


def test_an_admin_entry_naming_another_path_deletes_nothing(
    w: CW,
    capsys,
    shm: Path,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl import removal_delete

    bad_hook(w, shm)
    real = removal_delete.admin_check

    def forged(state, record, budget):  # noqa: ANN001, ANN202
        (Path(record["admin_dir"]) / "gitdir").write_text("/elsewhere/.git\n")
        return real(state, record, budget)

    monkeypatch.setattr(removal_delete, "admin_check", forged)
    e = err(w.create(capsys, "--name", "one"))
    assert e["details"]["unwound"] is False
    rec = w.records()[0]
    assert rec["state"] == "partial" and (Path(rec["path"]) / ".git").exists()


def test_create_refuses_an_empty_directory_that_is_a_real_mount_point(tmp_path: Path) -> None:
    import json
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
    script = Path(__file__).with_name("mountroot_userns_scenario.py")
    env = {"PATH": os.environ["PATH"], "HOME": str(work)}
    r = subprocess.run(
        [unshare, "-Urm", sys.executable, str(script), str(work)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["code"] == 20 and out["error"]["reason"] == "path_exists"
    assert "mount point" in out["error"]["message"]
    assert out["records"] == 0 and out["admin"] == [] and out["entries"] == []
