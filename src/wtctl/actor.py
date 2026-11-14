"""The actor: who is acting, and whether that is a human (docs/interface.md, Audit and log)."""

from __future__ import annotations

import re
from dataclasses import dataclass

ACTOR_RE = re.compile(r"(human|auto):[A-Za-z0-9._@-]{1,64}")
_USER_BAD = re.compile(r"[^A-Za-z0-9._@-]")


class ActorError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    value: str

    @property
    def is_human(self) -> bool:
        return self.value.startswith("human:")


def parse_actor(value: str) -> Actor:
    # fullmatch: `$` would accept a trailing newline.
    if not ACTOR_RE.fullmatch(value):
        raise ActorError(
            f"invalid actor {value!r}: expected (human|auto):<name> with 1-64 of [A-Za-z0-9._@-]"
        )
    return Actor(value)


def sanitize_user(name: str) -> str:
    """Fit an OS user name into the actor grammar (invalid characters become ``_``)."""
    return _USER_BAD.sub("_", name)[:64] or "unknown"


def resolve_actor(explicit: str | None, *, stdin_isatty: bool, me_name: str) -> Actor:
    """An explicit ``--actor`` always wins; otherwise a TTY on stdin means ``human:<me.name>``."""
    if explicit is not None:
        return parse_actor(explicit)
    if stdin_isatty:
        return Actor(f"human:{sanitize_user(me_name)}")
    return Actor("auto:unknown")
