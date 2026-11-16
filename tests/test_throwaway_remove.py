"""Throwaway removal (docs/interface.md, Throwaway removal): fd-based, no git, no confirmation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.removallib import FakeScanner
from tests.schemacheck import validate
from tests.throwlib import TW, leaks
from wtctl import fsafe, removal_delete
from wtctl.gateway import ProcessInfo


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> TW:
    return TW(tmp_path, private_home).build()


def mk(w: TW, *argv: str) -> dict[str, Any]:
    return w.made("--source", "local", *argv)


# -- the plain removal -------------------------------------------------------------------------


def test_removal_deletes_the_directory_and_marks_the_record(w: TW) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    data = w.removed(rec["id"])
    assert not p.exists() and w.troot.is_dir() and leaks(w.troot) == []
    assert data["record"]["state"] == "removed" and data["record"]["removed_at"]
    assert data["branch_deleted"] is False and data["branch_kept_reason"] is None
    assert data["overridden"] == [] and list(data["checks"]) == ["in_use"]
    assert data["checks"]["in_use"]["status"] == "pass"
    assert w.state_of(rec["id"]) == "removed" and w.leases() == []


def test_removal_is_by_id_prefix_or_path_and_needs_no_confirmation(w: TW) -> None:
    a, b = mk(w), mk(w)
    r = w.run("remove", a["path"])  # no --yes, no TTY, an automation actor
    assert r.code == 0, r.err
    r = w.run("remove", b["id"][:8], "--actor", "auto:other")
    assert r.code == 0, r.err
    assert leaks(w.troot) == []


def test_removal_runs_no_git_and_writes_no_audit_record(w: TW) -> None:
    from tests.createlib import Spy

    rec = mk(w)
    spy = Spy()
    w.git_runner = spy
    w.removed(rec["id"])
    assert [s for s in spy.subs() if s != "version"] == []
    assert w.audit_records() == []
    events = [e for e in w.log_events() if e["event"] == "throwaway_remove"]
    assert len(events) == 1 and events[0]["id"] == rec["id"] and events[0]["level"] == "info"


def test_removal_of_a_remote_throwaway_with_read_only_directories(w: TW) -> None:
    rec = w.made("--source", "remote")
    p = Path(rec["path"])
    (p / "ro" / "deeper").mkdir(parents=True)
    (p / "ro" / "deeper" / "f").write_text("x")
    (p / "ro" / "deeper").chmod(0o555)
    (p / "ro").chmod(0o555)
    (p / ".git" / "objects").chmod(0o555)  # git's own directories are read-only too
    p.chmod(0o500)
    w.removed(rec["id"])
    assert not p.exists()


def test_removal_never_follows_links_out_of_the_tree(w: TW, tmp_path: Path) -> None:
    outside = tmp_path / "keep"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "f").write_text("k")
    rec = mk(w)
    p = Path(rec["path"])
    (p / "dirlink").symlink_to(outside)
    (p / "filelink").symlink_to(outside / "sub" / "f")
    (p / "dangling").symlink_to(tmp_path / "nowhere")
    (p / "dirlink2").symlink_to(".")
    w.removed(rec["id"])
    assert (outside / "sub" / "f").read_text() == "k" and not p.exists()


def test_removal_handles_hostile_names(w: TW) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    names = ["a\nb", "-rf", "--no-preserve-root", " lead", "trail ", "é‮.txt", "x" * 200]
    for n in names:
        (p / n).write_text("x")
    (p / "d\nir").mkdir()
    (p / "d\nir" / ".git").mkdir()
    w.removed(rec["id"])
    assert not p.exists()


def test_a_missing_id_is_not_found_and_a_tombstone_is_exit_10(w: TW) -> None:
    w.failed(w.rm("wt-zzzzzzzz"), 10)
    rec = mk(w)
    w.removed(rec["id"])
    err = w.failed(w.rm(rec["id"]), 10)
    assert err["details"]["state"] == "removed"


@pytest.mark.parametrize(
    "flag", [["--merged-head", "1" * 40], ["--holder", "h"], ["--keep-branch"]]
)
def test_worktree_only_flags_are_usage_errors(w: TW, flag: list[str]) -> None:
    rec = mk(w)
    w.failed(w.rm(rec["id"], *flag), 2)
    assert Path(rec["path"]).is_dir() and w.state_of(rec["id"]) == "active"


def test_force_is_a_usage_error_for_a_throwaway_even_for_a_human(w: TW) -> None:
    rec = mk(w)
    w.failed(w.rm(rec["id"], "--force", human=True), 2)
    w.failed(w.rm(rec["id"], "--force"), 2)  # and without a human actor
    assert Path(rec["path"]).is_dir()


def test_a_creating_record_cannot_be_removed(w: TW) -> None:
    rec = mk(w)
    w.set_state(rec["id"], "creating")
    err = w.failed(w.rm(rec["id"]), 20)
    assert err["reason"] == "unlinked" and Path(rec["path"]).is_dir()


def test_dry_run_is_status_and_changes_nothing(w: TW) -> None:
    rec = mk(w)
    r = w.run("remove", rec["id"], "--dry-run")
    assert r.code == 0, r.err
    data = r.json()
    validate("wtctl/cli/remove.json", data)
    assert data["dry_run"] is True and data["would_remove"] is True and data["reason"] is None
    assert data["object_type"] == "throwaway" and data["human_required"] is False
    assert Path(rec["path"]).is_dir() and w.state_of(rec["id"]) == "active"


# -- in use ------------------------------------------------------------------------------------


def test_a_process_inside_refuses_removal(w: TW) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    w.scanner.procs = [ProcessInfo(4242, "vim", str(p / ".git"), ())]
    err = w.failed(w.rm(rec["id"]), 20)
    assert err["reason"] == "in_use" and err["processes"][0]["pid"] == 4242
    assert p.is_dir() and w.state_of(rec["id"]) == "active"
    w.scanner.procs = []
    w.removed(rec["id"])


def test_an_open_file_inside_refuses_removal(w: TW) -> None:
    rec = mk(w)
    w.scanner.procs = [ProcessInfo(7, "tail", "/", (str(Path(rec["path"]) / "c1.txt"),))]
    assert w.failed(w.rm(rec["id"]), 20)["reason"] == "in_use"


def test_an_unavailable_scanner_refuses_with_no_processes(w: TW) -> None:
    rec = mk(w)
    w.scanner.fail = True
    err = w.failed(w.rm(rec["id"]), 20)
    assert err["reason"] == "in_use" and err["processes"] == []
    assert Path(rec["path"]).is_dir()


def test_a_real_process_with_its_cwd_inside_refuses_and_then_lets_go(w: TW) -> None:
    w.real_scanner = True
    rec = mk(w)
    p = Path(rec["path"])
    (p / "sub").mkdir()
    # started by a shell that exits, so it is not a child of this process (wtctl's own children
    # are excluded from the check)
    out = subprocess.run(
        ["sh", "-c", 'cd "$0" && { sleep 60 >/dev/null 2>&1 </dev/null & echo $!; }',
         str(p / "sub")],
        capture_output=True, text=True, check=True, timeout=30,
    )  # fmt: skip
    pid = int(out.stdout.strip())
    try:
        time.sleep(0.3)
        err = w.failed(w.rm(rec["id"]), 20)
        assert err["reason"] == "in_use"
        assert pid in [x["pid"] for x in err["processes"]]
        assert p.is_dir()
    finally:
        # the exact pid we launched, after checking it is still our sleep
        cmdline = Path(f"/proc/{pid}/cmdline")
        if cmdline.exists() and cmdline.read_bytes().startswith(b"sleep\0"):
            os.kill(pid, 15)
    for _ in range(50):
        if not Path(f"/proc/{pid}").exists():
            break
        time.sleep(0.1)
    w.removed(rec["id"])
    assert not p.exists()


def test_a_process_that_appears_during_the_hooks_is_caught_by_the_recheck(
    w: TW, tmp_path: Path
) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    seen = tmp_path / "hook-ran"
    w.top["hooks"] = {"pre-remove": [{"cmd": f"touch {seen}"}]}

    class Late(FakeScanner):
        def scan(self, target: str | None = None) -> list[ProcessInfo]:
            if seen.exists():
                return [ProcessInfo(99, "late", str(p), ())]
            return []

    w.scanner = Late()
    assert w.failed(w.rm(rec["id"]), 20)["reason"] == "in_use"
    assert p.is_dir() and w.state_of(rec["id"]) == "active"


# -- hooks -------------------------------------------------------------------------------------


def test_hook_payloads_and_order(w: TW, tmp_path: Path) -> None:
    rec = mk(w, "--label", "run=1")
    pre, post = tmp_path / "pre.json", tmp_path / "post.json"
    w.top["hooks"] = {
        "pre-remove": [{"cmd": f"cat > {pre}; test -d {rec['path']}"}],
        "post-remove": [{"cmd": f"cat > {post}; test ! -e {rec['path']}"}],
    }
    w.removed(rec["id"])
    a, b = json.loads(pre.read_text()), json.loads(post.read_text())
    validate("wtctl/hooks/pre-remove.json", a)
    validate("wtctl/hooks/post-remove.json", b)
    assert a["object_type"] == "throwaway" and a["record"]["state"] == "active"
    assert a["overridden"] == [] and a["merged_head"] is None and a["labels"] == ["run=1"]
    assert b["record"]["state"] == "removed" and b["branch_deleted"] is False


def test_a_veto_and_an_escalation_delete_nothing(w: TW) -> None:
    rec = mk(w)
    w.top["hooks"] = {"pre-remove": [{"cmd": "exit 3"}]}
    w.failed(w.rm(rec["id"]), 11)
    w.top["hooks"] = {"pre-remove": [{"cmd": "exit 13"}]}
    w.failed(w.rm(rec["id"]), 13)
    assert Path(rec["path"]).is_dir() and w.state_of(rec["id"]) == "active"
    # an escalation counts as allow only for a human with the flag
    w.removed(rec["id"], "--override-escalation", human=True)
    assert not Path(rec["path"]).exists()


def test_a_failing_post_hook_only_warns(w: TW) -> None:
    rec = mk(w)
    w.top["hooks"] = {"post-remove": [{"cmd": "exit 5"}]}
    data = w.removed(rec["id"])
    assert data["warnings"] and not Path(rec["path"]).exists()


# -- the recorded location is checked ----------------------------------------------------------


def test_a_directory_replaced_by_another_is_unlinked_and_untouched(w: TW) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    moved = Path(str(p) + ".moved")
    os.rename(p, moved)
    p.mkdir(mode=0o700)
    (p / "theirs").write_text("not ours")
    err = w.failed(w.rm(rec["id"]), 20)
    assert err["reason"] == "unlinked"
    assert (p / "theirs").read_text() == "not ours" and (moved / "c1.txt").exists()
    assert w.state_of(rec["id"]) == "active"  # nothing was deleted: the record is as it was


def test_a_directory_replaced_by_a_symlink_is_unlinked_and_untouched(w: TW, tmp_path: Path) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep").write_text("k")
    os.rename(p, Path(str(p) + ".moved"))
    p.symlink_to(other)
    w.failed(w.rm(rec["id"]), 20)
    assert (other / "keep").read_text() == "k" and p.is_symlink()
    assert w.state_of(rec["id"]) == "active"


def test_a_replaced_root_is_unlinked_and_nothing_is_deleted(w: TW, tmp_path: Path) -> None:
    rec = mk(w)
    moved = tmp_path / "moved-root"
    os.rename(w.troot, moved)
    w.troot.mkdir(mode=0o700)
    err = w.failed(w.rm(rec["id"]), 20)
    assert err["reason"] == "unlinked"
    assert (moved / rec["id"] / "c1.txt").exists() and os.listdir(w.troot) == []
    assert w.state_of(rec["id"]) == "active"


def test_a_symlinked_root_is_unlinked(w: TW, tmp_path: Path) -> None:
    rec = mk(w)
    moved = tmp_path / "moved-root"
    os.rename(w.troot, moved)
    w.troot.symlink_to(moved)
    w.failed(w.rm(rec["id"]), 20)
    assert (moved / rec["id"] / "c1.txt").exists()


def test_a_missing_root_is_unlinked_not_gone(w: TW, tmp_path: Path) -> None:
    rec = mk(w)
    os.rename(w.troot, tmp_path / "moved-root")
    w.failed(w.rm(rec["id"]), 20)
    assert w.state_of(rec["id"]) == "active"


def test_a_vanished_directory_marks_the_record_gone_with_a_warning(w: TW) -> None:
    rec = mk(w)
    shutil.rmtree(rec["path"])
    data = w.removed(rec["id"])
    assert data["record"]["state"] == "gone" and data["warnings"]
    assert w.state_of(rec["id"]) == "gone"


def test_a_pre_remove_hook_that_swaps_the_directory_deletes_nothing_outside(
    w: TW, tmp_path: Path
) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("k")
    w.top["hooks"] = {
        "pre-remove": [{"cmd": f"mv {p} {p}.moved && ln -s {victim} {p}"}],
    }
    w.failed(w.rm(rec["id"]), 20)
    assert (victim / "keep").read_text() == "k" and (Path(str(p) + ".moved") / "c1.txt").exists()


def swap_during_delete(mp: pytest.MonkeyPatch, when: str, target: tuple[str, ...], fn: Any) -> None:
    real = fsafe.delete_tree

    def wrapped(anchor: Any, parent_fd: int, name: str, **kw: Any) -> Any:
        def hook(stage: str, path: tuple[str, ...]) -> None:
            if stage == when and path == target:
                fn()

        return real(anchor, parent_fd, name, hook=hook, **kw)

    mp.setattr(fsafe, "delete_tree", wrapped)


def test_directory_swapped_for_a_symlink_mid_delete_deletes_nothing_outside(
    w: TW, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inproc: None
) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    outside = tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    (outside / "sub" / "keep").write_text("k")
    (p / "tree" / "a").mkdir(parents=True)
    (p / "tree" / "a" / "f").write_text("x")
    a = p / "tree" / "a"

    def swap() -> None:
        os.rename(a, str(a) + ".moved")
        os.symlink(outside, a)

    swap_during_delete(monkeypatch, "classified", ("tree", "a"), swap)
    r = w.rm(rec["id"])
    assert (outside / "sub" / "keep").read_text() == "k"
    assert r.code == 1 and r.json()["error"]["code"] == "partial"
    assert w.state_of(rec["id"]) == "partial"


def test_the_throwaway_directory_itself_swapped_mid_delete(
    w: TW, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inproc: None
) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    (p / "tree").mkdir()
    (p / "tree" / "f").write_text("x")

    def swap() -> None:
        os.rename(p, str(p) + ".moved")
        os.symlink(outside, p)

    swap_during_delete(monkeypatch, "opened", ("tree",), swap)
    r = w.rm(rec["id"])
    assert (outside / "keep").read_text() == "k" and r.code == 1


# -- partial ----------------------------------------------------------------------------------


def stuck(mp: pytest.MonkeyPatch, name: str) -> dict[str, bool]:
    flag = {"on": True}
    real = os.unlink

    def unlink(path: Any, *, dir_fd: int | None = None) -> None:
        if flag["on"] and path == name:
            raise PermissionError(1, "denied")
        real(path, dir_fd=dir_fd)

    mp.setattr(os, "unlink", unlink)
    mp.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, unlink})
    return flag


def test_an_entry_that_cannot_be_deleted_leaves_a_partial_record_that_automation_finishes(
    w: TW, monkeypatch: pytest.MonkeyPatch, inproc: None
) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    (p / "stuck.txt").write_text("s")
    flag = stuck(monkeypatch, "stuck.txt")
    r = w.rm(rec["id"])
    assert r.code == 1
    err = r.json()["error"]
    validate("wtctl/cli/error.json", r.json())
    assert err["code"] == "partial" and err["details"]["remaining_path"] == str(p)
    assert f"{p}/stuck.txt" in err["details"]["remaining"]
    assert w.state_of(rec["id"]) == "partial"
    (row,) = w.rows("SELECT last_error FROM records WHERE id = ?", rec["id"])
    assert "stuck.txt" in row["last_error"] and (p / "stuck.txt").exists()
    assert not (p / "c1.txt").exists()
    # finishing a partial throwaway is not human-only: an automation actor does it
    flag["on"] = False
    w.removed(rec["id"])
    assert not p.exists() and w.state_of(rec["id"]) == "removed"


def test_a_partial_record_whose_directory_is_gone_finishes_as_gone(w: TW) -> None:
    rec = mk(w)
    w.set_state(rec["id"], "partial")
    shutil.rmtree(rec["path"])
    assert w.removed(rec["id"])["record"]["state"] == "gone"


def test_a_partial_record_in_use_refuses_and_a_replaced_directory_is_unlinked(w: TW) -> None:
    rec = mk(w)
    w.set_state(rec["id"], "partial")
    w.scanner.procs = [ProcessInfo(5, "x", rec["path"], ())]
    assert w.failed(w.rm(rec["id"]), 20)["reason"] == "in_use"
    w.scanner.procs = []
    p = Path(rec["path"])
    os.rename(p, str(p) + ".moved")
    p.mkdir(mode=0o700)
    w.failed(w.rm(rec["id"]), 20)
    assert w.state_of(rec["id"]) == "partial" and os.listdir(p) == []


def test_a_crash_after_the_first_deletion_leaves_a_truthful_record(
    w: TW, monkeypatch: pytest.MonkeyPatch, inproc: None
) -> None:
    rec = mk(w)

    def boom(*a: Any, **k: Any) -> Any:
        assert w.state_of(rec["id"]) == "partial"  # flipped before the first entry goes
        raise KeyboardInterrupt

    real = removal_delete.delete_throwaway
    monkeypatch.setattr(removal_delete, "delete_throwaway", boom)
    with pytest.raises(KeyboardInterrupt):
        w.rm(rec["id"])
    assert w.state_of(rec["id"]) == "partial" and w.leases() == []
    monkeypatch.setattr(removal_delete, "delete_throwaway", real)
    w.removed(rec["id"])


def test_a_refusal_at_the_open_step_leaves_the_record_active(w: TW) -> None:
    rec = mk(w)
    p = Path(rec["path"])
    os.rename(p, str(p) + ".moved")
    p.mkdir(mode=0o700)
    w.failed(w.rm(rec["id"]), 20)
    assert w.state_of(rec["id"]) == "active"
    (row,) = w.rows("SELECT last_error FROM records WHERE id = ?", rec["id"])
    assert row["last_error"] is None


# -- concurrency -------------------------------------------------------------------------------


def test_a_held_record_lease_makes_remove_busy(w: TW) -> None:
    from wtctl.gateway import Gateway
    from wtctl.state import State, record_lease

    rec = mk(w)
    st = State(w.db, Gateway())
    lease = st.try_lease(record_lease(rec["id"]))
    assert lease is not None
    try:
        assert w.failed(w.rm(rec["id"]), 12)["code"] == "busy"
    finally:
        lease.release()
        st.close()
    assert Path(rec["path"]).is_dir()


def test_two_removals_of_one_throwaway_do_not_both_delete(w: TW) -> None:
    rec = mk(w)
    w.config()
    cmd = [sys.executable, "-m", "wtctl", "--config", str(w.tmp / "config.yaml"), "--json",
           "--actor", "auto:t", "remove", rec["id"], "--yes"]  # fmt: skip
    procs = [
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(3)
    ]
    codes = [(p.communicate(timeout=120), p.returncode)[1] for p in procs]
    assert codes.count(0) >= 1 and set(codes) <= {0, 10, 12, 20}, codes
    assert not Path(rec["path"]).exists() and w.state_of(rec["id"]) == "removed"


# -- real mounts (an unprivileged user namespace) ----------------------------------------------


def test_mount_points_are_never_crossed_by_the_real_child(tmp_path: Path) -> None:
    unshare, mount = shutil.which("unshare"), shutil.which("mount")
    if not (unshare and mount):
        pytest.skip("real mount tests need unshare and mount")
    probe = tmp_path / "probe"
    probe.mkdir()
    ok = subprocess.run([unshare, "-Urm", mount, "-t", "tmpfs", "none", str(probe)], check=False)
    if ok.returncode != 0:
        pytest.skip("unprivileged user+mount namespaces are unavailable here")
    work = tmp_path / "ns"
    work.mkdir()
    scenario = Path(__file__).with_name("throwaway_mount_scenario.py")
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    r = subprocess.run(
        [unshare, "-Urm", sys.executable, str(scenario), str(work)],
        capture_output=True, text=True, timeout=120, env=env, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["below"]["status"] == "partial"
    assert out["below"]["remaining"] == [f"{work}/below/wt-abcdefgh/sub/mnt"]
    assert out["below"]["inside_kept"] == "precious" and out["below"]["plain_gone"]
    assert out["on"] == {"status": "refused", "kept": "precious"}
    assert out["root"]["status"] == "refused" and out["root"]["kind"] in ("identity", "mount_point")
