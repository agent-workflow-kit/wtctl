from __future__ import annotations

import pytest

from wtctl.errors import UsageError
from wtctl.labels import MAX_LABELS, merge_labels, parse_label, remove_labels, render


def test_parse_splits_at_first_equals() -> None:
    assert parse_label("k=v=w") == ("k", "v=w")


def test_empty_value_is_allowed() -> None:
    assert parse_label("k=") == ("k", "")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "novalue",
        "=v",
        "K=v",  # uppercase key
        "-k=v",
        ".k=v",
        "k k=v",
        "k=v v",
        "k=v\n",  # trailing newline must not slip through a `$` anchor
        "k\n=v",
        "k=v\x00",
        "k=é",  # non-ASCII value
        "é=v",
        "a" * 65 + "=v",
        "k=" + "v" * 257,
        "k=$(id)",
        "k=a;b",
        "k=a b",
        "k='",
    ],
)
def test_invalid_labels_are_usage_errors(bad: str) -> None:
    with pytest.raises(UsageError):
        parse_label(bad)


def test_boundaries_accepted() -> None:
    assert parse_label("a" * 64 + "=" + "v" * 256)[0] == "a" * 64
    assert parse_label("0=" + "A-z".replace("-", "") + "._:/#@+=,-") is not None


def test_render_sorted_unique() -> None:
    assert render([("b", "1"), ("a", "2"), ("a", "1"), ("b", "1")]) == ["a=1", "a=2", "b=1"]


def test_multiset_key_may_carry_several_values() -> None:
    assert merge_labels([("a", "1")], ["a=2"]) == {("a", "1"), ("a", "2")}


def test_adding_identical_pair_is_noop() -> None:
    assert merge_labels([("a", "1")], ["a=1", "a=1"]) == {("a", "1")}


def test_limit_is_32_pairs() -> None:
    cur = [(f"k{i}", "v") for i in range(MAX_LABELS)]
    assert len(merge_labels(cur, ["k0=v"])) == MAX_LABELS  # no-op at the limit is fine
    with pytest.raises(UsageError, match="32"):
        merge_labels(cur, ["extra=v"])


def test_limit_counts_distinct_new_pairs_only() -> None:
    cur = [(f"k{i}", "v") for i in range(MAX_LABELS - 1)]
    assert len(merge_labels(cur, ["x=1", "x=1"])) == MAX_LABELS
    with pytest.raises(UsageError):
        merge_labels(cur, ["x=1", "x=2"])


def test_validation_happens_before_any_change() -> None:
    cur = [("a", "1")]
    with pytest.raises(UsageError):
        merge_labels(cur, ["b=2", "BAD"])
    assert cur == [("a", "1")]


def test_remove_absent_pair_is_noop_and_key_alone_is_invalid() -> None:
    assert remove_labels({("a", "1"), ("a", "2")}, ["a=1", "z=9"]) == {("a", "2")}
    with pytest.raises(UsageError):
        remove_labels({("a", "1")}, ["a"])
