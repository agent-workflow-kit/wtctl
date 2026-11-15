"""A world for the removal-check tests: a created worktree and a ``status`` runner."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.createlib import CW
from tests.schemacheck import validate
from tests.test_create import ok
from wtctl import baselinesvc, removalfs, repobaseline
from wtctl.gateway import Gateway, ProcessInfo, ScannerUnavailable


class FakeScanner:
    """Processes the test says exist; ``fail`` makes the scan unavailable."""

    def __init__(self) -> None:
        self.procs: list[ProcessInfo] = []
        self.fail = False
        self.targets: list[str | None] = []

    def scan(self, target: str | None = None) -> list[ProcessInfo]:
        self.targets.append(target)
        if self.fail:
            raise ScannerUnavailable("no scanner")
        return list(self.procs)


@pytest.fixture
def inproc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)
    monkeypatch.setattr(removalfs, "RUNNER", repobaseline.inproc)


class RW(CW):
    """``CW`` with one created worktree."""

    scanner: FakeScanner

    def start(
        self, capsys: pytest.CaptureFixture[str], *argv: str, bootstrap: bool = False
    ) -> RW:
        self.build()
        return self.start_built(capsys, *argv, bootstrap=bootstrap)

    def start_built(
        self, capsys: pytest.CaptureFixture[str], *argv: str, bootstrap: bool = True
    ) -> RW:
        """Create the worktree in a world whose config the test has already set up."""
        self.scanner = FakeScanner()
        extra = [] if bootstrap else ["--no-bootstrap"]
        self.record = ok(self.create(capsys, "--branch", "work", *extra, *argv))["record"]
        self.rid = self.record["id"]
        self.wt = Path(self.record["path"])
        self.admin = Path(self.record["admin_dir"])
        return self

    def status(
        self, capsys: pytest.CaptureFixture[str], *argv: str, target: str | None = None,
        cmd: tuple[str, ...] = ("status",), human: bool = False,
    ) -> Any:  # fmt: skip
        import contextlib
        import io

        from tests.helpers import Run
        from wtctl.cli import main
        from wtctl.context import Runtime

        actor = ["--actor", "human:tester" if human else "auto:tester"]
        out, err = io.StringIO(), io.StringIO()
        rt = Runtime(
            stdin_isatty=False, me_name="alice", gateway=Gateway(scanner=self.scanner)
        )
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(
                ["--config", self.config(), "--json", *actor, *cmd, target or self.rid, *argv],
                runtime=rt,
            )
        return Run(code, out.getvalue(), err.getvalue())

    def st(self, capsys: pytest.CaptureFixture[str], *argv: str, **kw: Any) -> dict[str, Any]:
        r = self.status(capsys, *argv, **kw)
        assert r.code == 0, (r.out, r.err)
        data = r.json()
        validate("wtctl/cli/status.json", data)
        return data

    def db_exec(self, sql: str, *args: object) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def push_work(self, text: str = "w") -> str:
        """Commit in the worktree and push the branch to the remote."""
        (self.wt / f"{text}.txt").write_text(text)
        self.g(["add", "."], self.wt)
        self.g(["commit", "-q", "-m", text], self.wt)
        return self.g(["rev-parse", "HEAD"], self.wt)

    def push(self, branch: str = "work") -> None:
        self.g(["push", "-q", str(self.remote), f"{branch}:{branch}"], self.wt)
