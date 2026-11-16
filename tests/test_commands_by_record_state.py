"""docs/interface.md "Commands by record state", as a parametrized test.

Three kinds of record (an owned worktree, an adopted worktree, a throwaway) are put in each of the
nine record states and every state-addressed command is run against each. The expected exit code,
error code, reason and ``--json`` shape (validated against ``schemas/``) come from ``exp`` below,
which is written from the table.

Keeping the table and this module in step: ``DOC_CELLS`` repeats every cell of the markdown table
verbatim (links and backticks stripped). ``test_the_doc_table_is_the_one_tested`` parses the table
out of interface.md and fails on any difference in rows, columns or cell text, so editing the
table without revisiting ``exp`` is a red test; ``test_every_cell_has_an_expectation`` fails when a
state/column of the table has no expectation here. ``test_every_command_is_covered`` fails when a
command is added to the registry without being classified below.

States are seeded in ``state.db`` (the way the other state tests do) on top of a real worktree,
adopted worktree or throwaway, because only the record's state matters to these commands; the
paths that reconcile and the crash-recovery rows depend on use the real flows.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.adoptlib import foreign, tombstone
from tests.createlib import REPO
from tests.extractlib import XW
from tests.helpers import Run
from tests.reclib import RC
from tests.schemacheck import validate
from wtctl import registry
from wtctl.state import State, record_lease

DOC = Path(__file__).resolve().parent.parent / "docs" / "interface.md"

KINDS = ("worktree", "adopted", "throwaway")
LIVE = ("creating", "active", "partial", "missing", "unlinked", "clone_missing")
TOMBSTONES = ("gone", "removed", "forgotten")
STATES = LIVE + TOMBSTONES


@pytest.fixture(autouse=True)
def _inproc(inproc: None) -> None:
    """Baseline and removal-check children run in this process (they have their own tests)."""


# -- the table ----------------------------------------------------------------------------------

COLUMNS = (
    "show",
    "status",
    "remove",
    "bootstrap",
    "extract",
    "baseline check / accept",
    "adopt (its path)",
    "forget",
    "label",
)

# Every cell of the table in interface.md, text as rendered (no links, no backticks).
DOC_CELLS: dict[str, tuple[str, ...]] = {
    "creating": (
        "0",
        "0, reason: unlinked",
        "20 unlinked",
        "20 unlinked",
        "20 unlinked",
        "20 unlinked",
        "20 path_exists with holder_id",
        "0",
        "0",
    ),
    "active": (
        "0",
        "0",
        "Remove order, Throwaway removal",
        "Re-run (owned only; adopted: 20 not_owned)",
        "Extraction rules",
        "baseline check, baseline accept",
        "0, existing: true",
        "forget",
        "0",
    ),
    "unlinked, missing, clone_missing": (
        "0",
        "0, reason: unlinked (checks that run git skipped)",
        "20 unlinked",
        "20 unlinked",
        "(never a throwaway state)",
        "20 unlinked (clone_missing: baseline accept re-attaches a moved clone)",
        "20 unlinked (missing: 10, the path is not a git worktree)",
        "0",
        "0",
    ),
    "partial": (
        "0",
        "0 (worktree: as Partial records, human_required: true)",
        "Partial records (worktree, human-only); Throwaway removal (throwaway)",
        "20 unlinked",
        "20 unlinked",
        "20 unlinked",
        "20 unlinked",
        "0",
        "0",
    ),
    "gone, removed, forgotten": (
        "0 (full id)",
        "10",
        "10",
        "10",
        "10",
        "10",
        "0, a new record",
        "10",
        "10",
    ),
}

# The commands (and actor variants) that answer each column; ``exp`` has a value for each.
COLUMN_CMDS: dict[str, tuple[str, ...]] = {
    "show": ("show",),
    "status": ("status", "remove_dry"),
    "remove": ("remove",),
    "bootstrap": ("bootstrap",),
    "extract": ("extract",),
    "baseline check / accept": ("baseline_check", "baseline_accept"),
    "adopt (its path)": ("adopt",),
    "forget": ("forget",),
    "label": ("label_add", "label_remove"),
}

# Registry commands and where each is tested. A command with no record-state dependence says why.
COVERED = {
    "show": "per-state matrix",
    "status": "per-state matrix",
    "remove": "per-state matrix; adopted main worktree; vanished throwaway",
    "bootstrap": "per-state matrix; actor gate",
    "extract": "per-state matrix; vanished throwaway",
    "baseline_check": "per-state matrix",
    "baseline_accept": "per-state matrix; actor gate",
    "adopt": "per-state matrix",
    "forget": "per-state matrix; actor gate",
    "label_add": "per-state matrix",
    "label_remove": "per-state matrix",
    "list": "test_list_by_state",
    "prune": "test_prune_by_state",
    "reconcile": "test_reconcile_by_state",
    "create": "test_create_reuse_by_state",
}
NO_STATE_DEPENDENCE = {
    "locate": "resolves a repo id to its clone; reads no record",
    "clone": "clones a repo id; reads no record",
    "export": "prints audit.jsonl or log.jsonl; reads no record",
    "cron_install": "writes crontab entries; reads no record",
    "cron_uninstall": "removes crontab entries; reads no record",
}


def _plain(cell: str) -> str:
    cell = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell).replace("`", "")
    return " ".join(cell.split())


def doc_table() -> dict[str, tuple[str, ...]]:
    text = DOC.read_text()
    section = text.split("\n### Commands by record state\n", 1)[1].split("\n### ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("|")]
    cells = [[_plain(c) for c in ln.strip().strip("|").split("|")] for ln in rows]
    assert cells[0][0] == "State" and set(cells[1][0]) <= set("-"), cells[:2]
    assert tuple(cells[0][1:]) == COLUMNS, cells[0]
    return {r[0]: tuple(r[1:]) for r in cells[2:]}


def doc_states() -> list[str]:
    return [s for label in doc_table() for s in label.split(", ")]


def test_the_doc_table_is_the_one_tested() -> None:
    parsed = doc_table()
    assert list(parsed) == list(DOC_CELLS)
    for label, cells in DOC_CELLS.items():
        assert dict(zip(COLUMNS, parsed[label], strict=True)) == dict(
            zip(COLUMNS, cells, strict=True)
        ), f"interface.md row {label!r} changed: revisit exp() and DOC_CELLS"


def test_every_cell_has_an_expectation() -> None:
    assert sorted(doc_states()) == sorted(STATES)
    for state in doc_states():
        for column in COLUMNS:
            for cmd in COLUMN_CMDS[column]:
                for kind in KINDS:
                    for actor in actors(cmd):
                        assert exp(cmd, actor, kind, state), (cmd, actor, kind, state)


def test_every_command_is_covered() -> None:
    keys = {c.key for c in registry.COMMANDS}
    assert set(COVERED).isdisjoint(NO_STATE_DEPENDENCE)
    assert keys == set(COVERED) | set(NO_STATE_DEPENDENCE), keys ^ (
        set(COVERED) | set(NO_STATE_DEPENDENCE)
    )
    tested = {c for cmds in COLUMN_CMDS.values() for c in cmds}
    assert {c for c in COVERED if "per-state matrix" in COVERED[c]} <= tested | {"remove"}


# -- expectations -------------------------------------------------------------------------------

HUMAN_GATED = ("bootstrap", "baseline_accept", "forget")


def actors(cmd: str) -> tuple[str, ...]:
    """Actor variants a command is run with: the human-gated ones with both, `remove` with both."""
    return ("human", "auto") if cmd in (*HUMAN_GATED, "remove") else ("auto",)


def exp(cmd: str, actor: str, kind: str, state: str) -> str:
    """The documented outcome: ``ok``, or ``<exit>`` / ``<exit>:<reason or marker>``.

    ``10:tomb`` is exit 10 with ``details: {state, removed_at}``.
    """
    worktree = kind != "throwaway"
    human = actor == "human"
    if cmd in HUMAN_GATED and not human:
        return "13"  # actor gate: before record resolution, whatever the target
    if cmd == "show":
        return "ok"  # a tombstone only by its full id (the tests use the full id)
    if state in TOMBSTONES:
        if cmd == "adopt":  # a path never matches a tombstone; a throwaway root is refused first
            return "2" if not worktree else "ok"
        return "10:tomb"
    if cmd in ("label_add", "label_remove", "status", "remove_dry"):
        return "ok"
    if cmd == "remove":
        if state != "active" and state != "partial":
            return "20:unlinked"  # the state check comes before the human gate
        if worktree and (kind == "adopted" or state == "partial"):
            return "ok" if human else "13"
        return "ok"
    if cmd == "extract":
        if worktree:
            return "2"
        return "ok" if state == "active" else "20:unlinked"
    if cmd == "forget":
        return "ok" if worktree else "2"
    if not worktree:
        return "2"  # bootstrap, baseline check/accept, adopt refuse throwaways
    if cmd == "adopt":
        return {"creating": "20:path_exists", "active": "ok", "missing": "10"}.get(
            state, "20:unlinked"
        )
    if state != "active":
        return "20:unlinked"  # bootstrap, baseline check, baseline accept
    if cmd == "bootstrap" and kind == "adopted":
        return "20:not_owned"
    return "ok"


# -- the world ----------------------------------------------------------------------------------

SCHEMAS = {
    "show": "show",
    "status": "status",
    "remove": "remove",
    "remove_dry": "remove",
    "bootstrap": "bootstrap",
    "extract": "extract",
    "baseline_check": "baseline-check",
    "baseline_accept": "baseline-accept",
    "adopt": "adopt",
    "forget": "forget",
    "label_add": "label-add",
    "label_remove": "label-remove",
}
ERROR_CODES = {
    "2": "usage",
    "10": "not_found",
    "12": "busy",
    "13": "confirmation_required",
    "20": "refused",
}
COMMAND_ARGV = tuple(SCHEMAS)


class SW(XW):
    """One record of the kind under test, in the state under test."""

    rec: dict[str, Any]
    dest: Path
    empty_list: str

    def build(self) -> SW:
        super().build()
        return self

    def seed(self, kind: str, state: str) -> SW:
        self.build()
        self.rec = self.make(kind)
        self.dest = self.plain_dest()
        self.empty_list = self.listfile([])
        self.put_in(self.rec["id"], state, self.rec["path"])
        return self

    def make(self, kind: str, name: str = "work") -> dict[str, Any]:
        if kind == "worktree":
            r = self.run("create", "--repo", REPO, "--branch", name, "--no-bootstrap")
            assert r.code == 0, (r.out, r.err)
            return r.json()["record"]
        if kind == "adopted":
            r = self.run("adopt", str(foreign(self, name)), "--repo", REPO)
            assert r.code == 0, (r.out, r.err)
            return r.json()["record"]
        return self.made("--source", "local")

    def put_in(self, rid: str, state: str, path: str) -> None:
        if state in TOMBSTONES:
            tombstone(self, rid, state)
        else:
            self.set_state(rid, state)
            if state == "missing":
                shutil.rmtree(path)

    def argv(self, cmd: str) -> list[str]:
        rid, path = self.rec["id"], self.rec["path"]
        return {
            "show": ["show", rid],
            "status": ["status", rid],
            "remove": ["remove", rid, "--yes"],
            "remove_dry": ["remove", rid, "--dry-run"],
            "bootstrap": ["bootstrap", rid],
            "extract": ["extract", rid, "--dest", str(self.dest), "--files-from", self.empty_list],
            "baseline_check": ["baseline", "check", rid],
            "baseline_accept": ["baseline", "accept", rid, "--yes"],
            "adopt": ["adopt", path, "--repo", REPO],
            "forget": ["forget", rid],
            "label_add": ["label", "add", rid, "k=v"],
            "label_remove": ["label", "remove", rid, "k=v"],
        }[cmd]

    def row_state(self, rid: str) -> str:
        return self.state_of(rid)


def check(r: Run, cmd: str, want: str, state: str) -> dict[str, Any]:
    """Assert the exit code, error code, reason and schema of one run; returns the JSON."""
    data = r.json()
    if want == "ok":
        assert r.code == 0, (cmd, state, r.out, r.err)
        validate(f"wtctl/cli/{SCHEMAS[cmd]}.json", data)
        return data
    code, _, marker = want.partition(":")
    assert r.code == int(code), (cmd, state, want, r.code, r.out, r.err)
    validate("wtctl/cli/error.json", data)
    err = data["error"]
    assert err["code"] == ERROR_CODES[code], (cmd, state, err)
    if marker == "tomb":
        assert err["details"]["state"] == state and err["details"]["removed_at"], err
    elif marker:
        assert err["reason"] == marker, (cmd, state, err)
    return data


def check_ok_shape(cmd: str, kind: str, state: str, data: dict[str, Any]) -> None:
    """What the table says a successful cell returns (beyond the schema)."""
    worktree = kind != "throwaway"
    if cmd == "show":
        assert data["state"] == state and data["object_type"] == (
            "worktree" if worktree else "throwaway"
        )
    elif cmd in ("status", "remove_dry"):
        assert data["id"] and ("dry_run" in data) == (cmd == "remove_dry")
        if state == "active":
            assert data["would_remove"] is True and data["reason"] is None
            assert data["human_required"] is (kind == "adopted")
        elif state == "partial":
            assert data["human_required"] is worktree  # worktree: human-only; throwaway: no
        else:
            assert data["would_remove"] is False and data["reason"] == "unlinked"
            assert data["human_required"] is (kind == "adopted")
    elif cmd == "baseline_check":
        assert data["changed"] is False
    elif cmd == "adopt" and state == "active":
        assert data["existing"] is True and data["record"]["state"] == "active"
    elif cmd == "adopt":  # a tombstone's path is recorded anew
        assert data["existing"] is False and data["record"]["state"] == "active"
    elif cmd == "extract":
        assert data["written"] == [] and data["deleted"] == []


def run_cell(w: SW, cmd: str, actor: str) -> Run:
    return w.run(*w.argv(cmd), human=actor == "human")


def consuming(cmd: str, actor: str, kind: str, state: str) -> bool:
    """A successful ``remove`` or ``forget`` ends the record's life: it gets a world of its own."""
    return cmd in ("remove", "forget") and exp(cmd, actor, kind, state) == "ok"


CELLS = [(cmd, actor) for cmd in COMMAND_ARGV for actor in actors(cmd)]


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("kind", KINDS)
def test_commands_by_record_state(
    tmp_path: Path, private_home: Path, kind: str, state: str
) -> None:
    """Every command that leaves the record alone, in one world per kind and state."""
    w = SW(tmp_path, private_home).seed(kind, state)
    rid = w.rec["id"]
    status_seen: dict[str, Any] = {}
    for cmd, actor in CELLS:
        want = exp(cmd, actor, kind, state)
        if consuming(cmd, actor, kind, state):
            continue
        data = check(run_cell(w, cmd, actor), cmd, want, state)
        if want == "ok":
            check_ok_shape(cmd, kind, state, data)
        if cmd in ("status", "remove_dry") and want == "ok":
            status_seen[cmd] = data
    if status_seen:  # remove --dry-run is status with "dry_run": true
        dry = dict(status_seen["remove_dry"])
        assert dry.pop("dry_run") is True
        assert {k: v for k, v in dry.items() if k != "warnings"} == {
            k: v for k, v in status_seen["status"].items() if k != "warnings"
        }
    assert w.row_state(rid) == state  # nothing above changed the record's state


def consuming_cells() -> list[Any]:
    return [
        pytest.param(cmd, actor, kind, state, id=f"{cmd}-{actor}-{kind}-{state}")
        for kind in KINDS
        for state in STATES
        for cmd, actor in CELLS
        if consuming(cmd, actor, kind, state)
    ]


@pytest.mark.parametrize(("cmd", "actor", "kind", "state"), consuming_cells())
def test_commands_that_end_the_record(
    tmp_path: Path, private_home: Path, cmd: str, actor: str, kind: str, state: str
) -> None:
    w = SW(tmp_path, private_home).seed(kind, state)
    rid, path = w.rec["id"], Path(w.rec["path"])
    data = check(run_cell(w, cmd, actor), cmd, "ok", state)
    if cmd == "remove":
        assert data["record"]["state"] == "removed" and w.row_state(rid) == "removed"
        assert not path.exists()
    else:  # forget touches nothing on disk
        assert data["record"]["state"] == "forgotten" and w.row_state(rid) == "forgotten"
        assert (path.exists()) == (state != "missing")


# -- actor gate and tombstone addressing --------------------------------------------------------


def test_the_actor_gate_comes_before_record_resolution(tmp_path: Path, private_home: Path) -> None:
    """Non-human `bootstrap`, `baseline accept` and plain `forget`: 13 whatever the target."""
    w = SW(tmp_path, private_home).seed("throwaway", "removed")
    unknown = "wt-zzzzzzzz"
    targets = [w.rec["id"], unknown, "wt-ab", w.rec["path"], "/no/such/path"]
    for target in targets:
        for argv in (["bootstrap"], ["baseline", "accept"], ["forget"]):
            r = w.run(*argv, target)
            assert r.code == 13, (argv, target, r.out)
            validate("wtctl/cli/error.json", r.json())
            assert r.json()["error"]["code"] == "confirmation_required"
    # An empty --holder counts as none: 13 for a non-human, 2 for a human.
    assert w.run("forget", unknown, "--holder", "").code == 13
    assert w.run("forget", unknown, "--holder", "", human=True).code == 2
    # Usage errors (stage 1) still win over the gate.
    assert w.run("--verbose", "--quiet", "forget", unknown).code == 2
    # With --holder a non-human is past the gate and reaches record resolution.
    r = w.run("forget", unknown, "--holder", "h1")
    assert r.code == 10, r.out


def test_a_tombstone_is_found_by_its_full_id_only(tmp_path: Path, private_home: Path) -> None:
    w = SW(tmp_path, private_home).seed("worktree", "removed")
    rid = w.rec["id"]
    assert w.run("show", rid).code == 0
    assert w.run("show", rid[3:]).code == 0  # the full id without its optional `wt-`
    for prefix in (rid[:-1], rid[3:-1]):  # a prefix, with and without `wt-`
        for argv in (["show"], ["status"], ["label", "add"]):
            tail = ["k=v"] if argv[0] == "label" else []
            r = w.run(*argv, prefix, *tail)
            assert r.code == 10, (argv, prefix, r.out)
            assert "details" not in r.json()["error"] or not r.json()["error"]["details"]


# -- records that other commands act on as a set ------------------------------------------------


def seed_all_states(w: SW, kind: str) -> dict[str, str]:
    """One record of ``kind`` per state; returns state -> id."""
    ids: dict[str, str] = {}
    for n, state in enumerate(STATES):
        rec = w.make(kind, f"{state}-{n}")
        w.put_in(rec["id"], state, rec["path"])
        ids[state] = rec["id"]
    return ids


def test_list_by_state(tmp_path: Path, private_home: Path) -> None:
    """`list` hides tombstones unless `--state` names them; every live state is listed."""
    w = SW(tmp_path, private_home).build()
    ids = seed_all_states(w, "worktree")

    def listed(*argv: str) -> set[str]:
        r = w.run("list", *argv)
        assert r.code == 0, (r.out, r.err)
        validate("wtctl/cli/list.json", r.json())
        return {x["id"] for x in r.json()["records"]}

    assert listed() == {ids[s] for s in LIVE}
    for state in STATES:
        assert listed("--state", state) == {ids[state]}
    assert listed("--state", "removed", "--state", "gone") == {ids["removed"], ids["gone"]}
    assert listed("--all") == {ids[s] for s in LIVE}


def test_prune_by_state(tmp_path: Path, private_home: Path) -> None:
    """`prune` considers only `active` and `partial` throwaways (and never a worktree)."""
    w = SW(tmp_path, private_home).build()
    ids = seed_all_states(w, "throwaway")
    worktree = w.make("worktree", "bystander")["id"]
    for rid in [*ids.values(), worktree]:
        w.age(rid, 30)
    data = w.pruned("--older-than", "1d")
    assert sorted(data["removed"]) == sorted([ids["active"], ids["partial"]])
    for state in STATES:
        want = "removed" if state in ("active", "partial") else state
        assert w.state_of(ids[state]) == want, state
    assert w.state_of(worktree) == "active"


def test_reconcile_by_state(tmp_path: Path, private_home: Path) -> None:
    """What `reconcile` does with each state (interface.md "Reconcile"; the full edge cases are
    in the test_reconcile_* modules)."""
    w = RC(tmp_path, private_home).build()
    healthy, vanished, partial, linked = (w.wt(n) for n in ("a", "b", "c", "d"))
    crashed = w.wt("e", holder="agent-1")
    from tests.test_reconcile_creating import crash_after_add

    crash_after_add(w, crashed)
    w.vanish(vanished)
    w.update("UPDATE records SET state='partial' WHERE id=?", partial["id"])
    w.update("UPDATE records SET state='clone_missing' WHERE id=?", linked["id"])
    tombs = {}
    for state in TOMBSTONES:
        rec = w.wt(f"t-{state}")
        w.update(
            "UPDATE records SET state=?, removed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id=?",
            state, rec["id"],
        )  # fmt: skip
        tombs[state] = rec["id"]
    data = w.reconciled()
    state_of = {r["id"]: r["state"] for r in w.records()}
    assert state_of[healthy["id"]] == "active"
    assert state_of[vanished["id"]] == "missing"  # first sighting: never straight to gone
    assert state_of[partial["id"]] == "partial"  # only a human finishes a partial record
    assert state_of[crashed["id"]] == "active"  # adopted_after_crash
    assert "adopted_after_crash" in w.kinds(data)
    assert w.row(crashed["id"])["bootstrap_state"] == "interrupted"
    assert state_of[linked["id"]] in ("clone_missing", "active")  # never `gone`, never deleted
    assert Path(linked["path"]).is_dir() and Path(partial["path"]).is_dir()
    for state, rid in tombs.items():
        assert state_of[rid] == state  # inside tombstone_days: untouched


# -- create --holder reuse ----------------------------------------------------------------------


@pytest.mark.parametrize("state", ["creating", "active", "partial", "missing", "unlinked"])
def test_create_reuse_by_state(tmp_path: Path, private_home: Path, state: str) -> None:
    """A retried `create` with the holder gets its record back only when it is `active`; the other
    live states refuse with `path_exists` (a `creating` one names its `holder_id`)."""
    w = RC(tmp_path, private_home).build()
    rec = w.wt("n1", holder="h1")
    again = ["create", "--repo", REPO, "--name", "n1", "--branch", rec["branch"],
             "--no-bootstrap", "--holder", "h1"]  # fmt: skip
    if state != "active":
        w.set_state(rec["id"], state)
        if state == "missing":
            shutil.rmtree(rec["path"])
    r = w.run(*again)
    if state == "active":
        assert r.code == 0 and r.json()["reused"] is True
        assert r.json()["record"]["id"] == rec["id"]
        other = w.run(*again[:-1], "h2")
        assert other.code == 20 and other.json()["error"]["reason"] == "path_exists"
        return
    assert r.code == 20, (r.out, r.err)
    validate("wtctl/cli/error.json", r.json())
    assert r.json()["error"]["reason"] == "path_exists"
    if state == "creating":
        assert r.json()["error"]["holder_id"] == rec["id"]


def test_create_after_the_record_is_removed_is_a_new_record(
    tmp_path: Path, private_home: Path
) -> None:
    w = RC(tmp_path, private_home).build()
    rec = w.wt("n1", holder="h1")
    assert w.run("remove", rec["id"], "--yes", "--holder", "h1", human=True).code == 0
    r = w.run("create", "--repo", REPO, "--name", "n1", "--branch", "again", "--no-bootstrap",
              "--holder", "h1")  # fmt: skip
    assert r.code == 0, (r.out, r.err)
    assert r.json()["reused"] is False and r.json()["record"]["id"] != rec["id"]


# -- rows that need more than a state -----------------------------------------------------------


def test_an_adopted_main_worktree_is_refused_before_the_human_gate(
    tmp_path: Path, private_home: Path
) -> None:
    w = SW(tmp_path, private_home).build()
    r = w.run("adopt", str(w.clone), "--repo", REPO)
    assert r.code == 0, (r.out, r.err)
    rid = r.json()["record"]["id"]
    for human in (False, True):
        out = w.run("remove", rid, "--yes", human=human)
        assert out.code == 20 and out.json()["error"]["reason"] == "main_worktree", out.out
    assert w.state_of(rid) == "active"


def test_a_throwaway_whose_directory_vanished_while_active(
    tmp_path: Path, private_home: Path
) -> None:
    w = SW(tmp_path, private_home).build()
    rec = w.made("--source", "local")
    shutil.rmtree(rec["path"])
    dest = w.plain_dest()
    r = w.run("extract", rec["id"], "--dest", str(dest), "--files-from", w.listfile([]))
    assert r.code == 20 and r.json()["error"]["reason"] == "unlinked", r.out
    r = w.run("remove", rec["id"], "--yes")
    assert r.code == 0, (r.out, r.err)
    validate("wtctl/cli/remove.json", r.json())
    assert w.state_of(rec["id"]) == "gone" and r.json()["warnings"]


def test_a_live_creator_makes_lease_taking_commands_busy(
    tmp_path: Path, private_home: Path
) -> None:
    """While a `creating` record's creator is alive the commands that take its lease exit 12,
    `busy`; the ones that take none still answer (the `creating` row)."""
    w = SW(tmp_path, private_home).seed("worktree", "creating")
    other = SW.make(w, "throwaway")
    w.set_state(other["id"], "creating")
    st = State(w.db, _gateway())
    leases = [st.lease(record_lease(x), wait=0) for x in (w.rec["id"], other["id"])]
    try:
        busy = [
            ("remove", True),
            ("bootstrap", True),
            ("baseline_accept", True),
            ("adopt", False),
            ("forget", True),
            ("label_add", False),
        ]
        for cmd, human in busy:
            r = w.run(*w.argv(cmd), human=human)
            assert r.code == 12, (cmd, r.out, r.err)
            assert r.json()["error"]["code"] == "busy"
        r = w.run("extract", other["id"], "--dest", str(w.dest), "--files-from", w.empty_list)
        assert r.code == 12, r.out
        for cmd in ("show", "status", "remove_dry", "baseline_check"):
            r = w.run(*w.argv(cmd))
            assert r.code == (20 if cmd == "baseline_check" else 0), (cmd, r.out)
        assert w.run("forget", w.rec["id"], "--dry-run", human=True).code == 0
    finally:
        for lease in leases:
            lease.release()
        st.close()


def _gateway() -> Any:
    from wtctl.gateway import Gateway

    return Gateway()
