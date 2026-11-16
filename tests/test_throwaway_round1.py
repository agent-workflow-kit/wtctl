"""Throwaway create and prune: the guards a hostile swap or a lost lease must trip.

Each test here pins one guard (a re-check of the directory's identity, a lease check, a refusal of
a forbidden config key, a deletion-argument check ...) so that removing the guard fails a test.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import REPO, Spy
from tests.throwlib import TW, leaks
from wtctl import throwaway
from wtctl.gateway import GitResult


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> TW:
    t = TW(tmp_path, private_home).build()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "canary").write_text("untouched")
    return t


def outside_intact(w: TW) -> None:
    assert sorted(os.listdir(w.tmp / "outside")) == ["canary"]
    assert (w.tmp / "outside" / "canary").read_text() == "untouched"


def snapshot(root: Path) -> dict[str, Any]:
    """Every path below ``root`` with its content (or link target)."""
    out: dict[str, Any] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = ("link", os.readlink(p))
        elif p.is_file():
            out[rel] = ("file", p.read_bytes())
        else:
            out[rel] = ("dir", None)
    return out


# -- a swap of the directory or the root at every git call --------------------------------------

# git commands (not the runner's own `config --list` checks, between which and the command a swap
# cannot be guarded) made while the throwaway directory exists
REMOTE_CALLS = 10
LOCAL_CALLS = 12
CASES = [("remote", "0", i) for i in range(REMOTE_CALLS)] + [
    ("local", d, i) for d in ("0", "1") for i in range(LOCAL_CALLS) if d == "0" or 4 <= i <= 9
]


def make_swap(w: TW, kind: str, decoy: list[Path]) -> Any:
    """Replace the throwaway directory (or the whole root) by a look-alike copy, once."""
    done: list[bool] = []

    def swap() -> None:
        (rid,) = [p.name for p in w.troot.iterdir()]
        aside = w.tmp / "aside"
        if kind == "dir":
            os.rename(w.troot / rid, aside)
            os.mkdir(w.troot / rid, 0o700)
            shutil.copytree(aside / ".git", w.troot / rid / ".git", symlinks=True)
            decoy.append(w.troot / rid)
        else:
            os.rename(w.troot, aside)
            os.mkdir(w.troot, 0o700)
            shutil.copytree(aside / rid, w.troot / rid, symlinks=True)
            decoy.append(w.troot)
        done.append(True)

    return swap, done


@pytest.mark.parametrize("kind", ["dir", "root"])
@pytest.mark.parametrize(("source", "depth", "index"), CASES)
def test_a_swapped_directory_or_root_fails_the_create_and_nothing_is_written_into_the_look_alike(
    w: TW, kind: str, source: str, depth: str, index: int
) -> None:
    decoy: list[Path] = []
    swap, done = make_swap(w, kind, decoy)
    seen: dict[str, Any] = {"n": -1}

    def after(sub, argv, kw, result):  # noqa: ANN001, ANN202
        if "--list" in argv or done:
            return None
        if w.troot.exists() and any(w.troot.iterdir()):
            seen["n"] += 1
            if seen["n"] == index:
                swap()
                seen["snap"] = snapshot(decoy[0])
        return None

    w.git_runner = Spy(after=after)
    r = w.throw("--source", source, "--depth", depth)
    assert done, "the swap point was never reached"
    err = w.failed(r, 12)
    assert err["code"] == "create_failed", err
    assert snapshot(decoy[0]) == seen["snap"], "something wrote into the look-alike"
    (row,) = w.records()
    assert row["state"] == "gone" and w.leases() == []
    outside_intact(w)


def test_a_swap_right_after_the_shallow_file_is_written_stops_the_index_pack(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import fsafe

    decoy: list[Path] = []
    swap, done = make_swap(w, "dir", decoy)
    real = fsafe.create_new_file
    snap: dict[str, Any] = {}

    def create_new_file(parent_fd: int, name: str, **kw: Any) -> int:
        fd = real(parent_fd, name, **kw)
        if name == "shallow" and not done:
            swap()  # after the file is written the directory is replaced
            snap["s"] = snapshot(decoy[0])
        return fd

    monkeypatch.setattr(fsafe, "create_new_file", create_new_file)
    err = w.failed(w.throw("--source", "local", "--depth", "1"), 12)
    assert err["code"] == "create_failed" and done
    assert snapshot(decoy[0]) == snap["s"], "something wrote into the look-alike"
    outside_intact(w)


# -- a swap between the root check and the directory's creation ---------------------------------


@pytest.mark.parametrize("kind", ["empty-dir", "symlink"])
def test_a_root_replaced_after_it_was_recorded_gets_no_directory(
    w: TW, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl.state import State

    first = w.made("--source", "remote")  # records the root
    real = State.insert_record_leased

    def insert(self: State, *a: Any, **kw: Any) -> Any:
        out = real(self, *a, **kw)
        aside = w.tmp / "old-root"
        os.rename(w.troot, aside)
        if kind == "symlink":
            os.symlink(aside, w.troot)
        else:
            os.mkdir(w.troot, 0o700)
        return out

    monkeypatch.setattr(State, "insert_record_leased", insert)
    err = w.failed(w.throw("--source", "remote"), 12)
    assert err["code"] == "create_failed"
    if kind == "empty-dir":
        assert os.listdir(w.troot) == [], "a directory was made in the replacing root"
    else:
        assert os.listdir(w.tmp / "old-root") == [first["id"]]
    assert outside_ok(w)


def outside_ok(w: TW) -> bool:
    outside_intact(w)
    return True


# -- leases --------------------------------------------------------------------------------------


def capture_lease(mp: pytest.MonkeyPatch) -> list[Any]:
    from wtctl.state import State

    held: list[Any] = []
    real = State.insert_record_leased

    def insert(self: State, *a: Any, **kw: Any) -> Any:
        record, lease = real(self, *a, **kw)
        held.append(lease)
        return record, lease

    mp.setattr(State, "insert_record_leased", insert)
    return held


LOSE_AT_INIT = [("remote", "init", "fetch"), ("local", "init", "pack-objects")]


@pytest.mark.parametrize(("source", "lose_at", "must_not_run"), LOSE_AT_INIT)
def test_a_lease_lost_before_the_fill_stops_the_create_before_anything_is_fetched(
    w: TW, monkeypatch: pytest.MonkeyPatch, source: str, lose_at: str, must_not_run: str
) -> None:
    held = capture_lease(monkeypatch)

    def after(sub, argv, kw, result):  # noqa: ANN001, ANN202
        if sub == lose_at:
            held[0].lost = True
        return None

    spy = Spy(after=after)
    w.git_runner = spy
    err = w.failed(w.throw("--source", source), 12)
    assert err["code"] == "busy"
    assert must_not_run not in spy.subs()
    assert leaks(w.troot) == [] and w.records() == []


@pytest.mark.parametrize("source", ["remote", "local"])
def test_a_lease_lost_during_the_fill_never_activates_the_record(
    w: TW, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    held = capture_lease(monkeypatch)

    def after(sub, argv, kw, result):  # noqa: ANN001, ANN202
        if sub == "rev-parse" and "HEAD" in argv:
            held[0].lost = True
        return None

    w.git_runner = Spy(after=after)
    err = w.failed(w.throw("--source", source), 12)
    assert err["code"] == "busy"
    assert not any(r["state"] == "active" for r in w.records())
    assert leaks(w.troot) == []


def test_a_lease_lost_during_removal_does_not_mark_the_record_removed(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import removal_delete
    from wtctl.state import State

    rec = w.made("--source", "local")
    leases: list[Any] = []
    real_lease = State.lease

    def lease(self: State, *a: Any, **kw: Any) -> Any:
        got = real_lease(self, *a, **kw)
        leases.append(got)
        return got

    real_delete = removal_delete.delete_throwaway

    def delete(*a: Any, **kw: Any) -> Any:
        out = real_delete(*a, **kw)
        leases[-1].lost = True
        return out

    monkeypatch.setattr(State, "lease", lease)
    monkeypatch.setattr(removal_delete, "delete_throwaway", delete)
    err = w.failed(w.rm(rec["id"]), 12)
    assert err["code"] == "busy"
    assert w.state_of(rec["id"]) != "removed"


# -- the config check before a token is sent -----------------------------------------------------

TOKEN = "tok-2c4e6a8b0d1f3a5c7e9b"
FORBIDDEN = [
    ("core.fsmonitor", "[core]\n\tfsmonitor = /bin/true\n"),
    ("http.extraHeader", "[http]\n\textraHeader = X-Evil: 1\n"),
    ("include.path", "[include]\n\tpath = {inc}\n"),
    ("core.sshCommand", "[core]\n\tsshCommand = /bin/true\n"),
    ("filter.x.clean", '[filter "x"]\n\tclean = cat\n'),
]


def test_the_owned_keys_are_exactly_what_wtctl_writes() -> None:
    assert throwaway.OWNED_KEYS == frozenset(
        {
            "core.repositoryformatversion",
            "core.filemode",
            "core.bare",
            "core.logallrefupdates",
            "core.symlinks",
            "core.ignorecase",
            "core.precomposeunicode",
            "fetch.fsckobjects",
        }
    )


@pytest.mark.parametrize(("key", "text"), FORBIDDEN, ids=[k for k, _ in FORBIDDEN])
def test_a_foreign_config_key_stops_the_fetch_so_the_token_is_never_sent(
    w: TW, monkeypatch: pytest.MonkeyPatch, key: str, text: str
) -> None:
    monkeypatch.setenv("WT_TEST_TOKEN", TOKEN)
    w.top["hosts"] = {"example.test": {"token_env": "WT_TEST_TOKEN"}}
    w.repo_cfg = {"clone_url": "https://example.test/org/repo.git"}
    sent: list[bool] = []
    inc = w.tmp / "included.cfg"
    inc.write_text("[user]\n\tname = x\n")

    def after(sub, argv, kw, result):  # noqa: ANN001, ANN202
        if sub == "config" and "fetch.fsckObjects" in argv and not sent:
            sent.append(False)  # planted once, after the last key wtctl writes
            with open(Path(kw["cwd"]) / "config", "a") as f:
                f.write(text.replace("{inc}", str(inc)))
        return None

    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "fetch":
            sent.append(True)
        return None

    w.git_runner = Spy(before=before, after=after)
    r = w.throw("--source", "remote")
    err = w.failed(r, 12)
    assert err["code"] == "unsafe_repo_config", err
    assert True not in sent, "the fetch ran"
    assert TOKEN not in r.out + r.err
    assert leaks(w.troot) == [] and w.records() == []


def test_an_http_url_is_refused_before_any_token_is_resolved(
    w: TW, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marker = tmp_path / "token-command-ran"
    monkeypatch.setenv("WT_TEST_TOKEN", TOKEN)
    w.top["hosts"] = {
        "example.test": {
            "token_env": "WT_TEST_TOKEN",
            "token_command": ["/bin/sh", "-c", f"touch {marker}; echo x"],
        }
    }
    w.repo_cfg = {"clone_url": "http://example.test/org/repo.git"}
    seen: list[Any] = []

    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "fetch":
            seen.append((list(argv), dict(kw["env"])))
        return None

    w.git_runner = Spy(before=before)
    r = w.throw("--source", "remote")
    assert r.code == 12 and TOKEN not in r.out + r.err
    assert not marker.exists()
    for argv, env in seen:
        assert not any(TOKEN in a for a in argv)
        assert not any(TOKEN in v or "Authorization" in v for v in env.values())


# -- the deletion arguments -----------------------------------------------------------------------


def good_record(w: TW) -> dict[str, Any]:
    return dict(w.made("--source", "local"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: {**r, "path": r["path"] + "x"},
        lambda r: {**r, "path": os.path.join(r["base_dir"], "..", r["id"])},
        lambda r: {**r, "path": os.path.join(os.path.dirname(r["base_dir"]), r["id"])},
        lambda r: {**r, "dir_id": None},
        lambda r: {**r, "root_id": None},
        lambda r: {**r, "base_dir": None},
        lambda r: {**r, "base_dir": ""},
    ],
    ids=[
        "path-suffix",
        "dotdot",
        "other-parent",
        "no-dir-id",
        "no-root-id",
        "no-base",
        "empty-base",
    ],
)
def test_deletion_arguments_are_refused_unless_they_name_exactly_base_dir_slash_id(
    w: TW, mutate: Any
) -> None:
    from wtctl import fsafe, fsafe_child, removal_delete

    rec = good_record(w)
    bad = mutate(rec)
    with pytest.raises(fsafe.FsafeError):
        removal_delete.throwaway_args(bad)
    out = removal_delete.delete_throwaway(bad, fsafe_child.Budget())
    assert out.status == "refused"
    assert Path(rec["path"]).is_dir() and (Path(rec["path"]) / "c1.txt").exists()


def test_deletion_arguments_carry_the_recorded_identities(w: TW) -> None:
    from wtctl import removal_delete

    rec = good_record(w)
    args = removal_delete.throwaway_args(rec)
    dev, ino = (int(x) for x in rec["dir_id"].split(":"))
    rdev, rino = (int(x) for x in rec["root_id"].split(":"))
    assert args["name"] == rec["id"] and args["anchor"]["path"] == rec["base_dir"]
    assert list(args["root_identity"]) == [dev, ino]
    assert list(args["anchor"]["identity"]) == [rdev, rino]


# -- the directory vanishes ----------------------------------------------------------------------


@pytest.mark.parametrize(("source", "after_sub"), [("remote", "fetch"), ("local", "index-pack")])
def test_a_directory_that_vanishes_mid_create_is_create_failed_not_internal(
    w: TW, source: str, after_sub: str
) -> None:
    def after(sub, argv, kw, result):  # noqa: ANN001, ANN202
        if sub == after_sub and w.troot.exists():
            (rid,) = os.listdir(w.troot)
            os.rename(w.troot / rid, w.tmp / "moved-away")
        return None

    w.git_runner = Spy(after=after)
    err = w.failed(w.throw("--source", source), 12)
    assert err["code"] == "create_failed"
    assert w.records() == [] and w.leases() == []
    assert (w.tmp / "moved-away" / ".git").is_dir()
    outside_intact(w)


# -- the root: owner and mount point ------------------------------------------------------------


def test_a_root_owned_by_someone_else_is_refused(w: TW, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    from wtctl import fsafe

    w.made("--source", "remote")
    root_ino = w.troot.stat().st_ino
    real = fsafe.Fs.adjust

    def adjust(self: Any, st: Any) -> Any:
        st = real(self, st)
        return dataclasses.replace(st, uid=st.uid + 1) if st.ino == root_ino else st

    monkeypatch.setattr(fsafe.Fs, "adjust", adjust)
    err = w.failed(w.throw("--source", "remote"), 12)
    assert err["code"] == "create_failed" and "owned by you" in err["message"]
    assert os.listdir(w.troot) != [] and len(w.records()) == 1


def test_a_root_that_is_a_mount_point_is_refused_without_a_directory(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import fsafe
    from wtctl.gateway import MountEntry

    w.made("--source", "remote")
    table = [MountEntry(1, str(w.troot), "tmpfs", "none")]
    monkeypatch.setattr(fsafe.DEFAULT_FS, "mount_table", lambda: table)
    err = w.failed(w.throw("--source", "remote"), 12)
    assert err["code"] == "create_failed" and err["details"]["cause"] == "anchor_changed"
    assert len(w.records()) == 1 and len(os.listdir(w.troot)) == 1


# -- what git is told ------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["remote", "local"])
def test_git_is_told_not_to_deref_head_and_to_work_in_the_throwaway(w: TW, source: str) -> None:
    calls: list[tuple[str, list[str], dict[str, Any]]] = []

    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        calls.append((sub, list(argv), {"cwd": kw.get("cwd")}))
        return None

    w.git_runner = Spy(before=before)
    rec = w.made("--source", source)
    (upd,) = [c for c in calls if c[0] == "update-ref"]
    assert upd[1][-3:] == ["--no-deref", "HEAD", w.shas["c3"]]
    (rt,) = [c for c in calls if c[0] == "read-tree"]
    assert f"--work-tree={rec['path']}" in rt[1] and str(rt[2]["cwd"]) == rec["path"]
    assert f"--git-dir={rec['path']}/.git" in rt[1]
    head = Path(rec["path"], ".git", "HEAD").read_text()
    assert head == w.shas["c3"] + "\n"  # detached: not a symref that update-ref followed
    assert not list(Path(rec["path"], ".git", "refs", "heads").iterdir())


@pytest.mark.parametrize("depth", ["0", "50"])
def test_the_fetch_takes_no_tags_and_no_submodules(w: TW, depth: str) -> None:
    seen: list[list[str]] = []

    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "fetch":
            seen.append(list(argv))
        return None

    w.git_runner = Spy(before=before)
    w.made("--source", "remote", "--depth", depth)
    (argv,) = seen
    assert "--no-tags" in argv and "--no-recurse-submodules" in argv
    assert ("--depth" in argv) is (depth != "0")


def test_a_local_throwaway_whose_head_is_not_the_commit_is_refused(w: TW) -> None:
    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "rev-parse" and "HEAD" in argv:
            return GitResult(0, b"1" * 40 + b"\n", b"")
        return None

    w.git_runner = Spy(before=before)
    err = w.failed(w.throw("--source", "local"), 12)
    assert err["code"] == "create_failed" and "HEAD" in err["message"]
    assert leaks(w.troot) == [] and w.records() == []


def test_dry_run_does_not_take_the_global_lease(w: TW) -> None:
    from wtctl.gateway import Gateway
    from wtctl.state import GLOBAL_LEASE, State

    rec = w.made("--source", "local")
    w.age(rec["id"], 10)
    st = State(w.db, Gateway())
    lease = st.try_lease(GLOBAL_LEASE)
    assert lease is not None
    try:
        data = w.pruned("--older-than", "1d", "--dry-run")
        assert data["already_running"] is False and data["removed"] == [rec["id"]]
    finally:
        lease.release()
        st.close()


# -- prune: what it must leave alone ----------------------------------------------------------


def test_prune_ignores_creating_throwaways_and_worktree_records_whatever_their_age(w: TW) -> None:
    mid: dict[str, Any] = {}

    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "fetch" and "r" not in mid:
            (row,) = [r for r in w.records() if r["state"] == "creating"]
            w.age(row["id"], 30)
            mid["id"] = row["id"]
            mid["r"] = w.prune("--older-than", "1d", gateway=None)
            mid["dry"] = w.prune("--older-than", "1d", "--dry-run", gateway=None).json()
        return None

    wt = w.run("create", "--repo", REPO, "--branch", "local-b", "--no-bootstrap")
    assert wt.code == 0, wt.err
    wt_id = wt.json()["record"]["id"]
    w.age(wt_id, 30)
    w.git_runner = Spy(before=before)
    w.made("--source", "remote")
    out = mid["r"].json()
    assert mid["r"].code == 0
    for bucket in (out["removed"], [s["id"] for s in out["skipped"]]):
        assert mid["id"] not in bucket and wt_id not in bucket
    assert w.state_of(wt_id) == "active" and w.state_of(mid["id"]) == "active"
    dry = mid["dry"]  # selection alone, no lease
    for bucket in (dry["removed"], [x["id"] for x in dry["skipped"]]):
        assert mid["id"] not in bucket and wt_id not in bucket


def test_a_throwaway_created_exactly_at_the_cutoff_is_kept(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from wtctl.state import State

    cutoff = "2026-03-01T12:00:00.000Z"
    monkeypatch.setattr(State, "timestamp_before", lambda self, seconds: cutoff)
    at, older, newer = (w.made("--source", "local") for _ in range(3))
    conn = sqlite3.connect(w.db)
    for rec, stamp in ((at, cutoff), (older, "2026-03-01T11:59:59.999Z"),
                       (newer, "2026-03-01T12:00:00.001Z")):  # fmt: skip
        conn.execute("UPDATE records SET created = ? WHERE id = ?", (stamp, rec["id"]))
    conn.commit()
    conn.close()
    dry = w.pruned("--older-than", "1d", "--dry-run")  # selection alone, no lease
    assert dry["removed"] == [older["id"]]
    data = w.pruned("--older-than", "1d")
    assert data["removed"] == [older["id"]]
    assert w.state_of(at["id"]) == "active" and w.state_of(newer["id"]) == "active"


def test_an_in_use_throwaway_never_runs_a_pre_remove_hook(w: TW, tmp_path: Path) -> None:
    from wtctl.gateway import ProcessInfo

    rec = w.made("--source", "local")
    ran = tmp_path / "hook-ran"
    w.top["hooks"] = {"pre-remove": [{"cmd": f"touch {ran}"}]}
    w.scanner.procs = [ProcessInfo(4242, "vim", rec["path"], ())]
    assert w.failed(w.rm(rec["id"]), 20)["reason"] == "in_use"
    assert not ran.exists()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE records SET state = 'creating' WHERE id = ?",
        "UPDATE records SET object_type = 'worktree' WHERE id = ?",
    ],
    ids=["state", "object_type"],
)
def test_a_record_that_changed_during_the_hooks_is_busy_and_nothing_is_deleted(
    w: TW, tmp_path: Path, sql: str
) -> None:
    rec = w.made("--source", "local")
    import sqlite3

    script = tmp_path / "change.py"
    script.write_text(
        "import sqlite3, sys\n"
        f"c = sqlite3.connect({str(w.db)!r}); c.execute({sql!r}, ({rec['id']!r},)); c.commit()\n"
    )
    w.top["hooks"] = {"pre-remove": [{"cmd": f"{sys.executable} {script}"}]}
    r = w.rm(rec["id"])
    assert r.code == 12 and w.failed(r, 12)["code"] == "busy"
    assert Path(rec["path"]).is_dir()
    _ = sqlite3
