"""`list` and `show` against a seeded state.db: goldens, filters, addressing, escaping."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.listhelpers import SHA, cli, seed
from tests.schemacheck import validate
from tests.statehelpers import FakeClock, make_state
from wtctl.state import State

A, B, C, D = "wt-aaaaaaaa", "wt-aaaabbbb", "wt-cccccccc", "wt-dddddddd"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def st(tmp_path: Path, clock: FakeClock) -> State:
    return make_state(tmp_path, clock=clock)


def ids(r) -> list[str]:  # noqa: ANN001
    return [x["id"] for x in r.json()["records"]]


# --- show -----------------------------------------------------------------------------------


def test_show_golden_worktree_validates_against_the_schema(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, labels=["team=x", "a=b", "a=a"], holders=["h2", "h1"])
    r = cli(capsys, st, "--json", "show", A)
    assert r.code == 0
    obj = r.json()
    validate("wtctl/cli/show.json", obj)
    validate("wtctl/cli/record.json", obj)
    assert obj["labels"] == ["a=a", "a=b", "team=x"]  # sorted key=value strings
    assert obj["holders"] == ["h1", "h2"]
    assert obj["branch"] == "b-aaaaaaaa"  # what ticket-work relies on
    assert obj["bootstrap"] == {"state": "none", "steps": [], "created_paths_count": 0}
    assert "bootstrap_state" not in obj


def test_show_golden_throwaway(capsys, st) -> None:  # noqa: ANN001
    seed(st, C, object_type="throwaway")
    obj = cli(capsys, st, "--json", "show", C).json()
    validate("wtctl/cli/show.json", obj)
    assert obj["bootstrap"] is None
    assert obj["holders"] == []


def test_show_reports_bootstrap_steps_of_the_last_run(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, bootstrap_state="failed")
    with st._txn() as c:
        c.execute(
            "INSERT INTO bootstrap_runs VALUES (?,?,?,?,?,?,?,?)",
            (A, 1, 1, '["old"]', 0, 5, "", "t"),
        )
        c.execute(
            "INSERT INTO bootstrap_runs VALUES (?,?,?,?,?,?,?,?)",
            (A, 2, 1, '["make","deps"]', 0, 7, "ok", "t"),
        )
        c.execute(
            "INSERT INTO bootstrap_runs VALUES (?,?,?,?,?,?,?,?)",
            (A, 2, 2, '["make","test"]', None, None, None, "t"),
        )
        c.execute("INSERT INTO bootstrap_paths VALUES (?,?,?,?)", (A, "node_modules", "dir", None))
    obj = cli(capsys, st, "--json", "show", A).json()
    validate("wtctl/cli/show.json", obj)
    boot = obj["bootstrap"]
    assert boot["state"] == "failed"
    assert boot["created_paths_count"] == 1
    assert [s["argv"] for s in boot["steps"]] == [["make", "deps"], ["make", "test"]]
    assert boot["steps"][1] == {
        "argv": ["make", "test"],
        "exit": None,
        "duration_ms": 0,
        "output_tail": "",
    }


def test_show_by_prefix_with_and_without_wt(capsys, st) -> None:  # noqa: ANN001
    seed(st, C)
    assert cli(capsys, st, "--json", "show", "cccc").json()["id"] == C
    assert cli(capsys, st, "--json", "show", "wt-ccccc").json()["id"] == C


def test_show_by_absolute_path_resolves_symlinks(capsys, st, tmp_path: Path) -> None:  # noqa: ANN001
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    seed(st, A, path=str(real))
    assert cli(capsys, st, "--json", "show", str(link)).json()["id"] == A
    r = cli(capsys, st, "--json", "show", str(tmp_path / "nothing"))
    assert r.code == 10


def test_show_prefix_too_short_is_usage_and_relative_path_is_not_a_path(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    assert cli(capsys, st, "--json", "show", "aaa").code == 2
    assert cli(capsys, st, "--json", "show", "wt-aa").code == 2
    assert cli(capsys, st, "--json", "show", "some/where").code == 2
    assert cli(capsys, st, "--json", "show", "zzzzzz").code == 10  # cannot be an id


def test_show_ambiguous_prefix_exits_21_with_candidates(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    seed(st, B)
    r = cli(capsys, st, "--json", "show", "aaaa")
    assert r.code == 21
    err = r.json()["error"]
    validate("wtctl/cli/error.json", r.json())
    assert sorted(c["id"] for c in err["candidates"]) == [A, B]
    assert cli(capsys, st, "--json", "show", "aaaaa").json()["id"] == A  # longer prefix is unique


def test_show_tombstone_only_by_full_id(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, state="removed", path="/gone")
    full = cli(capsys, st, "--json", "show", A)
    assert full.code == 0
    obj = full.json()
    validate("wtctl/cli/show.json", obj)
    assert obj["state"] == "removed"
    assert obj["removed_at"] is not None
    for sel in ("aaaa", "wt-aaaaaaa", "/gone"):
        r = cli(capsys, st, "--json", "show", sel)
        assert r.code == 10, sel


def test_show_unknown_full_id_exits_10(capsys, st) -> None:  # noqa: ANN001
    r = cli(capsys, st, "--json", "show", "wt-aaaaaaaa")
    assert r.code == 10
    validate("wtctl/cli/error.json", r.json())


def test_show_runs_no_git(capsys, st, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    import subprocess

    seed(st, A)

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("show ran a subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert cli(capsys, st, "show", A).code == 0


def test_show_human_escapes_untrusted_fields(capsys, st) -> None:  # noqa: ANN001
    seed(
        st,
        A,
        path="/w/evil\nfake: line\x1b[31m",
        branch="b‮evil\r\nid: wt-zzzzzzzz",
        last_error="boom\x00\nstate: active",
    )
    r = cli(capsys, st, "show", A)
    assert r.code == 0
    assert "\x1b" not in r.out and "‮" not in r.out and "\r" not in r.out
    assert "\x00" not in r.out
    lines = r.out.splitlines()
    # one line per field: an embedded newline must not forge another field
    assert not any(line.startswith(("fake:", "id: wt-z")) for line in lines)
    assert sum(1 for line in lines if line.startswith("state:")) == 1
    assert "\\x0a" in r.out and "\\u202e" in r.out and "\\x1b" in r.out


def test_show_human_prints_failed_output_tail_escaped(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, bootstrap_state="failed")
    with st._txn() as c:
        c.execute(
            "INSERT INTO bootstrap_runs VALUES (?,?,?,?,?,?,?,?)",
            (A, 1, 1, '["x"]', 1, 3, "bad\n\x1b[2Jtail", "t"),
        )
    r = cli(capsys, st, "show", A)
    assert "\x1b" not in r.out
    assert "bad\\x0a\\x1b[2Jtail" in r.out


# --- list -----------------------------------------------------------------------------------


def test_list_golden_validates_and_is_newest_first(capsys, st, clock) -> None:  # noqa: ANN001
    seed(st, A)
    clock.advance(60)
    seed(st, C, object_type="throwaway")
    clock.advance(60)
    seed(st, D, labels=["k=v"])
    r = cli(capsys, st, "--json", "list")
    assert r.code == 0
    validate("wtctl/cli/list.json", r.json())
    assert ids(r) == [D, C, A]
    assert r.json()["foreign"] == []


def test_list_empty_is_valid(capsys, st) -> None:  # noqa: ANN001
    r = cli(capsys, st, "--json", "list")
    assert r.json() == {"records": [], "foreign": []}
    validate("wtctl/cli/list.json", r.json())
    assert cli(capsys, st, "list").out == ""


def test_list_excludes_tombstones_unless_state_names_them(capsys, st, clock) -> None:  # noqa: ANN001
    seed(st, A)
    clock.advance(1)
    seed(st, B, state="removed", path="/x")
    clock.advance(1)
    seed(st, C, state="missing", path="/y")
    assert ids(cli(capsys, st, "--json", "list")) == [C, A]
    assert ids(cli(capsys, st, "--json", "list", "--state", "removed")) == [B]
    assert ids(cli(capsys, st, "--json", "list", "--state", "removed", "--state", "active")) == [
        B,
        A,
    ]
    r = cli(capsys, st, "--json", "list", "--state", "removed")
    validate("wtctl/cli/list.json", r.json())


def test_list_unknown_state_is_usage(capsys, st) -> None:  # noqa: ANN001
    r = cli(capsys, st, "--json", "list", "--state", "bogus")
    assert r.code == 2
    assert r.json()["error"]["code"] == "usage"


def test_list_filters_and_together(capsys, st, clock) -> None:  # noqa: ANN001
    seed(st, A, labels=["env=ci", "team=x"])
    clock.advance(1)
    seed(st, B, repo="example.com/o/other", labels=["env=ci"], path="/p2")
    clock.advance(1)
    seed(st, C, object_type="throwaway", labels=["env=ci", "team=x"], path="/p3")
    assert ids(cli(capsys, st, "--json", "list", "--label", "env=ci")) == [C, B, A]
    assert ids(cli(capsys, st, "--json", "list", "--label", "env=ci", "--label", "team=x")) == [
        C,
        A,
    ]
    assert ids(cli(capsys, st, "--json", "list", "--label", "team=y")) == []
    assert ids(cli(capsys, st, "--json", "list", "--repo", "example.com/o/other")) == [B]
    assert ids(cli(capsys, st, "--json", "list", "--throwaways")) == [C]
    assert ids(cli(capsys, st, "--json", "list", "--worktrees", "--label", "team=x")) == [A]
    assert ids(
        cli(
            capsys,
            st,
            "--json",
            "list",
            "--repo",
            "example.com/o/r",
            "--label",
            "env=ci",
            "--worktrees",
        )
    ) == [A]


def test_list_label_filter_is_exact_not_a_pattern(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, labels=["k=value", "k=vx", "k=v."])
    seed(st, B, path="/b", labels=["k=", "k=v_"])
    assert ids(cli(capsys, st, "--json", "list", "--label", "k=v")) == []
    assert ids(cli(capsys, st, "--json", "list", "--label", "k=v_")) == [B]  # `_` is not a wildcard
    assert ids(cli(capsys, st, "--json", "list", "--label", "k=v.")) == [A]
    assert ids(cli(capsys, st, "--json", "list", "--label", "k=")) == [B]
    assert ids(cli(capsys, st, "--json", "list", "--label", "k=value", "--label", "k=vx")) == [A]


@pytest.mark.parametrize("bad", ["novalue", "K=v", "k=v w", "=v", "k=v\n", "k=é"])
def test_list_bad_label_selector_is_usage(capsys, st, bad: str) -> None:  # noqa: ANN001
    assert cli(capsys, st, "--json", "list", "--label", bad).code == 2


@pytest.mark.parametrize("bad", ["", "UPPER/x", "no_slash", "a/..", "a/b c", "a/b\n"])
def test_list_bad_repo_is_usage(capsys, st, bad: str) -> None:  # noqa: ANN001
    assert cli(capsys, st, "--json", "list", "--repo", bad).code == 2


def test_list_older_than_compares_created(capsys, st, clock) -> None:  # noqa: ANN001
    seed(st, A)
    clock.advance(3 * 3600)
    seed(st, B, path="/b")
    clock.advance(60)
    assert ids(cli(capsys, st, "--json", "list", "--older-than", "1h")) == [A]
    assert ids(cli(capsys, st, "--json", "list", "--older-than", "1d")) == []
    assert ids(cli(capsys, st, "--json", "list", "--older-than", "30s")) == [B, A]
    # exactly the age: not strictly older
    assert ids(cli(capsys, st, "--json", "list", "--older-than", "180m")) == [A]


@pytest.mark.parametrize(
    "huge",
    ["999999d", "400000d", "999999999w", "99999999999d", "9" * 30 + "d", "9" * 5000 + "w"],
)
def test_list_older_than_huge_duration_matches_nothing(capsys, st, huge: str) -> None:  # noqa: ANN001
    seed(st, A)
    r = cli(capsys, st, "--json", "list", "--older-than", huge)
    assert r.code == 0
    assert ids(r) == []


@pytest.mark.parametrize("bad", ["0s", "1", "h", "-1h", "1.5h", "1y", "1h\n", " 1h"])
def test_list_bad_older_than_is_usage(capsys, st, bad: str) -> None:  # noqa: ANN001
    assert cli(capsys, st, "--json", "list", "--older-than", bad).code == 2


def test_list_all_adds_an_empty_foreign_list_for_now(capsys, st) -> None:  # noqa: ANN001
    seed(st, A)
    r = cli(capsys, st, "--json", "list", "--all")
    assert r.code == 0
    validate("wtctl/cli/list.json", r.json())
    assert r.json()["foreign"] == []


def test_list_throwaways_and_worktrees_are_exclusive(capsys, st) -> None:  # noqa: ANN001
    assert cli(capsys, st, "list", "--throwaways", "--worktrees").code == 2


def test_list_human_table(capsys, st, clock) -> None:  # noqa: ANN001
    seed(st, A, name="alpha", branch="feat/x")
    clock.advance(2 * 86400)
    seed(st, C, object_type="throwaway", path="/t1")
    clock.advance(90)
    r = cli(capsys, st, "list")
    lines = r.out.splitlines()
    assert lines[0].split() == ["ID", "TYPE", "STATE", "REPO", "NAME/REF", "BRANCH", "AGE"]
    assert lines[1].split() == [C, "throwaway", "active", "example.com/o/r", SHA[:12], "-", "1m"]
    assert lines[2].split() == [A, "worktree", "active", "example.com/o/r", "alpha", "feat/x", "2d"]


def test_list_human_escapes_untrusted_cells(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, name="n", branch="b\ninjected\x1b[0m‮")
    seed(st, B, path="/b", labels=["k=v"])
    r = cli(capsys, st, "list")
    assert len(r.out.splitlines()) == 3  # header + two rows
    assert "\x1b" not in r.out and "‮" not in r.out
    assert "b\\x0ainjected\\x1b[0m\\u202e" in r.out


def test_list_labels_are_in_json_but_not_a_table_column(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, labels=["z=1", "a=2"])
    r = cli(capsys, st, "--json", "list")
    assert r.json()["records"][0]["labels"] == ["a=2", "z=1"]


def test_list_dry_run_is_rejected(capsys, st) -> None:  # noqa: ANN001
    assert cli(capsys, st, "--dry-run", "list").code == 2


def test_show_human_renders_a_null_step_exit_as_killed(capsys, st) -> None:  # noqa: ANN001
    seed(st, A, bootstrap_state="failed")
    with st._txn() as c:
        c.execute(
            "INSERT INTO bootstrap_runs VALUES (?,?,?,?,?,?,?,?)",
            (A, 1, 1, '["make","test"]', None, None, None, "t"),
        )
    out = cli(capsys, st, "show", A).out
    assert "exit=killed" in out
    assert "exit=None" not in out
