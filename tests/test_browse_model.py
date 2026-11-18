"""The browse view-model and renderer: no curses, no terminal (docs/interface.md, browse)."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from tests.listhelpers import seed
from tests.statehelpers import make_state
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
    return bm.Browser(lambda: (recs, foreign or []), starter or FakeStarter(), now=lambda: NOW)


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
    foreign = [
        {"foreign": True, "path": "/w/other", "branch": "x", "head": None, "clone_path": "/c"}
    ]
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


# --- review round 1 -----------------------------------------------------------------------


def test_clipped_table_cells_end_in_a_tilde_and_keep_their_width(
    records: list[dict[str, Any]],
) -> None:
    rec = dict(records[1], name="N" * 200, branch="b" * 100)
    lines = browser([rec]).render(24, 250)
    row = next(x for x in lines if x.style == "selected").text
    assert ("N" * (bm.MAX_CELL - 1) + "~") in row and ("b" * (bm.MAX_CELL - 1) + "~") in row
    header = lines[1].text
    assert bm.display_width(row) <= bm.display_width(header) + 3 * bm.MAX_CELL  # columns align
    assert "N" * bm.MAX_CELL not in row


def test_reload_returns_to_the_list_when_the_open_record_is_gone(
    records: list[dict[str, Any]],
) -> None:
    box = {"recs": records}
    b = bm.Browser(lambda: (box["recs"], []), FakeStarter(), now=lambda: NOW)
    keys(b, "enter")
    gone = records[0]["id"]
    box["recs"] = records[1:]
    keys(b, "r")
    out = text(b.render(24, 100))
    assert "TYPE" in out  # back in the list, not another record's detail
    assert f"{gone} is gone" in out
    assert b.selected_id() != gone


def test_reload_keeps_the_open_record_when_it_still_exists(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    keys(b, "enter", "r")
    out = text(b.render(40, 100))
    assert f"id: {records[0]['id']}" in out and "is gone" not in out


def test_reload_drops_finished_status_results_but_keeps_a_running_job(
    records: list[dict[str, Any]],
) -> None:
    starter = FakeStarter()
    b = browser(records, starter=starter)
    keys(b, "enter", "s")
    starter.handles[0].outcome = bm.StatusOutcome(True, ("marker-line",))
    b.tick()
    keys(b, "r")
    assert "marker-line" not in text(b.render(40, 100))
    keys(b, "s")  # runs again
    assert len(starter.handles) == 2
    keys(b, "r")  # a running job survives the reload
    assert "running" in text(b.render(40, 100))
    b.close()
    assert starter.handles[1].cancelled


def test_a_failed_reload_keeps_finished_status_results(records: list[dict[str, Any]]) -> None:
    from wtctl.errors import WtctlError

    starter = FakeStarter()
    fail = {"on": False}

    def loader() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if fail["on"]:
            raise WtctlError("internal", "boom")
        return records, []

    b = bm.Browser(loader, starter, now=lambda: NOW)
    keys(b, "enter", "s")
    starter.handles[0].outcome = bm.StatusOutcome(True, ("marker-line",))
    b.tick()
    fail["on"] = True
    keys(b, "r")
    assert "marker-line" in text(b.render(40, 100))


@pytest.mark.parametrize("ch", ["ᅟ", "ᅠ", "ᇿ", "ㅤ", "ﾠ", "⠀"])
def test_invisible_fillers_are_escaped(ch: str) -> None:
    out = bm.sanitize(f"a{ch}b", 50)
    assert out == f"a\\u{ord(ch):04x}b"


@pytest.mark.parametrize("ch", ["㉈", "㉏", "䷀", "䷿"])
def test_glibc_wide_characters_count_two_columns(ch: str) -> None:
    assert bm.char_width(ch) == 2
    assert bm.display_width("a" + ch) == 3


def test_resize_key_leaves_the_message_alone(records: list[dict[str, Any]]) -> None:
    from wtctl.errors import WtctlError

    calls = {"n": 0}

    def loader() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise WtctlError("internal", "boom")
        return records, []

    b = bm.Browser(loader, FakeStarter(), now=lambda: NOW)
    keys(b, "r", "resize")
    assert "reload failed" in text(b.render(24, 100))
    keys(b, "enter", "r", "resize")
    assert "reload failed" in text(b.render(24, 100))


# --- sanitizing, in more detail ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("a\x7fb", "a\\x7fb"),
        ("a͸b", "a\\u0378b"),  # unassigned
        ("é", "e\\u0301"),  # combining mark (Mn)
        ("a⃝b", "a\\u20ddb"),  # enclosing mark (Me)
        ("a\U000e0001b", "a\\U000e0001b"),  # tag (Cf), astral: 8 digits
        ("a\U000f0000b", "a\\U000f0000b"),  # private use, astral
    ],
)
def test_sanitize_escape_forms(raw: str, expect: str) -> None:
    assert bm.sanitize(raw, 50) == expect


def test_detail_title_and_body_are_sanitized(records: list[dict[str, Any]]) -> None:
    evil = "x\x1b[2J\x9b‮͸"
    rec = dict(
        records[0],
        id="wt-" + evil,
        name=evil,
        branch=evil,
        state=evil,
        labels=[evil + "=" + evil],
    )
    b = browser([rec])
    keys(b, "enter")
    lines = b.render(60, 200)
    assert "\\x1b[2J" in lines[0].text  # the title shows the escape visibly
    for ln in lines:
        for c in ln.text:
            assert c.isprintable(), repr(ln.text)
    assert "\\u202e" in text(lines) and "\\u0378" in text(lines)


def test_remove_hint_is_shell_quoted() -> None:
    rec = {
        "id": "wt-a b'c;d", "object_type": "worktree", "state": "active", "repo": "r/x/y",
        "name": "n", "ref": None, "branch": None, "created": "2026-01-01T00:00:00.000Z",
        "labels": [], "path": "/w/x",
    }  # fmt: skip
    b = browser([rec])
    keys(b, "enter")
    import shlex

    assert f"wtctl remove {shlex.quote(rec['id'])}" in text(b.render(40, 200))


def test_filter_hint_and_title_are_sanitized(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    b.filter_text = "a\x1b[2J"  # as if typed through a path that bypassed the key filter
    out = b.render(24, 100)
    assert all("\x1b" not in x.text for x in out)
    keys(b, "/")
    assert all("\x1b" not in x.text for x in b.render(24, 100))


def _many(n: int = 60) -> list[dict[str, Any]]:
    return [
        {"id": f"wt-{i:08d}", "object_type": "worktree", "state": "active", "repo": "r/x/y",
         "name": f"n{i}", "ref": None, "branch": None, "created": "2026-01-01T00:00:00.000Z",
         "labels": [], "path": f"/w/{i}"}
        for i in range(n)
    ]  # fmt: skip


def test_k_moves_up_and_page_keys_move_by_the_body_minus_one() -> None:
    b = browser(_many())
    b.render(20, 100)  # body = 17
    keys(b, "j", "j", "k")
    assert b.selected_id() == "wt-00000001"
    keys(b, "pgdn")
    assert b.selected_id() == f"wt-{1 + 16:08d}"
    keys(b, "pgup", "pgup")
    assert b.selected_id() == "wt-00000000"  # clamps at the top
    keys(b, "end", "pgdn")
    assert b.selected_id() == "wt-00000059"  # clamps at the bottom


def test_detail_page_and_jump_keys_scroll() -> None:
    rec = dict(_many(1)[0], last_error="E" * 3000)
    b = browser([rec])
    keys(b, "enter")
    top = text(b.render(10, 40))
    keys(b, "pgdn")
    assert text(b.render(10, 40)) != top
    keys(b, "G")
    bottom = text(b.render(10, 40))
    assert bottom != top
    keys(b, "pgup")
    assert text(b.render(10, 40)) != bottom
    keys(b, "g")
    assert text(b.render(10, 40)) == top


def test_filter_matches_the_path_only_there(records: list[dict[str, Any]]) -> None:
    recs = [dict(r, path="/w/unique-" + r["id"][-4:] + "-zz") for r in records]
    b = browser(recs)
    keys(b, "/", *"unique-bbbb", "enter")
    assert b.visible_ids() == ["wt-bbbbbbbb"]


def test_filter_lowercases_only_the_haystack_side_as_designed(
    records: list[dict[str, Any]],
) -> None:
    rec = dict(records[1], name="MixedCaseName")
    b = browser([rec])
    keys(b, "/", *"mixedcase", "enter")
    assert len(b.visible_ids()) == 1
    keys(b, "/", "esc", "/", *"MIXEDCASE", "enter")
    assert len(b.visible_ids()) == 1


def test_esc_in_the_list_clears_an_applied_filter(records: list[dict[str, Any]]) -> None:
    b = browser(records)
    keys(b, "/", *"beta", "enter")
    assert len(b.visible_ids()) == 1
    keys(b, "esc")
    assert len(b.visible_ids()) == 3 and b.filter_text == ""


def test_named_and_non_printable_keys_are_not_typed_into_the_filter(
    records: list[dict[str, Any]],
) -> None:
    b = browser(records)
    keys(b, "/", "down", "pgup", "enter" if False else "home", "\x00", "\t", "\x7f")
    assert b.filter_text == ""


def test_start_status_oserror_shows_cannot_start(records: list[dict[str, Any]]) -> None:
    def refuse(target: str) -> bm.StatusHandle:
        raise OSError("no exec")

    b = bm.Browser(lambda: (records, []), refuse, now=lambda: NOW)
    keys(b, "enter", "s")
    assert "cannot start" in text(b.render(40, 100))


def test_long_cells_are_clipped_to_max_cell(records: list[dict[str, Any]]) -> None:
    rec = dict(records[0], repo="example.com/o/" + "r" * 300)
    row = next(x for x in browser([rec]).render(24, 300) if x.style == "selected").text
    assert "r" * bm.MAX_CELL not in row


def test_foreign_entries_are_listed() -> None:
    foreign = [
        {"foreign": True, "path": "/w/other", "branch": "zed", "head": None, "clone_path": "/c"}
    ]
    out = text(browser([], foreign=foreign).render(24, 100))
    assert "foreign" in out and "zed" in out and "1/1" in out
