"""The synthetic-monorepo generator, at tiny sizes (the timing runs themselves are ``bench``)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.bench import fixture as fx
from tests.bench import world as bw
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


# -- the benchmark directory guard (no data generated) ----------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "/",
        "",
        ".",
        "..",
        "../",
        "~",
        "/tmp",
        "/tmp/x",
        "/var/tmp/x",
        "/dev/shm",
        "/run/user",
    ],
)
def test_bench_dir_refuses_dangerous_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    if value == "~":
        value = str(Path.home())
    monkeypatch.setenv("BENCH_DIR", value)
    with pytest.raises(pytest.fail.Exception):
        bw.bench_dir()


def test_bench_dir_refuses_an_ancestor_of_the_home_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/home")
    for value in ("/home", str(Path("/home").resolve())):
        monkeypatch.setenv("BENCH_DIR", value)
        with pytest.raises(pytest.fail.Exception):
            bw.bench_dir()


def test_bench_dir_refuses_the_repository_root_and_its_ancestors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for p in (bw.ROOT, *bw.ROOT.parents):
        monkeypatch.setenv("BENCH_DIR", str(p))
        with pytest.raises(pytest.fail.Exception):
            bw.bench_dir()


def test_bench_dir_default_is_dot_bench_under_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BENCH_DIR", raising=False)
    assert bw.bench_dir() == bw.ROOT / ".bench"


def test_prepare_refuses_a_non_empty_directory_without_the_marker(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("precious")
    with pytest.raises(pytest.fail.Exception):
        bw.prepare_bench_dir(tmp_path, min_free_gib=0)
    assert (tmp_path / "keep.txt").read_text() == "precious"


def test_prepare_reuses_an_empty_or_marked_directory(tmp_path: Path) -> None:
    base = tmp_path / "b"
    bw.prepare_bench_dir(base, min_free_gib=0)
    assert (base / bw.MARKER).exists()
    (base / "old").write_text("x")
    bw.prepare_bench_dir(base, min_free_gib=0)
    assert not (base / "old").exists() and (base / bw.MARKER).exists()


def test_prepare_skips_before_touching_anything_when_the_disk_is_too_small(
    tmp_path: Path,
) -> None:
    target = tmp_path / "new" / "deeper"
    with pytest.raises(pytest.skip.Exception):
        bw.prepare_bench_dir(target, min_free_gib=10**9)
    assert not (tmp_path / "new").exists()


def test_cleanup_keeps_only_results_and_marker(tmp_path: Path) -> None:
    base = tmp_path / "b"
    bw.prepare_bench_dir(base, min_free_gib=0)
    (base / "results.json").write_text("{}")
    (base / "wt").mkdir()
    (base / "wt" / "f").write_text("x")
    (base / "ref.git").mkdir()
    (base / "last.out").write_text("x")
    bw.cleanup_bench_dir(base)
    assert sorted(p.name for p in base.iterdir()) == [bw.MARKER, "results.json"]


def test_a_killed_group_leaves_no_survivor(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    bw.kill_group(proc)
    assert proc.poll() is not None


def test_fast_import_reaps_its_process_when_the_stream_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, home = tmp_path / "r.git", tmp_path / "home"
    fx.init_remote(remote, home)
    procs: list[subprocess.Popen[Any]] = []
    real = subprocess.Popen

    def spy(*a: object, **kw: object) -> subprocess.Popen[Any]:
        p = real(*a, **kw)  # type: ignore[call-overload]
        procs.append(p)
        return p

    monkeypatch.setattr(subprocess, "Popen", spy)

    def broken() -> Iterator[bytes]:
        yield b"blob\n"
        raise ValueError("stream failed")

    with pytest.raises(ValueError):
        fx._fast_import(remote, home, broken())  # pyright: ignore[reportPrivateUsage]
    assert len(procs) == 1 and procs[0].poll() is not None
