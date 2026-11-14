"""Validate golden outputs against schemas/ with a registry keyed by each schema's $id."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


@cache
def _registry() -> Registry:
    resources = []
    for path in sorted(SCHEMAS.rglob("*.json")):
        doc = json.loads(path.read_text())
        resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


def validate(schema_id: str, instance: Any) -> None:
    """Raise jsonschema.ValidationError when ``instance`` does not match ``schema_id``."""
    registry = _registry()
    schema = registry.get_or_retrieve(schema_id).value.contents
    Draft202012Validator(schema, registry=registry).validate(instance)
