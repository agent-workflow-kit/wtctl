"""docs/interface.md "Commands by record state", as a parametrized test.

Three kinds of record (an owned worktree, an adopted worktree, a throwaway) are put in each of the
nine record states and every state-addressed command is run against each. The expected exit code,
error code, reason and ``--json`` shape (validated against ``schemas/``) come from ``exp`` below,
which is written from the table.

Keeping the table and this module in step: ``DOC_CELLS`` repeats every cell of the markdown table
verbatim (links and backticks stripped). ``test_the_doc_table_is_the_one_tested`` parses the table
out of interface.md and fails on any difference in rows, columns or cell text, so editing the
table without revisiting ``exp`` is a red test (the paragraph above the table and the bullets
below it are pinned the same way); ``test_every_cell_has_an_expectation`` fails when a state/column
of the table has no expectation here, or when ``exp`` disagrees with the table's exit code (apart
from the documented exit 2 and 13 exceptions). ``test_every_command_is_covered`` fails when a
command is added to the registry without being classified below, or names a test that is missing.

Out of scope here: the ``clone_missing`` cell "baseline accept re-attaches a moved clone" is
covered by ``tests/test_reconcile_pointers.py``. Seeded throwaways in ``unlinked``, ``missing`` and
``clone_missing`` are not in the matrix: reconcile sends a vanished throwaway to ``gone``, so those
pairs are unreachable (the table says "never a throwaway state").

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

# The paragraph above the table and the bullets under it, as rendered (same normalization).
DOC_PRECEDING = (
    "What each single-target command does with a record in each state (exit code and "
    "outcome). A state-dependent condition exits 20 (or 10 for a tombstone), never 2; exit 2 "
    "is reserved for usage, including a command applied to the wrong object type (except that"
    " a non-human running bootstrap, baseline accept or forget without --holder exits 13 "
    "first, at the actor gate, whatever the target): bootstrap, baseline check|accept, forget"
    " and adopt (a path in the throwaway root) refuse throwaways, and extract refuses "
    "worktrees, with exit 2 in every live state (a full id naming a tombstone exits 10 first,"
    " since record resolution comes before the command's own checks)."
)
DOC_BULLETS = (
    (
        "**Tombstones:** only a full id finds one (Identifiers); the 10 carries error.details: "
        "{state, removed_at}. A path never matches a tombstone, so adopt of a worktree's path "
        "records it anew (the path must be a git worktree, otherwise exit 10); a path in the "
        "throwaway root is refused with exit 2 whatever the record's state."
    ),
    (
        "**forget** follows its own rules in every live state (plain forget is human-only; "
        "--holder removes a holder)."
    ),
    (
        "**A throwaway whose directory vanished** while its record is still active (before "
        "Reconcile saw it): remove marks it gone and exits 0 with a warning; extract exits 20, "
        "unlinked."
    ),
    (
        "**A live creator.** Commands that take the per-record lease (remove, bootstrap, extract,"
        " baseline accept, adopt of its path, forget, label) wait for a creating record's lease "
        "while its creator is alive and then exit 12, busy; the creating row shows the outcome "
        "once the creator has died."
    ),
    (
        "**A crashed create** leaves a creating record; a retried create reports branch_in_use or"
        " path_exists with its holder_id until Reconcile settles it. Then a retry succeeds "
        "normally if the record became gone; if it was reclaimed (adopted_after_crash), a retry "
        'with one of its holders gets it back with "reused": true (Retry after a crash), and any '
        "other retry is still refused. A retry made while the creator is alive waits for it as "
        "described in Retry after a crash."
    ),
)

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
    "browse": "read-only view of the records of every state; its exit never depends on a state",
    "validate": "checks the config and environment; reads no state and no record",
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


def doc_prose() -> tuple[str, tuple[str, ...]]:
    """The paragraph before the table and the bullets after it, normalized like the cells."""
    text = DOC.read_text()
    section = text.split("\n### Commands by record state\n", 1)[1].split("\n### ", 1)[0]
    lines = [ln for ln in section.splitlines() if ln.strip()]
    first_row = next(i for i, ln in enumerate(lines) if ln.startswith("|"))
    before = " ".join(lines[:first_row])
    bullets = tuple(_plain(ln[2:]) for ln in lines if ln.startswith("- "))
    return _plain(before), bullets


def doc_states() -> list[str]:
    return [s for label in doc_table() for s in label.split(", ")]


def test_the_doc_table_is_the_one_tested() -> None:
    parsed = doc_table()
    assert list(parsed) == list(DOC_CELLS)
    for label, cells in DOC_CELLS.items():
        assert dict(zip(COLUMNS, parsed[label], strict=True)) == dict(
            zip(COLUMNS, cells, strict=True)
        ), f"interface.md row {label!r} changed: revisit exp() and DOC_CELLS"


def test_the_prose_around_the_table_is_the_one_tested() -> None:
    before, bullets = doc_prose()
    assert before == DOC_PRECEDING, "interface.md: the paragraph above the table changed"
    assert bullets == DOC_BULLETS, "interface.md: a bullet under the table changed: revisit exp()"


WRONG_FOR_THROWAWAY = ("bootstrap", "baseline_check", "baseline_accept", "forget", "adopt")
EXPECTATION = re.compile(r"^(ok|\d+(:\w+)?)$")


def test_every_cell_has_an_expectation() -> None:
    """Every (cmd, actor, kind, state) has a well-formed expectation whose exit class agrees with
    the table cell, except for the exit 2 (wrong object type) and 13 (actor gate) the paragraph
    above the table documents, and `adopt` of a `missing` record (the cell's "missing: 10")."""
    assert sorted(doc_states()) == sorted(STATES)
    table = doc_table()
    for label, cells in table.items():
        for state in label.split(", "):
            for column, cell in zip(COLUMNS, cells, strict=True):
                for cmd in COLUMN_CMDS[column]:
                    for kind in KINDS:
                        for actor in actors(cmd):
                            want = exp(cmd, actor, kind, state)
                            assert EXPECTATION.match(want), (cmd, actor, kind, state, want)
                            lead = re.match(r"\d+", cell)
                            if lead is None:  # a reference to another section, or "never"
                                continue
                            got = 0 if want == "ok" else int(want.partition(":")[0])
                            allowed = {int(lead.group())}
                            if (kind == "throwaway" and cmd in WRONG_FOR_THROWAWAY) or (
                                kind != "throwaway" and cmd == "extract"
                            ):
                                allowed.add(2)  # wrong object type
                            if cmd in (*HUMAN_GATED, "remove") and actor != "human":
                                allowed.add(13)  # actor gate
                            if "missing: 10" in cell and state == "missing":
                                allowed.add(10)
                            assert got in allowed, (cmd, actor, kind, state, want, cell)


def test_exp_rejects_what_it_does_not_know() -> None:
    unknown = [
        ("nope", "auto", "worktree", "active"),
        ("show", "robot", "worktree", "active"),
        ("show", "auto", "branch", "active"),
        ("show", "auto", "worktree", "limbo"),
    ]
    for bad in unknown:
        with pytest.raises(ValueError):
            exp(*bad)


def test_every_command_is_covered() -> None:
    keys = {c.key for c in registry.COMMANDS}
    assert set(COVERED).isdisjoint(NO_STATE_DEPENDENCE)
    assert keys == set(COVERED) | set(NO_STATE_DEPENDENCE), keys ^ (
        set(COVERED) | set(NO_STATE_DEPENDENCE)
    )
    tested = {c for cmds in COLUMN_CMDS.values() for c in cmds}
    assert {c for c in COVERED if "per-state matrix" in COVERED[c]} <= tested


# What the free-text parts of COVERED stand for.
COVERING_TESTS = {
    "per-state matrix": "test_commands_by_record_state",
    "actor gate": "test_the_actor_gate_comes_before_record_resolution",
    "adopted main worktree": "test_an_adopted_main_worktree_is_refused_before_the_human_gate",
    "vanished throwaway": "test_a_throwaway_whose_directory_vanished_while_active",
}


def test_every_covering_test_exists() -> None:
    for cmd, where in COVERED.items():
        for part in (p.strip() for p in where.split(";")):
            name = part if part.startswith("test_") else COVERING_TESTS.get(part)
            assert name is not None, (cmd, part)
            assert callable(globals().get(name)), (cmd, part, name)


# -- expectations -------------------------------------------------------------------------------

HUMAN_GATED = ("bootstrap", "baseline_accept", "forget")


def actors(cmd: str) -> tuple[str, ...]:
    """Actor variants a command is run with: the human-gated ones with both, `remove` with both."""
    return ("human", "auto") if cmd in (*HUMAN_GATED, "remove") else ("auto",)


def exp(cmd: str, actor: str, kind: str, state: str) -> str:
    """The documented outcome: ``ok``, or ``<exit>`` / ``<exit>:<reason or marker>``.

    ``10:tomb`` is exit 10 with ``details: {state, removed_at}``.
    """
    if (
        cmd not in SCHEMAS
        or actor not in ("human", "auto")
        or kind not in KINDS
        or state not in STATES
    ):
        raise ValueError(f"no expectation for {(cmd, actor, kind, state)}")
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
        # bootstrap, baseline check, baseline accept. For `clone_missing`, a human `baseline
        # accept` with an unmoved clone is still 20 unlinked; the moved-clone re-attach is tested
        # in tests/test_reconcile_pointers.py::test_baseline_accept_reattaches_a_moved_clone.
        return "20:unlinked"
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


def check(r: Run, cmd: str, want: str, state: str, rid: str | None = None) -> dict[str, Any]:
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
        if marker == "path_exists" and rid is not None:  # the `creating` record is the holder
            assert err["holder_id"] == rid, (cmd, state, err)
    return data


GIT_CHECKS = ("locked", "baseline", "fetch")  # the checks that run git


def check_ok_shape(cmd: str, kind: str, state: str, data: dict[str, Any]) -> None:
    """What the table says a successful cell returns (beyond the schema)."""
    worktree = kind != "throwaway"
    if cmd == "show":
        assert data["state"] == state and data["object_type"] == (
            "worktree" if worktree else "throwaway"
        )
    elif cmd in ("status", "remove_dry"):
        # A `partial` or `creating` status shape (human_required, would_remove) is not in the table,
        # which says only "0"; it rests on the implementation, not the documentation.
        assert data["id"] and ("dry_run" in data) == (cmd == "remove_dry")
        if state == "active":
            assert data["would_remove"] is True and data["reason"] is None
            assert data["human_required"] is (kind == "adopted")
        elif state == "partial":
            assert data["human_required"] is worktree  # worktree: human-only; throwaway: no
        else:
            assert data["would_remove"] is False and data["reason"] == "unlinked"
            assert data["human_required"] is (kind == "adopted")
            if state != "creating":  # unlinked, missing, clone_missing: git is not run
                skipped = {n: c["status"] for n, c in data["checks"].items() if n in GIT_CHECKS}
                assert skipped and set(skipped.values()) == {"skipped"}, (cmd, state, skipped)
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


def reachable(kind: str, state: str) -> bool:
    """Reconcile sends a vanished throwaway to `gone` (never `unlinked`, `missing` or
    `clone_missing`), so a throwaway in those states cannot occur."""
    return not (kind == "throwaway" and state in ("unlinked", "missing", "clone_missing"))


MATRIX = [pytest.param(k, st, id=f"{k}-{st}") for k in KINDS for st in STATES if reachable(k, st)]


def snapshot(w: SW, rid: str, ignore: tuple[str, ...] = ()) -> dict[str, Any]:
    """The whole record, as `show` prints it and as the database row holds it. ``updated`` is left
    out: adding and then removing a label legitimately moves it. ``ignore`` names fields a command
    in the sequence legitimately rewrites (a successful `bootstrap` re-run records its outcome)."""
    skip = {"updated", *ignore}
    shown = w.run("show", rid)
    assert shown.code == 0, shown.out
    record = {k: v for k, v in shown.json().items() if k not in skip}
    row = {k: v for k, v in dict(w.rows("SELECT * FROM records WHERE id = ?", rid)[0]).items()
           if k not in skip}  # fmt: skip
    return {"shown": record, "row": row}


@pytest.mark.parametrize(("kind", "state"), MATRIX)
def test_commands_by_record_state(
    tmp_path: Path, private_home: Path, kind: str, state: str
) -> None:
    """Every command that leaves the record alone, in one world per kind and state."""
    w = SW(tmp_path, private_home).seed(kind, state)
    rid = w.rec["id"]
    status_seen: dict[str, Any] = {}
    rewritten = (
        ("bootstrap", "bootstrap_state") if exp("bootstrap", "human", kind, state) == "ok" else ()
    )
    before = snapshot(w, rid, rewritten)
    for cmd, actor in CELLS:
        want = exp(cmd, actor, kind, state)
        if consuming(cmd, actor, kind, state):
            continue
        data = check(run_cell(w, cmd, actor), cmd, want, state, rid)
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
    after = snapshot(w, rid, rewritten)  # nor any other field (labels round-trip)
    changed = {
        (part, k): (before[part][k], after[part][k])
        for part in before
        for k in before[part]
        if before[part][k] != after[part][k]
    }
    assert not changed, changed


def consuming_cells() -> list[Any]:
    return [
        pytest.param(cmd, actor, kind, state, id=f"{cmd}-{actor}-{kind}-{state}")
        for kind in KINDS
        for state in STATES
        for cmd, actor in CELLS
        if reachable(kind, state) and consuming(cmd, actor, kind, state)
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


def test_adopt_of_a_removed_worktrees_deleted_path_is_not_a_directory(
    tmp_path: Path, private_home: Path
) -> None:
    """The table's "a tombstone's path is recorded anew" needs the path to exist: after a real
    `remove` the directory is gone, so `adopt` of it exits 10 (not a directory)."""
    w = SW(tmp_path, private_home).build()
    rec = w.make("worktree")
    assert w.run("remove", rec["id"], "--yes", human=True).code == 0
    assert w.state_of(rec["id"]) == "removed" and not Path(rec["path"]).exists()
    r = w.run("adopt", rec["path"], "--repo", REPO)
    assert r.code == 10, (r.out, r.err)
    validate("wtctl/cli/error.json", r.json())
    assert r.json()["error"]["code"] == "not_found"
    assert w.state_of(rec["id"]) == "removed"


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
