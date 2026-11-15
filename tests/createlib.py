"""A world for ``create`` tests: a local bare remote, a clone, a config file and the CLI."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import Run, run_cli
from tests.vectorlib import plain_git

REPO = "example.test/org/repo"


class CW:
    def __init__(self, tmp: Path, home: Path) -> None:
        self.tmp = tmp
        self.home = home
        self.remote = tmp / "remote.git"
        self.work = tmp / "work"
        self.clone = tmp / "clone"
        self.root = tmp / "wt"
        self.top: dict[str, Any] = {"root": str(self.root), "lease_wait": "1s"}
        self.repo_cfg: dict[str, Any] = {}
        self.extra_args: list[str] = []
        self.shas: dict[str, str] = {}

    # -- git ------------------------------------------------------------------------------

    def g(self, args: list[str], cwd: Path | None = None, **kw: Any) -> str:
        r = plain_git(args, cwd=cwd or self.clone, home=self.home, **kw)
        assert r.returncode == 0, (args, r.stderr.decode())
        return r.stdout.decode().strip()

    def commit(self, name: str, cwd: Path) -> str:
        (cwd / f"{name}.txt").write_text(name)
        self.g(["add", "."], cwd)
        self.g(["commit", "-q", "-m", name], cwd)
        return self.g(["rev-parse", "HEAD"], cwd)

    def build(self) -> CW:
        """Remote with main (c1..c3) and ``feature/remote-only``; a clone with main and the local
        branch ``local-b`` (and its own ``refs/remotes``, which create never reads or writes)."""
        self.work.mkdir()
        self.g(["init", "-q", "-b", "main"], self.work)
        self.shas["c1"] = self.commit("c1", self.work)
        self.shas["c2"] = self.commit("c2", self.work)
        self.shas["c3"] = self.commit("c3", self.work)
        self.g(["checkout", "-q", "-b", "feature/remote-only"], self.work)
        self.shas["r1"] = self.commit("r1", self.work)
        self.g(["checkout", "-q", "main"], self.work)
        self.g(["clone", "-q", "--bare", str(self.work), str(self.remote)], self.tmp)
        self.g(["config", "uploadpack.allowFilter", "true"], self.remote)
        self.g(["clone", "-q", "--branch", "main", str(self.remote), str(self.clone)], self.tmp)
        self.g(["branch", "local-b", "main"])
        self.repo_cfg = {"clone": str(self.clone), "clone_url": str(self.remote)}
        return self

    def advance_remote(self, name: str = "n1") -> str:
        """A new commit on the remote's main that the clone does not have."""
        sha = self.commit(name, self.work)
        self.g(["push", "-q", str(self.remote), "main"], self.work)
        return sha

    # -- config and cli -------------------------------------------------------------------

    def config(self) -> str:
        data = {**self.top, "repos": {REPO: self.repo_cfg}}
        path = self.tmp / "config.yaml"
        path.write_text(json.dumps(data))
        return str(path)

    def cli(
        self, capsys: pytest.CaptureFixture[str], *argv: str, human: bool = False, json_out=True
    ) -> Run:
        actor = ["--actor", "human:tester"] if human else ["--actor", "auto:tester"]
        flags = ["--json"] if json_out else []
        return run_cli(capsys, "--config", self.config(), *flags, *actor, *argv)

    def create(self, capsys: pytest.CaptureFixture[str], *argv: str, **kw: Any) -> Run:
        return self.cli(capsys, "create", "--repo", REPO, *argv, **kw)

    # -- inspection -----------------------------------------------------------------------

    @property
    def db(self) -> Path:
        return self.home / ".local" / "state" / "wtctl" / "state.db"

    def rows(self, sql: str, *args: object) -> list[sqlite3.Row]:
        if not self.db.exists():
            return []
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def count(self, table: str) -> int:
        rows = self.rows(f"SELECT count(*) AS n FROM {table}")
        return rows[0]["n"] if rows else 0

    def records(self) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM records ORDER BY created, id")

    def refs(self) -> dict[str, str]:
        out = self.g(["for-each-ref", "--format=%(refname) %(objectname)"])
        return dict(line.split(" ", 1) for line in out.splitlines())

    def admin_entries(self) -> list[str]:
        d = self.clone / ".git" / "worktrees"
        return sorted(os.listdir(d)) if d.is_dir() else []

    def worktree_paths(self) -> list[str]:
        out = self.g(["worktree", "list", "--porcelain"])
        return [ln[len("worktree ") :] for ln in out.splitlines() if ln.startswith("worktree ")]

    def snapshot(self) -> dict[str, Any]:
        """Everything a failed create must leave as it found it."""
        return {
            "refs": self.refs(),
            "admin": self.admin_entries(),
            "worktrees": self.worktree_paths(),
            "config": (self.clone / ".git" / "config").read_text(),
            "dirs": sorted(
                str(p.relative_to(self.root)) for p in self.root.rglob("*")
            )
            if self.root.exists()
            else [],
        }


    def plant_creating(
        self, *, branch: str | None, name: str, holder: str | None = None, state: str = "creating"
    ) -> str:
        """A record as a crashed create leaves it (no worktree on disk)."""
        from wtctl.gateway import Gateway
        from wtctl.state import State

        st = State(self.db, Gateway())
        rid = st.allocate_id()
        path = str(self.root.resolve() / "worktrees" / "example.test" / "org" / "repo" / name)
        st.insert_record(
            {
                "id": rid, "object_type": "worktree", "state": state, "repo": REPO,
                "path": path, "name": name, "branch": branch, "owned": True,
                "clone_path": str(self.clone),
            },  # fmt: skip
            holders=[holder] if holder else [],
        )
        st.close()
        return rid

    def set_state(self, rid: str, state: str) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE records SET state = ? WHERE id = ?", (state, rid))
        conn.commit()
        conn.close()


def script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)
