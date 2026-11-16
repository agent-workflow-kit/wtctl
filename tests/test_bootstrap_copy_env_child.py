"""copy_env source walk and reads run in a child process under the walk deadline (#55)."""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.bootlib import Repo
from wtctl import bootstrapper as b
from wtctl import fsafe, fsafe_child

BLOCK = [sys.executable, "-c", "import sys, time; sys.stdin.read(); time.sleep(600)"]


@pytest.fixture
def r(tmp_path: Path, private_home: Path) -> Repo:
    return Repo(tmp_path, private_home)


def copy(r: Repo, patterns: list[str], **kw: Any) -> tuple[list[b.Copied], list[str]]:
    warns: list[str] = []
    with r.open() as wt:
        tracked = b.tracked_paths(r.git, wt)
        got = b.copy_env_files(
            wt, source_root=str(r.main), patterns=patterns, tracked=tracked, warn=warns.append, **kw
        )
    return got, warns


class Spy:
    """Wraps ``fsafe_child.run_op``: records the ops and pids, and can stall chosen ops."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, stall: set[int] | None = None) -> None:
        self.ops: list[str] = []
        self.pids: list[int] = []
        self.stall = stall or set()  # 0-based indexes of the calls to replace by a stalled worker
        real = fsafe_child.run_op

        def run_op(op: str, args: dict[str, object], **kw: Any) -> Any:
            n = len(self.ops)
            self.ops.append(op)
            kw["on_start"] = self.pids.append
            if n in self.stall:
                kw["argv"] = BLOCK
                kw["timeout"] = 0.5
            return real(op, args, **kw)

        monkeypatch.setattr(fsafe_child, "run_op", run_op)

    def assert_all_dead(self) -> None:
        assert self.pids
        for pid in self.pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)


def test_the_walk_and_the_reads_run_in_children(r: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch)
    r.write(".env", "A=1\n", root=r.main)
    got, warns = copy(r, [".env"])
    assert [c.path for c in got] == [".env"] and warns == []
    assert spy.ops == ["env_match", "env_read"]
    spy.assert_all_dead()  # each child has exited


def test_a_hung_walk_is_killed_by_pid_and_nothing_is_copied(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = Spy(monkeypatch, stall={0})
    r.write(".env", "A=1\n", root=r.main)
    got, warns = copy(r, [".env"])
    assert got == [] and not (r.wt / ".env").exists()
    assert spy.ops == ["env_match"]
    assert any("deadline" in w and "nothing copied" in w for w in warns)
    spy.assert_all_dead()


def test_hung_reads_are_killed_and_nothing_is_copied(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = Spy(monkeypatch, stall={1})
    r.write(".env", "A=1\n", root=r.main)
    r.write(".env.two", "B=2\n", root=r.main)
    got, warns = copy(r, [".env*"])
    assert got == [] and not (r.wt / ".env").exists() and not (r.wt / ".env.two").exists()
    assert any("deadline" in w for w in warns)
    spy.assert_all_dead()


def test_a_later_hung_batch_keeps_the_verified_earlier_files_only(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "ENV_BATCH_BYTES", 4)  # one file per batch
    spy = Spy(monkeypatch, stall={2})  # match, read of .env.a, read of .env.b hangs
    for n in "abc":
        r.write(f".env.{n}", f"{n}=1\n", root=r.main)
    got, warns = copy(r, [".env.*"])
    assert [c.path for c in got] == [".env.a"]
    assert (r.wt / ".env.a").exists() and not (r.wt / ".env.b").exists()
    assert not (r.wt / ".env.c").exists()
    assert spy.ops == ["env_match", "env_read", "env_read"]  # no third read after the deadline
    assert any("deadline" in w for w in warns)
    spy.assert_all_dead()


def test_many_files_over_the_batch_size_are_all_copied(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "ENV_BATCH_BYTES", 10)
    spy = Spy(monkeypatch)
    for i in range(7):
        r.write(f".env.{i}", f"value-{i}\n", root=r.main)
    got, warns = copy(r, [".env.*"])
    assert sorted(c.path for c in got) == [f".env.{i}" for i in range(7)] and warns == []
    assert spy.ops.count("env_read") > 1
    assert (r.wt / ".env.3").read_text() == "value-3\n"


def test_an_interrupt_kills_the_child_and_copies_nothing(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids: list[int] = []
    real = fsafe_child.run_op

    def run_op(op: str, args: dict[str, object], **kw: Any) -> Any:
        def boom(pid: int) -> None:
            pids.append(pid)
            raise KeyboardInterrupt

        return real(op, args, **{**kw, "on_start": boom})

    monkeypatch.setattr(fsafe_child, "run_op", run_op)
    r.write(".env", "A=1\n", root=r.main)
    with pytest.raises(KeyboardInterrupt):
        copy(r, [".env"])
    assert not (r.wt / ".env").exists()
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_the_lease_is_renewed_before_each_child(r: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch)
    ticks: list[int] = []
    r.write(".env", "A=1\n", root=r.main)
    got, _ = copy(r, [".env"], on_tick=lambda: ticks.append(len(spy.ops)))
    assert [c.path for c in got] == [".env"]
    assert ticks == [0, 1]  # before the walk and before the read


def test_a_lost_lease_stops_the_copy(r: Repo) -> None:
    r.write(".env", "A=1\n", root=r.main)
    got, warns = copy(r, [".env"], on_tick=lambda: True)
    assert got == [] and not (r.wt / ".env").exists()
    assert any("lease" in w for w in warns)


def test_the_hostile_name_round_trips_through_the_child(r: Repo) -> None:
    name = "e\nvé ‮.env"
    r.write(name, "x", root=r.main)
    got, warns = copy(r, ["e*"])
    assert [c.path for c in got] == [name] and warns == []
    assert (r.wt / name).read_text() == "x"


def test_a_name_that_is_not_utf8_never_reaches_the_read(r: Repo) -> None:
    bad = os.fsencode(str(r.main)) + b"/.env-\xff"
    with open(bad, "wb") as f:
        f.write(b"x")
    got, warns = copy(r, [".env-*"])
    assert got == [] and any("UTF-8" in w for w in warns)


# -- the operations themselves ----------------------------------------------------------------


def anchor_args(p: Path) -> dict[str, Any]:
    return {"anchor": {"path": str(p), "identity": None}, "common_id": None}


def test_env_match_op_reports_the_root_identity_and_sorted_matches(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / ".env").write_text("1")
    (tmp_path / ".env").write_text("2")
    st = os.stat(tmp_path)
    res = fsafe_child.run_op(
        "env_match", {**anchor_args(tmp_path), "plans": [[".env"], ["**", ".env"]], "limit": 1000}
    )
    assert res.ok and res.value is not None
    assert res.value["identity"] == [st.st_dev, st.st_ino]
    assert res.value["matches"] == [[".env"], ["a", ".env"]]
    assert res.value["refuse"] is None


def test_env_match_op_copies_nothing_when_the_walk_stops_at_its_limit(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("1")  # found before the walk runs out of budget
    for i in range(20):
        (tmp_path / f"d{i:02d}").mkdir()
        (tmp_path / f"d{i:02d}" / "f").write_text("x")
    res = fsafe_child.run_op(
        "env_match", {**anchor_args(tmp_path), "plans": [[".env"], ["**", ".env"]], "limit": 10}
    )
    assert res.ok and res.value is not None
    assert res.value["matches"] == []
    assert any("stopped early (limit)" in w for w in res.value["warnings"])


def test_env_walk_limit_covers_a_monorepo_with_installed_dependencies(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Measured in docs/design.md (Large-repository timing): a 200,000 limit stopped a 215,000-entry
    # checkout short, and a 365,000-entry checkout is walked in about a second. The limit the
    # parent hands the walk child must stay above that and match the nested-scan default.
    seen: list[dict[str, object]] = []
    real = fsafe_child.run_op

    def run_op(op: str, args: dict[str, object], **kw: Any) -> Any:
        if op == "env_match":
            seen.append(args)
        return real(op, args, **kw)

    monkeypatch.setattr(fsafe_child, "run_op", run_op)
    r.write(".env", "A=1\n", root=r.main)
    copy(r, [".env"])
    assert len(seen) == 1
    limit = seen[0]["limit"]
    assert limit == b.ENV_WALK_LIMIT
    assert isinstance(limit, int) and limit > 365_000


def test_env_match_op_returns_the_same_prefix_and_warning_with_a_bounded_walk(
    tmp_path: Path,
) -> None:
    for i in range(12):
        (tmp_path / f"f{i:02d}").write_text("x")
    res = fsafe_child.run_op(
        "env_match",
        {**anchor_args(tmp_path), "plans": [["**", "*"]], "limit": 1000, "return_max": 3},
    )
    assert res.ok and res.value is not None
    assert res.value["matches"] == [["f00"], ["f01"], ["f02"]]
    assert [w for w in res.value["warnings"] if "only the first" in w] == [
        "copy_env: only the first 3 matches are considered"
    ]


def test_env_match_op_without_a_cut_has_no_warning(tmp_path: Path) -> None:
    root = tmp_path / "w"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}").write_text("x")
    res = fsafe_child.run_op(
        "env_match", {**anchor_args(root), "plans": [["*"]], "limit": 1000, "return_max": 3}
    )
    assert res.ok and res.value is not None
    assert len(res.value["matches"]) == 3 and res.value["warnings"] == []


def test_bounded_matches_keep_the_smallest_and_stay_small() -> None:
    m = b.BoundedMatches(keep=4)
    peak = 0
    for i in reversed(range(100)):
        m.add((f"f{i:03d}",))
        peak = max(peak, len(m))
    m.update([("a",), ("b",)])
    assert peak <= 2 * 4 + 1
    assert m.cut
    assert sorted(m)[:4] == [("a",), ("b",), ("f000",), ("f001",)]
    m.clear()
    assert len(m) == 0 and not m.cut


def test_env_match_op_refuses_a_foreign_git_identity(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    res = fsafe_child.run_op(
        "env_match",
        {**anchor_args(tmp_path), "plans": [[".env"]], "limit": 10, "common_id": "1:2"},
    )
    assert res.ok and res.value is not None and res.value["refuse"]
    assert res.value["matches"] == []


def test_env_match_op_caps_its_warnings(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("permission bits do not stop root")
    dirs = []
    for i in range(b.ENV_WARN_CAP + 20):
        d = tmp_path / f"d{i}"
        d.mkdir()
        d.chmod(0)
        dirs.append(d)
    try:
        res = fsafe_child.run_op(
            "env_match", {**anchor_args(tmp_path), "plans": [["**", "x"]], "limit": 10_000}
        )
    finally:
        for d in dirs:
            d.chmod(0o700)
    assert res.ok and res.value is not None
    assert len(res.value["warnings"]) == b.ENV_WARN_CAP + 1
    assert "more warnings" in res.value["warnings"][-1]


def test_env_read_op_returns_base64_and_per_file_errors(tmp_path: Path) -> None:
    (tmp_path / "ok").write_bytes(b"\x00\xffdata")
    (tmp_path / "big").write_bytes(b"123456789")
    os.symlink(tmp_path / "ok", tmp_path / "ln")
    st = os.stat(tmp_path)
    res = fsafe_child.run_op(
        "env_read",
        {
            "anchor": {"path": str(tmp_path), "identity": [st.st_dev, st.st_ino]},
            "paths": [["ok"], ["big"], ["ln"], ["gone"]],
            "max_bytes": 8,
            "batch_bytes": 1000,
        },
    )
    assert res.ok and res.value is not None and res.value["next"] == 4
    f = res.value["files"]
    assert base64.b64decode(f[0]["data"]) == b"\x00\xffdata"
    assert f[1]["kind"] == "limit" and f[2]["kind"] == "symlink" and f[3]["kind"] == "missing"


def test_env_read_op_refuses_a_swapped_root(tmp_path: Path) -> None:
    (tmp_path / "ok").write_text("x")
    res = fsafe_child.run_op(
        "env_read",
        {
            "anchor": {"path": str(tmp_path), "identity": [1, 2]},
            "paths": [["ok"]],
            "max_bytes": 8,
            "batch_bytes": 100,
        },
    )
    assert not res.ok and res.error is not None
    assert isinstance(res.error, fsafe.FsafeError)


def test_env_read_op_stops_a_batch_at_the_byte_cap(tmp_path: Path) -> None:
    for n in "abc":
        (tmp_path / n).write_bytes(b"12345")
    st = os.stat(tmp_path)
    res = fsafe_child.run_op(
        "env_read",
        {
            "anchor": {"path": str(tmp_path), "identity": [st.st_dev, st.st_ino]},
            "paths": [["a"], ["b"], ["c"]],
            "max_bytes": 100,
            "batch_bytes": 6,
        },
    )
    assert res.ok and res.value is not None
    assert res.value["next"] == 2 and len(res.value["files"]) == 2  # always progresses


# -- the bounded walk result (R1) -------------------------------------------------------------


def test_env_match_op_returns_only_the_first_sorted_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "ENV_MATCH_RETURN_MAX", 3)
    for i in range(10):
        (tmp_path / f"f{i}").write_text("x")
    val = b.env_match_op({**anchor_args(tmp_path), "plans": [["f*"]], "limit": 1000})
    assert val["matches"] == [["f0"], ["f1"], ["f2"]]  # the prefix of the sorted list
    assert sum("only the first 3 matches" in w for w in val["warnings"]) == 1


def test_env_match_op_stops_at_the_path_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "ENV_MATCH_RETURN_BYTES", 20)
    for i in range(10):
        (tmp_path / f"name{i}").write_text("x")  # 5 characters each
    val = b.env_match_op({**anchor_args(tmp_path), "plans": [["name*"]], "limit": 1000})
    assert len(val["matches"]) == 3 and val["matches"][0] == ["name0"]
    assert sum("only the first 3 matches" in w for w in val["warnings"]) == 1


def test_env_match_op_does_not_warn_at_or_below_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "ENV_MATCH_RETURN_MAX", 3)
    for i in range(3):
        (tmp_path / f"f{i}").write_text("x")
    val = b.env_match_op({**anchor_args(tmp_path), "plans": [["f*"]], "limit": 1000})
    assert len(val["matches"]) == 3 and val["warnings"] == []


def test_the_parent_only_considers_the_capped_prefix(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "ENV_MATCH_RETURN_MAX", 2)
    for i in range(5):
        r.write(f".env.{i}", f"{i}\n", root=r.main)
    got, warns = copy(r, [".env.*"])
    assert [c.path for c in got] == [".env.0", ".env.1"]
    assert sum("only the first 2 matches" in w for w in warns) == 1


# -- the shared budget and the read results (R3, R5) -------------------------------------------


def test_a_spent_read_budget_is_reported_as_such_not_as_a_killed_child(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = fsafe_child.Budget
    monkeypatch.setattr(fsafe_child, "Budget", lambda total, **kw: real(0.0, **kw))
    spy = Spy(monkeypatch)
    r.write(".env", "A=1\n", root=r.main)
    got, warns = copy(r, [".env"])
    assert got == [] and len(spy.pids) == 1  # only the walk started a child
    assert any("time budget" in w and "nothing more copied" in w for w in warns)
    assert not any("was killed" in w for w in warns)


def test_the_reads_share_one_budget(r: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b, "ENV_BATCH_BYTES", 4)  # one file per batch
    pids: list[int] = []
    real = fsafe_child.run_op
    reads = [0]

    def run_op(op: str, args: dict[str, object], **kw: Any) -> Any:
        kw["on_start"] = pids.append
        res = real(op, args, **kw)
        if op == "env_read":
            reads[0] += 1
            budget = kw.get("budget")
            if budget is not None:
                budget.charge(10_000)  # the first batch used up the whole budget
        return res

    monkeypatch.setattr(fsafe_child, "run_op", run_op)
    for n in "abc":
        r.write(f".env.{n}", f"{n}=1\n", root=r.main)
    got, warns = copy(r, [".env.*"])
    assert [c.path for c in got] == [".env.a"]
    assert len(pids) == 2  # the walk and the first read; the second read started no child
    assert any("time budget" in w for w in warns)


def test_the_reads_use_the_root_the_walk_reported(r: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    real = fsafe_child.run_op

    def run_op(op: str, args: dict[str, object], **kw: Any) -> Any:
        res = real(op, args, **kw)
        if op == "env_match":  # swap the root between the walk and the read
            os.rename(r.main, str(r.main) + ".old")
            r.main.mkdir()
            (r.main / ".env").write_text("EVIL=1\n")
        return res

    monkeypatch.setattr(fsafe_child, "run_op", run_op)
    r.write(".env", "A=1\n", root=r.main)
    got, warns = copy(r, [".env"])
    assert got == [] and not (r.wt / ".env").exists()
    assert any("reading the source failed" in w for w in warns)


def fake_reads(monkeypatch: pytest.MonkeyPatch, files: list[dict[str, Any]]) -> None:
    from wtctl import removalfs, repobaseline

    def runner(op: str, args: dict[str, Any], **kw: Any) -> Any:
        if op == "env_read":
            return fsafe_child.OpResult(True, value={"files": files, "next": len(files)})
        return repobaseline.inproc(op, args)

    monkeypatch.setattr(removalfs, "RUNNER", runner)


def test_malformed_and_oversized_read_entries_are_skipped_with_a_warning(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = base64.b64encode(b"x" * (b.ENV_FILE_MAX_BYTES + 1)).decode("ascii")
    good = base64.b64encode(b"C=3\n").decode("ascii")
    fake_reads(monkeypatch, [{"data": "!!not base64!!"}, {"data": big}, {"data": good}])
    for n in "abc":
        r.write(f".env.{n}", "x", root=r.main)
    got, warns = copy(r, [".env.*"])
    assert [c.path for c in got] == [".env.c"]
    assert not (r.wt / ".env.a").exists() and not (r.wt / ".env.b").exists()
    assert (r.wt / ".env.c").read_text() == "C=3\n"
    assert sum("not copied" in w for w in warns) == 2


# -- the parent's per-file warnings are bounded (R6) -------------------------------------------


def skipped_warnings(r: Repo, monkeypatch: pytest.MonkeyPatch, n: int) -> list[str]:
    from wtctl import removalfs

    names = [f"f{i:04d}" for i in range(n)]

    def runner(op: str, args: dict[str, Any], **kw: Any) -> Any:
        return fsafe_child.OpResult(
            True,
            value={
                "identity": [1, 2],
                "matches": [[x] for x in names],
                "warnings": [],
                "refuse": None,
            },
        )

    monkeypatch.setattr(removalfs, "RUNNER", runner)
    warns: list[str] = []
    with r.open() as wt:
        got = b.copy_env_files(
            wt, source_root=str(r.main), patterns=["f*"], tracked=set(names), warn=warns.append
        )
    assert got == []
    return warns


def test_per_file_skip_warnings_are_capped(r: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    warns = skipped_warnings(r, monkeypatch, b.ENV_WARN_CAP + 30)
    assert len(warns) == b.ENV_WARN_CAP + 1
    assert warns[-1] == "copy_env: 30 more warnings were suppressed"


def test_count_cap_warning_survives_the_suppression_cap(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import removalfs

    names = [f"bad\0{i}" for i in range(b.ENV_WARN_CAP + 10)] + ["ok"]

    def runner(op: str, args: dict[str, Any], **kw: Any) -> Any:
        return fsafe_child.OpResult(
            True,
            value={
                "identity": [1, 2],
                "matches": [[x] for x in names],
                "warnings": [],
                "refuse": None,
            },
        )

    monkeypatch.setattr(removalfs, "RUNNER", runner)
    monkeypatch.setattr(b, "ENV_FILE_MAX_COUNT", 0)
    warns: list[str] = []
    with r.open() as wt:
        b.copy_env_files(
            wt, source_root=str(r.main), patterns=["*"], tracked=set(), warn=warns.append
        )
    assert "copy_env: more than 0 matches; the rest are skipped" in warns
    assert warns[-1] == "copy_env: 10 more warnings were suppressed"


def test_per_file_skip_warnings_below_the_cap_are_all_kept(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    warns = skipped_warnings(r, monkeypatch, b.ENV_WARN_CAP)
    assert len(warns) == b.ENV_WARN_CAP and not any("suppressed" in w for w in warns)
