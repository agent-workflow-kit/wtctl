"""A world for the reconcile tests: worktrees and throwaways, a fake clock and a reconcile runner."""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from pathlib import Path
from typing import Any

from tests.createlib import REPO
from tests.helpers import Run
from tests.schemacheck import validate
from tests.throwlib import TW
from wtctl.gateway import Gateway


class FakeClock:
    """Wall time the test moves by hand (forwards and backwards); monotonic time is real."""

    def __init__(self) -> None:
        self.t = dt.datetime.now(dt.UTC)

    def now(self) -> dt.datetime:
        return self.t

    def monotonic(self) -> float:
        return time.monotonic()

    def advance(self, seconds: float) -> None:
        self.t += dt.timedelta(seconds=seconds)


class RC(TW):
    """``TW`` plus worktree helpers and ``reconcile`` under a fake clock."""

    def __init__(self, tmp: Path, home: Path) -> None:
        super().__init__(tmp, home)
        self.clock = FakeClock()

    def build(self) -> RC:
        super().build()
        self.top["reconcile"] = {"missing_grace": "1h"}
        return self

    # -- running -----------------------------------------------------------------------------

    def gw(self) -> Gateway:
        extra: dict[str, Any] = {}
        if self.git_runner is not None:
            extra["git_runner"] = self.git_runner
        return Gateway(scanner=self.scanner, clock=self.clock, **extra)

    def rc(self, *argv: str, human: bool = False) -> Run:
        return self.run("reconcile", *argv, human=human, gateway=self.gw())

    def reconciled(self, *argv: str) -> dict[str, Any]:
        r = self.rc(*argv)
        assert r.code == 0, (r.out, r.err)
        data = r.json()
        validate("wtctl/cli/reconcile.json", data)
        return data

    # -- worktrees ---------------------------------------------------------------------------

    def wt(
        self, name: str = "one", *argv: str, branch: str | None = None, holder: str | None = None
    ) -> dict[str, Any]:
        extra = ["--holder", holder] if holder else []
        r = self.run(
            "create", "--repo", REPO, "--branch", branch or f"b-{name}", "--name", name,
            "--no-bootstrap", *extra, *argv,
        )  # fmt: skip
        assert r.code == 0, (r.out, r.err)
        return r.json()["record"]

    def row(self, rid: str) -> sqlite3.Row:
        return self.rows("SELECT * FROM records WHERE id = ?", rid)[0]

    def update(self, sql: str, *args: object) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def push_branch(self, wt: Path, branch: str) -> None:
        self.g(["push", "-q", str(self.remote), f"{branch}:{branch}"], wt)

    def vanish(self, rec: dict[str, Any]) -> None:
        """The worktree directory is deleted by hand (the admin entry stays)."""
        import shutil

        shutil.rmtree(rec["path"])

    def settle_missing(self, rid: str, runs: int = 2) -> None:
        """Run reconcile until the record has been seen missing by ``runs`` runs, with the clock
        moved past the grace period (1h) after the first."""
        for n in range(runs):
            self.reconciled()
            if n == 0:
                self.clock.advance(2 * 3600)

    @staticmethod
    def kinds(data: dict[str, Any]) -> list[str]:
        return [r["kind"] for r in data["reports"]]

    @staticmethod
    def actions(data: dict[str, Any]) -> list[tuple[str, str | None, str | None, str]]:
        return [(c["id"], c["from"], c["to"], c["action"]) for c in data["changes"]]
