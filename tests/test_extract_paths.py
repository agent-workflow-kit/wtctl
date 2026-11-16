"""Extraction path rules (docs/interface.md, Extraction rules: Lists and Path validation)."""

from __future__ import annotations

import pytest

from wtctl import extraction


def err(raw: bytes) -> str | None:
    return extraction.check_path(raw)[1]


# -- lists -------------------------------------------------------------------------------------


def test_list_split_by_newline_when_there_is_no_nul() -> None:
    assert extraction.parse_list(b"a\nb/c\n\n\nd") == [b"a", b"b/c", b"d"]


def test_list_split_by_nul_keeps_newlines_inside_names() -> None:
    assert extraction.parse_list(b"a\nb\0c\0\0") == [b"a\nb", b"c"]


def test_empty_list_is_empty() -> None:
    assert extraction.parse_list(b"") == []
    assert extraction.parse_list(b"\n\n") == []
    assert extraction.parse_list(b"\0\0") == []


# -- valid paths -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"a",
        b"a/b/c.txt",
        b"dir/.gitignore",  # not .git
        b"dir/.gitattributes",
        b"x.git/y",
        b"git/y",
        b".github/workflows/ci.yml",
        b"sp ace/\xc3\xa9.txt",
        "日本語/ファイル".encode(),
        b"a" * 255,
        b"-leading-dash",
        b"~tilde",
        b"a.b.c",
    ],
)
def test_valid_paths(raw: bytes) -> None:
    parts, error = extraction.check_path(raw)
    assert error is None and parts is not None
    assert "/".join(parts).encode() == raw


# -- invalid_path ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"/abs",
        b"/",
        b"../x",
        b"a/../x",
        b"a/..",
        b"..",
        b".",
        b"./a",
        b"a/./b",
        b"a//b",
        b"a/",
        b"a\\b",
        b"\\a",
        b"C:\\x",
        b"a:b",
        b"a/b:c",
        b"a\x01b",
        b"a\x1fb",
        b"a\x7fb",
        b"a\tb",
        b"a\rb",
        b"a\nb",
        b"a\0b",
        b"\xff\xfe",  # not UTF-8
        b"a/\xc3",  # truncated UTF-8
        b"\xed\xa0\x80",  # a UTF-16 surrogate encoded in UTF-8
        b"a" * 256,  # component over 255 bytes
        b"/".join([b"abcdefghij"] * 410),  # path over 4096 bytes
        "é".encode() * 128,  # 256 bytes in one component
        b"a\xc2\x85b",  # U+0085 is a C1 control, but only U+0000-U+001F and U+007F are named
    ][:-1],
)
def test_invalid_paths(raw: bytes) -> None:
    assert err(raw) == "invalid_path"


def test_path_of_exactly_4096_bytes_is_valid() -> None:
    comp = b"a" * 100
    raw = b"/".join([comp] * 40) + b"/" + b"b" * (4096 - 40 * 101 - 1)
    assert len(raw) == 4096
    assert err(raw) is None
    assert err(raw + b"c") == "invalid_path"


def test_component_of_255_bytes_ok_256_not() -> None:
    assert err(b"a" * 255) is None
    assert err(b"a" * 256) == "invalid_path"
    assert err(("é" * 127).encode()) is None  # 254 bytes


# -- git_component -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ".git",
        ".GIT",
        ".Git",
        ".gIt",
        ".git.",
        ".git..",
        ".git ",
        ".git. .",
        "git~1",
        "GIT~1",
        "git~2",
        "git~12",
        ".git\u200c",  # zero-width non-joiner (HFS+ ignorable)
        ".g\u200dit",
        ".gi\ufefft",
        "\uff0egit",  # fullwidth full stop? compatibility form of '.'
        ".\uff47it",  # fullwidth g
        ".\u202eg\u202cit",
        ".git::$INDEX_ALLOCATION",
        ".git:stream",
    ],
)
@pytest.mark.parametrize("shape", ["{n}", "a/{n}", "{n}/x", "a/{n}/b/c"])
def test_dotgit_aliases_are_git_components(name: str, shape: str) -> None:
    assert err(shape.format(n=name).encode()) == "git_component"


def test_git_component_beats_other_rules_only_for_the_name() -> None:
    assert err(b"/.git") == "invalid_path"  # absolute is checked first
    assert err(b"a/../.git") == "invalid_path"


def test_similar_names_are_not_git_components() -> None:
    for name in (".gitx", "x.git", ".gi", "git", "git~", "gitx~1", ".git-x", ".g it", "dotgit"):
        assert err(name.encode()) is None, name


# -- duplicates --------------------------------------------------------------------------------


def lists(files: list[bytes], deletes: list[bytes] | None = None) -> extraction.Plan:
    return extraction.plan_lists(files, deletes or [], max_files=1000)


def errors(plan: extraction.Plan) -> dict[str, str]:
    return {e.shown: e.error for e in plan.entries if e.error}


def test_plain_lists_have_no_errors() -> None:
    plan = lists([b"a", b"b/c"], [b"d", b"e/f"])
    assert errors(plan) == {}
    assert [e.shown for e in plan.files] == ["a", "b/c"]
    assert [e.shown for e in plan.deletes] == ["d", "e/f"]


def test_same_path_twice_is_a_duplicate() -> None:
    assert errors(lists([b"a", b"a"])) == {"a": "duplicate"}


def test_same_path_in_both_lists_is_a_duplicate() -> None:
    assert errors(lists([b"a"], [b"a"])) == {"a": "duplicate"}


def test_case_folded_names_collide() -> None:
    plan = lists([b"Dir/File.TXT", b"dir/file.txt"])
    assert set(errors(plan).values()) == {"duplicate"} and len(errors(plan)) >= 1


def test_unicode_equivalent_names_collide() -> None:
    nfc = "caf\u00e9".encode()
    nfd = "cafe\u0301".encode()
    plan = lists([nfc, nfd])
    assert len(errors(plan)) >= 1 and set(errors(plan).values()) == {"duplicate"}


def test_ignorable_code_points_collide() -> None:
    plan = lists([b"ab", "a\u200bb".encode()])
    assert len(errors(plan)) >= 1


def test_trailing_dot_and_space_collide() -> None:
    plan = lists([b"name", b"name.", b"name "])
    assert len(errors(plan)) >= 2


def test_prefix_directory_of_another_path_is_a_duplicate() -> None:
    plan = lists([b"a", b"a/b"])
    assert set(errors(plan).values()) == {"duplicate"}
    assert set(errors(plan)) == {"a", "a/b"}


def test_prefix_across_lists_is_a_duplicate() -> None:
    plan = lists([b"a/b"], [b"a"])
    assert set(errors(plan)) == {"a", "a/b"}


def test_case_folded_prefix_is_a_duplicate() -> None:
    plan = lists([b"A", b"a/b"])
    assert set(errors(plan).values()) == {"duplicate"}


def test_invalid_entries_never_hide_the_others() -> None:
    plan = lists([b"ok", b"../bad", b".git/x", b"ok2"], [b"/abs"])
    assert errors(plan) == {"../bad": "invalid_path", ".git/x": "git_component", "/abs": "invalid_path"}


def test_shown_path_is_escaped_and_lossless_for_valid_utf8() -> None:
    plan = lists([b"a\x01b", b"\xff"])
    shown = [e.shown for e in plan.entries]
    assert shown == ["a\\x01b", "\\xff"]
    assert all("\n" not in s and "\x01" not in s for s in shown)


# -- limits ------------------------------------------------------------------------------------


def test_entries_over_max_files_are_limit_exceeded() -> None:
    plan = extraction.plan_lists([f"f{i}".encode() for i in range(5)], [], max_files=3)
    assert errors(plan) == {"f3": "limit_exceeded", "f4": "limit_exceeded"}


def test_deletions_have_their_own_count_limit() -> None:
    plan = extraction.plan_lists(
        [b"a"], [f"d{i}".encode() for i in range(4)], max_files=3
    )
    assert errors(plan) == {"d3": "limit_exceeded"}
