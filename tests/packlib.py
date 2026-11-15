"""Helpers for the pack-transfer tests: a lagging clone, a mirror, hostile packs."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from tests.mirrorlib import World
from tests.vectorlib import plain_git
from wtctl import fsafe
from wtctl.gateway import Gateway, GitResult, run_git
from wtctl.gitsetup import tmp_root
from wtctl.mirror import Mirror


def lagging(w: World, extra: int = 2) -> tuple[list[str], list[str]]:
    """A remote and a clone; the remote then gets ``extra`` more commits the clone lacks."""
    old = w.build(3)
    new = [w.commit(f"n{i}") for i in range(extra)]
    w.g(["push", "-q", "origin", "HEAD:main"])
    return old, new


def common_id(w: World) -> fsafe.Identity:
    st = os.stat(w.clone / ".git")
    return fsafe.Identity(st.st_dev, st.st_ino)


def refreshed(w: World) -> Mirror:
    m = w.mirror()
    w.refresh(m)
    return m


def cg(w: World, args: list[str], *, cwd: Path | None = None, stdin: bytes | None = None) -> str:
    """Unhardened git in the clone (the test's own view)."""
    r = plain_git(["--git-dir", str(w.clone / ".git"), *args], cwd=cwd or w.tmp, home=w.home,
                  stdin=stdin)  # fmt: skip
    assert r.returncode == 0, (args, r.stderr.decode())
    return r.stdout.decode().strip()


def pack_dir_entries(w: World) -> list[str]:
    return sorted(os.listdir(w.clone / ".git" / "objects" / "pack"))


def git_tree_snapshot(git_dir: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every file below ``git_dir`` except objects/pack: mtime_ns and content."""
    snap: dict[str, tuple[int, bytes | None]] = {}
    for root, dirs, files in os.walk(git_dir):
        rel = os.path.relpath(root, git_dir)
        if rel == os.path.join("objects", "pack"):
            dirs[:] = []
            continue
        for f in files:
            p = Path(root, f)
            st = p.lstat()
            snap[str(Path(rel, f))] = (
                st.st_mtime_ns,
                p.read_bytes() if stat.S_ISREG(st.st_mode) else None,
            )
    return snap


def tmp_entries() -> list[str]:
    return sorted(os.listdir(tmp_root()))


class Recorder:
    """A git runner that records every argv and can fail chosen commands."""

    def __init__(self, fail: str | None = None, env_override: dict[str, str] | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, str], str]] = []
        self.fail = fail
        self.env_override = env_override or {}

    def __call__(self, argv: list[str], **kw: object) -> GitResult:
        env: dict[str, str] = dict(kw["env"])  # type: ignore[call-overload]  # noqa: E501
        self.calls.append((list(argv), env, str(kw["cwd"])))
        if self.fail is not None and self.fail in argv:
            return GitResult(128, b"", b"fatal: injected failure\n")
        env.update(self.env_override)
        kw["env"] = env
        return run_git(argv, **kw)  # type: ignore[arg-type]

    def gateway(self) -> Gateway:
        return Gateway(git_runner=self)
