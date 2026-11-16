"""``create --throwaway`` (docs/interface.md, Throwaway create order): real git, loopback only."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import REPO, Spy
from tests.schemacheck import validate
from tests.throwlib import TW, leaks
from tests.vectorlib import plain_git
from wtctl import removalfs, throwaway
from wtctl.fsafe_child import OpResult
from wtctl.gateway import GitResult


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> TW:
    return TW(tmp_path, private_home).build()


def fail_sub(name: str, *, containing: str | None = None):  # noqa: ANN201
    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == name and (containing is None or containing in argv):
            return GitResult(128, b"", b"fatal: injected failure\n")
        return None

    return before


def nothing_left(w: TW) -> None:
    """A failed create leaves no directory, no record and no lease; its id stays burned."""
    assert leaks(w.troot) == []
    assert w.records() == [] and w.leases() == []
    assert w.count("ids") >= 1


# -- the plain cases --------------------------------------------------------------------------


def test_remote_source_creates_a_detached_private_checkout(w: TW) -> None:
    rec = w.made("--source", "remote")
    p = Path(rec["path"])
    assert rec["object_type"] == "throwaway" and rec["state"] == "active"
    assert rec["source"] == "remote" and rec["depth"] == 50 and rec["ref"] == w.shas["c3"]
    assert rec["base_dir"] == str(w.troot) and p == w.troot / rec["id"]
    assert rec["fetch_url"] == str(w.remote) and rec["clone_path"] == str(w.clone)
    assert rec["owned"] is None and rec["branch"] is None and rec["bootstrap"] is None
    assert stat.S_IMODE(p.stat().st_mode) == 0o700
    assert stat.S_IMODE(w.troot.stat().st_mode) == 0o700
    assert rec["dir_id"] == f"{p.stat().st_dev}:{p.stat().st_ino}"
    assert rec["root_id"] == f"{w.troot.stat().st_dev}:{w.troot.stat().st_ino}"
    assert (p / ".git" / "HEAD").read_text() == w.shas["c3"] + "\n"  # detached
    assert [f.name for f in sorted(p.iterdir())] == [".git", "c1.txt", "c2.txt", "c3.txt"]
    assert w.tg(p, "status", "--porcelain") == ""
    assert w.count("anchors") == 1


def test_the_repository_is_isolated_and_quiet(w: TW) -> None:
    p = Path(w.made("--source", "remote")["path"])
    cfg = w.tg(p, "config", "--file", str(p / ".git" / "config"), "--list")
    keys = {line.split("=", 1)[0] for line in cfg.splitlines()}
    assert keys <= throwaway.OWNED_KEYS, keys
    assert "core.logallrefupdates=false" in cfg.lower() and "fetch.fsckobjects=true" in cfg.lower()
    assert not any(k.startswith(("remote.", "branch.", "url.", "include")) for k in keys)
    assert (p / ".git" / "info" / "attributes").read_text() == "* -filter\n"
    names = set(os.listdir(p / ".git"))
    assert not names & {"FETCH_HEAD", "logs", "hooks", "ORIG_HEAD", "packed-refs"}, names
    assert "refs" in names and not list((p / ".git" / "refs").rglob("*.lock"))


def test_the_users_global_git_config_is_never_read(w: TW, tmp_path: Path) -> None:
    hit = tmp_path / "hit"
    tpl = tmp_path / "tpl"
    (tpl / "hooks").mkdir(parents=True)
    (tpl / "hooks" / "post-checkout").write_text(f"#!/bin/sh\ntouch {hit}\n")
    (tpl / "hooks" / "post-checkout").chmod(0o755)
    (w.home / ".gitconfig").write_text(
        f"[init]\n\ttemplateDir = {tpl}\n[core]\n\thooksPath = {tpl}/hooks\n"
        f"\tfsmonitor = touch {hit}\n"
        f'[filter "evil"]\n\tsmudge = touch {hit}\n\tclean = touch {hit}\n'
    )
    (w.work / ".gitattributes").write_text("*.txt filter=evil\n")
    w.g(["add", "."], w.work)
    w.g(["commit", "-q", "-m", "attrs"], w.work)
    sha = w.g(["rev-parse", "HEAD"], w.work)
    w.g(["push", "-q", str(w.remote), "main"], w.work)
    p = Path(w.made("--source", "remote", ref=sha)["path"])
    assert not hit.exists() and not (p / ".git" / "hooks").exists()
    assert (p / "c1.txt").read_text() == "c1"


@pytest.mark.parametrize(
    ("extra", "count", "shallow"),
    [
        (["--depth", "1"], 1, True),
        (["--depth", "2"], 2, True),
        (["--depth", "0"], 3, False),
        ([], 3, False),  # the default depth, 50, is more than the history
    ],
)
def test_remote_depth(w: TW, extra: list[str], count: int, shallow: bool) -> None:
    rec = w.made("--source", "remote", *extra)
    p = Path(rec["path"])
    assert int(w.tg(p, "rev-list", "--count", "HEAD")) == count
    assert (p / ".git" / "shallow").exists() is shallow
    assert rec["depth"] == (int(extra[1]) if extra else 50)


def test_the_default_depth_is_the_config_default(w: TW) -> None:
    w.top["throwaway"] = {"depth": 1}
    rec = w.made("--source", "remote")
    assert rec["depth"] == 1 and (Path(rec["path"]) / ".git" / "shallow").exists()


def test_local_source_copies_full_history_by_pack(w: TW) -> None:
    rec = w.made("--source", "local")
    p = Path(rec["path"])
    assert rec["source"] == "local" and rec["depth"] == 0 and rec["fetch_url"] == str(w.clone)
    assert w.tg(p, "rev-list", "--count", "HEAD") == "3"
    assert not (p / ".git" / "shallow").exists()
    assert (p / ".git" / "info" / "attributes").read_text() == "* -filter\n"
    assert not os.path.exists(p / ".git" / "logs")
    w.tg(p, "fsck", "--strict", "--no-dangling")


def test_local_source_depth_one_is_shallow(w: TW) -> None:
    rec = w.made("--source", "local", "--depth", "1")
    p = Path(rec["path"])
    assert rec["depth"] == 1 and (p / ".git" / "shallow").read_text() == w.shas["c3"] + "\n"
    assert w.tg(p, "rev-list", "--count", "HEAD") == "1"


def test_local_source_ignores_the_configured_depth(w: TW) -> None:
    w.top["throwaway"] = {"depth": 1}
    assert w.made("--source", "local")["depth"] == 0


@pytest.mark.parametrize("depth", ["2", "50"])
def test_local_source_with_another_depth_is_a_usage_error(w: TW, depth: str) -> None:
    r = w.throw("--source", "local", "--depth", depth)
    w.failed(r, 2)
    assert w.records() == [] and not w.troot.exists()


def test_auto_prefers_the_local_clone_when_it_has_the_commit(w: TW) -> None:
    rec = w.made()
    assert rec["source"] == "local" and rec["depth"] == 0 and rec["fetch_url"] == str(w.clone)


def test_auto_uses_the_remote_when_the_clone_lacks_the_commit(w: TW) -> None:
    new = w.advance_remote()
    rec = w.made(ref=new)
    assert rec["source"] == "remote" and rec["fetch_url"] == str(w.remote)
    assert (Path(rec["path"]) / "n1.txt").read_text() == "n1"


@pytest.mark.parametrize("depth", ["2", "50"])
def test_auto_with_an_explicit_depth_other_than_0_or_1_uses_the_remote(w: TW, depth: str) -> None:
    assert w.made("--depth", depth)["source"] == "remote"


@pytest.mark.parametrize("depth", ["0", "1"])
def test_auto_with_depth_0_or_1_keeps_the_local_source(w: TW, depth: str) -> None:
    assert w.made("--depth", depth)["source"] == "local"


def test_auto_without_a_clone_uses_the_remote_and_says_nothing(w: TW) -> None:
    w.repo_cfg = {"clone_url": str(w.remote)}
    r = w.throw()
    assert r.code == 0, r.err
    data = r.json()
    assert data["record"]["source"] == "remote" and data["record"]["clone_path"] is None
    assert data["warnings"] == []


def test_local_source_without_a_clone_is_not_found(w: TW) -> None:
    w.repo_cfg = {"clone_url": str(w.remote)}
    w.failed(w.throw("--source", "local"), 10)
    nothing_left(w) if w.count("ids") else None
    assert w.records() == []


def test_a_clone_that_does_not_match_is_a_warning_for_auto_and_remote_only(w: TW) -> None:
    w.g(["remote", "set-url", "origin", "https://other.test/x/y.git"])
    assert w.failed(w.throw("--source", "local"), 12)["code"] == "remote_mismatch"
    r = w.throw("--source", "remote")
    assert r.code == 0, r.err
    data = r.json()
    assert data["record"]["source"] == "remote" and data["record"]["clone_path"] is None
    assert any(
        "remote_mismatch" in x or "does not" in x or "no clone" in x for x in data["warnings"]
    )


def test_local_without_the_commit_is_ref_not_found(w: TW) -> None:
    new = w.advance_remote()
    assert w.failed(w.throw("--source", "local", ref=new), 12)["code"] == "ref_not_found"
    nothing_left(w)


@pytest.mark.parametrize("source", ["remote", "auto"])
def test_unknown_commit_is_ref_not_found_and_leaves_nothing(w: TW, source: str) -> None:
    assert w.failed(w.throw("--source", source, ref="1" * 40), 12)["code"] == "ref_not_found"
    nothing_left(w)


def test_a_non_commit_is_ref_not_found(w: TW) -> None:
    tree = w.g(["rev-parse", "main^{tree}"])
    assert w.failed(w.throw("--source", "remote", ref=tree), 12)["code"] == "ref_not_found"
    nothing_left(w)


def test_the_source_decision_and_url_order() -> None:
    assert throwaway.choose_source(
        "auto", explicit_depth=None, default_depth=50, clone_has_ref=True, clone_shallow=False
    ) == ("local", 0)
    # a shallow clone cannot give the full history: auto goes to the remote, which has it
    assert throwaway.choose_source(
        "auto", explicit_depth=None, default_depth=50, clone_has_ref=True, clone_shallow=True
    ) == ("remote", 50)
    assert throwaway.choose_source(
        "auto", explicit_depth=1, default_depth=50, clone_has_ref=True, clone_shallow=True
    ) == ("local", 1)
    assert throwaway.choose_source(
        "auto", explicit_depth=None, default_depth=50, clone_has_ref=False, clone_shallow=False
    ) == ("remote", 50)
    assert throwaway.choose_source(
        "remote", explicit_depth=None, default_depth=7, clone_has_ref=True, clone_shallow=False
    ) == ("remote", 7)
    pick = throwaway.remote_url_choice
    assert pick(repo_clone_url="r", clone_fetch_url="c", host_url="h") == "r"
    assert pick(repo_clone_url=None, clone_fetch_url="c", host_url="h") == "c"
    assert pick(repo_clone_url=None, clone_fetch_url=None, host_url="h") == "h"


# -- usage ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--branch", "b"],
        ["--detach"],
        ["--base", "main"],
        ["--name", "n"],
        ["--holder", "h"],
        ["--copy-env"],
        ["--no-bootstrap"],
        ["--depth", "-1"],
        ["--hook", "pre-remove=true"],
        ["--label", "Bad=x"],
    ],
)
def test_usage_errors(w: TW, argv: list[str]) -> None:
    w.failed(w.throw(*argv), 2)
    assert w.records() == [] and not w.troot.exists()


@pytest.mark.parametrize("bad", ["bad id", "../x", "a\nb", "Example.test/Org/Repo", ""])
@pytest.mark.parametrize("dry", [[], ["--dry-run"]])
def test_an_invalid_repo_id_is_a_usage_error(w: TW, bad: str, dry: list[str]) -> None:
    r = w.run("create", "--throwaway", "--repo", bad, "--ref", w.shas["c3"], *dry)
    w.failed(r, 2)
    nothing_left_but_ids(w)


def nothing_left_but_ids(w: TW) -> None:
    assert leaks(w.troot) == [] and w.records() == []


def test_ref_is_required_and_must_be_a_full_sha(w: TW) -> None:
    r = w.run("create", "--throwaway", "--repo", REPO)
    w.failed(r, 2)
    w.failed(w.throw(ref="main"), 2)
    w.failed(w.throw(ref=w.shas["c3"][:12]), 2)
    w.failed(w.throw(ref=w.shas["c3"].upper()), 2)


def test_source_and_depth_are_only_for_throwaways(w: TW) -> None:
    w.failed(w.run("create", "--repo", REPO, "--depth", "1"), 2)
    w.failed(w.run("create", "--repo", REPO, "--source", "local"), 2)


# -- failure at each step removes the directory and the record ---------------------------------


@pytest.mark.parametrize(
    ("source", "sub", "containing", "code"),
    [
        ("remote", "init", None, "create_failed"),
        ("remote", "config", "core.logAllRefUpdates", "create_failed"),
        ("remote", "config", "fetch.fsckObjects", "create_failed"),
        ("remote", "fetch", None, "fetch_failed"),
        ("remote", "update-ref", None, "create_failed"),
        ("remote", "read-tree", None, "create_failed"),
        ("remote", "rev-parse", "HEAD", "create_failed"),
        ("local", "init", None, "create_failed"),
        ("local", "rev-list", None, "create_failed"),
        ("local", "pack-objects", None, "create_failed"),
        ("local", "index-pack", None, "create_failed"),
        ("local", "update-ref", None, "create_failed"),
        ("local", "read-tree", None, "create_failed"),
    ],
)
def test_a_failing_step_deletes_the_directory_and_the_record(
    w: TW, source: str, sub: str, containing: str | None, code: str
) -> None:
    w.git_runner = Spy(before=fail_sub(sub, containing=containing))
    err = w.failed(w.throw("--source", source), 12)
    assert err["code"] == code
    nothing_left(w)


def test_an_unreadable_config_listing_is_internal_and_still_cleans_up(w: TW) -> None:
    # every `config` call after the directory exists fails: git cannot be run in the throwaway
    state = {"n": 0}

    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "config" and "--list" in argv:
            state["n"] += 1
            if state["n"] > 1 and Path(w.troot).exists():
                return GitResult(128, b"", b"fatal: injected\n")
        return None

    w.git_runner = Spy(before=before)
    assert w.failed(w.throw("--source", "remote"), 1)["code"] == "internal"
    nothing_left(w)


def test_a_fetch_that_succeeds_without_the_commit_is_ref_not_found(w: TW) -> None:
    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        return GitResult(0, b"", b"") if sub == "fetch" else None

    w.git_runner = Spy(before=before)
    assert w.failed(w.throw("--source", "remote"), 12)["code"] == "ref_not_found"
    nothing_left(w)


def test_head_that_is_not_the_requested_commit_is_refused(w: TW) -> None:
    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "rev-parse" and "HEAD" in argv:
            return GitResult(0, (w.shas["c1"] + "\n").encode(), b"")
        return None

    w.git_runner = Spy(before=before)
    assert w.failed(w.throw("--source", "remote"), 12)["code"] == "create_failed"
    nothing_left(w)


def test_a_directory_that_cannot_be_deleted_leaves_a_partial_record(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stuck(op: str, args: dict[str, Any], **_: Any) -> OpResult:
        if op == "delete_tree":
            return OpResult(
                True,
                value={
                    "removed": 1,
                    "remaining_total": 1,
                    "remaining": [{"path": ["x"], "reason": "error", "errno": 1}],
                },
            )
        raise AssertionError(op)

    monkeypatch.setattr(removalfs, "RUNNER", stuck)
    w.git_runner = Spy(before=fail_sub("read-tree"))
    err = w.failed(w.throw("--source", "remote"), 12)
    assert "partial" in err["message"]
    (row,) = w.records()
    assert row["state"] == "partial" and row["last_error"]
    assert w.leases() == []
    monkeypatch.setattr(removalfs, "RUNNER", None)
    w.git_runner = None
    assert w.pruned("--if-configured")["removed"] == [row["id"]]
    assert leaks(w.troot) == [] and w.state_of(row["id"]) == "removed"


def test_an_interrupt_deletes_the_directory_and_the_record_too(w: TW) -> None:
    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "read-tree":
            raise KeyboardInterrupt
        return None

    w.git_runner = Spy(before=before)
    with pytest.raises(KeyboardInterrupt):
        w.throw("--source", "remote")
    nothing_left(w)


def test_a_failure_before_the_identity_is_recorded_leaves_no_directory_and_no_record(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl.errors import WtctlError
    from wtctl.state import State

    real = State.transition

    def transition(self: State, rid: str, frm: Any, to: str, **fields: Any) -> Any:
        if "dir_id" in fields:
            raise WtctlError("internal", "injected: the identity could not be recorded")
        return real(self, rid, frm, to, **fields)

    monkeypatch.setattr(State, "transition", transition)
    w.failed(w.throw("--source", "remote"), 1)
    nothing_left(w)


def test_a_new_directory_that_is_not_private_is_deleted_with_its_record(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import fsafe

    real = fsafe.open_dir
    once: list[bool] = []

    def loose(anchor: Any, parent_fd: int, name: str, **kw: Any) -> int:
        fd = real(anchor, parent_fd, name, **kw)
        if name.startswith("wt-") and not once:  # the new throwaway directory
            once.append(True)
            os.fchmod(fd, 0o777)
        return fd

    monkeypatch.setattr(fsafe, "open_dir", loose)
    err = w.failed(w.throw("--source", "remote"), 12)
    assert err["code"] == "create_failed" and "not private" in err["message"]
    nothing_left(w)


# -- the record and its leases while it is created ---------------------------------------------


def test_the_record_is_creating_and_leased_with_its_identity_before_anything_is_written(
    w: TW,
) -> None:
    seen: dict[str, Any] = {}

    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub in ("init", "fetch") and sub not in seen:
            (row,) = w.records()
            path = Path(row["path"])
            seen[sub] = {
                "state": row["state"],
                "dir_id": row["dir_id"],
                "fetch_url": row["fetch_url"],
                "source": row["source"],
                "depth": row["depth"],
                "root_id": row["root_id"],
                "lease": f"record:{row['id']}" in w.leases() or w.leases(),
                "listing": sorted(os.listdir(path)),
                "real": f"{path.stat().st_dev}:{path.stat().st_ino}",
            }
        return None

    w.git_runner = Spy(before=before)
    rec = w.made("--source", "remote")
    init, fetch = seen["init"], seen["fetch"]
    assert init["state"] == "creating" and init["dir_id"] == init["real"] and init["lease"]
    assert init["listing"] == [] and init["fetch_url"] is None and init["source"] == "remote"
    assert init["depth"] == 50 and init["root_id"] == rec["root_id"]
    assert fetch["fetch_url"] == str(w.remote) and fetch["state"] == "creating"
    assert w.leases() == [] and w.state_of(rec["id"]) == "active"


def test_the_root_is_recorded_once_and_a_replaced_root_is_refused(w: TW) -> None:
    first = w.made("--source", "remote")
    (row,) = w.rows("SELECT path, dev_ino FROM anchors")
    assert row["path"] == str(w.troot) and row["dev_ino"] == first["root_id"]
    assert w.made("--source", "remote")["root_id"] == first["root_id"]
    moved = w.tmp / "old-root"
    os.rename(w.troot, moved)
    w.troot.mkdir(mode=0o700)
    err = w.failed(w.throw("--source", "remote"), 12)
    assert err["code"] == "create_failed" and err["details"]["cause"] == "anchor_changed"
    (anchor,) = err["details"]["anchors"]
    assert anchor["anchor"] == "throwaway_root" and anchor["path"] == str(w.troot)
    assert anchor["recorded"] == first["root_id"] and anchor["changed"] is True
    assert os.listdir(w.troot) == []
    assert [r["state"] for r in w.records()] == ["active", "active"]


def test_a_root_that_became_a_mount_point_is_anchor_changed_without_a_changed_identity(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import fsafe

    first = w.made("--source", "remote")

    def mounted(*a: Any, **kw: Any) -> Any:
        raise fsafe.FsafeError("mount_point", "is a mount point")

    monkeypatch.setattr(fsafe, "open_anchor", mounted)
    err = w.failed(w.throw("--source", "remote"), 12)
    assert err["details"]["cause"] == "anchor_changed"
    (anchor,) = err["details"]["anchors"]
    assert anchor["recorded"] == anchor["current"] == first["root_id"]
    assert anchor["changed"] is False


def test_an_existing_root_is_recorded_at_its_first_use(w: TW) -> None:
    w.troot.mkdir(parents=True, mode=0o700)
    rec = w.made("--source", "remote")
    assert rec["root_id"] == f"{w.troot.stat().st_dev}:{w.troot.stat().st_ino}"
    assert w.count("anchors") == 1


def test_a_group_writable_root_is_refused(w: TW) -> None:
    w.troot.mkdir(parents=True)
    w.troot.chmod(0o770)
    assert w.failed(w.throw("--source", "remote"), 12)["code"] == "create_failed"
    assert w.records() == [] and os.listdir(w.troot) == []


def test_a_symlinked_root_is_resolved_once(w: TW, tmp_path: Path) -> None:
    real = tmp_path / "real-throwaways"
    real.mkdir(mode=0o700)
    link = tmp_path / "link-throwaways"
    link.symlink_to(real)
    w.top["throwaway_root"] = str(link)
    rec = w.made("--source", "remote")
    assert rec["base_dir"] == str(real) and rec["path"] == str(real / rec["id"])
    assert not os.path.islink(rec["path"]) and not os.path.islink(rec["base_dir"])
    # the same through a symlinked ancestor
    anc = tmp_path / "anc"
    anc.symlink_to(tmp_path)
    w.top["throwaway_root"] = str(anc / "real-throwaways")
    assert w.made("--source", "remote")["base_dir"] == str(real)


def test_a_root_swapped_for_a_link_to_another_directory_is_refused(w: TW, tmp_path: Path) -> None:
    w.made("--source", "remote")
    other = tmp_path / "attacker-dir"
    other.mkdir(mode=0o700)
    os.rename(w.troot, tmp_path / "old-root")
    os.symlink(other, w.troot)
    for extra in ([], ["--dry-run"]):
        err = w.failed(w.throw("--source", "remote", *extra), 12)
        assert err["details"]["cause"] == "anchor_changed"
        (anchor,) = err["details"]["anchors"]
        assert anchor["path"] == str(w.troot) and anchor["changed"] is True
    assert os.listdir(other) == [] and w.records() != []  # nothing was written there


def test_a_symlinked_root_retargeted_to_another_directory_is_refused(w: TW, tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(mode=0o700)
    second.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(first)
    w.top["throwaway_root"] = str(link)
    w.made("--source", "remote")
    link.unlink()
    link.symlink_to(second)
    err = w.failed(w.throw("--source", "remote"), 12)
    assert err["details"]["cause"] == "anchor_changed"
    assert os.listdir(second) == []


@pytest.mark.parametrize("dry", [[], ["--dry-run"]])
def test_a_root_inside_another_throwaway_is_a_usage_error(w: TW, dry: list[str]) -> None:
    outer = w.made("--source", "remote")
    w.top["throwaway_root"] = str(Path(outer["path"]) / "nested-root")
    w.failed(w.throw("--source", "remote", *dry), 2)
    assert not (Path(outer["path"]) / "nested-root").exists()
    assert [r["id"] for r in w.records()] == [outer["id"]]


@pytest.mark.parametrize("where", ["clone", "inside-clone"])
def test_a_root_in_a_clone_is_a_usage_error(w: TW, where: str) -> None:
    root = w.clone if where == "clone" else w.clone / "sub" / "root"
    w.top["throwaway_root"] = str(root)
    w.failed(w.throw("--source", "remote"), 2)
    assert w.records() == []
    assert not (w.clone / "sub").exists()


def claude_session_unsafe(
    path: str, *, home: str, env_path: str, xdg: dict[str, str]
) -> str | None:
    """claude-session's unsafe write-target rules (its interface, Path rules; copied, pinned),
    for a cwd: `/`, `$HOME` or an ancestor, a `PATH` directory or inside one, an XDG home or
    inside / containing one, `~/.claude` or inside it, the default state root, and anything
    inside a `$HOME/.<name>` entry."""

    def inside(p: str, d: str) -> bool:
        return p == d or p.startswith(d.rstrip("/") + "/")

    p = os.path.realpath(path)
    if p == "/" or inside(home, p):
        return "home or ancestor"
    for d in env_path.split(":"):
        if d.startswith("/") and inside(p, os.path.realpath(d)):
            return f"PATH dir {d}"
    for name, d in xdg.items():
        d = os.path.realpath(d)
        if inside(p, d) or inside(d, p):
            return name
    if inside(p, os.path.join(home, ".claude")):
        return "~/.claude"
    if inside(p, os.path.join(home, ".local", "state", "claude-session")):
        return "state root"
    rel = os.path.relpath(p, os.path.realpath(home))
    if not rel.startswith("..") and rel.split(os.sep)[0].startswith("."):
        return "hidden home directory"
    return None


def test_the_default_location_passes_claude_sessions_unsafe_cwd_rules(
    tmp_path: Path, private_home: Path
) -> None:
    home = str(private_home)
    xdg = {
        "XDG_CONFIG_HOME": str(private_home / ".config"),
        "XDG_DATA_HOME": str(private_home / ".local" / "share"),
        "XDG_STATE_HOME": str(private_home / ".local" / "state"),
    }
    rules: dict[str, Any] = {"home": home, "env_path": "/usr/bin:/bin:/usr/local/bin", "xdg": xdg}
    # the copied rules do refuse what they should (a control)
    for bad in ("/", home, str(private_home.parent), f"{home}/.ssh/x", f"{home}/.config/t",
                f"{home}/.local/state/claude-session/s", "/usr/bin/x"):  # fmt: skip
        assert claude_session_unsafe(bad, **rules), bad
    # the defaults: root `~/wtctl` (no config), throwaway root `<root>/throwaway`
    cw = TW(tmp_path, private_home).build()
    del cw.top["root"]
    rec = cw.made("--source", "remote")
    assert rec["path"].startswith(str(private_home / "wtctl" / "throwaway") + "/")
    assert claude_session_unsafe(rec["path"], **rules) is None
    # the same for a configured root outside the home directory
    other = TW(tmp_path / "o", private_home)
    (tmp_path / "o").mkdir()
    other.build()
    assert claude_session_unsafe(other.made("--source", "remote")["path"], **rules) is None


@pytest.mark.parametrize("where", [".hidden/t", ".config/wtctl/t", ".local/state/t"])
def test_a_root_in_a_hidden_home_directory_is_refused_at_config(
    w: TW, private_home: Path, where: str
) -> None:
    w.top["throwaway_root"] = str(private_home / where)
    err = w.failed(w.throw("--source", "remote"), 2)
    assert err["code"] == "config_invalid"
    assert not (private_home / where).exists()


# -- dry run, hooks, log -----------------------------------------------------------------------


def test_dry_run_fetches_nothing_and_writes_nothing(w: TW, tmp_path: Path) -> None:
    hit = tmp_path / "hook-ran"
    w.top["hooks"] = {"pre-create": [{"cmd": f"touch {hit}"}]}
    spy = Spy()
    w.git_runner = spy
    r = w.throw("--source", "remote", "--dry-run")
    assert r.code == 0, r.err
    data = r.json()
    validate("wtctl/cli/create.json", data)
    assert data["dry_run"] is True and data["record"]["state"] == "creating"
    assert data["record"]["source"] == "remote" and data["record"]["fetch_url"] is None
    assert not {"fetch", "init", "read-tree"} & set(spy.subs())
    assert w.records() == [] and w.leases() == [] and not hit.exists() and not w.troot.exists()


def test_pre_create_payload_veto_and_escalation(w: TW, tmp_path: Path) -> None:
    out = tmp_path / "payload.json"
    w.top["hooks"] = {"pre-create": [{"cmd": f"cat > {out}"}]}
    rec = w.made("--source", "auto", "--label", "k=v")
    payload = json.loads(out.read_text())
    validate("wtctl/hooks/pre-create.json", payload)
    assert payload["object_type"] == "throwaway" and payload["source"] == "auto"
    assert payload["ref"] == w.shas["c3"] and payload["labels"] == ["k=v"]
    assert payload["path"] == rec["path"] and payload["clone_path"] == str(w.clone)
    assert payload["branch"] is None and payload["branch_action"] is None
    w.top["hooks"] = {"pre-create": [{"cmd": "echo no >&2; exit 3"}]}
    w.failed(w.throw(), 11)
    w.top["hooks"] = {"pre-create": [{"cmd": "exit 13"}]}
    w.failed(w.throw(), 13)
    assert [r["state"] for r in w.records()] == ["active"]
    assert leaks(w.troot) == [rec["id"]] + [f"{rec['id']}/{x}" for x in leaks(w.troot / rec["id"])]


def test_post_create_payload(w: TW, tmp_path: Path) -> None:
    out = tmp_path / "post.json"
    w.top["hooks"] = {"post-create": [{"cmd": f"cat > {out}"}]}
    rec = w.made("--source", "remote", "--label", "run=1")
    payload = json.loads(out.read_text())
    validate("wtctl/hooks/post-create.json", payload)
    assert payload["record"]["id"] == rec["id"] and payload["record"]["state"] == "active"
    assert payload["bootstrap"] is None and payload["object_type"] == "throwaway"


def test_labels_and_the_log_but_no_audit(w: TW) -> None:
    rec = w.made("--source", "remote", "--label", "run=7", "--label", "k=v")
    assert rec["labels"] == ["k=v", "run=7"] and rec["holders"] == []
    events = [e for e in w.log_events() if e["event"] == "throwaway_create"]
    assert len(events) == 1 and events[0]["id"] == rec["id"] and events[0]["level"] == "info"
    assert w.audit_records() == []


def test_a_vetoed_create_logs_a_warning(w: TW) -> None:
    w.top["hooks"] = {"pre-create": [{"cmd": "exit 3"}]}
    w.failed(w.throw(), 11)
    events = [e for e in w.log_events() if e["event"] == "throwaway_create"]
    assert events and events[-1]["level"] == "warn"


# -- credentials ------------------------------------------------------------------------------

TOKEN = "tok-4f9c2a7e1b3d5c6a8e0f"


def test_a_token_never_reaches_the_throwaway_argv_logs_or_output(
    w: TW, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WT_TEST_TOKEN", TOKEN)
    w.top["hosts"] = {"example.test": {"token_env": "WT_TEST_TOKEN"}}
    w.repo_cfg = {"clone_url": "https://example.test/org/repo.git"}
    seen: dict[str, Any] = {}

    def before(sub, argv, kw):  # noqa: ANN001, ANN202
        if sub == "fetch":
            env = kw["env"]
            seen["argv"] = list(argv)
            seen["env_has_header"] = any(
                v.startswith("Authorization: Basic ") for k, v in env.items() if "VALUE" in k
            )
            seen["config"] = (Path(kw["cwd"]) / "config").read_text()
            seen["scheme"] = env["GIT_ALLOW_PROTOCOL"]
            return GitResult(128, b"", f"fatal: bad {TOKEN} credentials\n".encode())
        return None

    w.git_runner = Spy(before=before)
    r = w.throw("--source", "remote")
    assert r.code == 12
    assert seen["env_has_header"] and seen["scheme"] == "https"  # the control: it was sent
    assert not any(TOKEN in a for a in seen["argv"]) and TOKEN not in seen["config"]
    assert "remote." not in seen["config"] and "extraheader" not in seen["config"].lower()
    for text in (r.out, r.err, json.dumps(w.log_events())):
        assert TOKEN not in text
    for f in (w.db, w.db.parent / "log.jsonl"):
        assert not f.exists() or TOKEN.encode() not in f.read_bytes()
    nothing_left(w)


def test_a_token_is_not_sent_over_plaintext(w: TW, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WT_TEST_TOKEN", TOKEN)
    w.top["hosts"] = {"example.test": {"token_env": "WT_TEST_TOKEN"}}
    w.repo_cfg = {"clone_url": "http://example.test/org/repo.git"}
    spy = Spy(before=fail_sub("fetch"))
    w.git_runner = spy
    r = w.throw("--source", "remote")
    assert r.code == 12 and TOKEN not in r.out + r.err
    assert "fetch" not in spy.subs()
    nothing_left(w)


def test_the_throwaway_keeps_no_credentials_or_urls(w: TW) -> None:
    p = Path(w.made("--source", "remote")["path"])
    blob = b"".join(f.read_bytes() for f in (p / ".git").rglob("*") if f.is_file()
                    and "objects" not in f.parts)  # fmt: skip
    assert str(w.remote).encode() not in blob  # no remote, no FETCH_HEAD, no config entry


# -- concurrency ------------------------------------------------------------------------------


def test_concurrent_creates_get_distinct_ids_and_directories(w: TW) -> None:
    w.config()  # writes config.yaml
    cmd = [sys.executable, "-m", "wtctl", "--config", str(w.tmp / "config.yaml"), "--json",
           "--actor", "auto:t", "create", "--throwaway", "--repo", REPO, "--ref", w.shas["c3"],
           "--source", "remote"]  # fmt: skip
    procs = [
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    outs = [(p.communicate(timeout=120), p.returncode) for p in procs]
    assert all(code == 0 for _, code in outs), outs
    ids = {json.loads(o)["record"]["id"] for (o, _), _ in outs}
    assert len(ids) == 4
    assert sorted(os.listdir(w.troot)) == sorted(ids)
    assert {r["state"] for r in w.records()} == {"active"} and w.leases() == []
    assert w.count("anchors") == 1


# -- the throwaway can be pushed from by an explicit URL ---------------------------------------


@pytest.mark.parametrize(
    ("argv", "shallow"),
    [
        (["--source", "remote"], False),  # depth 50: the whole (short) history
        (["--source", "remote", "--depth", "1"], True),
        (["--source", "local"], False),
        (["--source", "local", "--depth", "1"], True),
    ],
)
def test_a_fast_forward_push_from_the_throwaway_by_explicit_url(
    w: TW, argv: list[str], shallow: bool
) -> None:
    p = Path(w.made(*argv)["path"])
    assert (p / ".git" / "shallow").exists() is shallow
    (p / "pushed.txt").write_text("p")
    w.tg(p, "add", "pushed.txt")
    w.tg(p, "commit", "-q", "-m", "pushed")
    new = w.tg(p, "rev-parse", "HEAD")
    w.tg(p, "push", "-q", str(w.remote), "HEAD:refs/heads/main")
    assert (
        plain_git(["rev-parse", "main"], cwd=w.remote, home=w.home).stdout.decode().strip() == new
    )
    assert plain_git(["rev-list", "--count", "main"], cwd=w.remote, home=w.home).stdout == b"4\n"
