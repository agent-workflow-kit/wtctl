"""The browse view-model and renderer: no curses, no terminal (docs/interface.md, browse)."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from tests.listhelpers import seed
from tests.statehelpers import FakeClock, make_state
from wtctl import browse_model as bm
from wtctl.record_view import record_object

NOW = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)


class FakeHandle:
    def __init__(self, outcome: bm.StatusOutcome | None = None) -> None:
        self.outcome = outcome
        self.cancelled = False

    def poll(self) -> bm.StatusOutcome | None:
        return self.outcome

    def cancel(self) -> None:
        self.cancelled = True


class FakeStarter:
    def __init__(self) -> None:
        self.targets: list[str] = []
        self.handles: list[FakeHandle] = []

    def __call__(self, target: str) -> FakeHandle:
        self.targets.append(target)
        h = FakeHandle()
        self.handles.append(h)
        return h


@pytest.fixture
def records(tmp_path: Any) -> list[dict[str, Any]]:
    st = make_state(tmp_path)
    seed(st, "wt-aaaaaaaa", labels=["team=x"], repo="example.com/o/alpha")
    seed(st, "wt-bbbbbbbb", state="partial", repo="example.com/o/beta", branch="feature/zed")
    seed(st, "wt-cccccccc", object_type="throwaway", repo="example.com/o/alpha")
    recs = [record_object(st, r) for r in st.records(newest_first=True)]
    st.close()
    return recs


def browser(
    recs: list[dict[str, Any]],
    foreign: list[dict[str, Any]] | None = None,
    starter: FakeStarter | None = None,
) -> bm.Browser:
    return bm.Browser(
        lambda: (recs, foreign or []), starter or FakeStarter(), now=lambda: NOW
    )


def text(lines: list[bm.Line]) -> str:
    return "\n".join(line.text for line in lines)


def keys(b: bm.Browser, *ks: str) -> None:
    for k in ks:
        b.handle_key(k)


# --- sanitizing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "a\x1b[31mred\x1b[0m",
        "a\x1b]0;title\x07b",
        "a\x9bc",
        "a\rb\nc\td",
        "x‮y⁦z",
        "x y z",
        "a\ud800b",
        "a\x00b",
        "é́́",
        "ab",
    ],
)
def test_sanitize_leaves_no_control_or_spoofing_character(raw: str) -> None:
    out = bm.sanitize(raw, 200)
    assert out.isascii() or all(c.isprintable() for c in out)
    for c in out:
        assert c.isprintable(), repr(c)
    assert "\x1b" not in out


def test_sanitize_shows_the_escape_visibly() -> None:
    assert bm.sanitize("a\x1b[31m", 50) == "a\\x1b[31m"
    assert bm.sanitize("x‮y", 50) == "x\\u202ey"


def test_sanitize_bounds_length_with_a_marker() -> None:
    out = bm.sanitize("a" * 10_000, 20)
    assert len(out) == 20 and out.endswith("~")
    assert bm.sanitize("short", 20) == "short"


def test_sanitize_counts_columns_not_characters() -> None:
    out = bm.sanitize("中" * 20, 10)  # wide characters: two columns each
    assert bm.display_width(out) <= 10


# --- list ---------------------------------------------------------------------------------


def test_list_shows_header_and_every_record(records: list[dict[str, Any]]) -> None:
    out = text(browser(records).render(24, 100))
    for needle in ("ID", "TYPE", "STATE", "REPO", "wt-aaaaaaaa", "wt-bbbbbbbb", "wt-cccccccc"):
        assert needle in out
    assert "3/3" in out
    assert "partial" in out and "throwaway" in out


def test_rows_keep_the_order_of_the_loader(records: list[dict[str, Any]]) -> None:
    out = text(browser(records).render(24, 100)).splitlines()
    ids = [x for line in out for x in ("wt-aaaaaaaa", "wt-bbbbbbbb", "wt-cccccccc") if x in line]
    assert ids == [r["id"] for r in records]


def test_selection_moves_and_clamps(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    keys(b, "k", "k")  # already at the top
    assert b.selected_id() == records[0]["id"]
    keys(b, "j", "j", "j", "j", "down")
    assert b.selected_id() == records[-1]["id"]
    keys(b, "home")
    assert b.selected_id() == records[0]["id"]
    keys(b, "end")
    assert b.selected_id() == records[-1]["id"]


def test_selected_line_is_styled(records: list[dict[str, Any]]) -> None:
    lines = browser(records).render(24, 100)
    sel = [x for x in lines if x.style == "selected"]
    assert len(sel) == 1 and records[0]["id"] in sel[0].text


def test_scrolling_keeps_the_selection_visible() -> None:
    recs = [
        {"id": f"wt-{i:08d}", "object_type": "worktree", "state": "active", "repo": "r/x/y",
         "name": f"n{i}", "ref": None, "branch": None, "created": "2026-01-01T00:00:00.000Z",
         "labels": [], "path": f"/w/{i}"}
        for i in range(100)
    ]  # fmt: skip
    b = browser(recs)
    for _ in range(60):
        b.handle_key("j")
    out = text(b.render(12, 80))
    assert "wt-00000060" in out
    assert "wt-00000000" not in out
    keys(b, "pgup")
    assert b.selected_id() != "wt-00000060"


# --- filter -------------------------------------------------------------------------------


def test_filter_narrows_by_substring_case_insensitively(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    keys(b, "/", "B", "E", "T", "A", "enter")
    out = text(b.render(24, 100))
    assert "wt-bbbbbbbb" in out and "wt-aaaaaaaa" not in out
    assert "1/3" in out


def test_filter_matches_labels_and_branch(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    keys(b, "/", *"team=x", "enter")
    assert b.visible_ids() == ["wt-aaaaaaaa"]
    keys(b, "/", "esc")  # esc clears
    assert len(b.visible_ids()) == 3
    keys(b, "/", *"zed", "enter")
    assert b.visible_ids() == ["wt-bbbbbbbb"]


def test_filter_with_no_match_shows_an_empty_notice(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    keys(b, "/", *"nomatch", "enter")
    assert "no records match" in text(b.render(24, 100))
    keys(b, "enter", "j", "k")  # nothing selected: keys do not crash
    assert b.selected_id() is None


def test_filter_input_ignores_control_characters_and_is_bounded(
    records: list[dict[str, Any]],
) -> None:
    b = browser(records)
    keys(b, "/", "\x1b[", "\x00", *("a" * 500))
    assert "\x1b" not in b.filter_text and len(b.filter_text) <= bm.MAX_FILTER
    keys(b, *["backspace"] * 600)
    assert b.filter_text == ""


def test_typing_in_the_filter_does_not_trigger_commands(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    assert b.handle_key("/") and b.handle_key("q") and b.handle_key("s")
    assert b.filter_text == "qs"


def test_empty_state_database() -> None:
    out = text(browser([]).render(24, 80))
    assert "0/0" in out and "no records" in out


# --- detail and status --------------------------------------------------------------------


def test_enter_opens_the_show_view(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    keys(b, "enter")
    out = text(b.render(40, 100))
    assert f"id: {records[0]['id']}" in out
    assert "state:" in out and "repo:" in out
    assert f"wtctl remove {records[0]['id']}" in out
    assert "never removes" in out
    keys(b, "esc")
    assert "TYPE" in text(b.render(24, 100))  # back at the list


def test_detail_wraps_long_values_and_scrolls(records: list[dict[str, Any]]) -> None:
    long = dict(records[0], last_error="E" * 500)
    b = browser([long])
    keys(b, "enter")
    lines = b.render(10, 40)
    assert all(bm.display_width(x.text) <= 39 for x in lines)
    first = text(lines)
    keys(b, "j", "j", "j")
    assert text(b.render(10, 40)) != first
    keys(b, "k", "k", "k", "k", "k")  # does not go above the top
    assert text(b.render(10, 40)) == first


def test_status_runs_on_demand_and_shows_the_outcome(records: list[dict[str, Any]]) -> None:
    starter = FakeStarter()
    b = browser(records, starter=starter)
    keys(b, "enter")
    assert starter.targets == []  # nothing runs until asked
    keys(b, "s")
    assert starter.targets == [records[0]["id"]]
    assert "running" in text(b.render(40, 100))
    keys(b, "s")  # a second request while running starts nothing
    assert len(starter.targets) == 1
    starter.handles[0].outcome = bm.StatusOutcome(True, ("x: would be removed", "  dirty: fail"))
    assert b.tick() is True
    out = text(b.render(40, 100))
    assert "would be removed" in out and "dirty: fail" in out
    assert b.tick() is False


def test_status_failure_is_shown_as_an_error(records: list[dict[str, Any]]) -> None:
    starter = FakeStarter()
    b = browser(records, starter=starter)
    keys(b, "enter", "s")
    starter.handles[0].outcome = bm.StatusOutcome(False, ("status failed: timed out",))
    b.tick()
    lines = b.render(40, 100)
    assert any(x.style == "error" and "timed out" in x.text for x in lines)


def test_status_result_survives_leaving_and_returning(records: list[dict[str, Any]]) -> None:
    starter = FakeStarter()
    b = browser(records, starter=starter)
    keys(b, "enter", "s")
    starter.handles[0].outcome = bm.StatusOutcome(True, ("marker-line",))
    b.tick()
    keys(b, "esc", "enter")
    assert "marker-line" in text(b.render(40, 100))


def test_status_cancel_and_close(records: list[dict[str, Any]]) -> None:
    starter = FakeStarter()
    b = browser(records, starter=starter)
    keys(b, "enter", "s", "c")
    assert starter.handles[0].cancelled
    assert "cancelled" in text(b.render(40, 100))
    keys(b, "s")
    b.close()
    assert starter.handles[1].cancelled


def test_status_is_not_offered_for_foreign_entries() -> None:
    starter = FakeStarter()
    foreign = [{"foreign": True, "path": "/w/other", "branch": "x", "head": None, "clone_path": "/c"}]
    b = browser([], foreign=foreign, starter=starter)
    keys(b, "enter", "s")
    assert starter.targets == []
    out = text(b.render(24, 80))
    assert "/w/other" in out and "foreign" in out
    assert "wtctl remove" not in out


# --- refresh, help, quit ------------------------------------------------------------------


def test_reload_picks_up_new_records_and_keeps_the_selection(
    records: list[dict[str, Any]],
) -> None:
    box = {"recs": records[:2]}
    b = bm.Browser(lambda: (box["recs"], []), FakeStarter(), now=lambda: NOW)
    keys(b, "j")
    sel = b.selected_id()
    box["recs"] = records
    keys(b, "r")
    assert len(b.visible_ids()) == 3 and b.selected_id() == sel


def test_reload_failure_keeps_the_old_data(records: list[dict[str, Any]]) -> None:
    from wtctl.errors import WtctlError

    calls = {"n": 0}

    def loader() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise WtctlError("internal", "boom \x1b[31m")
        return records, []

    b = bm.Browser(loader, FakeStarter(), now=lambda: NOW)
    keys(b, "r")
    out = text(b.render(24, 100))
    assert "reload failed" in out and "\x1b" not in out
    assert len(b.visible_ids()) == 3


def test_help_and_quit(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    keys(b, "?")
    assert "read-only" in text(b.render(24, 100))
    keys(b, "esc")
    assert "TYPE" in text(b.render(24, 100))
    assert b.handle_key("q") is False
    assert browser(records).handle_key("Q") is False


def test_unknown_keys_are_ignored(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    for k in ("x", "\x00", "f5", "", "ab", "‮", "resize"):
        assert b.handle_key(k) is True


# --- hostile record fields ----------------------------------------------------------------


HOSTILE = "evil\x1b[2J\x1b]0;pwn\x07‮\r\n\x9b31m" + "Z" * 5000


def test_hostile_fields_never_reach_the_screen_raw(records: list[dict[str, Any]]) -> None:
    rec = dict(
        records[0],
        repo="example.com/o/" + HOSTILE,
        name=HOSTILE,
        branch=HOSTILE,
        path="/w/" + HOSTILE,
        labels=[HOSTILE + "=" + HOSTILE],
        last_error=HOSTILE,
        holders=[HOSTILE],
    )
    b = browser([rec, *records[1:]])
    for width in (200, 80, 30):
        lines = b.render(30, width)
        for ln in lines:
            assert "\x1b" not in ln.text and "\r" not in ln.text and "\n" not in ln.text
            assert all(c.isprintable() for c in ln.text)
            assert bm.display_width(ln.text) < width
    keys(b, "enter")
    for ln in b.render(60, 200):
        assert all(c.isprintable() for c in ln.text)
        assert "\x1b" not in ln.text
    keys(b, "esc", "/", *"evil", "enter")  # filtering over hostile text still works
    assert b.visible_ids() == [rec["id"]]


def test_hostile_line_cannot_fake_a_selection_or_a_row(records: list[dict[str, Any]]) -> None:
    rec = dict(records[1], name="x\n> wt-ffffffff active")
    lines = browser([rec]).render(24, 100)
    assert sum("wt-ffffffff" in x.text for x in lines) == 1  # the escaped text, on the same line
    assert len([x for x in lines if x.style == "selected"]) == 1


def test_hostile_status_output_is_sanitized(records: list[dict[str, Any]]) -> None:
    starter = FakeStarter()
    b = browser(records, starter=starter)
    keys(b, "enter", "s")
    starter.handles[0].outcome = bm.StatusOutcome(True, ("a\x1b[2Jb‮",))
    b.tick()
    for ln in b.render(40, 100):
        assert "\x1b" not in ln.text and "‮" not in ln.text


# --- small terminals ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("h", "w"), [(1, 1), (2, 10), (4, 80), (24, 5), (24, 23), (5, 24), (3, 200), (0, 0)]
)
def test_tiny_terminals_never_overflow(records: list[dict[str, Any]], h: int, w: int) -> None:
    b = browser(records)
    for mode_keys in ([], ["enter"], ["esc", "?"]):
        keys(b, *mode_keys)
        lines = b.render(h, w)
        assert len(lines) <= max(h, 0)
        for ln in lines:
            assert bm.display_width(ln.text) <= max(w - 1, 0)


def test_too_small_terminal_says_so(records: list[dict[str, Any]]) -> None:
    out = text(browser(records).render(3, 30))
    assert "too small" in out
    assert browser(records).handle_key("q") is False  # quit still works there


def test_resize_re_renders_to_the_new_size(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    wide = b.render(24, 120)
    narrow = b.render(24, 40)
    assert text(wide) != text(narrow)
    assert all(bm.display_width(x.text) <= 39 for x in narrow)


def test_json_roundtrip_of_records_is_not_needed_for_rendering(
    records: list[dict[str, Any]],
) -> None:
    # records are plain JSON-able dicts (the same objects `list --json` prints)
    json.dumps(records)
