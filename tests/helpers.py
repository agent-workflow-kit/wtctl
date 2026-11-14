"""Helpers to run the CLI in-process with fakes."""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest

from wtctl.cli import main
from wtctl.context import Result, Runtime


@dataclass
class Run:
    code: int
    out: str
    err: str

    def json(self) -> Any:
        return json.loads(self.out)


def run_cli(
    capsys: pytest.CaptureFixture[str],
    *argv: str,
    tty: bool = False,
    me: str = "alice",
) -> Run:
    code = main(list(argv), runtime=Runtime(stdin_isatty=tty, me_name=me))
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)


def install_handler(monkeypatch: pytest.MonkeyPatch, key: str, fn: Any) -> None:
    """Register ``fn`` as the handler module for command ``key`` (no shared file edited)."""
    module = types.ModuleType(f"wtctl.commands.{key}")
    module.run = fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"wtctl.commands.{key}", module)


__all__ = ["Result", "Run", "install_handler", "run_cli"]
