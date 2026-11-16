"""A real repository with a linked worktree, and the baseline computed in-process."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tests.bootlib import Repo
from wtctl import repobaseline


class World:
    def __init__(self, tmp: Path, home: Path, **spec_over: Any) -> None:
        self.repo = Repo(tmp, home)
        self.tmp = tmp
        self.home = home
        self.common = str(self.repo.main / ".git")
        st = os.stat(self.common)
        self.common_id = f"{st.st_dev}:{st.st_ino}"
        self.over = spec_over

    @property
    def wt(self) -> Path:
        return self.repo.wt

    def spec(self, **over: Any) -> repobaseline.Spec:
        base: dict[str, Any] = {
            "common_dir": self.common,
            "common_id": self.common_id,
            "admin_dir": self.repo.admin_dir,
            "worktree": str(self.repo.wt),
            "dir_id": self.repo.dir_id,
            "home": str(self.home),
        }
        return repobaseline.Spec(**{**base, **self.over, **over})

    def compute(self, **over: Any) -> repobaseline.Baseline:
        return repobaseline.compute(self.spec(**over), runner=repobaseline.inproc)

    def digest(self, **over: Any) -> str:
        return self.compute(**over).digest

    def cfg(self, text: str, name: str = "config") -> Path:
        p = Path(self.common) / name
        p.write_text(text)
        return p

    def append_cfg(self, text: str, name: str = "config") -> None:
        with open(Path(self.common) / name, "a") as fh:
            fh.write(text)


def find(items: list[repobaseline.Item], cls: str, suffix: str, key: str | None = None):  # noqa: ANN201
    return [i for i in items if i.cls == cls and i.path.endswith(suffix) and i.key == key]
