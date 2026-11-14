from __future__ import annotations

import pytest

from wtctl.actor import Actor, ActorError, parse_actor, resolve_actor, sanitize_user


@pytest.mark.parametrize(
    "value",
    ["human:alice", "auto:cron", "auto:unknown", "human:a.b_c@d-e", "human:" + "a" * 64],
)
def test_valid_actors(value: str) -> None:
    assert parse_actor(value).value == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "human:",
        "human",
        "robot:x",
        "Human:x",
        "human:" + "a" * 65,
        "human:a b",
        "human:a/b",
        "human:a:b",
        "human:é",
        "human:a\n",
        "auto:x\nhuman:y",
        " human:x",
    ],
)
def test_invalid_actors(value: str) -> None:
    with pytest.raises(ActorError):
        parse_actor(value)


def test_is_human_follows_the_prefix() -> None:
    assert parse_actor("human:x").is_human
    assert not parse_actor("auto:x").is_human


def test_explicit_actor_wins_over_tty() -> None:
    assert resolve_actor("auto:bot", stdin_isatty=True, me_name="alice") == Actor("auto:bot")
    assert resolve_actor("human:bob", stdin_isatty=False, me_name="alice") == Actor("human:bob")


def test_default_with_tty_is_human_me_name() -> None:
    assert resolve_actor(None, stdin_isatty=True, me_name="alice") == Actor("human:alice")


def test_default_without_tty_is_auto_unknown() -> None:
    assert resolve_actor(None, stdin_isatty=False, me_name="alice") == Actor("auto:unknown")


def test_me_name_outside_the_grammar_is_sanitized() -> None:
    assert sanitize_user("al ice/é") == "al_ice__"
    assert sanitize_user("x" * 100) == "x" * 64
    assert sanitize_user("") == "unknown"
    assert resolve_actor(None, stdin_isatty=True, me_name="a b").value == "human:a_b"
