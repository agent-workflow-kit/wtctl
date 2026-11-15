"""A world for mirror tests: a local bare remote, a clone, state, config and a hardened git."""

from __future__ import annotations

import contextlib
import os
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tests.vectorlib import GIT, plain_git
from wtctl.config import Config
from wtctl.gateway import Gateway
from wtctl.hardgit import Git
from wtctl.mirror import Mirror
from wtctl.state import State, state_path

REPO = "example.test/org/repo"


def forge_loose_object(git_dir: Path, claimed_sha: str, kind: bytes, content: bytes) -> None:
    """Store ``content`` as a loose object under ``claimed_sha`` (not its hash): what an agent
    that can write an object store can do."""
    d = git_dir / "objects" / claimed_sha[:2]
    d.mkdir(parents=True, exist_ok=True)
    target = d / claimed_sha[2:]
    with contextlib.suppress(FileNotFoundError):
        os.chmod(target, 0o644)
        target.unlink()
    target.write_bytes(zlib.compress(b"%s %d\0" % (kind, len(content)) + content))


@dataclass
class World:
    tmp: Path
    home: Path
    gateway: Gateway = field(default_factory=Gateway)
    cfg: Config = field(default_factory=Config)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.remote = self.tmp / "remote.git"
        self.work = self.tmp / "work"
        self.clone = self.tmp / "clone"
        self.state = State(state_path(), Gateway())

    def g(self, args: list[str], cwd: Path | None = None) -> str:
        r = plain_git(args, cwd=cwd or self.work, home=self.home)
        assert r.returncode == 0, (args, r.stderr.decode())
        return r.stdout.decode().strip()

    def commit(self, name: str, cwd: Path | None = None) -> str:
        cwd = cwd or self.work
        (cwd / f"{name}.txt").write_text(name)
        self.g(["add", "."], cwd)
        self.g(["commit", "-q", "-m", name], cwd)
        return self.g(["rev-parse", "HEAD"], cwd)

    def build(self, n: int = 3) -> list[str]:
        """A remote with ``n`` commits on main (filtering allowed), cloned to ``clone``."""
        self.work.mkdir()
        self.g(["init", "-q", "-b", "main"])
        shas = [self.commit(f"c{i}") for i in range(1, n + 1)]
        self.g(["clone", "-q", "--bare", str(self.work), str(self.remote)], self.tmp)
        self.g(["config", "uploadpack.allowFilter", "true"], self.remote)
        self.g(["remote", "add", "origin", str(self.remote)])
        self.g(["clone", "-q", str(self.remote), str(self.clone)], self.tmp)
        self.record()
        return shas

    def record(self, repo: str = REPO, clone: Path | None = None, url: str | None = None) -> None:
        clone = clone or self.clone
        self.state.put_clone(
            repo, path=str(clone), common_dir=str(clone / ".git"),
            fetch_url=url or str(self.remote),
        )  # fmt: skip

    def git(self) -> Git:
        return Git(self.gateway, GIT, str(self.home))

    def mirror(
        self,
        *,
        repo: str = REPO,
        clone: Path | None = None,
        url: str | None = None,
        environ: dict[str, str] | None = None,
    ) -> Mirror:
        clone = clone or self.clone
        return Mirror(
            git=self.git(), cfg=self.cfg, state=self.state, gateway=self.gateway, repo=repo,
            clone_path=str(clone), common_dir=str(clone / ".git"),
            fetch_url=url or str(self.remote), environ=environ or {"PATH": "/usr/bin:/bin"},
            warn=self.warnings.append,
        )  # fmt: skip

    def refresh(self, m: Mirror):  # noqa: ANN201
        return m.refresh_locked()


FakeRunner = Callable[..., object]
