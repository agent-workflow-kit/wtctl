"""A real repository with a linked worktree, for the bootstrap tests (no network)."""

from __future__ import annotations

import os
from pathlib import Path

from tests.vectorlib import GIT, plain_git
from wtctl.bootstrapper import open_worktree
from wtctl.gateway import Gateway
from wtctl.hardgit import Git

GITIGNORE = "node_modules/\n*.log\n.env\nbuild/\n.cache/\n"


class Repo:
    """``main`` (the clone) with tracked ``.gitignore``, ``README.md`` and ``src/app.py`` and
    a linked worktree ``wt`` on branch ``feat``."""

    def __init__(self, tmp: Path, home: Path) -> None:
        self.tmp = tmp
        self.home = home
        self.main = tmp / "main"
        self.wt = tmp / "wt"
        self.main.mkdir()
        self.g(["init", "-q", "-b", "main"], self.main)
        (self.main / ".gitignore").write_text(GITIGNORE)
        (self.main / "README.md").write_text("readme\n")
        (self.main / "src").mkdir()
        (self.main / "src" / "app.py").write_text("print(1)\n")
        self.g(["add", "."], self.main)
        self.g(["commit", "-q", "-m", "c1"], self.main)
        self.g(["worktree", "add", "-q", "-b", "feat", str(self.wt)], self.main)
        self.admin_dir = self.g(["rev-parse", "--absolute-git-dir"], self.wt)
        st = os.stat(self.wt)
        self.dir_id = f"{st.st_dev}:{st.st_ino}"
        self.git = Git(Gateway(), GIT, str(home))

    def g(self, args: list[str], cwd: Path | None = None) -> str:
        r = plain_git(args, cwd=cwd or self.wt, home=self.home)
        assert r.returncode == 0, (args, r.stderr.decode())
        return r.stdout.decode().strip()

    def open(self):  # noqa: ANN201
        return open_worktree(str(self.wt), self.admin_dir, self.dir_id)

    def write(self, rel: str, data: str | bytes = "x", *, root: Path | None = None) -> Path:
        p = (root or self.wt) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            p.write_text(data)
        else:
            p.write_bytes(data)
        return p
