"""Sparse-checkout worktrees (docs/interface.md, Sparse checkouts), against real git."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.removelib import RR
from tests.schemacheck import validate
from wtctl import sparse
from wtctl.errors import UsageError

# -- the pure rules ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "/",
        "-x",
        "--help",
        "/abs",
        "../x",
        "a/../b",
        "a/./b",
        ".",
        "a//b",
        "a\nb",
        "a\rb",
        "a\0b",
        "a\tb",
        ".git",
        "a/.GIT/b",
        "*",
        "a*",
        "a?b",
        "a[b]",
        "!neg",
        "a!b",
        "#c",
        "a\\b",
        'a"b',
        " a",
        "a ",
        "a/ b",
        "a.",
        "x" * 1025,
        "a/" + "x" * 256,
        "\udcff",
    ],
)
def test_hostile_directories_are_refused(bad: str) -> None:
    with pytest.raises(UsageError):
        sparse.normalize([bad])


def test_too_many_and_none_are_refused() -> None:
    with pytest.raises(UsageError):
        sparse.normalize([])
    with pytest.raises(UsageError):
        sparse.normalize([f"d{i}" for i in range(sparse.MAX_DIRS + 1)])
    assert len(sparse.normalize([f"d{i}" for i in range(sparse.MAX_DIRS)])) == sparse.MAX_DIRS


def test_normalize_trims_sorts_dedups_and_drops_nested() -> None:
    assert sparse.normalize(["b/", "a/x", "a/x/", "a/x/y", "ünï/ç"]) == ["a/x", "b", "ünï/ç"]


def test_pattern_file_is_what_git_writes_for_a_cone() -> None:
    assert sparse.pattern_file(["a/b", "c"]).decode() == ("/*\n!/*/\n/a/\n!/a/*/\n/a/b/\n/c/\n")


def test_cone_membership() -> None:
    cone = sparse.Cone(["a/b", "c"])
    for path in ("top.txt", "a/direct.txt", "a/b/x", "a/b/y/z", "c/q"):
        assert cone.includes(path), path
    for path in ("a/other/x", "d/x", "a/bb/x", "cc/x", "a/b.txt/x"):
        assert not cone.includes(path), path


# -- a world with directories --------------------------------------------------------------------

TREE = [
    "top.txt",
    "a/direct.txt",
    "a/b/deep.txt",
    "a/b/c/deeper.txt",
    "a/other/x.txt",
    "d/one.txt",
    "d/e/two.txt",
    "f/three.txt",
    "ünï/ç/uni.txt",
]


class SW(RR):
    def build_tree(self) -> SW:
        self.build()
        for rel in TREE:
            p = self.work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(rel)
        self.g(["add", "."], self.work)
        self.g(["commit", "-q", "-m", "tree"], self.work)
        self.g(["push", "-q", str(self.remote), "main"], self.work)
        return self

    def sparse_start(self, capsys: Any, *dirs: str) -> SW:  # noqa: ANN401
        argv: list[str] = []
        for d in dirs:
            argv += ["--sparse", d]
        return self.start_built(capsys, *argv, bootstrap=False)  # type: ignore[return-value]

    def disk(self) -> set[str]:
        return {
            str(p.relative_to(self.wt))
            for p in self.wt.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(self.wt).parts[:1]
        }

    def skipped(self) -> set[str]:
        out = self.g(["ls-files", "-v", "-z"], self.wt)
        return {rec[2:] for rec in out.split("\0") if rec[:1] == "S"}

    def tracked(self) -> set[str]:
        return {p for p in self.g(["ls-files", "-z"], self.wt).split("\0") if p}


@pytest.fixture
def w(tmp_path: Path, private_home: Path, inproc) -> SW:  # noqa: ANN001
    return SW(tmp_path, private_home).build_tree()


def paths(d: dict[str, Any], name: str) -> list[tuple[str, str]]:
    return [(x["path"], x.get("detail", "")) for x in d["checks"][name]["details"]]


# -- create ------------------------------------------------------------------------------------


@pytest.mark.parametrize("dirs", [["a/b"], ["d", "a/b/c"], ["f", "ünï/ç"], ["a"]])
def test_create_checks_out_exactly_the_cone(w: SW, capsys, dirs: list[str]) -> None:  # noqa: ANN001
    w.sparse_start(capsys, *dirs)
    cone = sparse.Cone(sparse.normalize(dirs))
    all_tracked = w.tracked()
    want = {p for p in all_tracked if cone.includes(p)}
    assert w.disk() == want
    assert w.skipped() == all_tracked - want  # git's own bits agree with wtctl's computation
    assert w.record["sparse"] == sparse.normalize(dirs)
    assert (w.admin / "info" / "sparse-checkout").read_bytes() == sparse.pattern_file(
        sparse.normalize(dirs)
    )


def test_create_writes_no_git_config(w: SW, capsys) -> None:  # noqa: ANN001
    before = (w.clone / ".git" / "config").read_text()
    w.sparse_start(capsys, "a/b")
    assert (w.clone / ".git" / "config").read_text() == before
    assert not (w.admin / "config.worktree").exists()


def test_status_and_remove_of_a_clean_sparse_worktree(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    d = w.st(capsys)
    assert d["would_remove"] is True and d["reason"] is None
    assert {k for k, v in d["checks"].items() if v["status"] == "fail"} == set()
    w.removed()
    assert not w.wt.exists()
    assert w.rows("SELECT count(*) AS n FROM sparse_dirs")[0]["n"] == 1  # tombstone keeps it


def test_show_reports_the_cone_and_a_full_checkout_has_none(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "d")
    r = w.cli(capsys, "show", w.rid)
    assert r.code == 0
    validate("wtctl/cli/show.json", r.json())
    assert r.json()["sparse"] == ["d"]


def test_full_checkout_record_has_null_sparse(w: SW, capsys) -> None:  # noqa: ANN001
    w.start_built(capsys, bootstrap=False)
    assert w.record["sparse"] is None


def test_detached_sparse_worktree(w: SW, capsys) -> None:  # noqa: ANN001
    from tests.test_create import ok

    rec = ok(w.create(capsys, "--detach", "--no-bootstrap", "--sparse", "f"))["record"]
    assert rec["branch"] is None and rec["sparse"] == ["f"]


def test_dry_run_reports_the_cone_and_writes_nothing(w: SW, capsys) -> None:  # noqa: ANN001
    from tests.test_create import ok

    before = w.snapshot()
    data = ok(w.create(capsys, "--branch", "x", "--sparse", "f/", "--dry-run"))
    assert data["record"]["sparse"] == ["f"] and data["dry_run"] is True
    assert w.snapshot() == before


@pytest.mark.parametrize(
    "bad", ["", "-x", "../x", "/etc", "a\nb", "*", ".git", "a/../../b", "x" * 2000]
)
def test_create_refuses_hostile_patterns_with_nothing_written(
    w: SW,
    capsys,  # noqa: ANN001
    bad: str,
) -> None:
    before = w.snapshot()
    r = w.create(capsys, "--branch", "x", "--sparse", bad)
    assert r.code == 2, (r.out, r.err)
    assert w.snapshot() == before
    assert w.count("records") == 0


def test_a_pattern_cannot_smuggle_a_second_pattern_or_option(w: SW, capsys) -> None:  # noqa: ANN001
    r = w.create(capsys, "--branch", "x", "--sparse", "a\n/*\n!/a")
    assert r.code == 2
    r = w.create(capsys, "--branch", "x", "--sparse", "--no-checkout")
    assert r.code == 2


def test_throwaway_refuses_sparse(w: SW, capsys) -> None:  # noqa: ANN001
    r = w.cli(capsys, "create", "--throwaway", "--repo", "example.test/org/repo",
              "--ref", "a" * 40, "--sparse", "a")  # fmt: skip
    assert r.code == 2


def test_a_missing_directory_is_fine_and_leaves_the_top_files(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "no/such/dir")
    assert w.disk() == {"top.txt", "c1.txt", "c2.txt", "c3.txt"}
    assert w.st(capsys)["would_remove"] is True


# -- content checks: nothing hides behind the skip-worktree bits ---


def test_edit_inside_the_cone_is_dirty(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    (w.wt / "a/b/deep.txt").write_text("edited")
    d = w.st(capsys)
    assert paths(d, "tracked") == [("a/b/deep.txt", "changed")]
    assert d["reason"] == "dirty"


def test_deleting_a_file_inside_the_cone_is_dirty(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    (w.wt / "a/direct.txt").unlink()
    assert paths(w.st(capsys), "tracked") == [("a/direct.txt", "changed")]


def test_new_untracked_file_in_a_sparse_worktree(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    (w.wt / "f").mkdir()
    (w.wt / "f/new.txt").write_text("n")
    d = w.st(capsys)
    assert [p for p, _ in paths(d, "untracked")] == ["f/new.txt"]
    assert d["checks"]["tracked"]["status"] == "pass"


def test_skip_worktree_set_on_an_edited_file_inside_the_cone_is_found(
    w: SW,
    capsys,  # noqa: ANN001
) -> None:
    w.sparse_start(capsys, "a/b")
    (w.wt / "a/b/deep.txt").write_text("hidden edit")
    w.g(["update-index", "--skip-worktree", "a/b/deep.txt"], w.wt)
    assert w.g(["status", "--porcelain"], w.wt) == ""  # plain git is fooled: the precondition
    d = w.st(capsys)
    assert ("a/b/deep.txt", "hidden_changes") in paths(d, "tracked")
    assert ("a/b/deep.txt", "changed") in paths(d, "tracked")
    assert d["reason"] == "dirty"


def test_an_edit_to_a_file_outside_the_cone_is_found(w: SW, capsys) -> None:  # noqa: ANN001
    """The bit is where the checkout put it, but the file is there and different."""
    w.sparse_start(capsys, "a/b")
    (w.wt / "f").mkdir()
    (w.wt / "f/three.txt").write_text("written behind the bit")
    assert w.g(["status", "--porcelain"], w.wt) == ""
    d = w.st(capsys)
    assert paths(d, "tracked") == [("f/three.txt", "changed")]


def test_assume_unchanged_on_a_skipped_path_is_found(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    w.g(["update-index", "--assume-unchanged", "f/three.txt"], w.wt)
    d = w.st(capsys)
    assert ("f/three.txt", "hidden_changes") in paths(d, "tracked")


def test_staged_change_to_a_skipped_path_is_found(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    (w.tmp / "forged").write_text("forged")
    blob = w.g(["hash-object", "-w", str(w.tmp / "forged")], w.wt)
    w.g(["update-index", "--cacheinfo", f"100644,{blob},f/three.txt"], w.wt)
    d = w.st(capsys)
    assert ("f/three.txt", "changed") in paths(d, "tracked")


def test_removing_a_skipped_path_from_the_index_is_found(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    w.g(["update-index", "--force-remove", "f/three.txt"], w.wt)
    d = w.st(capsys)
    assert ("f/three.txt", "changed") in paths(d, "tracked")


def test_clearing_the_bit_of_a_skipped_path_is_not_a_change(w: SW, capsys) -> None:  # noqa: ANN001
    """Absent and outside the cone is the state wtctl made; the bit itself carries no content."""
    w.sparse_start(capsys, "a/b")
    w.g(["update-index", "--no-skip-worktree", "f/three.txt"], w.wt)
    assert w.st(capsys)["would_remove"] is True


def test_the_cone_comes_from_the_record_not_from_the_admin_area(w: SW, capsys) -> None:  # noqa: ANN001
    """Rewriting the pattern file or the config cannot widen what is accepted as skipped."""
    w.sparse_start(capsys, "a/b")
    (w.admin / "info" / "sparse-checkout").write_text("/*\n")
    w.g(["update-index", "--skip-worktree", "a/b/deep.txt"], w.wt)
    d = w.st(capsys)
    assert ("a/b/deep.txt", "hidden_changes") in paths(d, "tracked")


def test_remove_refuses_a_hidden_edit_without_force(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    (w.wt / "a/b/deep.txt").write_text("e")
    w.g(["update-index", "--skip-worktree", "a/b/deep.txt"], w.wt)
    assert w.refused()["reason"] == "dirty"
    assert w.intact()


# -- the baseline covers the sparse state ----------------------------------------------------------


def test_baseline_check_passes_then_catches_the_pattern_file(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    ok = w.cli(capsys, "baseline", "check", w.rid)
    assert ok.code == 0, (ok.out, ok.err)
    pf = w.admin / "info" / "sparse-checkout"
    pf.write_text(pf.read_text() + "/f/\n")
    r = w.cli(capsys, "baseline", "check", w.rid)
    assert r.code == 20
    err = r.json()["error"]
    assert err["reason"] == "repo_config_changed"
    assert any(i["path"].endswith("/info/sparse-checkout") for i in err["items"])
    assert w.refused()["reason"] == "repo_config_changed"
    assert w.intact()


def test_baseline_catches_a_deleted_pattern_file(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    (w.admin / "info" / "sparse-checkout").unlink()
    assert w.cli(capsys, "baseline", "check", w.rid).code == 20


@pytest.mark.parametrize(
    ("key", "value"),
    [("core.sparseCheckout", "true"), ("core.sparseCheckoutCone", "true"),
     ("extensions.worktreeConfig", "true")],
)  # fmt: skip
def test_baseline_catches_sparse_config_keys(w: SW, capsys, key: str, value: str) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    w.g(["config", key, value])
    assert w.cli(capsys, "baseline", "check", w.rid).code == 20


def test_baseline_catches_sparse_keys_in_the_worktree_config(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    (w.admin / "config.worktree").write_text("[core]\n\tsparseCheckout = true\n")
    assert w.cli(capsys, "baseline", "check", w.rid).code == 20


def test_a_forced_sparse_config_does_not_change_the_content_checks(w: SW, capsys) -> None:  # noqa: ANN001
    """Content checks ignore sparse settings an agent sets (they are forced off for the private
    index), so the verdict is the same as without them."""
    w.sparse_start(capsys, "a/b")
    (w.wt / "a/b/deep.txt").write_text("edited")
    (w.admin / "config.worktree").write_text(
        "[core]\n\tsparseCheckout = true\n\tsparseCheckoutCone = true\n"
    )
    w.g(["config", "extensions.worktreeConfig", "true"])
    d = w.st(capsys)
    assert ("a/b/deep.txt", "changed") in paths(d, "tracked")


def test_a_format_3_baseline_ignores_the_pattern_file(w: SW, capsys) -> None:  # noqa: ANN001
    """A record recorded before wtctl covered the file is compared as it was recorded."""
    w.start_built(capsys, bootstrap=False)
    w.db_exec("INSERT OR REPLACE INTO baseline_meta(record_id, format) VALUES (?, 3)", w.rid)
    (w.admin / "info").mkdir(exist_ok=True)
    (w.admin / "info" / "sparse-checkout").write_text("/*\n")
    r = w.cli(capsys, "baseline", "check", w.rid)
    assert r.code == 0
    d = r.json()
    validate("wtctl/cli/baseline-check.json", d)
    assert d["format"] == 3
    assert [i["path"].endswith("/info/sparse-checkout") for i in d["new_coverage"]] == [True]


def test_a_current_format_baseline_has_no_format_notice(w: SW, capsys) -> None:  # noqa: ANN001
    w.sparse_start(capsys, "a/b")
    d = w.cli(capsys, "baseline", "check", w.rid).json()
    assert "format" not in d and "new_coverage" not in d


def test_a_current_format_baseline_notices_a_planted_pattern_file(w: SW, capsys) -> None:  # noqa: ANN001
    w.start_built(capsys, bootstrap=False)
    (w.admin / "info").mkdir(exist_ok=True)
    (w.admin / "info" / "sparse-checkout").write_text("/*\n")
    assert w.cli(capsys, "baseline", "check", w.rid).code == 20


# -- a worktree that git made sparse is not a wtctl cone ---


def test_git_made_sparse_worktree_still_needs_force(w: SW, capsys) -> None:  # noqa: ANN001
    """No recorded cone: every skip-worktree entry is reported, as before (D53)."""
    w.start_built(capsys, bootstrap=False)
    w.g(["sparse-checkout", "set", "--no-cone", "c1.txt"], w.wt)
    d = w.st(capsys)
    assert d["checks"]["tracked"]["status"] == "fail"
    assert {x for _, x in paths(d, "tracked")} >= {"hidden_changes"}


# -- round 1 review ----------------------------------------------------------------------------


def test_cone_size_cap_is_a_usage_error_before_anything_is_written(w: SW, capsys) -> None:  # noqa: ANN001
    """Valid-looking deep directories would make a pattern file far past the baseline's file
    limit; the request is refused up front, with exit 2 and nothing written."""
    deep = "/".join("y" * 200 for _ in range(4))
    dirs = [f"{'x' * 200}/{i}/{deep}" for i in range(256)]
    with pytest.raises(UsageError):
        sparse.normalize(dirs)
    before = w.snapshot()
    r = w.create(capsys, "--branch", "x", *[a for d in dirs for a in ("--sparse", d)])
    assert r.code == 2, (r.out, r.err)
    assert w.snapshot() == before


def test_pattern_file_size_cap_leaves_ordinary_requests_alone() -> None:
    assert len(sparse.pattern_file(sparse.normalize(["a/b"]))) < sparse.MAX_PATTERN_BYTES


def test_create_with_bootstrap_keeps_a_current_baseline(w: SW, capsys) -> None:  # noqa: ANN001
    w.start_built(capsys, "--sparse", "a/b", bootstrap=True)
    assert w.record["sparse"] == ["a/b"]
    assert w.rows("SELECT format FROM baseline_meta WHERE record_id = ?", w.rid)[0]["format"] == 4
    assert w.cli(capsys, "baseline", "check", w.rid).code == 0
    d = w.st(capsys)
    assert d["would_remove"] is True and d["reason"] is None


def test_case_folding_checkout_that_disagrees_with_the_cone_fails_create(w: SW, capsys) -> None:  # noqa: ANN001
    """With core.ignorecase git checks out a/b for the request A/B while wtctl's cone says it is
    outside: create fails (the record stays `creating` for reconcile, as for any failure after
    the worktree was added) rather than record a cone that could hide a deleted file."""
    w.g(["config", "core.ignorecase", "true"])
    r = w.create(capsys, "--branch", "x", "--sparse", "A/B", "--no-bootstrap")
    assert r.code == 12, (r.out, r.err)
    e = r.json()["error"]
    assert e["code"] == "create_failed"
    assert "does not match the requested directories" in e["message"]
    assert [x["state"] for x in w.records()] == ["creating"]


def test_a_failed_cone_check_does_not_survive_reconcile_as_a_trusted_cone(w: SW, capsys) -> None:  # noqa: ANN001
    """The failed create leaves `creating`; reconcile adopts it, and a file deleted in the
    case-folded directory must not read as clean: the cone is dropped, so the skip-worktree
    tag hides a change instead of being trusted."""
    from tests.removallib import FakeScanner

    w.scanner = FakeScanner()
    w.g(["config", "core.ignorecase", "true"])
    r = w.create(capsys, "--branch", "x", "--sparse", "A/B", "--no-bootstrap")
    assert r.code == 12, (r.out, r.err)
    rid = w.records()[0]["id"]
    assert w.rows("SELECT dir FROM sparse_dirs WHERE record_id = ?", rid) == []
    rc = w.cli(capsys, "reconcile")
    assert rc.code == 0, (rc.out, rc.err)
    rec = w.records()[0]
    assert rec["state"] == "active"
    (Path(rec["path"]) / "a/b/deep.txt").unlink()
    d = w.st(capsys, target=rid)
    assert d["would_remove"] is False, d


def test_reconcile_drops_a_recorded_cone_the_checkout_disagrees_with(w: SW, capsys) -> None:  # noqa: ANN001
    """A create killed after recording the cone but before verifying it: reconcile re-runs the
    comparison and adopts the record without the cone."""
    from tests.removallib import FakeScanner

    w.scanner = FakeScanner()
    w.g(["config", "core.ignorecase", "true"])
    assert w.create(capsys, "--branch", "x", "--sparse", "A/B", "--no-bootstrap").code == 12
    rid = w.records()[0]["id"]
    w.db_exec("INSERT INTO sparse_dirs(record_id, dir) VALUES (?, ?)", rid, "A/B")
    assert w.cli(capsys, "reconcile").code == 0
    assert w.rows("SELECT dir FROM sparse_dirs WHERE record_id = ?", rid) == []
    (Path(w.records()[0]["path"]) / "a/b/deep.txt").unlink()
    assert w.st(capsys, target=rid)["would_remove"] is False


def test_case_folding_agreeing_checkout_is_accepted(w: SW, capsys) -> None:  # noqa: ANN001
    w.g(["config", "core.ignorecase", "true"])
    w.sparse_start(capsys, "a/b")
    assert "a/b/deep.txt" in w.disk()
    assert w.skipped() == w.tracked() - {p for p in w.tracked() if sparse.Cone(["a/b"]).includes(p)}


def test_a_submodule_of_a_sparse_worktree_has_no_cone_of_its_own(w: SW, capsys) -> None:  # noqa: ANN001
    """A skip-worktree bit on a submodule file is a hidden change even when the file's path
    inside the submodule would be outside the parent's cone."""
    from tests.test_removal_submodules import FILE_ALLOW, add_sub, bare_origin

    origin = bare_origin(w, "sub")
    src = w.tmp / "sub-src"
    (src / "d").mkdir()
    (src / "d" / "x.txt").write_text("x")
    w.g(["add", "."], src)
    w.g(["commit", "-q", "-m", "more"], src)
    w.g(["push", "-q", str(origin), "main"], src)
    del FILE_ALLOW
    w.sparse_start(capsys, "vendor/sub")
    sub = add_sub(w, capsys, origin)
    w.g(["update-index", "--skip-worktree", "d/x.txt"], sub)
    d = w.st(capsys)
    detail = {"detail": "submodule", "path": "vendor/sub", "reason": "hidden_changes"}
    assert detail in d["checks"]["nested_repos"]["details"], d["checks"]["nested_repos"]
    assert (d["would_remove"], d["reason"]) == (False, "dirty")
