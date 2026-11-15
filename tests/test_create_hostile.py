"""``create`` under failure and attack: no leaks, nothing planted runs, nothing outside the root."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.createlib import CW, REPO, Spy, cli_spy, script
from tests.test_create import ok, refused
from tests.vectorlib import plain_git
from wtctl.gateway import GitResult


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def settled(w: CW) -> dict:
    """The clone's state without the (legitimately created) root directories."""
    snap = w.snapshot()
    snap.pop("dirs")
    return snap


def leases(w: CW) -> list[str]:
    return [r["name"] for r in w.rows("SELECT name FROM leases")]


def fail_sub(name: str, *, containing: str | None = None):  # noqa: ANN201
    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == name and (containing is None or containing in argv):
            return GitResult(128, b"", b"fatal: injected failure\n")
        return None

    return before


# -- failures leave nothing behind --------------------------------------------------------------


def test_a_real_worktree_add_failure_leaves_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["branch", "d/e", "main"])
    before = settled(w)
    err = refused(w.create(capsys, "--branch", "d"), 12)
    assert err["code"] == "create_failed"
    assert settled(w) == before and w.records() == [] and leases(w) == []
    assert w.count("ids") == 1
    assert not (w.root / "worktrees" / "example.test" / "org" / "repo" / "d").exists()


def test_a_branch_leaked_by_a_failed_add_is_deleted(w: CW, capsys) -> None:  # noqa: ANN001
    before = settled(w)

    def make_then_fail(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "worktree" and "-b" in argv:
            branch = argv[argv.index("-b") + 1]
            sha = argv[-1]
            g = ["--git-dir", str(w.clone / ".git")]
            plain_git([*g, "update-ref", f"refs/heads/{branch}", sha], cwd=w.tmp, home=w.home)
            return GitResult(128, b"", b"fatal: injected\n")
        return None

    r = cli_spy(w, Spy(before=make_then_fail))
    assert refused(r, 12)["code"] == "create_failed"
    assert settled(w) == before and w.records() == [] and leases(w) == []


def test_the_leaked_branch_is_deleted_only_if_it_did_not_move(w: CW, capsys) -> None:  # noqa: ANN001
    moved = {}

    def make_move_fail(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "worktree" and "-b" in argv:
            branch = argv[argv.index("-b") + 1]
            moved["ref"] = f"refs/heads/{branch}"
            g = ["--git-dir", str(w.clone / ".git")]
            plain_git([*g, "update-ref", moved["ref"], w.shas["c1"]], cwd=w.tmp, home=w.home)
            return GitResult(128, b"", b"fatal: injected\n")
        return None

    r = cli_spy(w, Spy(before=make_move_fail))
    assert refused(r, 12)["code"] == "create_failed"
    # the branch is not at the sha this create planned (c3): somebody else's, so it stays
    assert w.refs()[moved["ref"]] == w.shas["c1"]
    assert w.records() == []


def test_a_failed_track_add_removes_the_upstream_section(w: CW, capsys) -> None:  # noqa: ANN001
    before = settled(w)
    r = cli_spy(w, Spy(before=fail_sub("worktree")), "--branch", "feature/remote-only")
    assert refused(r, 12)["code"] == "create_failed"
    assert settled(w) == before and w.records() == []


def test_a_stale_branch_section_of_a_new_branch_is_not_ours_to_remove(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["config", "branch.mine.remote", "elsewhere"])
    r = cli_spy(w, Spy(before=fail_sub("worktree")), "--branch", "mine")
    refused(r, 12)
    assert w.g(["config", "branch.mine.remote"]) == "elsewhere"


def test_an_unexpected_exception_still_unwinds(w: CW, capsys) -> None:  # noqa: ANN001
    before = settled(w)

    def boom(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "worktree":
            raise RuntimeError("boom")

    r = cli_spy(w, Spy(before=boom))
    assert r.code == 1
    assert settled(w) == before and w.records() == [] and leases(w) == []


def test_an_interrupt_unwinds_and_releases_the_leases(w: CW, capsys) -> None:  # noqa: ANN001
    before = settled(w)

    def interrupt(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "worktree":
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        cli_spy(w, Spy(before=interrupt))
    assert settled(w) == before and w.records() == [] and leases(w) == []


def test_a_failure_after_the_add_keeps_a_truthful_creating_record(w: CW, capsys) -> None:  # noqa: ANN001
    def break_inspect(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "rev-parse" and "--absolute-git-dir" in argv:
            return GitResult(128, b"", b"fatal: injected\n")

    r = cli_spy(w, Spy(before=break_inspect))
    assert refused(r, 12)["code"] == "create_failed"
    [rec] = w.records()
    assert rec["state"] == "creating" and rec["dir_id"] is None
    assert Path(rec["path"], ".git").exists()  # the worktree is there: the record must stay
    assert f"refs/heads/{rec['branch']}" in w.refs()  # and so is its branch
    assert leases(w) == []  # the creator is gone: reconcile can settle it


def test_a_base_transfer_failure_leaves_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    w.advance_remote()
    before = settled(w)
    packs_before = sorted(os.listdir(w.clone / ".git" / "objects" / "pack"))
    r = cli_spy(w, Spy(before=fail_sub("pack-objects")))
    assert refused(r, 12)["code"] == "create_failed"
    assert settled(w) == before and w.records() == [] and leases(w) == []
    assert sorted(os.listdir(w.clone / ".git" / "objects" / "pack")) == packs_before


def test_the_record_and_both_leases_exist_while_the_worktree_is_added(w: CW, capsys) -> None:  # noqa: ANN001
    seen: dict = {}

    def look(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "worktree":
            seen["records"] = [(r["id"], r["state"]) for r in w.records()]
            seen["leases"] = leases(w)

    data = ok(cli_spy(w, Spy(before=look)))
    rid = data["record"]["id"]
    assert seen["records"] == [(rid, "creating")]
    assert sorted(seen["leases"]) == sorted([f"record:{rid}", f"clone:{w.clone}"])
    assert leases(w) == []  # all released at the end


def test_the_mirror_is_refreshed_before_anything_else_is_written(w: CW, capsys) -> None:  # noqa: ANN001
    spy = Spy()
    ok(cli_spy(w, spy))
    subs = spy.subs()
    assert subs.index("fetch") < subs.index("worktree")


# -- planted clone config and hooks are inert ---------------------------------------------------


def plant(w: CW) -> Path:
    markers = w.tmp / "markers"
    markers.mkdir()
    payload = script(w.tmp / "payload.sh", f'echo "$0 $*" >> {markers}/hit\nexit 1')
    git_dir = w.clone / ".git"
    for hook in ("post-checkout", "post-commit", "reference-transaction", "post-merge"):
        (git_dir / "hooks" / hook).write_text(f"#!/bin/sh\n{payload} {hook}\n")
        (git_dir / "hooks" / hook).chmod(0o755)
    w.g(["config", "core.fsmonitor", payload])
    w.g(["config", "core.hooksPath", str(git_dir / "hooks")])
    w.g(["config", "core.sshCommand", payload])
    w.g(["config", "core.pager", payload])
    w.g(["config", "diff.external", payload])
    w.g(["config", "filter.x.smudge", payload])
    w.g(["config", "filter.x.clean", payload])
    w.g(["config", "filter.x.process", payload])
    w.g(["config", "filter.x.required", "true"])
    w.g(["config", "gc.auto", "1"])
    return markers


def test_planted_config_and_hooks_do_not_run(w: CW, capsys) -> None:  # noqa: ANN001
    # a base commit whose files go through the planted filter on checkout
    work = w.work
    (work / ".gitattributes").write_text("* filter=x\n")
    w.g(["add", "."], work)
    w.g(["commit", "-q", "-m", "attrs"], work)
    w.g(["push", "-q", str(w.remote), "main"], work)
    markers = plant(w)
    # the vectors are live: unhardened git fires them
    plain_git(["status"], cwd=w.clone, home=w.home)
    plain_git(["checkout", "-q", "main"], cwd=w.clone, home=w.home)
    assert (markers / "hit").exists(), "the control did not fire: the planted vectors are dead"
    (markers / "hit").unlink()
    ok(w.create(capsys))
    ok(w.create(capsys, "--branch", "feature/remote-only"))
    ok(w.create(capsys, "--branch", "local-b"))
    ok(w.create(capsys, "--detach"))
    assert sorted(os.listdir(markers)) == []


def test_a_planted_alternates_file_makes_the_clone_unusable(w: CW, capsys) -> None:  # noqa: ANN001
    info = w.clone / ".git" / "objects" / "info"
    info.mkdir(exist_ok=True)
    (info / "alternates").write_text(str(w.tmp / "evil") + "\n")
    r = w.create(capsys)
    assert r.code == 12 and r.json()["error"]["code"] == "remote_mismatch"
    assert w.records() == []


def test_a_changed_remote_url_is_refused_before_anything_is_written(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["remote", "set-url", "origin", "https://evil.test/org/other.git"])
    before = settled(w)
    r = w.create(capsys)
    assert r.code == 12 and r.json()["error"]["code"] == "remote_mismatch"
    assert settled(w) == before and w.records() == []


def test_a_changed_clone_identity_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys))
    import sqlite3

    conn = sqlite3.connect(w.db)
    conn.execute("UPDATE clones SET common_id = '1:1'")
    conn.commit()
    conn.close()
    before = settled(w)
    err = refused(w.create(capsys), 12)
    assert err["code"] == "create_failed" and err["details"]["cause"] == "anchor_changed"
    assert settled(w) == before and len(w.records()) == 1


# -- the worktree root --------------------------------------------------------------------------


def test_a_symlinked_worktree_root_is_resolved_once(w: CW, capsys) -> None:  # noqa: ANN001
    real = w.tmp / "real-root"
    real.mkdir()
    link = w.tmp / "link-root"
    link.symlink_to(real)
    w.repo_cfg["worktree_root"] = str(link)
    rec = ok(w.create(capsys))["record"]
    assert rec["base_dir"] == str(real.resolve())
    assert rec["path"].startswith(str(real.resolve()) + "/")
    assert rec["dir_id"] == f"{os.stat(rec['path']).st_dev}:{os.stat(rec['path']).st_ino}"


def test_a_symlink_planted_below_the_root_is_never_followed(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys))  # records the root
    outside = w.tmp / "outside"
    outside.mkdir()
    host = w.root.resolve() / "worktrees" / "example.test"
    # replace the host directory by a symlink to somewhere else
    import shutil

    shutil.rmtree(host)
    host.symlink_to(outside)
    before = settled(w)
    r = w.create(capsys)
    assert r.code == 12, r.out
    assert r.json()["error"]["code"] == "create_failed"
    assert os.listdir(outside) == []
    assert settled(w) == before and len(w.records()) == 1


def test_a_replaced_worktree_root_is_anchor_changed(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys))
    root = w.root.resolve() / "worktrees"
    root.rename(w.root / "old-worktrees")
    root.mkdir()
    before = settled(w)
    err = refused(w.create(capsys), 12)
    assert err["code"] == "create_failed" and err["details"]["cause"] == "anchor_changed"
    [anchor] = err["details"]["anchors"]
    assert anchor["anchor"] == "worktree_root" and anchor["changed"] is True
    assert settled(w) == before
    assert os.listdir(root) == []


def test_root_identity_is_recorded_on_first_use(w: CW, capsys) -> None:  # noqa: ANN001
    root = w.root.resolve() / "worktrees"
    root.mkdir(parents=True)
    ok(w.create(capsys))
    [row] = w.rows("SELECT * FROM anchors")
    assert (
        row["path"] == str(root)
        and row["dev_ino"] == f"{os.stat(root).st_dev}:{os.stat(root).st_ino}"
    )


@pytest.mark.parametrize("template", ["{root}/../x/{name}", "{root}/a/../../x/{name}"])
def test_a_template_that_leaves_the_root_exits_2(w: CW, capsys, template: str) -> None:  # noqa: ANN001
    w.top["worktree_path"] = template
    r = w.create(capsys)
    assert r.code == 2
    assert w.records() == [] and not (w.tmp / "x").exists()


def test_a_prefix_cannot_traverse(w: CW, capsys) -> None:  # noqa: ANN001
    w.top["branch_prefix"] = "../"
    assert w.create(capsys).code == 2
    w.top["branch_prefix"] = "a/../"
    assert w.create(capsys).code == 2
    assert w.records() == []


def test_a_root_inside_the_clone_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg["worktree_root"] = str(w.clone / "wts")
    assert w.create(capsys).code == 2
    assert not (w.clone / "wts").exists()


def test_a_path_above_a_recorded_worktree_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    w.plant_creating(branch=None, name="deep", state="missing")  # recorded, nothing on disk
    inner = w.root.resolve() / "worktrees" / "example.test" / "org" / "repo" / "deep"
    assert not inner.exists()
    w.top["worktree_path"] = "{root}/example.test/org/{name}"
    r = w.create(capsys, "--name", "repo")
    assert r.code == 2, r.out
    assert w.count("records") == 1


# -- refs: tags and options never stand in for branches ------------------------------------------


def test_a_tag_never_stands_in_for_a_base_branch(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["tag", "v1", w.shas["c1"]])
    err = refused(w.create(capsys, "--base", "v1"), 12)
    assert err["code"] == "ref_not_found"
    w.g(["tag", "tagged", w.shas["c1"]])
    data = ok(w.create(capsys, "--base", "main"))
    assert data["record"]["base_sha"] == w.shas["c3"]


def test_a_tag_with_the_branch_name_does_not_win(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["tag", "local-b", w.shas["c1"]])
    data = ok(w.create(capsys, "--branch", "local-b"))
    assert w.g(["rev-parse", "HEAD"], Path(data["record"]["path"])) == w.shas["c3"]
    assert w.g(["symbolic-ref", "HEAD"], Path(data["record"]["path"])) == "refs/heads/local-b"


def test_remote_tracking_names_are_not_branches(w: CW, capsys) -> None:  # noqa: ANN001
    err = refused(w.create(capsys, "--base", "origin/main"), 12)
    assert err["code"] == "ref_not_found"
    err = refused(w.create(capsys, "--base", "heads/main"), 12)
    assert err["code"] == "ref_not_found"


def test_a_ref_that_is_a_tree_not_a_commit(w: CW, capsys) -> None:  # noqa: ANN001
    tree = w.g(["rev-parse", "main^{tree}"])
    assert refused(w.create(capsys, "--ref", tree), 12)["code"] == "ref_not_found"


# -- names with odd characters ------------------------------------------------------------------


def test_unicode_branches_get_ascii_names_and_escaped_output(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--branch", "café/ü‮evil"))
    rec = data["record"]
    assert rec["branch"] == "café/ü‮evil" and rec["name"].isascii()
    assert Path(rec["path"]).name == rec["name"]
    r = w.create(capsys, "--branch", "bidi/‮", json_out=False)
    assert r.code == 0 and "‮" not in r.err and "‮" not in r.out


def test_a_flag_shaped_branch_in_a_template_is_rejected(w: CW, capsys) -> None:  # noqa: ANN001
    w.top["branch_template"] = "-{name}"
    assert w.create(capsys).code == 2
    assert w.records() == []


# -- concurrency --------------------------------------------------------------------------------


def run_create(w: CW, *argv: str) -> subprocess.Popen[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(w.home),
        "XDG_CONFIG_HOME": str(w.home / ".config"),
        "XDG_STATE_HOME": str(w.home / ".local" / "state"),
        "XDG_DATA_HOME": str(w.home / ".local" / "share"),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "wtctl", "--config", w.config(), "--json", "--actor", "auto:t",
         "create", "--repo", REPO, *argv],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=w.tmp,
    )  # fmt: skip


def finish(p: subprocess.Popen[str]) -> tuple[int, str]:
    out, _ = p.communicate(timeout=120)
    return p.returncode, out


def test_two_creates_for_one_branch_have_one_winner(w: CW) -> None:
    w.top["lease_wait"] = "60s"
    a = run_create(w, "--branch", "local-b")
    b = run_create(w, "--branch", "local-b")
    results = sorted([finish(a), finish(b)], key=lambda r: r[0])
    assert [r[0] for r in results] == [0, 20], results
    assert '"branch_in_use"' in results[1][1] or '"path_exists"' in results[1][1]
    assert len(w.records()) == 1 and leases(w) == []
    assert len(w.worktree_paths()) == 2


def test_two_creates_for_one_holder_both_succeed(w: CW) -> None:
    w.top["lease_wait"] = "60s"
    a = run_create(w, "--branch", "local-b", "--holder", "h1")
    b = run_create(w, "--branch", "local-b", "--holder", "h1")
    ra, rb = finish(a), finish(b)
    assert ra[0] == 0 and rb[0] == 0, (ra, rb)
    import json

    reused = sorted(json.loads(r[1])["reused"] for r in (ra, rb))
    assert reused == [False, True]
    assert json.loads(ra[1])["record"]["id"] == json.loads(rb[1])["record"]["id"]
    assert len(w.records()) == 1


def test_many_creates_with_derived_names_get_distinct_names(w: CW) -> None:
    w.top["lease_wait"] = "120s"
    procs = [run_create(w, "--branch", "topic") for _ in range(1)] + [
        run_create(w, "--detach"),
        run_create(w, "--detach"),
    ]
    codes = [finish(p)[0] for p in procs]
    assert codes == [0, 0, 0]
    assert len({r["name"] for r in w.records()}) == 3
    assert leases(w) == []


def test_a_held_clone_lease_gives_busy(w: CW, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import Gateway
    from wtctl.state import State, clone_lease

    holder = subprocess.Popen(["sleep", "60"])
    try:
        ok(w.create(capsys, "--detach"))  # creates state.db
        st = State(w.db, Gateway(), pid=holder.pid)
        lease = st.lease(clone_lease(str(w.clone)), wait=1)
        try:
            before = settled(w)
            err = refused(w.create(capsys, "--name", "late"), 12)
            assert err["code"] == "busy"
            assert settled(w) == before
        finally:
            lease.release()
            st.close()
    finally:
        holder.terminate()  # the exact Popen we started
        holder.wait(timeout=10)


# -- filter drivers -----------------------------------------------------------------------------


def lfs_world(w: CW, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = w.tmp / "bin"
    bindir.mkdir()
    script(bindir / "git-lfs", "exit 0")
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    (w.home / ".gitconfig").write_text(
        '[filter "lfs"]\n\tprocess = git-lfs filter-process\n\tclean = git-lfs clean -- %f\n'
        "\trequired = true\n"
    )
    w.top["create"] = {"allow_filters": ["lfs"]}


def test_an_honored_driver_refuses_unsafe_repo_config(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    lfs_world(w, monkeypatch)
    w.g(["config", "lfs.url", "https://evil.test/lfs"])
    before = settled(w)
    err = refused(w.create(capsys), 12)
    assert err["code"] == "unsafe_repo_config" and err["details"]["keys"] == ["lfs.url"]
    assert settled(w) == before and w.records() == []


def test_an_honored_driver_refuses_unsafe_lfsconfig_of_the_checkout(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    lfs_world(w, monkeypatch)
    (w.work / ".lfsconfig").write_text("[lfs]\n\turl = https://evil.test/lfs\n")
    w.g(["add", "."], w.work)
    w.g(["commit", "-q", "-m", "lfsconfig"], w.work)
    w.g(["push", "-q", str(w.remote), "main"], w.work)
    before = settled(w)
    err = refused(w.create(capsys), 12)
    assert err["code"] == "unsafe_repo_config" and err["details"]["keys"] == ["lfs.url"]
    assert settled(w) == before and w.records() == []  # nothing checked out, nothing leaked


def test_the_driver_check_only_runs_for_an_honored_driver(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["config", "lfs.url", "https://evil.test/lfs"])
    ok(w.create(capsys))


# -- more vectors -------------------------------------------------------------------------------


def test_a_planted_promisor_cannot_fetch_during_the_checkout(w: CW, capsys) -> None:  # noqa: ANN001
    # a partial clone without a checkout: the blobs of `local-b` are missing, so `worktree add`
    # needs them, and the planted uploadpack is what a lazy fetch would run
    shutil_rm = __import__("shutil").rmtree
    shutil_rm(w.clone)
    plain_git(["clone", "-q", "--no-checkout", "--filter=blob:none", f"file://{w.remote}",
               str(w.clone)], cwd=w.tmp, home=w.home)  # fmt: skip
    w.g(["remote", "set-url", "origin", str(w.remote)])
    w.g(["branch", "local-b", "main"])
    markers = w.tmp / "markers"
    markers.mkdir()
    payload = script(w.tmp / "payload.sh", f'echo "$0 $*" >> {markers}/hit\nexit 1')
    w.g(["config", "remote.origin.uploadpack", payload])
    w.g(["config", "extensions.partialClone", "origin"])
    plain_git(["cat-file", "-p", "local-b:c1.txt"], cwd=w.clone, home=w.home)
    assert (markers / "hit").exists(), "the control did not fire: the lazy fetch is dead"
    (markers / "hit").unlink()
    before = settled(w)
    r = w.create(capsys, "--branch", "local-b")
    assert r.code in (0, 12), r.out
    assert sorted(os.listdir(markers)) == []
    if r.code == 12:
        assert settled(w) == before and w.records() == []


def test_a_worktree_with_a_forged_porcelain_path_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    forged = w.tmp / f"x\n\nworktree {w.tmp}/fake\nHEAD {w.shas['c3']}\nbranch refs/heads/zzz"
    r = plain_git(["worktree", "add", "-q", "--detach", str(forged)], cwd=w.clone, home=w.home)
    if r.returncode != 0:
        pytest.skip("this git refuses a newline in a worktree path")
    r = w.create(capsys, "--branch", "local-b")
    assert r.code == 1 and "worktree" in r.json()["error"]["message"]
    assert w.records() == []


def test_a_newline_in_the_rendered_path_is_create_failed(w: CW, capsys) -> None:  # noqa: ANN001
    w.top["worktree_path"] = "{root}/a\nb/{name}"
    r = w.create(capsys)
    assert r.code in (2, 12)
    assert w.records() == []


def test_golden_json_shape(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--name", "golden", "--label", "k=v", "--holder", "h"))
    rec = data["record"]
    for volatile in ("id", "created", "updated", "dir_id", "common_id", "admin_dir"):
        assert rec.pop(volatile)
    root = str(w.root.resolve() / "worktrees")
    assert rec == {
        "object_type": "worktree", "owned": True, "state": "active", "repo": REPO,
        "clone_path": str(w.clone), "common_dir": str(w.clone / ".git"),
        "base_dir": root, "root_id": None, "path": f"{root}/example.test/org/repo/golden",
        "name": "golden", "branch": "wt/golden", "created_branch": True,
        "base": "refs/heads/main", "base_sha": w.shas["c3"], "head_at_create": w.shas["c3"],
        "ref": None, "source": None, "depth": None, "fetch_url": str(w.remote),
        "remote": "origin", "labels": ["k=v"], "holders": ["h"], "baseline_digest": None,
        "bootstrap": {"state": "none", "steps": [], "created_paths_count": 0},
        "missing_since": None, "removed_at": None, "last_error": None,
    }  # fmt: skip
    assert set(data) == {"record", "branch_action", "bootstrap", "reused", "warnings", "dry_run"}
