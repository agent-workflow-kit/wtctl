"""``adopt`` (docs/interface.md, adopt), against real git and no network."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from tests.adoptlib import adopt, err_of, foreign, inproc_baseline, ok_adopt, world
from tests.createlib import CW, REPO
from tests.schemacheck import validate


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    inproc_baseline(monkeypatch)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return world(tmp_path, private_home)


def rec_of(w: CW, capsys, path: Path | str, *argv: str, **kw) -> dict:  # noqa: ANN001, ANN003
    return ok_adopt(adopt(w, capsys, path, *argv, **kw))["record"]


# -- the plain cases ---------------------------------------------------------------------------


def test_adopt_a_linked_worktree(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w, "ext")
    data = ok_adopt(adopt(w, capsys, path))
    r = data["record"]
    assert data["existing"] is False and data["dry_run"] is False
    assert r["object_type"] == "worktree" and r["owned"] is False and r["state"] == "active"
    assert r["repo"] == REPO and r["path"] == str(path) and r["name"] == "ext"
    assert r["branch"] == "b-ext" and r["created_branch"] is False
    assert r["clone_path"] == str(w.clone.resolve())
    assert r["common_dir"] == str(w.clone.resolve() / ".git")
    assert r["admin_dir"] == str(w.clone.resolve() / ".git" / "worktrees" / "ext")
    assert r["base_dir"] is None and r["root_id"] is None
    assert r["head_at_create"] == w.shas["c3"]
    assert r["fetch_url"] == str(w.remote) and r["remote"] == "origin"
    assert r["holders"] == [] and r["bootstrap"]["state"] == "none"
    st = os.stat(path)
    assert r["dir_id"] == f"{st.st_dev}:{st.st_ino}"
    cst = os.stat(w.clone / ".git")
    assert r["common_id"] == f"{cst.st_dev}:{cst.st_ino}"
    assert len(r["baseline_digest"]) == 64
    # the worktree was not changed
    assert (path / ".git").is_file()


def test_adopt_the_main_worktree(w: CW, capsys) -> None:  # noqa: ANN001
    r = rec_of(w, capsys, w.clone)
    assert r["owned"] is False and r["admin_dir"] is None
    assert r["path"] == str(w.clone.resolve()) and r["name"] == "clone"
    assert r["common_dir"] == r["path"] + "/.git"
    assert r["branch"] == "main" and r["head_at_create"] == w.shas["c3"]


def test_a_detached_worktree_has_no_branch(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w, "det")
    w.g(["checkout", "-q", "--detach", "HEAD~1"], path)
    r = rec_of(w, capsys, path)
    assert r["branch"] is None and r["head_at_create"] == w.shas["c2"]


def test_the_baseline_stash_and_reflog_start_are_recorded(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w, "ext")
    (path / "s.txt").write_text("s")
    w.g(["add", "s.txt"], path)
    w.g(["stash"], path)
    stash = w.g(["rev-parse", "refs/stash"])
    r = rec_of(w, capsys, path)
    rows = w.rows("SELECT * FROM baseline_items WHERE record_id = ?", r["id"])
    assert rows and w.rows("SELECT baseline_digest FROM records")[0][0] == r["baseline_digest"]
    assert [x["oid"] for x in w.rows("SELECT oid FROM stash_ids WHERE record_id = ?", r["id"])] == [
        stash
    ]
    row = w.rows("SELECT reflog_start_ts, reflog_start_sha FROM records")[0]
    assert row["reflog_start_ts"] and len(row["reflog_start_sha"]) == 40


def test_baseline_check_passes_then_sees_a_later_change(w: CW, capsys) -> None:  # noqa: ANN001
    (w.clone / ".git" / "hooks" / "planted-before").write_text("#!/bin/sh\n")
    path = foreign(w, "ext")
    r = rec_of(w, capsys, path)
    assert w.cli(capsys, "baseline", "check", r["id"]).code == 0  # recorded as it was
    (w.clone / ".git" / "hooks" / "planted-after").write_text("#!/bin/sh\n")
    assert (
        err_of(w.cli(capsys, "baseline", "check", r["id"]), 20)["reason"] == "repo_config_changed"
    )


def test_no_hooks_or_git_config_run_during_adopt(w: CW, capsys, tmp_path: Path) -> None:  # noqa: ANN001
    path = foreign(w, "ext")  # plain git ran its own hooks while making this
    marker = tmp_path / "marker"
    hook = tmp_path / "evil.sh"
    hook.write_text(f"#!/bin/sh\necho ran >> {marker}\n")
    hook.chmod(0o755)
    for key in ("core.fsmonitor", "core.pager", "core.editor", "diff.external",
                "credential.helper", "core.sshCommand", "gpg.program"):  # fmt: skip
        w.g(["config", key, str(hook)])
    w.g(["config", "core.hooksPath", str(tmp_path)])
    for name in ("reference-transaction", "post-checkout", "post-commit", "pre-commit"):
        for d in (tmp_path, w.clone / ".git" / "hooks"):
            (d / name).write_text(f"#!/bin/sh\necho ran >> {marker}\n")
            os.chmod(d / name, 0o755)
    (w.clone / ".git" / "config.worktree").write_text(f"[core]\n\tfsmonitor = {hook}\n")
    marker.unlink(missing_ok=True)
    rec_of(w, capsys, path)
    rec_of(w, capsys, w.clone)
    assert not marker.exists()


def test_the_real_child_process_runs_the_baseline(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl import baselinesvc

    monkeypatch.setattr(baselinesvc, "RUNNER", None)
    r = rec_of(w, capsys, foreign(w, "ext"))
    assert len(r["baseline_digest"]) == 64


# -- names, labels, holders ---------------------------------------------------------------------


def test_name_is_explicit_or_derived_and_suffixed(w: CW, capsys) -> None:  # noqa: ANN001
    a = foreign(w, "dup")
    b = w.tmp / "other" / "dup"
    b.parent.mkdir()
    w.g(["worktree", "add", "-q", "-b", "b2", str(b)])
    assert rec_of(w, capsys, a)["name"] == "dup"
    assert rec_of(w, capsys, b.resolve())["name"] == "dup-2"
    c = foreign(w, "c")
    assert rec_of(w, capsys, c, "--name", "Chosen.1")["name"] == "Chosen.1"


def test_an_explicit_name_that_collides_is_path_exists_with_holder_id(w: CW, capsys) -> None:  # noqa: ANN001
    first = rec_of(w, capsys, foreign(w, "one"), "--name", "same")
    second = foreign(w, "two")
    err = err_of(adopt(w, capsys, second, "--name", "SAME"), 20, "path_exists")
    assert err["holder_id"] == first["id"]
    assert w.count("records") == 1


def test_a_weird_basename_is_sanitized(w: CW, capsys) -> None:  # noqa: ANN001
    base = w.tmp / "foreign"
    base.mkdir()
    path = base / "-.weird name!"
    w.g(["worktree", "add", "-q", "-b", "weird", str(path)])
    name = rec_of(w, capsys, path.resolve())["name"]
    assert name and name[0].isalnum() and " " not in name and "!" not in name


def test_labels_are_recorded(w: CW, capsys) -> None:  # noqa: ANN001
    r = rec_of(w, capsys, foreign(w), "--label", "b=2", "--label", "a=1", "--label", "a=1")
    assert r["labels"] == ["a=1", "b=2"]


def test_holders_are_reference_counted_across_adopts(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    first = ok_adopt(adopt(w, capsys, path, "--holder", "agent#1"))
    assert first["existing"] is False and first["record"]["holders"] == ["agent#1"]
    second = ok_adopt(adopt(w, capsys, path, "--holder", "agent#2", "--name", "ignored"))
    assert second["existing"] is True and second["record"]["id"] == first["record"]["id"]
    assert second["record"]["holders"] == ["agent#1", "agent#2"]
    assert second["record"]["name"] == first["record"]["name"]
    again = ok_adopt(adopt(w, capsys, path, "--holder", "agent#2"))
    assert again["record"]["holders"] == ["agent#1", "agent#2"]
    bare = ok_adopt(adopt(w, capsys, path))
    assert bare["existing"] is True and bare["record"]["holders"] == ["agent#1", "agent#2"]
    assert w.count("records") == 1


def test_adopting_an_owned_path_adds_the_holder_and_reports_it_owned(w: CW, capsys) -> None:  # noqa: ANN001
    created = json.loads(w.create(capsys).out)["record"]
    data = ok_adopt(adopt(w, capsys, created["path"], "--holder", "me"))
    assert data["existing"] is True and data["record"]["owned"] is True
    assert data["record"]["holders"] == ["me"]


def test_existing_labels_are_ignored(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    rec_of(w, capsys, path)
    r = ok_adopt(adopt(w, capsys, path, "--label", "x=1"))["record"]
    assert r["labels"] == []


# -- paths ---------------------------------------------------------------------------------------


def test_a_symlinked_path_is_recorded_by_realpath_and_found_again(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w, "real")
    link = w.tmp / "link"
    link.symlink_to(path)
    first = ok_adopt(adopt(w, capsys, link))
    assert first["record"]["path"] == str(path)
    again = ok_adopt(adopt(w, capsys, path, "--holder", "h"))
    assert again["existing"] is True and again["record"]["id"] == first["record"]["id"]


def test_a_symlinked_ancestor_is_resolved(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w, "real")
    (w.tmp / "via").symlink_to(path.parent)
    r = rec_of(w, capsys, w.tmp / "via" / "real")
    assert r["path"] == str(path)


def test_a_relative_path_is_taken_from_the_working_directory(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    path = foreign(w, "rel")
    monkeypatch.chdir(path.parent)
    assert rec_of(w, capsys, "rel")["path"] == str(path)


def test_a_missing_path_or_a_plain_file_is_not_a_git_worktree(w: CW, capsys) -> None:  # noqa: ANN001
    err_of(adopt(w, capsys, w.tmp / "nope"), 10)
    f = w.tmp / "afile"
    f.write_text("x")
    err_of(adopt(w, capsys, f), 10)
    d = w.tmp / "plain"
    d.mkdir()
    err_of(adopt(w, capsys, d), 10)
    assert w.count("records") == 0


def test_a_subdirectory_is_a_usage_error_naming_the_top_level(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    sub = path / "deep" / "er"
    sub.mkdir(parents=True)
    err = err_of(adopt(w, capsys, sub), 2)
    assert str(path) in err["message"]
    assert w.count("records") == 0


def test_a_submodule_checkout_is_a_usage_error(w: CW, capsys) -> None:  # noqa: ANN001
    sub = w.tmp / "subm"
    sub.mkdir()
    gd = w.clone / ".git" / "modules" / "subm"
    gd.mkdir(parents=True)
    (sub / ".git").write_text(f"gitdir: {gd}\n")
    err = err_of(adopt(w, capsys, sub), 2)
    assert "submodule" in err["message"]


def test_a_relative_submodule_pointer_is_a_usage_error(w: CW, capsys) -> None:  # noqa: ANN001
    sub = w.tmp / "relsub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
    err = err_of(adopt(w, capsys, sub), 2)
    assert "submodule" in err["message"]


def test_a_submodule_named_like_a_worktree_entry_is_a_usage_error(w: CW, capsys) -> None:  # noqa: ANN001
    sub = w.tmp / "nested"
    sub.mkdir()
    gd = w.clone / ".git" / "modules" / "a" / "worktrees" / "b"
    gd.mkdir(parents=True)
    (sub / ".git").write_text(f"gitdir: {gd}\n")
    err = err_of(adopt(w, capsys, sub), 2)
    assert "submodule" in err["message"]


def test_a_separate_git_dir_under_a_modules_directory_is_not_a_submodule(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    path = w.tmp / "sepgit"
    path.mkdir()
    (path / ".git").write_text(f"gitdir: {w.tmp}/modules/proj.git\n")
    err_of(adopt(w, capsys, path), 20, "unlinked")


def test_a_path_in_the_throwaway_root_or_state_dir_is_a_usage_error(w: CW, capsys) -> None:  # noqa: ANN001
    tr = w.root / "throwaway" / "wt-aaaaaaaa"
    tr.mkdir(parents=True)
    err_of(adopt(w, capsys, tr), 2)
    state_dir = w.db.parent
    state_dir.mkdir(parents=True, exist_ok=True)
    err_of(adopt(w, capsys, state_dir), 2)
    (state_dir / "mirrors").mkdir(exist_ok=True)
    err_of(adopt(w, capsys, state_dir / "mirrors"), 2)


def test_a_throwaway_record_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import Gateway
    from wtctl.state import State

    d = w.tmp / "elsewhere"
    d.mkdir()
    st = State(w.db, Gateway())
    rid = st.allocate_id()
    st.insert_record(
        {"id": rid, "object_type": "throwaway", "state": "active", "repo": REPO, "path": str(d)}
    )
    st.close()
    err_of(adopt(w, capsys, d), 2)


def test_a_path_with_a_newline_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    path = w.tmp / "foreign" / "a\nb"
    path.parent.mkdir()
    w.g(["worktree", "add", "-q", "-b", "nl", str(path)])
    err_of(adopt(w, capsys, path), 2)
    assert w.count("records") == 0


def test_nested_inside_or_around_another_worktree_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    outer = foreign(w, "outer")
    rec_of(w, capsys, outer)
    inner = outer / "inner"
    w.g(["worktree", "add", "-q", "-b", "inner", str(inner)])
    err_of(adopt(w, capsys, inner), 2)
    # the main worktree contains a linked one
    lin = w.clone / "sub-wt"
    w.g(["worktree", "add", "-q", "-b", "sub", str(lin)])
    err_of(adopt(w, capsys, w.clone), 2)
    err_of(adopt(w, capsys, lin), 2)
    assert w.count("records") == 1


# -- directories that only look like worktrees ---------------------------------------------------


def test_a_forged_dotgit_pointer_to_another_worktrees_admin_entry(w: CW, capsys) -> None:  # noqa: ANN001
    real = foreign(w, "real")
    fake = w.tmp / "fake"
    fake.mkdir()
    (fake / ".git").write_text((real / ".git").read_text())
    err_of(adopt(w, capsys, fake), 20, "unlinked")
    assert w.count("records") == 0


def test_a_forged_dotgit_pointer_to_a_missing_admin_entry(w: CW, capsys) -> None:  # noqa: ANN001
    fake = w.tmp / "fake"
    fake.mkdir()
    (fake / ".git").write_text(f"gitdir: {w.clone.resolve()}/.git/worktrees/nothing\n")
    err_of(adopt(w, capsys, fake), 20, "unlinked")


def test_an_admin_entry_whose_back_pointer_was_forged(w: CW, capsys) -> None:  # noqa: ANN001
    real = foreign(w, "real")
    moved = w.tmp / "moved"
    os.rename(real, moved)  # the admin entry still names the old path
    err_of(adopt(w, capsys, moved), 20, "unlinked")


def test_a_commondir_pointing_at_another_repository(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w, "real")
    other = w.tmp / "other.git"
    w.g(["init", "-q", "--bare", str(other)], w.tmp)
    admin = w.clone / ".git" / "worktrees" / "real"
    (admin / "commondir").write_text(str(other) + "\n")
    err_of(adopt(w, capsys, path), 20, "unlinked")
    assert w.count("records") == 0


def test_a_dotgit_symlink_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    real = foreign(w, "real")
    fake = w.tmp / "fake"
    fake.mkdir()
    (fake / ".git").symlink_to(real / ".git")
    err_of(adopt(w, capsys, fake), 20, "unlinked")


def test_a_relative_dotgit_pointer_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w, "real")
    (path / ".git").write_text("gitdir: ../../../clone/.git/worktrees/real\n")
    err_of(adopt(w, capsys, path), 20, "unlinked")


def test_a_dotgit_file_that_is_not_a_pointer_is_not_a_worktree(w: CW, capsys) -> None:  # noqa: ANN001
    fake = w.tmp / "fake"
    fake.mkdir()
    (fake / ".git").write_text("hello\n")
    err_of(adopt(w, capsys, fake), 10)


def test_a_planted_main_worktree_is_not_the_resolved_clone(w: CW, capsys) -> None:  # noqa: ANN001
    fake = w.tmp / "fake"
    fake.mkdir()
    w.g(["init", "-q", "-b", "main"], fake)
    w.g(["remote", "add", "origin", str(w.remote)], fake)
    err = err_of(adopt(w, capsys, fake), 12)
    assert err["code"] == "remote_mismatch"
    assert w.count("records") == 0


def test_a_worktree_of_a_clone_wtctl_cannot_resolve(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    err_of(w.cli(capsys, "adopt", str(path), "--repo", "example.test/other/thing"), 10)
    assert w.count("records") == 0


def test_a_clone_without_the_origin_remote_needs_a_repo(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    w.g(["remote", "rename", "origin", "up"])
    err = err_of(adopt(w, capsys, path, repo=False), 12)
    assert err["code"] == "remote_mismatch" and err["details"]["cause"] == "no_remote"
    w.g(["remote", "rename", "up", "origin"])


def test_the_repo_is_derived_from_the_origin_url(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    r = rec_of(w, capsys, path, repo=False)  # clone_url maps the local remote to REPO
    assert r["repo"] == REPO


def test_the_repo_is_derived_from_an_https_origin(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg = {"clone": str(w.clone)}
    w.g(["remote", "set-url", "origin", "https://user:secret@Example.test/org/repo.git"])
    path = foreign(w)
    r = rec_of(w, capsys, path, repo=False)
    assert r["repo"] == REPO and "secret" not in json.dumps(r)


def test_a_locked_worktree_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    w.g(["worktree", "lock", "--reason", "on a stick", str(path)])
    err = err_of(adopt(w, capsys, path), 20, "locked")
    assert err["details"]["lock_reason"] == "on a stick"
    assert w.count("records") == 0


def test_an_unborn_head_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    w.g(["checkout", "-q", "--orphan", "fresh"], path)
    err_of(adopt(w, capsys, path), 12)
    assert w.count("records") == 0


def test_a_sha256_clone_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    import shutil

    from tests.vectorlib import plain_git

    shutil.rmtree(w.clone)
    w.clone.mkdir()
    r = plain_git(["init", "-q", "--object-format=sha256", "-b", "main"], cwd=w.clone, home=w.home)
    if r.returncode != 0:
        pytest.skip("this git cannot create sha256 repositories")
    w.g(["remote", "add", "origin", str(w.remote)])
    err = err_of(adopt(w, capsys, w.clone), 12)
    assert err["code"] == "create_failed" and err["details"]["cause"] == "object_format"


def test_a_changed_common_dir_identity_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    rec_of(w, capsys, foreign(w, "first"))  # records the clone's identity
    conn = sqlite3.connect(w.db)
    conn.execute("UPDATE clones SET common_id = '1:2'")
    conn.commit()
    conn.close()
    err = err_of(adopt(w, capsys, path), 12)
    assert err["code"] == "create_failed" and err["details"]["cause"] == "anchor_changed"
    assert w.count("records") == 1


def test_a_worktree_on_a_fuse_file_system_is_refused(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl.commands import adopt as adopt_cmd

    path = foreign(w)
    real = adopt_cmd.describe_mount

    def fake(p: str, st: os.stat_result) -> dict[str, str]:
        info = real(p, st)
        return {**info, "fs_type": "fuse.sshfs"} if p == str(path) else info

    monkeypatch.setattr(adopt_cmd, "describe_mount", fake)
    err_of(adopt(w, capsys, path), 20, "unlinked")
    assert w.count("records") == 0


def test_a_mount_point_worktree_is_refused(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl import fsafe

    path = foreign(w)
    real = fsafe.Fs.mount_table

    def table(self):  # noqa: ANN001, ANN202
        t = real(self)
        from wtctl.gateway import MountEntry

        return [*(t or []), MountEntry(99, str(path), "ext4", "/dev/x")]

    monkeypatch.setattr(fsafe.Fs, "mount_table", table)
    err_of(adopt(w, capsys, path), 20, "unlinked")
    assert w.count("records") == 0


# -- record states for an already recorded path --------------------------------------------------


def adopted(w: CW, capsys) -> tuple[Path, dict]:  # noqa: ANN001
    path = foreign(w)
    return path, rec_of(w, capsys, path)


@pytest.mark.parametrize("state", ["unlinked", "clone_missing", "partial"])
def test_other_live_states_refuse_unlinked(w: CW, capsys, state: str) -> None:  # noqa: ANN001
    path, r = adopted(w, capsys)
    w.set_state(r["id"], state)
    err_of(adopt(w, capsys, path, "--holder", "h"), 20, "unlinked")
    assert w.rows("SELECT count(*) FROM holders")[0][0] == 0


def test_a_missing_record_is_not_a_git_worktree(w: CW, capsys) -> None:  # noqa: ANN001
    path, r = adopted(w, capsys)
    w.set_state(r["id"], "missing")
    err_of(adopt(w, capsys, path), 10)


def test_a_creating_record_is_path_exists_with_holder_id(w: CW, capsys) -> None:  # noqa: ANN001
    path, r = adopted(w, capsys)
    w.set_state(r["id"], "creating")
    err = err_of(adopt(w, capsys, path), 20, "path_exists")
    assert err["holder_id"] == r["id"]


def test_a_creating_record_with_a_live_creator_is_busy(w: CW, capsys) -> None:  # noqa: ANN001
    from wtctl.gateway import Gateway
    from wtctl.state import State, record_lease

    path, r = adopted(w, capsys)
    w.set_state(r["id"], "creating")
    st = State(w.db, Gateway())
    lease = st.lease(record_lease(r["id"]), wait=1)
    try:
        err_of(adopt(w, capsys, path), 12)
    finally:
        lease.release()
        st.close()


@pytest.mark.parametrize("state", ["forgotten", "removed", "gone"])
def test_a_tombstoned_path_is_adopted_anew(w: CW, capsys, state: str) -> None:  # noqa: ANN001
    path, r = adopted(w, capsys)
    w.set_state(r["id"], state)
    again = ok_adopt(adopt(w, capsys, path))
    assert again["existing"] is False and again["record"]["id"] != r["id"]
    assert again["record"]["name"] == "ext"  # the tombstone does not hold the name


@pytest.mark.parametrize(
    ("state", "code", "reason"),
    [
        ("active", 0, None),
        ("creating", 20, "path_exists"),
        ("unlinked", 20, "unlinked"),
        ("partial", 20, "unlinked"),
        ("missing", 10, None),
    ],
)
def test_a_recorded_path_follows_its_record_state_even_when_the_directory_is_gone(
    w: CW,
    capsys,
    state: str,
    code: int,
    reason: str | None,  # noqa: ANN001
) -> None:
    import shutil

    path, r = adopted(w, capsys)
    w.set_state(r["id"], state)
    shutil.rmtree(path)
    out = adopt(w, capsys, path)
    if code == 0:
        assert ok_adopt(out)["existing"] is True
    else:
        err_of(out, code, reason)


@pytest.mark.parametrize("state", ["creating", "unlinked", "missing"])
def test_dry_run_on_a_recorded_path_reports_the_record_state(w: CW, capsys, state: str) -> None:  # noqa: ANN001
    path, r = adopted(w, capsys)
    w.set_state(r["id"], state)
    expect = {"creating": (20, "path_exists"), "unlinked": (20, "unlinked"), "missing": (10, None)}
    code, reason = expect[state]
    err_of(adopt(w, capsys, path, "--dry-run"), code, reason)


def test_a_lost_clone_lease_refuses_busy_and_stores_nothing(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    from wtctl import baselinesvc

    path = foreign(w)
    real = baselinesvc.record_current

    def steal(*a, **kw):  # noqa: ANN002, ANN003
        out = real(*a, **kw)
        conn = sqlite3.connect(w.db)
        conn.execute("UPDATE leases SET pid = pid + 1000000, start_time = 'other'")
        conn.commit()
        conn.close()
        return out

    monkeypatch.setattr(baselinesvc, "record_current", steal)
    assert err_of(adopt(w, capsys, path), 12)["code"] == "busy"
    assert w.count("records") == 0


def test_a_path_that_is_not_utf8_encodable_is_a_usage_error_before_any_lease(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    err_of(adopt(w, capsys, "/tmp/bad\udcffname"), 2)
    assert w.count("records") == 0


def test_a_submodule_gitdir_under_a_path_containing_worktrees_is_still_a_submodule(
    w: CW,
    capsys,  # noqa: ANN001
) -> None:
    sub = w.tmp / "subm2"
    sub.mkdir()
    gd = w.tmp / "worktrees" / "super" / ".git" / "modules" / "subm2"
    gd.mkdir(parents=True)
    (sub / ".git").write_text(f"gitdir: {gd}\n")
    err = err_of(adopt(w, capsys, sub), 2)
    assert "submodule" in err["message"]


# -- dry runs ------------------------------------------------------------------------------------


def test_dry_run_changes_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    path = foreign(w)
    data = ok_adopt(adopt(w, capsys, path, "--dry-run", "--holder", "h"))
    assert data["dry_run"] is True and data["existing"] is False
    assert data["record"]["holders"] == ["h"] and data["record"]["state"] == "active"
    assert w.count("records") == 0 and w.count("ids") == 0 and w.count("baseline_items") == 0
    assert w.count("clones") == 0  # nothing stored, not even the clone's identity


def test_dry_run_on_an_existing_record_adds_no_holder(w: CW, capsys) -> None:  # noqa: ANN001
    path, r = adopted(w, capsys)
    data = ok_adopt(adopt(w, capsys, path, "--dry-run", "--holder", "h"))
    assert data["existing"] is True and data["dry_run"] is True
    assert data["record"]["holders"] == ["h"]
    assert w.rows("SELECT count(*) FROM holders")[0][0] == 0


# -- usage ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--name", "-bad"],
        ["--name", "a b"],
        ["--name", "x" * 65],
        ["--holder", "bad holder"],
        ["--holder", ""],
        ["--holder", "h" * 65],
        ["--label", "nokey"],
        ["--repo", "Not A Repo"],
    ],
)
def test_usage_errors_exit_2_and_change_nothing(w: CW, capsys, argv: list[str]) -> None:  # noqa: ANN001
    path = foreign(w)
    r = (
        w.cli(capsys, "adopt", str(path), *argv)
        if argv[0] == "--repo"
        else adopt(w, capsys, path, *argv)
    )
    err_of(r, 2)
    assert w.count("records") == 0


def test_an_automation_actor_may_adopt(w: CW, capsys) -> None:  # noqa: ANN001
    ok_adopt(adopt(w, capsys, foreign(w), human=False))


def test_adopt_json_output_validates(w: CW, capsys) -> None:  # noqa: ANN001
    r = adopt(w, capsys, foreign(w))
    validate("wtctl/cli/adopt.json", r.json())
    validate("wtctl/cli/record.json", r.json()["record"])
