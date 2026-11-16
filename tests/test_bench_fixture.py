"""The synthetic-monorepo generator, at tiny sizes (the timing runs themselves are ``bench``)."""

from __future__ import annotations

from pathlib import Path

from tests.bench import fixture as fx
from tests.bench.fixture import TINY


def g(remote: Path, home: Path, *args: str) -> str:
    return fx._git(remote, home, *args)


def test_remote_has_the_requested_dimensions(tmp_path: Path) -> None:
    remote, home = tmp_path / "r.git", tmp_path / "home"
    fx.build_remote(remote, home, TINY)
    assert g(remote, home, "rev-list", "--count", "main") == str(TINY.commits)
    tree = g(remote, home, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert len([p for p in tree if p.endswith(".ts")]) == TINY.files
    heads = g(
        remote, home, "for-each-ref", "--format=%(refname)", "refs/heads/feature"
    ).splitlines()
    assert len(heads) == TINY.branches
    tags = g(remote, home, "for-each-ref", "--format=%(refname)", "refs/tags").splitlines()
    assert len(tags) == TINY.tags
    size = g(remote, home, "cat-file", "-s", "main:assets/blob0.bin")
    assert size == str(TINY.large_blob_bytes)
    assert ".gitignore" in tree
    g(remote, home, "fsck", "--strict")


def test_generation_is_deterministic(tmp_path: Path) -> None:
    shas = []
    for name in ("a", "b"):
        remote = tmp_path / f"{name}.git"
        fx.build_remote(remote, tmp_path / f"home{name}", TINY)
        shas.append(g(remote, tmp_path / f"home{name}", "rev-parse", "main"))
    assert shas[0] == shas[1]


def test_stale_then_finish_equals_a_full_build(tmp_path: Path) -> None:
    home = tmp_path / "home"
    full, staged = tmp_path / "full.git", tmp_path / "staged.git"
    fx.build_remote(full, home, TINY)
    fx.build_remote(staged, home, TINY, stale_at=3)
    assert g(staged, home, "rev-list", "--count", "main") == "3"
    fx.finish_remote(staged, home, TINY, 3)
    assert g(staged, home, "rev-parse", "main") == g(full, home, "rev-parse", "main")
    assert g(staged, home, "for-each-ref") == g(full, home, "for-each-ref")


def test_touched_files_are_distinct_and_in_range() -> None:
    for i in range(1, 50):
        picked = fx.touched(TINY, i)
        assert len(picked) == len(set(picked)) == TINY.files_per_commit
        assert all(0 <= n < TINY.files for n in picked)


def test_paths_are_unique_and_deep() -> None:
    paths = {fx.file_path(TINY, n) for n in range(TINY.files)}
    assert len(paths) == TINY.files
    assert all(p.count("/") == 2 + TINY.depth for p in paths)


def test_populate_ignored_counts_files(tmp_path: Path) -> None:
    assert fx.populate_ignored(tmp_path, 3, 4) == 12
    assert len(list((tmp_path / "node_modules").rglob("*.js"))) == 12


def test_range_outside_history_is_refused(tmp_path: Path) -> None:
    remote, home = tmp_path / "r.git", tmp_path / "home"
    fx.init_remote(remote, home)
    try:
        fx.extend_main(remote, home, TINY, 0, TINY.commits + 1)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_git_log_parsing_and_table() -> None:
    from tests.bench.world import Results, Timed, parse_git_log

    calls = parse_git_log("1500000\tstatus\nnot a line\n250000\tfetch\n")
    assert calls == [(1.5, "status"), (0.25, "fetch")]
    res = Results()
    for wall in (3.0, 1.0, 2.0):
        res.add("op", Timed(["x"], 0, wall, 2048, "", "", calls))
    row = res.rows()[0]
    assert (row["median_s"], row["min_s"], row["max_s"], row["peak_rss_mib"]) == (2.0, 1.0, 3.0, 2)
    assert row["git_slowest"] == (1.5, "status")
    assert "| op | 3 | 2.0 | 1.0-3.0 |" in res.markdown()


def test_world_runs_the_real_cli_and_times_its_git_calls(tmp_path: Path) -> None:
    from tests.bench.world import REPO, World

    w = World(tmp_path / "b", TINY)
    (tmp_path / "b").mkdir()
    w.build(stale_at=3, copy_env_files=0)
    created = w.run("create", "--repo", REPO, "--no-bootstrap")
    assert created.code == 0, created.err
    assert created.rss_kib > 0 and created.wall > 0
    assert {"worktree", "fetch"} & {name for _, name in created.git_calls}
    rid = created.json()["record"]["id"]
    status = w.run("status", rid)
    assert status.code == 0, status.err
    assert w.run("remove", rid, "--yes").code == 0
