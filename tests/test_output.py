from __future__ import annotations

import json

from tests.schemacheck import validate
from wtctl.errors import WtctlError
from wtctl.output import escape_control, render_error, render_json, render_table


def test_escape_control_characters() -> None:
    assert escape_control("a\x1b[31mred\x1b[0m") == "a\\x1b[31mred\\x1b[0m"
    assert escape_control("line1\nline2\ttab\r") == "line1\\x0aline2\\x09tab\\x0d"
    assert escape_control("nul\x00del\x7f c1\x9b") == "nul\\x00del\\x7f c1\\x9b"


def test_escape_leaves_printable_and_unicode_alone() -> None:
    assert escape_control("plain /path-é-日本") == "plain /path-é-日本"


def test_escape_bidi_line_separators_and_lone_surrogates() -> None:
    assert escape_control("a‮b") == "a\\u202eb"
    assert escape_control("a b") == "a\\u2028b"
    assert escape_control("a\udcffb") == "a\\udcffb"


def test_table_escapes_cells_and_aligns() -> None:
    text = render_table(["id", "name"], [["wt-1", "evil\x1b[2Jname"], ["wt-22", "ok"]])
    lines = text.splitlines()
    assert "\x1b" not in text
    assert lines[0].split() == ["id", "name"]
    assert lines[1].startswith("wt-1 ") and "evil\\x1b[2Jname" in lines[1]
    assert lines[2].startswith("wt-22")
    assert len(lines) == 3


def test_table_cell_newline_does_not_add_rows() -> None:
    assert len(render_table(["a"], [["x\ny"]]).splitlines()) == 2


def test_render_json_is_one_line_and_stable() -> None:
    text = render_json({"b": 1, "a": "é\x1b"})
    assert text.count("\n") == 0
    assert json.loads(text) == {"b": 1, "a": "é\x1b"}


def test_render_error_json_golden() -> None:
    err = WtctlError("refused", "dirty tree\x1b[2J", reason="dirty")
    out, errtext = render_error(err, as_json=True)
    obj = json.loads(out)
    validate("wtctl/cli/error.json", obj)
    assert obj["error"]["message"] == "dirty tree\x1b[2J"  # JSON carries the raw value
    assert "\x1b" not in errtext
    assert "dirty tree" in errtext


def test_render_error_human_has_no_stdout() -> None:
    out, errtext = render_error(WtctlError("not_found", "no such record"), as_json=False)
    assert out == ""
    assert errtext.strip() == "wtctl: error: no such record"
