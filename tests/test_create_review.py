"""Review findings of ``create`` (R1..R17): each behavior here failed before its fix."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from tests.createlib import CW, Spy, cli_spy
from tests.test_create import ok, refused
from tests.test_create_hostile import fail_sub, finish, leases, lfs_world, run_create, settled
from tests.vectorlib import plain_git
from wtctl.gateway import Gateway, GitResult
from wtctl.state import State


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def repo_dir(w: CW) -> Path:
    return w.root.resolve() / "worktrees" / "example.test" / "org" / "repo"


def planted_branch(argv: list[str]) -> str | None:
    """The branch a create-time git command is about to create (new or old command shape)."""
    for a in argv:
        if a.startswith("refs/heads/wt/"):
            return a
    if "-b" in argv:
        return "refs/heads/" + argv[argv.index("-b") + 1]
    return None


def plant_same_sha(w: CW, ref: str) -> None:
    g = ["--git-dir", str(w.clone / ".git")]
    plain_git([*g, "update-ref", ref, w.shas["c3"]], cwd=w.tmp, home=w.home)


# -- R1: the unwind never deletes a branch this call did not create --------------------------------


def test_r1_a_foreign_branch_at_the_same_sha_is_refused_not_deleted(w: CW) -> None:
    def foreign(sub, argv, kw):  # noqa: ANN001, ANN202
        creating = (sub == "update-ref" and "-d" not in argv) or (
            sub == "worktree" and "-b" in argv
        )
        ref = planted_branch(argv)
        if creating and ref is not None:
            plant_same_sha(w, ref)
        return None

    r = cli_spy(w, Spy(before=foreign))
    err = refused(r, 20, "branch_exists")
    assert err["details"]["tip"] == w.shas["c3"]
    assert any(k.startswith("refs/heads/wt/") for k in w.refs())  # the foreign branch survives
    assert w.records() == [] and leases(w) == []


def test_r1_the_branch_is_created_create_only_before_the_add(w: CW) -> None:
    spy = Spy()
    r = cli_spy(w, spy)
    assert r.code == 0, r.err
    calls = spy.calls
    mine = [i for i, (sub, a) in enumerate(calls) if sub == "update-ref" and planted_branch(a)]
    adds = [i for i, (sub, a) in enumerate(calls) if sub == "worktree" and "add" in a]
    assert len(mine) == 1 and len(adds) == 1 and mine[0] < adds[0]
    ref = calls[mine[0]][1]
    assert ref[-1] == "0" * 40 and ref[-2] == w.shas["c3"]
    add = calls[adds[0]][1]
    assert "-b" not in add and add[-1].startswith("wt/")


def test_r1_a_case_variant_branch_is_a_collision(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["branch", "Case-B", "main"])
    err = refused(w.create(capsys, "--branch", "case-b"), 20, "branch_exists")
    assert err["details"]["tip"] == w.shas["c3"]
    assert w.records() == []


def test_r1_a_case_variant_of_a_template_branch_is_a_collision(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["branch", "WT/casey", "main"])
    refused(w.create(capsys, "--name", "casey"), 20, "branch_exists")
    data = ok(w.create(capsys, "--detach", "--name", "other"))
    assert data["record"]["name"] == "other"


# -- R2: the upstream unwind only removes what it wrote --------------------------------------------


def test_r2_a_failed_track_keeps_preexisting_keys_of_the_section(w: CW) -> None:
    w.g(["config", "branch.feature/remote-only.description", "mine"])
    w.g(["config", "branch.feature/remote-only.rebase", "true"])
    r = cli_spy(
        w, Spy(before=fail_sub("worktree", containing="add")), "--branch", "feature/remote-only"
    )
    assert refused(r, 12)["code"] == "create_failed"
    assert w.g(["config", "branch.feature/remote-only.description"]) == "mine"
    assert w.g(["config", "branch.feature/remote-only.rebase"]) == "true"
    assert "refs/heads/feature/remote-only" not in w.refs()


def test_r2_unwind_leaves_a_key_someone_else_changed(w: CW) -> None:
    def change_then_fail(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "worktree" and "add" in argv:
            w.g(["config", "branch.feature/remote-only.remote", "elsewhere"])
            return GitResult(128, b"", b"fatal: injected\n")
        return None

    r = cli_spy(w, Spy(before=change_then_fail), "--branch", "feature/remote-only")
    assert refused(r, 12)["code"] == "create_failed"
    assert w.g(["config", "branch.feature/remote-only.remote"]) == "elsewhere"
    assert "branch.feature/remote-only.merge" not in w.g(["config", "--list"])


def test_r2_the_upstream_is_written_after_the_ref_is_owned(w: CW) -> None:
    def foreign(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "update-ref" and "-d" not in argv and "refs/heads/feature/remote-only" in argv:
            g = ["--git-dir", str(w.clone / ".git")]
            ref = "refs/heads/feature/remote-only"
            plain_git([*g, "update-ref", ref, w.shas["r1"]], cwd=w.tmp, home=w.home)
        return None

    r = cli_spy(w, Spy(before=foreign), "--branch", "feature/remote-only")
    refused(r, 20, "branch_exists")
    assert "branch.feature/remote-only" not in w.g(["config", "--list"])


# -- R3: a failure after the add keeps everything and says where -----------------------------------


def test_r3_a_failure_after_the_add_names_the_record(w: CW) -> None:
    def break_inspect(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "rev-parse" and "--absolute-git-dir" in argv:
            return GitResult(128, b"", b"fatal: injected\n")
        return None

    err = refused(cli_spy(w, Spy(before=break_inspect)), 12)
    [rec] = w.records()
    d = err["details"]
    assert err["code"] == "create_failed"
    assert d["record"] == rec["id"] and d["branch"] == rec["branch"] and d["path"] == rec["path"]


# -- R5: the unwind reports what it could not undo -------------------------------------------------


def test_r5_a_failed_branch_delete_is_reported(w: CW) -> None:
    def fail_both(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "update-ref" and "-d" in argv:
            return GitResult(1, b"", b"error: injected\n")
        if sub == "worktree" and "add" in argv:
            return GitResult(128, b"", b"fatal: injected\n")
        return None

    err = refused(cli_spy(w, Spy(before=fail_both)), 12)
    assert "cleanup incomplete" in err["message"]
    assert any(k.startswith("refs/heads/wt/") for k in w.refs())
    assert w.records() == []


def test_r5_a_failed_record_delete_does_not_mask_the_original(
    w: CW, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self, record_id, *, expected):  # noqa: ANN001, ANN202
        raise RuntimeError("db gone")

    monkeypatch.setattr(State, "delete_record", boom)
    err = refused(cli_spy(w, Spy(before=fail_sub("worktree", containing="add"))), 12)
    assert err["code"] == "create_failed"
    assert "cleanup incomplete" in err["message"]


def test_r5_an_admin_entry_naming_the_path_counts_as_added(w: CW) -> None:
    def lose_dotgit(sub, argv, kw, result):  # noqa: ANN001, ANN202
        if sub == "worktree" and "add" in argv:
            os.unlink(os.path.join(argv[-2], ".git"))
            return GitResult(128, b"", b"fatal: injected\n")
        return None

    r = cli_spy(w, Spy(after=lose_dotgit))
    refused(r, 12)
    [rec] = w.records()
    assert rec["state"] == "creating"
    assert w.admin_entries()  # the admin entry is still there for reconcile


# -- R6: losing the activation compare-and-set is internal -----------------------------------------


def test_r6_activation_cas_loss_is_internal(w: CW, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # noqa: ANN001
    from wtctl.errors import WtctlError

    orig = State.transition

    def lose(self, record_id, expected, new, **kw):  # noqa: ANN001, ANN202
        if new == "active":
            raise WtctlError("busy", "another command changed it first")
        return orig(self, record_id, expected, new, **kw)

    monkeypatch.setattr(State, "transition", lose)
    err = refused(w.create(capsys), 1)
    assert err["code"] == "internal"


# -- R7: paths inside or around any live worktree --------------------------------------------------


def plant_other(w: CW, path: Path, name: str) -> None:
    st = State(w.db, Gateway())
    rid = st.allocate_id()
    st.insert_record(
        {"id": rid, "object_type": "worktree", "state": "active", "repo": "example.test/org/other",
         "path": str(path), "name": name, "branch": None, "owned": True,
         "clone_path": str(w.tmp / "elsewhere")},
        holders=[],
    )  # fmt: skip
    st.close()


def test_r7_a_worktree_inside_another_repos_worktree_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--detach", "--name", "first"))  # state.db and the root
    plant_other(w, w.root.resolve() / "worktrees" / "example.test" / "org" / "other-wt", "ow")
    w.top["worktree_path"] = "{root}/example.test/org/other-wt/{name}"
    r = w.create(capsys, "--name", "sub")
    assert r.code == 2, r.out
    assert len(w.records()) == 2


def test_r7_a_worktree_around_another_repos_worktree_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--detach", "--name", "first"))
    plant_other(w, w.root.resolve() / "worktrees" / "example.test" / "org" / "deep" / "x", "ow")
    w.top["worktree_path"] = "{root}/example.test/org/{name}"
    assert w.create(capsys, "--name", "deep").code == 2


def test_r7_a_worktree_inside_one_of_another_known_clone_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--detach", "--name", "first"))
    clone2 = w.tmp / "clone2"
    plain_git(["clone", "-q", str(w.remote), str(clone2)], cwd=w.tmp, home=w.home)
    unmanaged = w.root.resolve() / "worktrees" / "example.test" / "org" / "otherwt"
    w.g(["worktree", "add", "-q", "--detach", str(unmanaged)], clone2)
    st = State(w.db, Gateway())
    st.put_clone("example.test/org/other", path=str(clone2), common_dir=str(clone2 / ".git"))
    st.close()
    w.top["worktree_path"] = "{root}/example.test/org/otherwt/{name}"
    assert w.create(capsys, "--name", "sub").code == 2


# -- R8: a parent swapped for a symlink after the checks -------------------------------------------


def test_r8_a_parent_swapped_after_the_add_is_anchor_changed(w: CW) -> None:
    host = w.root.resolve() / "worktrees" / "example.test"

    def swap(sub, argv, kw, result):  # noqa: ANN001, ANN202
        if sub == "worktree" and "add" in argv:
            host.rename(w.tmp / "moved-host")
            host.symlink_to(w.tmp / "moved-host")
        return None

    err = refused(cli_spy(w, Spy(after=swap)), 12)
    assert err["code"] == "create_failed" and err["details"]["cause"] == "anchor_changed"
    assert err["details"]["anchors"]
    [rec] = w.records()
    assert rec["state"] == "creating"  # after the add: the record stays for reconcile


# -- R9: the admin entry count only counts real entries --------------------------------------------


def test_r9_an_empty_admin_dir_cannot_hide_a_forged_entry(w: CW, capsys) -> None:  # noqa: ANN001
    # the path closes its own record and opens a second, well-formed one
    forged = w.tmp / f"x\nHEAD {w.shas['c3']}\ndetached\n\nworktree {w.tmp}/fake"
    r = plain_git(["worktree", "add", "-q", "--detach", str(forged)], cwd=w.clone, home=w.home)
    if r.returncode != 0:
        pytest.skip("this git refuses a newline in a worktree path")
    (w.clone / ".git" / "worktrees" / "zz").mkdir()
    r2 = w.create(capsys, "--branch", "local-b")
    assert r2.code == 1 and "admin entries" in r2.json()["error"]["message"]
    assert w.records() == []


# -- R10, R11, R12 ---------------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_r10_an_unlistable_path_is_create_failed(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--detach", "--name", "first"))
    target = repo_dir(w) / "locked"
    target.mkdir()
    target.chmod(0)
    try:
        err = refused(w.create(capsys, "--name", "locked"), 12)
    finally:
        target.chmod(0o755)
    assert err["code"] == "create_failed"


def commit_lfsconfig(w: CW, how: str) -> None:
    p = w.work / ".lfsconfig"
    if how == "symlink":
        p.symlink_to("/etc/hostname")
    elif how == "dir":
        p.mkdir()
        (p / "f").write_text("x")
    else:
        p.write_bytes(b"[lfs\n\turl = \x00\n")
    w.g(["add", "."], w.work)
    w.g(["commit", "-q", "-m", "lfsconfig"], w.work)
    w.g(["push", "-q", str(w.remote), "main"], w.work)


@pytest.mark.parametrize("how", ["symlink", "dir"])
def test_r11_a_non_regular_lfsconfig_is_unsafe_repo_config(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
    how: str,  # noqa: ANN001
) -> None:
    lfs_world(w, monkeypatch)
    commit_lfsconfig(w, how)
    before = settled(w)
    err = refused(w.create(capsys), 12)
    assert err["code"] == "unsafe_repo_config" and err["details"]["keys"] == []
    assert settled(w) == before and w.records() == []


def test_r11_an_unparseable_lfsconfig_is_create_failed(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    lfs_world(w, monkeypatch)
    commit_lfsconfig(w, "garbage")
    before = settled(w)
    err = refused(w.create(capsys), 12)
    assert err["code"] == "create_failed"
    assert settled(w) == before and w.records() == []


def test_r12_a_state_read_error_fails_closed(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl.commands.create import Creator

    def broken(self):  # noqa: ANN001, ANN202
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Creator, "_clone_rows", broken)
    err = refused(w.create(capsys), 1)
    assert err["code"] == "internal"
    assert w.records() == []


# -- R13, R14, R16 ---------------------------------------------------------------------------------


def test_r13_userinfo_urls_in_git_stderr_are_scrubbed(w: CW) -> None:
    def leak(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "worktree" and "add" in argv:
            msg = b"fatal: unable to access 'https://bob:hunter2@example.test/x.git/': boom\n"
            return GitResult(128, b"", msg)
        return None

    r = cli_spy(w, Spy(before=leak))
    err = refused(r, 12)
    assert "hunter2" not in r.out and "hunter2" not in r.err
    assert err["code"] == "create_failed"


@pytest.mark.parametrize("name", ["post-remove", "pre-remove", "post-reconcile"])
def test_r16_hooks_that_cannot_fire_on_create_are_usage_errors(
    w: CW,
    capsys,
    name: str,  # noqa: ANN001
) -> None:
    r = w.create(capsys, "--hook", f"{name}=true")
    assert r.code == 2, r.out
    assert w.records() == []


# -- R17: more coverage ----------------------------------------------------------------------------


def test_explicit_name_reuse_negatives(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--name", "n1", "--holder", "h1"))
    refused(w.create(capsys, "--name", "n1", "--holder", "h2"), 20, "path_exists")
    refused(w.create(capsys, "--name", "n1"), 20, "path_exists")
    rid = w.plant_creating(branch=None, name="crashed", holder="h1")
    err = refused(w.create(capsys, "--name", "crashed", "--holder", "h1"), 20, "path_exists")
    assert err["holder_id"] == rid
    first = ok(w.create(capsys, "--name", "n3", "--holder", "h1"))["record"]
    w.set_state(first["id"], "unlinked")
    refused(w.create(capsys, "--name", "n3", "--holder", "h1"), 20, "path_exists")


def test_explicit_names_collide_ignoring_case(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--name", "Foo"))
    refused(w.create(capsys, "--name", "foo"), 20, "path_exists")


def test_holder_retry_when_the_template_gives_one_path_for_two_names(w: CW, capsys) -> None:  # noqa: ANN001
    first = ok(w.create(capsys, "--name", "x1", "--holder", "h1"))
    w.top["worktree_path"] = "{root}/example.test/org/repo/x{name}"  # name "1" -> the same path
    again = ok(w.create(capsys, "--name", "1", "--holder", "h1"))
    assert again["reused"] is True and again["record"]["id"] == first["record"]["id"]
    refused(w.create(capsys, "--name", "1", "--holder", "h2"), 20, "path_exists")


def test_two_derived_name_racers_get_distinct_names(w: CW) -> None:
    w.top["lease_wait"] = "120s"
    procs = [run_create(w, "--branch", "a/b"), run_create(w, "--branch", "a-b")]
    assert [finish(p)[0] for p in procs] == [0, 0]
    assert sorted(r["name"] for r in w.records()) == ["a-b", "a-b-2"]
    assert leases(w) == []


def test_a_failed_mirror_refresh_is_fetch_failed(w: CW) -> None:
    before = settled(w)
    r = cli_spy(w, Spy(before=fail_sub("fetch")))
    assert refused(r, 12)["code"] == "fetch_failed"
    assert settled(w) == before and w.records() == []


def test_a_mirror_refresh_asking_for_credentials_is_auth_required(w: CW) -> None:
    def auth(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "fetch":
            return GitResult(128, b"", b"fatal: Authentication failed for 'https://x.test/'\n")
        return None

    r = cli_spy(w, Spy(before=auth))
    assert refused(r, 12)["code"] == "auth_required"
    assert w.records() == []


def test_an_old_git_is_git_too_old(w: CW) -> None:
    def old(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "--version":
            return GitResult(0, b"git version 2.10.0\n", b"")
        return None

    r = cli_spy(w, Spy(before=old))
    assert refused(r, 12)["code"] == "git_too_old"
    assert w.records() == []


def test_a_sha256_clone_is_refused_by_create(w: CW, capsys) -> None:  # noqa: ANN001
    shutil.rmtree(w.clone)
    w.clone.mkdir()
    r = plain_git(["init", "-q", "--object-format=sha256", "-b", "main"], cwd=w.clone, home=w.home)
    if r.returncode != 0:
        pytest.skip("this git cannot create sha256 repositories")
    w.g(["remote", "add", "origin", str(w.remote)])
    err = refused(w.create(capsys), 12)
    assert err["code"] == "create_failed" and err["details"]["cause"] == "object_format"
    assert w.records() == []


def test_a_dry_run_without_a_clone_is_not_found_and_clones_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg = {"clone_url": str(w.remote)}
    r = w.create(capsys, "--dry-run")
    assert r.code == 10, r.out
    assert w.count("records") == 0 and w.count("clones") == 0


# -- targeted round 2 ------------------------------------------------------------------------------


def test_t1_unwind_restores_a_preexisting_upstream(w: CW) -> None:
    w.g(["config", "branch.feature/remote-only.remote", "elsewhere"])
    w.g(["config", "branch.feature/remote-only.merge", "refs/heads/other"])
    r = cli_spy(
        w, Spy(before=fail_sub("worktree", containing="add")), "--branch", "feature/remote-only"
    )
    assert refused(r, 12)["code"] == "create_failed"
    assert w.g(["config", "branch.feature/remote-only.remote"]) == "elsewhere"
    assert w.g(["config", "branch.feature/remote-only.merge"]) == "refs/heads/other"


def test_t2_a_broken_other_clone_does_not_block_create(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--detach", "--name", "first"))
    bad = w.tmp / "badclone"
    (bad / ".git" / "worktrees" / "x").mkdir(parents=True)
    (bad / ".git" / "worktrees" / "x" / "gitdir").write_text(f"{w.tmp}/gone/.git\n")
    st = State(w.db, Gateway())
    st.put_clone("example.test/org/bad", path=str(bad), common_dir=str(bad / ".git"))
    st.close()
    ok(w.create(capsys, "--detach", "--name", "second"))


def test_t2_an_unreadable_other_clone_names_it(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--detach", "--name", "first"))
    bad = w.tmp / "badclone"
    (bad / ".git").mkdir(parents=True)
    (bad / ".git" / "worktrees").write_text("not a directory")
    st = State(w.db, Gateway())
    st.put_clone("example.test/org/bad", path=str(bad), common_dir=str(bad / ".git"))
    st.close()
    r = w.create(capsys, "--detach", "--name", "second")
    assert r.code != 0
    assert "badclone" in r.out + r.err


def test_t3_an_unreadable_admin_dir_after_a_failed_add_keeps_the_record(
    w: CW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import worktrees

    armed = {"on": False}

    def fail_add(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "worktree" and "add" in argv:
            armed["on"] = True
            return GitResult(128, b"", b"fatal: injected\n")
        return None

    real = worktrees.admin_gitdirs

    def scan(common: str) -> list[str]:
        if armed["on"]:
            raise PermissionError(13, "denied")
        return real(common)

    monkeypatch.setattr(worktrees, "admin_gitdirs", scan)
    err = refused(cli_spy(w, Spy(before=fail_add)), 12)
    assert err["code"] == "create_failed"
    [rec] = w.records()
    assert rec["state"] == "creating"
    assert any(k.startswith("refs/heads/wt/") for k in w.refs())


def test_t4_a_timed_out_update_ref_that_created_the_branch_is_unwound(w: CW) -> None:
    def time_out(sub, argv, kw, result):  # noqa: ANN001, ANN202
        if sub == "update-ref" and "-d" not in argv and result.returncode == 0:
            return GitResult(-9, b"", b"", timed_out=True)
        return None

    err = refused(cli_spy(w, Spy(after=time_out)), 12)
    assert err["code"] == "create_failed"
    assert not any(k.startswith("refs/heads/wt/") for k in w.refs())
    assert w.records() == []


def test_t5_an_interrupt_after_the_add_is_not_its_own_cause(w: CW) -> None:
    def interrupt(sub, argv, kw, result):  # noqa: ANN001, ANN202
        if sub == "worktree" and "add" in argv:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt) as ei:
        cli_spy(w, Spy(after=interrupt))
    assert ei.value.__cause__ is not ei.value


def test_t5_the_kept_message_scrubs_urls(w: CW) -> None:
    def leak(sub, argv, kw, result):  # noqa: ANN001, ANN202
        if sub == "worktree" and "add" in argv:
            raise RuntimeError("boom https://user:s3cret@host.test/x.git")

    err = refused(cli_spy(w, Spy(after=leak)), 12)
    assert "s3cret" not in err["message"]
