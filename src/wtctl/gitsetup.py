"""Building the hardened :class:`~wtctl.hardgit.Git` of one invocation.

The binary is ``git_bin`` or the first absolute ``PATH`` entry with an executable ``git``, checked
once for version 2.34 or newer. Commands that run outside any repository (``clone``, ``init``)
need a fresh wtctl-private directory under ``${XDG_STATE_HOME:-~/.local/state}/wtctl/tmp/``
(:func:`private_dir`); its parent is also the ``HOME`` handed to git (the global config is
``/dev/null`` anyway, and ssh reads the passwd home).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import WtctlError
from .gateway import Gateway
from .hardgit import Git, check_version, resolve_binary
from .state import _prepare_dir, state_path


def tmp_root() -> Path:
    """The wtctl-private temp root (0700, owned by the user, not a symlink)."""
    root = state_path().parent / "tmp"
    _prepare_dir(root)
    return root


@contextmanager
def private_dir() -> Iterator[str]:
    """A fresh private directory ``P`` for git calls outside any repository; removed on exit."""
    root = tmp_root()
    path = tempfile.mkdtemp(prefix="p-", dir=root)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def open_git(config: Any, gateway: Gateway) -> Git:
    """The hardened git for this invocation (``git_too_old``, exit 12, below 2.34).

    Honored filter drivers (``load_filter_policy``) are wired by ``create`` itself; locate and
    clone never check out user content with filters.
    """
    git_bin = resolve_binary("git", os.environ.get("PATH", ""), config.git_bin)
    if git_bin is None:
        raise WtctlError("internal", "git not found: set git_bin or put git on an absolute PATH")
    home = str(tmp_root())
    with private_dir() as p:
        check_version(gateway, git_bin, home, p)
    return Git(gateway, git_bin, home)
