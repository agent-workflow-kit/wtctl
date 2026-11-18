"""``create`` for worktrees (docs/interface.md, Create order), against real git and no network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.createlib import CW, script
from tests.schemacheck import validate
from tests.vectorlib import plain_git


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> CW:
    return CW(tmp_path, private_home).build()


def schema_check(data: dict) -> None:
    """Validate against schemas/cli/create.json."""
    validate("wtctl/cli/create.json", data)


def ok(r) -> dict:  # noqa: ANN001
    assert r.code == 0, (r.out, r.err)
    data = r.json()
    schema_check(data)
    return data


def refused(r, code: int, reason: str | None = None) -> dict:  # noqa: ANN001
    assert r.code == code, (r.out, r.err)
    err = r.json()["error"]
    validate("wtctl/cli/error.json", r.json())
    if reason is not None:
        assert err["reason"] == reason
    return err


def remotes(w: CW) -> dict[str, str]:
    return {k: v for k, v in w.refs().items() if k.startswith("refs/remotes/")}


def worktree_root(w: CW) -> Path:
    return w.root.resolve() / "worktrees" / "example.test" / "org" / "repo"


# -- the plain cases ---------------------------------------------------------------------------


def test_new_branch_from_the_default_branch(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys))
    rec = data["record"]
    name = rec["id"][3:]
    assert rec["state"] == "active" and rec["owned"] is True and rec["object_type"] == "worktree"
    assert rec["name"] == name
    assert rec["branch"] == f"wt/{name}" and rec["created_branch"] is True
    assert rec["base"] == "refs/heads/main" and rec["base_sha"] == w.shas["c3"]
    assert rec["path"] == str(worktree_root(w) / name)
    assert rec["base_dir"] == str(w.root.resolve() / "worktrees")
    assert rec["clone_path"] == str(w.clone) and rec["common_dir"] == str(w.clone / ".git")
    assert rec["head_at_create"] == w.shas["c3"]
    assert rec["fetch_url"] == str(w.remote) and rec["remote"] == "origin"
    assert rec["holders"] == [] and rec["labels"] == []
    assert rec["bootstrap"]["state"] == "none"
    assert rec["admin_dir"] and rec["dir_id"] and rec["common_id"]
    assert data["branch_action"] == "new" and data["reused"] is False and data["dry_run"] is False
    assert data["bootstrap"] == {
        "state": "none", "steps": [], "created_paths_count": 0, "tracked_changes": []
    }  # fmt: skip
    wt = Path(rec["path"])
    assert (wt / "c3.txt").read_text() == "c3"
    assert w.g(["rev-parse", "--abbrev-ref", "HEAD"], wt) == rec["branch"]
    assert str(wt) in w.worktree_paths()
    # a new branch has no upstream: `--no-track`, and git's remote-branch guessing is not used
    assert w.g(["config", "--get-regexp", "^branch\\."]).count(rec["branch"]) == 0
    st = os.stat(wt)
    assert rec["dir_id"] == f"{st.st_dev}:{st.st_ino}"
    assert rec["admin_dir"] == str(
        w.clone / ".git" / "worktrees" / os.listdir(w.clone / ".git" / "worktrees")[0]
    )


def test_human_output_is_the_path_alone_on_stdout(w: CW, capsys) -> None:  # noqa: ANN001
    r = w.create(capsys, json_out=False)
    assert r.code == 0, r.err
    rows = w.records()
    assert r.out == rows[0]["path"] + "\n"
    assert r.err.strip()  # a summary goes to stderr


def test_existing_local_branch_is_checked_out_as_is(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--branch", "local-b"))
    rec = data["record"]
    assert data["branch_action"] == "existing" and rec["created_branch"] is False
    assert rec["branch"] == "local-b" and rec["name"] == "local-b"
    assert rec["base"] is None and rec["base_sha"] is None
    assert w.g(["rev-parse", "--abbrev-ref", "HEAD"], Path(rec["path"])) == "local-b"


def test_remote_only_branch_is_tracked_through_the_mirror(w: CW, capsys) -> None:  # noqa: ANN001
    assert "refs/heads/feature/remote-only" not in w.refs()
    remotes_before = remotes(w)
    data = ok(w.create(capsys, "--branch", "feature/remote-only"))
    rec = data["record"]
    assert data["branch_action"] == "track" and rec["created_branch"] is True
    assert rec["name"] == "feature-remote-only"
    assert w.refs()["refs/heads/feature/remote-only"] == w.shas["r1"]
    assert w.g(["config", "branch.feature/remote-only.remote"]) == "origin"
    assert w.g(["config", "branch.feature/remote-only.merge"]) == "refs/heads/feature/remote-only"
    assert (Path(rec["path"]) / "r1.txt").exists()
    assert remotes(w) == remotes_before
    assert not (w.clone / ".git" / "FETCH_HEAD").exists()


def test_detach_checks_out_the_base_tip(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--detach"))
    rec = data["record"]
    assert data["branch_action"] == "detached" and rec["branch"] is None
    assert rec["created_branch"] is False and rec["base_sha"] == w.shas["c3"]
    assert w.g(["rev-parse", "HEAD"], Path(rec["path"])) == w.shas["c3"]
    assert (
        plain_git(["symbolic-ref", "-q", "HEAD"], cwd=Path(rec["path"]), home=w.home).returncode
        == 1
    )
    assert rec["name"] == rec["id"][3:]


def test_ref_starts_a_new_branch_at_that_commit(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--ref", w.shas["c1"]))
    rec = data["record"]
    assert rec["head_at_create"] == w.shas["c1"] and rec["ref"] == w.shas["c1"]
    assert rec["base"] is None and rec["base_sha"] is None
    assert w.refs()[f"refs/heads/{rec['branch']}"] == w.shas["c1"]


def test_ref_with_detach(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--detach", "--ref", w.shas["c2"]))
    assert data["record"]["head_at_create"] == w.shas["c2"]
    assert data["branch_action"] == "detached"


def test_base_names_another_branch(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--base", "feature/remote-only"))
    rec = data["record"]
    assert rec["base"] == "refs/heads/feature/remote-only" and rec["base_sha"] == w.shas["r1"]
    assert (Path(rec["path"]) / "r1.txt").exists()


def test_base_from_repo_config(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg["base"] = "feature/remote-only"
    data = ok(w.create(capsys))
    assert data["record"]["base_sha"] == w.shas["r1"]


def test_base_prefers_the_remote_branch_over_the_local_one(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["branch", "-f", "local-b", w.shas["c1"]])
    w.g(["push", "-q", str(w.remote), f"{w.shas['c2']}:refs/heads/local-b"])
    data = ok(w.create(capsys, "--base", "local-b"))
    assert data["record"]["base_sha"] == w.shas["c2"]


def test_base_falls_back_to_the_local_branch(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["branch", "only-local", w.shas["c1"]])
    data = ok(w.create(capsys, "--base", "only-local"))
    assert data["record"]["base"] == "refs/heads/only-local"
    assert data["record"]["base_sha"] == w.shas["c1"]


def test_default_branch_comes_from_the_clones_row_not_the_mirror_head(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys))  # first create stores the row
    w.g(["symbolic-ref", "HEAD", "refs/heads/local-b"])  # the clone's own HEAD is irrelevant
    data = ok(w.create(capsys))
    assert data["record"]["base"] == "refs/heads/main"


def test_unknown_base_is_ref_not_found(w: CW, capsys) -> None:  # noqa: ANN001
    before = w.snapshot()
    err = refused(w.create(capsys, "--base", "nope"), 12)
    assert err["code"] == "ref_not_found"
    assert w.snapshot() == before and w.records() == []


def test_unknown_ref_is_ref_not_found(w: CW, capsys) -> None:  # noqa: ANN001
    before = w.snapshot()
    err = refused(w.create(capsys, "--ref", "1" * 40), 12)
    assert err["code"] == "ref_not_found"
    assert w.snapshot() == before and w.records() == []


def test_a_base_commit_the_clone_lacks_arrives_through_the_mirror(w: CW, capsys) -> None:  # noqa: ANN001
    new = w.advance_remote()
    remotes_before = remotes(w)
    assert plain_git(["cat-file", "-e", new], cwd=w.clone, home=w.home).returncode != 0
    data = ok(w.create(capsys))
    rec = data["record"]
    assert rec["base_sha"] == new and rec["head_at_create"] == new
    assert (Path(rec["path"]) / "n1.txt").exists()
    assert remotes(w) == remotes_before
    assert not (w.clone / ".git" / "FETCH_HEAD").exists()


def test_a_ref_known_only_to_the_mirror(w: CW, capsys) -> None:  # noqa: ANN001
    new = w.advance_remote()
    data = ok(w.create(capsys, "--detach", "--ref", new))
    assert data["record"]["head_at_create"] == new


def test_labels_and_holder_are_recorded(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--label", "k=v", "--label", "a=b", "--holder", "agent:1"))
    assert data["record"]["labels"] == ["a=b", "k=v"]
    assert data["record"]["holders"] == ["agent:1"]


# -- names and branches -------------------------------------------------------------------------


def test_name_is_derived_from_an_explicit_branch(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--branch", "topic/Fix_it.v2"))
    assert data["record"]["name"] == "topic-Fix_it.v2"
    assert data["record"]["path"].endswith("/topic-Fix_it.v2")


def test_explicit_name_and_template_branch(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--name", "mine"))
    assert data["record"]["name"] == "mine" and data["record"]["branch"] == "wt/mine"


def test_branch_prefix_and_template(w: CW, capsys) -> None:  # noqa: ANN001
    w.top["branch_prefix"] = "me/"
    w.top["branch_template"] = "{name}-{date}"
    data = ok(w.create(capsys, "--name", "x"))
    b = data["record"]["branch"]
    assert b.startswith("me/x-") and len(b) == len("me/x-") + 8 and b[-8:].isdigit()


def test_repo_branch_template_wins(w: CW, capsys) -> None:  # noqa: ANN001
    w.repo_cfg["branch_template"] = "r/{id}"
    data = ok(w.create(capsys, "--name", "x"))
    assert data["record"]["branch"] == f"r/{data['record']['id']}"


def test_derived_names_get_suffixes_against_records(w: CW, capsys) -> None:  # noqa: ANN001
    a = ok(w.create(capsys, "--branch", "feature/x"))
    b = ok(w.create(capsys, "--branch", "feature-x"))
    c = ok(w.create(capsys, "--branch", "feature_x_"))  # unrelated
    assert a["record"]["name"] == "feature-x"
    assert b["record"]["name"] == "feature-x-2"
    assert c["record"]["name"] == "feature_x_"


def test_name_collisions_ignore_case(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--detach", "--name", "Topic"))
    b = ok(w.create(capsys, "--branch", "topic"))
    assert b["record"]["name"] == "topic-2"


def test_derived_name_skips_an_existing_path_with_a_warning(w: CW, capsys) -> None:  # noqa: ANN001
    (worktree_root(w) / "topic").mkdir(parents=True)
    (worktree_root(w) / "topic" / "keep").write_text("x")
    data = ok(w.create(capsys, "--branch", "topic"))
    assert data["record"]["name"] == "topic-2"
    assert any("topic" in m for m in data["warnings"])
    assert (worktree_root(w) / "topic" / "keep").read_text() == "x"


def test_template_branch_collision_suffixes_the_name(
    w: CW,
    capsys,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ANN001
) -> None:
    # the derived name is the id; make the id predictable and take the branch it would build

    def fixed(self) -> str:  # noqa: ANN001
        self.reserve_id("wt-aaaaaaaa")
        return "wt-aaaaaaaa"

    monkeypatch.setattr("wtctl.state.State.allocate_id", fixed)
    w.g(["branch", "wt/aaaaaaaa", "main"])
    data = ok(w.create(capsys))
    assert data["record"]["name"] == "aaaaaaaa-2" and data["record"]["branch"] == "wt/aaaaaaaa-2"


def test_explicit_name_never_gets_a_suffix_on_a_branch_collision(w: CW, capsys) -> None:  # noqa: ANN001
    w.g(["branch", "wt/taken", "main"])
    err = refused(w.create(capsys, "--name", "taken"), 20, "branch_exists")
    assert err["details"]["tip"] == w.shas["c3"]
    assert w.records() == []


def test_explicit_name_collision_with_an_existing_path(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--name", "dup"))
    err = refused(w.create(capsys, "--name", "dup", "--branch", "local-b"), 20, "path_exists")
    assert err["holder_id"].startswith("wt-")


def test_existing_branch_with_base_is_branch_exists(w: CW, capsys) -> None:  # noqa: ANN001
    err = refused(w.create(capsys, "--branch", "local-b", "--base", "main"), 20, "branch_exists")
    assert err["details"]["tip"] == w.shas["c3"]


def test_existing_branch_with_another_ref_is_branch_exists(w: CW, capsys) -> None:  # noqa: ANN001
    refused(w.create(capsys, "--branch", "local-b", "--ref", w.shas["c1"]), 20, "branch_exists")


def test_existing_branch_with_its_own_tip_as_ref_is_fine(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--branch", "local-b", "--ref", w.shas["c3"]))


def test_remote_only_branch_with_base_is_branch_exists(w: CW, capsys) -> None:  # noqa: ANN001
    err = refused(
        w.create(capsys, "--branch", "feature/remote-only", "--base", "main"), 20, "branch_exists"
    )
    assert err["details"]["tip"] == w.shas["r1"]
    assert "refs/heads/feature/remote-only" not in w.refs()


# -- refusals -----------------------------------------------------------------------------------


def test_branch_checked_out_in_the_main_worktree(w: CW, capsys) -> None:  # noqa: ANN001
    err = refused(w.create(capsys, "--branch", "main"), 20, "branch_in_use")
    assert err["holder_path"] == str(w.clone) and err["holder_id"] is None


def test_branch_checked_out_in_a_managed_worktree(w: CW, capsys) -> None:  # noqa: ANN001
    first = ok(w.create(capsys, "--branch", "local-b"))["record"]
    err = refused(w.create(capsys, "--branch", "local-b"), 20, "branch_in_use")
    assert err["holder_id"] == first["id"] and err["holder_path"] == first["path"]


def test_branch_planned_by_a_creating_record(w: CW, capsys) -> None:  # noqa: ANN001
    rid = w.plant_creating(branch="local-b", name="crashed")
    err = refused(w.create(capsys, "--branch", "local-b"), 20, "branch_in_use")
    assert err["holder_id"] == rid


def test_path_that_is_a_nonempty_directory(w: CW, capsys) -> None:  # noqa: ANN001
    p = worktree_root(w) / "n1"
    p.mkdir(parents=True)
    (p / "f").write_text("x")
    err = refused(w.create(capsys, "--name", "n1"), 20, "path_exists")
    assert "holder_id" not in err
    assert (p / "f").read_text() == "x"


def test_path_that_is_an_empty_directory_is_used(w: CW, capsys) -> None:  # noqa: ANN001
    p = worktree_root(w) / "n1"
    p.mkdir(parents=True)
    ok(w.create(capsys, "--name", "n1"))
    assert (p / "c3.txt").exists()


def test_path_that_is_a_file_or_a_symlink(w: CW, capsys) -> None:  # noqa: ANN001
    wr = worktree_root(w)
    wr.mkdir(parents=True)
    (wr / "f").write_text("x")
    (wr / "s").symlink_to(w.tmp)
    refused(w.create(capsys, "--name", "f"), 20, "path_exists")
    refused(w.create(capsys, "--name", "s"), 20, "path_exists")
    assert not (w.tmp / "c3.txt").exists()


def test_path_held_by_a_creating_record(w: CW, capsys) -> None:  # noqa: ANN001
    rid = w.plant_creating(branch=None, name="crashed")
    err = refused(w.create(capsys, "--name", "crashed", "--branch", "local-b"), 20, "path_exists")
    assert err["holder_id"] == rid


# -- holders and reuse --------------------------------------------------------------------------


def test_holder_retry_returns_the_active_record(w: CW, capsys) -> None:  # noqa: ANN001
    first = ok(w.create(capsys, "--branch", "local-b", "--holder", "h1"))
    marker = w.tmp / "hook-ran"
    w.top["hooks"] = {"pre-create": [{"cmd": f"touch {marker}"}]}
    before = w.snapshot()
    again = ok(w.create(capsys, "--branch", "local-b", "--holder", "h1"))
    assert again["reused"] is True and again["branch_action"] is None and again["bootstrap"] is None
    assert again["record"] == first["record"]
    assert not marker.exists() and w.snapshot() == before and len(w.records()) == 1


def test_holder_retry_on_a_path_collision(w: CW, capsys) -> None:  # noqa: ANN001
    first = ok(w.create(capsys, "--name", "n1", "--branch", "local-b", "--holder", "h1"))
    again = ok(w.create(capsys, "--name", "n1", "--branch", "other", "--holder", "h1"))
    assert again["reused"] is True and again["record"]["id"] == first["record"]["id"]


def test_holder_retry_with_an_explicit_name_collision(w: CW, capsys) -> None:  # noqa: ANN001
    first = ok(w.create(capsys, "--name", "agent-7", "--holder", "h1"))
    again = ok(w.create(capsys, "--name", "agent-7", "--holder", "h1"))
    assert again["reused"] is True and again["record"] == first["record"]


def test_other_holder_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--branch", "local-b", "--holder", "h1"))
    refused(w.create(capsys, "--branch", "local-b", "--holder", "h2"), 20, "branch_in_use")


def test_no_holder_is_refused(w: CW, capsys) -> None:  # noqa: ANN001
    ok(w.create(capsys, "--branch", "local-b", "--holder", "h1"))
    refused(w.create(capsys, "--branch", "local-b"), 20, "branch_in_use")


def test_holder_does_not_reuse_a_creating_record(w: CW, capsys) -> None:  # noqa: ANN001
    w.plant_creating(branch="local-b", name="crashed", holder="h1")
    refused(w.create(capsys, "--branch", "local-b", "--holder", "h1"), 20, "branch_in_use")


def test_holder_does_not_reuse_an_unlinked_record(w: CW, capsys) -> None:  # noqa: ANN001
    first = ok(w.create(capsys, "--branch", "local-b", "--holder", "h1"))["record"]
    w.set_state(first["id"], "unlinked")
    refused(w.create(capsys, "--branch", "local-b", "--holder", "h1"), 20, "branch_in_use")


def test_reuse_in_a_dry_run_changes_nothing(w: CW, capsys) -> None:  # noqa: ANN001
    first = ok(w.create(capsys, "--branch", "local-b", "--holder", "h1"))
    again = ok(w.create(capsys, "--branch", "local-b", "--holder", "h1", "--dry-run"))
    assert again["reused"] is True and again["dry_run"] is False  # same answer either way
    assert again["record"] == first["record"]


# -- dry run ------------------------------------------------------------------------------------


def test_dry_run_writes_nothing_but_the_mirror(w: CW, capsys) -> None:  # noqa: ANN001
    marker = w.tmp / "hook-ran"
    w.top["hooks"] = {
        "pre-create": [{"cmd": f"touch {marker}"}],
        "post-create": [{"cmd": f"touch {marker}"}],
    }
    before = w.snapshot()
    ids_before = w.count("ids")
    data = ok(w.create(capsys, "--dry-run", "--label", "k=v", "--holder", "h"))
    assert data["dry_run"] is True and data["branch_action"] == "new" and data["reused"] is False
    rec = data["record"]
    assert rec["state"] == "creating" and rec["labels"] == ["k=v"] and rec["holders"] == ["h"]
    assert rec["base_sha"] == w.shas["c3"] and rec["dir_id"] is None
    assert w.snapshot() == before and w.records() == []
    assert w.count("ids") == ids_before
    assert not marker.exists() and not Path(rec["path"]).exists()
    assert not w.root.exists()  # not even the worktree root
    assert w.count("anchors") == 0
    assert w.rows("SELECT mirror_path FROM clones")[0]["mirror_path"]  # the refresh happened


def test_dry_run_reports_a_remote_only_branch_as_track(w: CW, capsys) -> None:  # noqa: ANN001
    data = ok(w.create(capsys, "--dry-run", "--branch", "feature/remote-only"))
    assert data["branch_action"] == "track"
    assert "refs/heads/feature/remote-only" not in w.refs()


def test_dry_run_does_not_copy_a_base_commit(w: CW, capsys) -> None:  # noqa: ANN001
    new = w.advance_remote()
    before = sorted(os.listdir(w.clone / ".git" / "objects" / "pack"))
    data = ok(w.create(capsys, "--dry-run"))
    assert data["record"]["base_sha"] == new
    assert sorted(os.listdir(w.clone / ".git" / "objects" / "pack")) == before


def test_dry_run_still_refuses(w: CW, capsys) -> None:  # noqa: ANN001
    refused(w.create(capsys, "--dry-run", "--branch", "main"), 20, "branch_in_use")


# -- hooks --------------------------------------------------------------------------------------


def test_pre_create_runs_before_any_branch_write(w: CW, capsys) -> None:  # noqa: ANN001
    out = w.tmp / "payload.json"
    snap = w.tmp / "refs.txt"
    hook = script(
        w.tmp / "hook.sh",
        f"cat > {out}\ngit --git-dir={w.clone}/.git for-each-ref > {snap}\n"
        f"ls {w.root} > {w.tmp}/rootls 2>&1 || true",
    )
    w.top["hooks"] = {"pre-create": [{"cmd": hook}]}
    data = ok(w.create(capsys, "--label", "k=v"))
    import json

    payload = json.loads(out.read_text())
    validate("wtctl/hooks/pre-create.json", payload)
    rec = data["record"]
    assert payload["object_id"] == rec["id"] and payload["path"] == rec["path"]
    assert payload["branch"] == rec["branch"] and payload["branch_action"] == "new"
    assert payload["base"] == "refs/heads/main" and payload["ref"] is None
    assert payload["clone_path"] == str(w.clone) and payload["labels"] == ["k=v"]
    assert payload["actor"] == "auto:tester" and payload["source"] is None
    assert rec["branch"] not in snap.read_text()
    assert "worktrees" not in (w.tmp / "rootls").read_text()  # no root dir written yet


def test_pre_create_veto_leaves_nothing_and_burns_the_id(w: CW, capsys) -> None:  # noqa: ANN001
    w.top["hooks"] = {"pre-create": [{"cmd": "echo no >&2; exit 3"}]}
    before = w.snapshot()
    err = refused(w.create(capsys), 11)
    assert err["code"] == "vetoed"
    assert w.snapshot() == before and w.records() == []
    assert w.count("ids") == 1


def test_pre_create_escalation_and_override(w: CW, capsys) -> None:  # noqa: ANN001
    w.top["hooks"] = {"pre-create": [{"cmd": "exit 13"}]}
    before = w.snapshot()
    refused(w.create(capsys), 13)
    assert w.snapshot() == before and w.records() == []
    ok(w.create(capsys, "--override-escalation", human=True))


def test_override_escalation_needs_a_human(w: CW, capsys) -> None:  # noqa: ANN001
    r = w.create(capsys, "--override-escalation")
    assert r.code == 2


def test_hook_flag_adds_a_hook(w: CW, capsys) -> None:  # noqa: ANN001
    marker = w.tmp / "m"
    ok(w.create(capsys, "--hook", f"pre-create=touch {marker}"))
    assert marker.exists()


def test_post_create_runs_after_activation(w: CW, capsys) -> None:  # noqa: ANN001
    import json
    import sys

    out = w.tmp / "post.json"
    seen = w.tmp / "seen"
    probe = (
        "import sqlite3,sys;"
        "print(sqlite3.connect(sys.argv[1]).execute('select state from records').fetchone()[0])"
    )
    hook = script(w.tmp / "post.sh", f'cat > {out}\n{sys.executable} -c "{probe}" {w.db} > {seen}')
    w.top["hooks"] = {"post-create": [{"cmd": hook}]}
    data = ok(w.create(capsys))
    payload = json.loads(out.read_text())
    validate("wtctl/hooks/post-create.json", payload)
    assert payload["record"]["id"] == data["record"]["id"]
    assert seen.read_text().strip() == "active"


def test_post_create_failure_is_a_warning(w: CW, capsys) -> None:  # noqa: ANN001
    w.top["hooks"] = {"post-create": [{"cmd": "exit 5"}]}
    data = ok(w.create(capsys))
    assert data["record"]["state"] == "active"
    assert any("post-create" in m for m in data["warnings"])


def test_decision_ids_from_hooks_do_not_break_create(w: CW, capsys) -> None:  # noqa: ANN001
    w.top["hooks"] = {"pre-create": [{"cmd": 'echo \'{"decision_id": "d1"}\''}]}
    ok(w.create(capsys, "--decision-id", "mine"))


# -- usage errors -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "-x",
        "--help",
        "a..b",
        "a@{b",
        "a b",
        "a\nb",
        "a\x07b",
        "@{-1}",
        "@",
        "HEAD",
        "a/",
        "/a",
        "a//b",
        "a.lock",
        "a~1",
        "a^b",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        ".a",
        "",
    ],
)
def test_hostile_branch_names_are_usage_errors(w: CW, capsys, bad: str) -> None:  # noqa: ANN001
    before = w.snapshot()
    r = w.create(capsys, f"--branch={bad}")
    assert r.code == 2, (bad, r.out)
    assert w.snapshot() == before and w.records() == []


@pytest.mark.parametrize("bad", ["-x", "refs/heads/main", "a..b", "a b", "@{-1}", "", "HEAD", "-"])
def test_hostile_base_names_are_usage_errors(w: CW, capsys, bad: str) -> None:  # noqa: ANN001
    assert w.create(capsys, f"--base={bad}").code == 2


@pytest.mark.parametrize(
    "bad", ["main", "HEAD", "-" + "a" * 39, "A" * 40, "a" * 39, "a" * 41, "g" * 40, "", "--all"]
)
def test_ref_must_be_a_full_lowercase_sha(w: CW, capsys, bad: str) -> None:  # noqa: ANN001
    assert w.create(capsys, f"--ref={bad}").code == 2


@pytest.mark.parametrize("bad", ["", "-x", ".x", "a/b", "a b", "x" * 65, "é", "a\nb", ".."])
def test_name_must_match_its_grammar(w: CW, capsys, bad: str) -> None:  # noqa: ANN001
    assert w.create(capsys, f"--name={bad}").code == 2


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "a\nb", "é"])
def test_holder_must_match_its_grammar(w: CW, capsys, bad: str) -> None:  # noqa: ANN001
    assert w.create(capsys, f"--holder={bad}").code == 2


@pytest.mark.parametrize("bad", ["nokey", "K=v", "a=b c", "=v"])
def test_labels_must_match_their_grammar(w: CW, capsys, bad: str) -> None:  # noqa: ANN001
    assert w.create(capsys, f"--label={bad}").code == 2


def test_branch_and_detach_are_exclusive(w: CW, capsys) -> None:  # noqa: ANN001
    assert w.create(capsys, "--branch", "x", "--detach").code == 2


def test_bad_hook_flag(w: CW, capsys) -> None:  # noqa: ANN001
    assert w.create(capsys, "--hook", "nonsense").code == 2


def test_bad_repo_id(w: CW, capsys) -> None:  # noqa: ANN001
    r = w.cli(capsys, "create", "--repo", "../x")
    assert r.code == 2


def test_unknown_repo_is_not_found(w: CW, capsys) -> None:  # noqa: ANN001
    r = w.cli(capsys, "create", "--repo", "example.test/org/none")
    assert r.code == 10


def test_path_that_is_a_mount_point_is_refused_by_the_mount_table(
    w: CW,
    capsys,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wtctl import fsafe, gateway

    p = worktree_root(w) / "n1"
    p.mkdir(parents=True)
    fake = [gateway.MountEntry(99, str(p.resolve()), "tmpfs", "none")]
    monkeypatch.setattr(fsafe.Fs, "mount_table", lambda self: fake)
    err = refused(w.create(capsys, "--name", "n1"), 20, "path_exists")
    assert "mount point" in err["message"]
    assert not any(p.iterdir()) and w.records() == []
