"""Shared fixtures: a private HOME/XDG tree for every test and no process hardening."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from wtctl import gateway


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point HOME and every XDG directory at a temp dir; tests never see the real ones."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    for name in [n for n in os.environ if n.startswith("WTCTL_")]:
        monkeypatch.delenv(name, raising=False)
    yield home


@pytest.fixture(autouse=True)
def no_dumpable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not make the pytest process non-dumpable; a dedicated test covers the real call."""
    monkeypatch.setattr(gateway, "disable_dumpable", lambda: True)
