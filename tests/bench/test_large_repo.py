"""Large-repository timings (issue #28). Marked ``bench``: deselected by default, never run in CI.

    BENCH_DIR=/some/big/disk/.bench uv run pytest -m bench tests/bench -s

Needs about 5 GB free under ``BENCH_DIR`` (default: ``.bench`` at the repository root, which is
gitignored). Generated data is removed afterwards unless ``BENCH_KEEP=1``. Knobs: ``BENCH_REPEATS``
(default 3), ``BENCH_IGNORED_FILES`` (default 150000), ``BENCH_STRESS`` (default 1: the
nested-scan scaling run with empty files up to past ``remove.nested_scan_limit``).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from tests.bench import fixture as fx
from tests.bench.world import (
    REPO,
    Results,
    Timed,
    World,
    bench_dir,
    cleanup_bench_dir,
    machine_info,
    prepare_bench_dir,
)

pytestmark = pytest.mark.bench

STALE_AT = 2000
ADVANCE = 1000
MIB = 1024 * 1024


def ok(run: Timed, *codes: int) -> Timed:
    assert run.code in (codes or (0,)), (run.argv, run.code, run.out[-800:], run.err[-800:])
    return run


def record_of(run: Timed) -> dict[str, str]:
    return run.json()["record"]


def test_large_repository_timings() -> None:
    base = bench_dir()
    prepare_bench_dir(base)
    repeats = int(os.environ.get("BENCH_REPEATS", "3"))
    ignored_files = int(os.environ.get("BENCH_IGNORED_FILES", "150000"))
    res = Results()
    try:
        _scenario(base, res, repeats, ignored_files)
    finally:
        info = machine_info(base)
        (base / "results.json").write_text(
            json.dumps({"machine": info, "rows": res.rows()}, indent=2)
        )
        print("\n" + json.dumps(info, indent=2) + "\n" + res.markdown())
        if os.environ.get("BENCH_KEEP") != "1":
            cleanup_bench_dir(base)  # everything but results.json and the marker


def phases() -> set[str]:
    return set(os.environ.get("BENCH_PHASES", "main,throwaway,many,reconcile,stress").split(","))


def outcome(run: Timed) -> str:
    """One line on what a ``status``/``remove --dry-run``/``reconcile`` run decided."""
    try:
        j = run.json()
    except ValueError:
        return f"exit {run.code}"
    if "checks" in j:
        bad = sorted(k for k, v in j["checks"].items() if v["status"] != "pass")
        return f"exit {run.code}; would_remove={j.get('would_remove')}; not pass: {bad or 'none'}"
    if "reports" in j:
        kinds: dict[str, int] = {}
        for r in j["reports"]:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        return f"exit {run.code}; changes={len(j['changes'])}; reports={kinds or 'none'}"
    return f"exit {run.code}"


def breakdown(run: Timed) -> str:
    """Seconds per git subcommand of one run, largest first (the rest of the wall time is wtctl
    itself: Python, its child walks and file-system work)."""
    totals: dict[str, float] = {}
    for sec, sub in run.git_calls:
        totals[sub] = totals.get(sub, 0.0) + sec
    top = sorted(totals.items(), key=lambda kv: -kv[1])[:6]
    return "git: " + ", ".join(f"{k} {v:.1f}s" for k, v in top)


def _scenario(base: Path, res: Results, repeats: int, ignored_files: int) -> None:
    d = fx.MONOREPO
    w = World(base, d)
    w.top["extract"] = {"max_file_bytes": 16 * MIB}
    w.build(stale_at=STALE_AT)
    print("fixture build (s):", {k: round(v, 1) for k, v in w.timeline.items()}, flush=True)
    # The clone's main worktree also holds installed dependencies, as a real one does.
    fx.populate_ignored(w.clone, ignored_files // 100, 100)
    res.add(
        "nested_repos walk in process, clone main worktree (~365k entries)",
        _scan_walk(w.clone, d.files + ignored_files),
    )
    res.add(
        "copy_env walk in process, clone main worktree (~365k entries)",
        _env_walk(w.clone, w.env_files),
    )
    w.git("config", "user.email", "bench@example.invalid")
    w.git("config", "user.name", "Bench")

    if "main" in phases():
        _main(w, res, d, repeats, ignored_files)
    if "throwaway" in phases():
        _throwaways(w, res, repeats)
    if "many" in phases():
        _many_records(base, res)
    if "reconcile" in phases():
        _reconcile_admin(base, res)
    if "stress" in phases():
        _stress(w, res)
    res.add("reconcile (big clone, no live records)", ok(w.run("reconcile")))


def _main(w: World, res: Results, d: fx.Dims, repeats: int, ignored_files: int) -> None:
    base = w.base
    # Reference: what plain git costs for the same transfer (the floor for a mirror refresh).
    t = time.monotonic()
    w._git(base, "clone", "-q", "--bare", f"file://{w.remote}", str(base / "ref.git"))
    res.add("reference: plain `git clone --bare` of the remote", _plain(time.monotonic() - t))
    shutil.rmtree(base / "ref.git")

    # Cold create: first mirror refresh, pack transfer of the clone's 18,000 missing commits,
    # checkout.
    cold = res.add(
        "create --no-bootstrap, cold (first mirror refresh, 18k-commit pack transfer)",
        ok(w.run("create", "--repo", REPO, "--no-bootstrap")),
    )
    res.notes[next(reversed(res.series))] = breakdown(cold)
    first = record_of(cold)
    assert w.git("rev-parse", first["base_sha"]) == first["base_sha"]
    res.add("remove, clean tree", ok(w.run("remove", first["id"], "--yes")))

    # The remote moves on: later mirror refresh and a small pack transfer.
    fx.advance_main(w.remote, w.ghome, d, d.commits, ADVANCE)
    st0 = ok(w.run("create", "--repo", REPO, "--no-bootstrap"))
    res.add(f"create --no-bootstrap, warm (refresh after {ADVANCE} new commits)", st0)
    res.notes[f"create --no-bootstrap, warm (refresh after {ADVANCE} new commits)"] = breakdown(st0)
    second = record_of(st0)
    res.add("status, clean tree, mirror already fresh", ok(w.run("status", second["id"])))
    fx.advance_main(w.remote, w.ghome, d, d.commits + ADVANCE, ADVANCE)
    res.add(
        f"status, clean tree, refreshing {ADVANCE} new commits first",
        ok(w.run("status", second["id"])),
    )
    ok(w.run("remove", second["id"], "--yes"))

    # Repeated: full create (bootstrap snapshots, copy_env, baseline), then the checks and remove.
    for n in range(repeats):
        c = ok(w.run("create", "--repo", REPO, "--copy-env"))
        res.add("create (bootstrap snapshots + copy_env + baseline), warm", c)
        res.notes["create (bootstrap snapshots + copy_env + baseline), warm"] = breakdown(c)
        rec = record_of(c)
        rid, wt = rec["id"], Path(rec["path"])
        warn = [x for x in c.json()["warnings"] if "limit" in x or "early" in x]
        if warn:
            res.notes["create (bootstrap snapshots + copy_env + baseline), warm"] = "; ".join(warn)
        t = time.monotonic()
        fx.populate_ignored(wt, ignored_files // 100, 100)
        print(f"populate {ignored_files} ignored files: {time.monotonic() - t:.1f}s", flush=True)
        res.add("status (+ node_modules ignored files)", ok(w.run("status", rid)))
        res.add("remove --dry-run", ok(w.run("remove", rid, "--dry-run")))
        res.add("baseline check", ok(w.run("baseline", "check", rid)))
        boot = ok(w.run("bootstrap", rid, "--copy-env"))
        res.add("bootstrap --copy-env (re-run; snapshots + env walk)", boot)
        if any("walk stopped early" in x for x in boot.json()["warnings"]):
            res.notes["bootstrap --copy-env (re-run; snapshots + env walk)"] = (
                "copy_env walk stopped early (limit)"
            )
        # A pushed branch tip: the remove then checks reachability against ~800 mirror refs.
        (wt / "bench-note.txt").write_text(f"{n}\n")
        w._git(wt, "add", "bench-note.txt")
        w._git(wt, "commit", "-q", "-m", f"bench {n}")
        w._git(wt, "push", "-q", str(w.remote), f"HEAD:refs/heads/{rec['branch']}")
        res.add(
            "remove --yes (350k-entry tree, pushed branch tip)", ok(w.run("remove", rid, "--yes"))
        )


def _scan_walk(root: Path, min_entries: int) -> Timed:
    """The ``nested_repos`` walk (``op_nested_scan``) alone, in this process, with no limit: the
    time one 60 s-deadline walk needs for this tree. ``out`` holds the entry count."""
    import resource

    from wtctl import fsafe_child

    t = time.monotonic()
    out = fsafe_child.op_nested_scan(
        {"anchor": {"path": str(root), "identity": None}, "limit": 50_000_000}
    )
    wall = time.monotonic() - t
    count = int(out["count"])  # pyright: ignore[reportArgumentType]
    assert count >= min_entries, (count, min_entries)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return Timed(["op_nested_scan"], 0, wall, rss, str(out["count"]), "")


def _env_walk(root: Path, expect_matches: int) -> Timed:
    """The ``copy_env`` source walk (``env_match_op``) alone, in this process, with no entry
    limit, so its rate is measured past ``ENV_WALK_LIMIT``."""
    import resource

    from wtctl import bootstrapper

    comps = bootstrapper._valid_pattern("**/.env.local")  # pyright: ignore[reportPrivateUsage]
    assert comps is not None
    t = time.monotonic()
    out = bootstrapper.env_match_op(
        {"anchor": {"path": str(root)}, "plans": [comps], "limit": 50_000_000}
    )
    wall = time.monotonic() - t
    assert not any("stopped early" in x for x in out["warnings"]), out["warnings"]
    assert len(out["matches"]) == expect_matches, (len(out["matches"]), expect_matches)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return Timed(["env_match_op"], 0, wall, rss, json.dumps(out["warnings"]), "")


def _plain(wall: float) -> Timed:
    return Timed(["git"], 0, wall, 0, "", "")


def _throwaways(w: World, res: Results, repeats: int) -> None:
    ref = w.git("rev-parse", "refs/remotes/origin/main")
    want = w.base / "extract-list"
    want.write_text("\0".join(f"assets/blob{k}.bin" for k in range(w.dims.large_blobs)))
    dest = w.base / "extracted"
    dest.mkdir()
    for source in ("remote", "local"):
        for _ in range(repeats):
            c = ok(
                w.run("create", "--throwaway", "--repo", REPO, "--ref", ref, "--source", source)
                if source == "local"
                else w.run(
                    "create",
                    "--throwaway",
                    "--repo",
                    REPO,
                    "--ref",
                    ref,
                    "--source",
                    source,
                    "--depth",
                    "50",
                )
            )
            label = "remote, depth 50" if source == "remote" else "local source (full history)"
            res.add(f"create --throwaway, {label}", c)
            res.notes[f"create --throwaway, {label}"] = breakdown(c)
            rid = record_of(c)["id"]
            if source == "remote":
                res.add(
                    "extract 3 x 16 MiB files from a throwaway",
                    ok(w.run("extract", rid, "--files-from", str(want), "--dest", str(dest))),
                )
                shutil.rmtree(dest)
                dest.mkdir()
            res.add(f"remove throwaway ({label})", ok(w.run("remove", rid, "--yes")))


def _small_world(base: Path) -> World:
    sw = World(base / "small", fx.Dims(2, 2, 2, 5, 60, 2, 5, 3, 1, 1024))
    sw.build(stale_at=60, copy_env_files=0)
    sw.top["lease_wait"] = "30s"
    return sw


def _many_records(base: Path, res: Results) -> None:
    """``list``/``prune``/``reconcile`` over many throwaway records."""
    sw = _small_world(base)
    try:
        _many_body(sw, res)
    finally:
        shutil.rmtree(sw.base, ignore_errors=True)


def _many_body(sw: World, res: Results) -> None:
    sha = sw.git("rev-parse", "HEAD")
    many = int(os.environ.get("BENCH_RECORDS", "300"))
    for _ in range(many):
        ok(sw.run("create", "--throwaway", "--repo", REPO, "--ref", sha, "--source", "local"))
    r = res.add(f"list ({many} throwaway records)", ok(sw.run("list")))
    assert len(r.json()["records"]) == many
    res.add(f"list --all ({many} records)", ok(sw.run("list", "--all")))
    run = res.add(f"reconcile ({many} live throwaways)", ok(sw.run("reconcile")))
    res.notes[f"reconcile ({many} live throwaways)"] = outcome(run)
    time.sleep(2)
    res.add(
        f"prune --throwaways ({many}, serial)",
        ok(sw.run("prune", "--throwaways", "--older-than", "1s")),
    )


def _reconcile_admin(base: Path, res: Results) -> None:
    """The admin-entry deletion decision of ``reconcile`` for a record whose directory vanished,
    in a clone whose ``worktrees/`` directory holds many entries (``op_admin_scan`` reads at most
    5000)."""
    sw = _small_world(base)
    try:
        _reconcile_body(sw, res)
    finally:
        shutil.rmtree(sw.base, ignore_errors=True)


def _reconcile_body(sw: World, res: Results) -> None:
    sw.top["reconcile"] = {"missing_grace": "1s"}
    admin = sw.clone / ".git" / "worktrees"
    have = 0
    for count in (0, 4_000, 6_000):
        for k in range(have, count):
            e = admin / f"fake{k:05d}"
            e.mkdir(parents=True, exist_ok=True)
            (e / "gitdir").write_text(f"/nonexistent/bench/{k}/.git\n")
        have = count
        rec = record_of(ok(sw.run("create", "--repo", REPO, "--no-bootstrap")))
        shutil.rmtree(rec["path"])
        ok(sw.run("reconcile"))  # records it as missing
        time.sleep(2)
        name = f"reconcile, missing record past its grace ({count} other admin entries)"
        run = res.add(name, ok(sw.run("reconcile")))
        res.notes[name] = outcome(run)


def _stress(w: World, res: Results) -> None:
    """Scale the nested scan toward and past ``remove.nested_scan_limit`` (2,000,000 entries)."""
    c = ok(w.run("create", "--repo", REPO, "--no-bootstrap"))
    rec = record_of(c)
    rid, wt = rec["id"], Path(rec["path"])
    entries = 215_000
    for target in (1_000_000, 2_200_000):
        need = target - entries
        per_dir = 1000
        start = entries
        t = time.monotonic()
        for k in range(start, target, per_dir):
            leaf = wt / "node_modules" / f"s{k // per_dir:05d}"
            leaf.mkdir(parents=True, exist_ok=True)
            for f in range(per_dir):
                os.close(os.open(leaf / f"e{f:04d}", os.O_CREAT | os.O_WRONLY))
        entries = target
        print(f"stress populate to {target}: {time.monotonic() - t:.1f}s (+{need})", flush=True)
        label = f"~{target / 1e6:.1f}M entries"
        scan = res.add(f"nested_repos walk in process, {label}", _scan_walk(wt, target - 215_000))
        res.notes[f"nested_repos walk in process, {label}"] = f"{scan.out} entries walked"
        walk = res.add(f"copy_env walk in process, {label}", _env_walk(wt, 0))
        res.notes[f"copy_env walk in process, {label}"] = f"warnings: {walk.out}"
        run = res.add(f"status, {label}", ok(w.run("status", rid)))
        res.notes[f"status, {label}"] = outcome(run)
        run = res.add(f"remove --dry-run, {label}", ok(w.run("remove", rid, "--dry-run"), 0, 20))
        res.notes[f"remove --dry-run, {label}"] = outcome(run)
    res.add(
        "remove --yes --force, ~2.2M entries", ok(w.run("remove", rid, "--yes", "--force"), 0, 1)
    )
