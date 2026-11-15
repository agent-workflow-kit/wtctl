"""Name, branch and path derivation (docs/interface.md, Identifiers > Names, Create order 7)."""

from __future__ import annotations

import re

import pytest

from wtctl import naming

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


@pytest.mark.parametrize(
    ("branch", "want"),
    [
        ("feature/login", "feature-login"),
        ("a//b", "a-b"),
        ("---x", "x"),
        (".hidden/x", "hidden-x"),
        ("-.-.x", "x"),
        ("fix_it.v2", "fix_it.v2"),
        ("naïve/résumé", "na-ve-r-sum-"),
        ("a%b", "a-b"),
        ("x" * 100, "x" * 64),
        ("///", ""),
        ("", ""),
    ],
)
def test_name_from_branch(branch: str, want: str) -> None:
    assert naming.name_from_branch(branch) == want


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "a/../b",
        "x\ny",
        "x\x00y",
        "‮evil",
        "é",
        "..",
        "a\\b",
        "${HOME}",
        "{name}",
        "💥/x",
        "\ud800x",
    ],
)
def test_derived_names_always_match_the_grammar(hostile: str) -> None:
    got = naming.name_from_branch(hostile)
    assert got == "" or NAME.fullmatch(got)


def test_derived_name_never_contains_a_separator_or_traversal() -> None:
    for hostile in ["../../x", "a/../b", "..", "...x", "a..b"]:
        got = naming.name_from_branch(hostile)
        assert "/" not in got
        assert got not in (".", "..")
        assert not got.startswith(".")


def test_name_from_id_is_the_eight_characters() -> None:
    assert naming.name_from_id("wt-abcdefg2") == "abcdefg2"


def test_suffix_fits_64_characters_by_cutting_the_base() -> None:
    base = "a" * 64
    got = naming.suffixed(base, 12)
    assert got.endswith("-12") and len(got) == 64 and NAME.fullmatch(got)
    assert naming.suffixed("abc", 2) == "abc-2"


def test_candidates_start_with_the_base_then_suffixes() -> None:
    it = naming.candidates("abc")
    assert [next(it) for _ in range(3)] == ["abc", "abc-2", "abc-3"]


def test_candidates_are_bounded() -> None:
    assert len(list(naming.candidates("abc"))) == naming.MAX_SUFFIX


def test_render_branch_prefix_and_template() -> None:
    got = naming.render_branch(
        "user/", "wt/{name}-{date}-{id}", name="n1", record_id="wt-abcdefg2", date="20260102"
    )
    assert got == "user/wt/n1-20260102-wt-abcdefg2"


def test_render_branch_keeps_unknown_braces_literally() -> None:
    got = naming.render_branch(
        "", "{evil.__class__}/{name}", name="n", record_id="wt-aaaaaaaa", date="d"
    )
    assert got == "{evil.__class__}/n"


def test_render_branch_substitutes_only_once() -> None:
    # a name cannot smuggle a placeholder (the grammar has no braces), but the renderer must not
    # re-expand its own output either
    got = naming.render_branch("", "{name}", name="a", record_id="wt-aaaaaaaa", date="d")
    assert got == "a"


def test_render_path_default_template() -> None:
    got = naming.render_path(
        "{root}/{host}/{path}/{name}",
        root="/r",
        host="example.test:8443",
        path="org/repo",
        name="n",
        record_id="wt-aaaaaaaa",
    )
    assert got == "/r/example.test_8443/org/repo/n"


def test_render_path_with_id() -> None:
    got = naming.render_path(
        "{root}/{id}", root="/r", host="h", path="p", name="n", record_id="wt-aaaaaaaa"
    )
    assert got == "/r/wt-aaaaaaaa"


@pytest.mark.parametrize("template", ["{root}/../x", "{root}/a/../../x", "{root}//{name}/.."])
def test_render_path_refuses_a_result_outside_the_root(template: str) -> None:
    with pytest.raises(ValueError):
        naming.render_path(
            template, root="/r", host="h", path="p", name="n", record_id="wt-aaaaaaaa"
        )


def test_render_path_refuses_a_result_that_is_the_root() -> None:
    with pytest.raises(ValueError):
        naming.render_path(
            "{root}/", root="/r", host="h", path="p", name="n", record_id="wt-aaaaaaaa"
        )


def test_render_path_refuses_a_newline() -> None:
    with pytest.raises(ValueError):
        naming.render_path(
            "{root}/a\nb", root="/r", host="h", path="p", name="n", record_id="wt-aaaaaaaa"
        )
