"""A world for ``remove`` tests: a created worktree and an in-process ``remove`` runner."""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import Run
from tests.removallib import RW
from tests.schemacheck import validate
from wtctl.cli import main
from wtctl.context import Runtime
from wtctl.gateway import Gateway


class RR(RW):
    """``RW`` with a ``remove`` runner (a human or an automation actor, a TTY or none)."""

    def rm(
        self,
        *argv: str,
        human: bool = False,
        tty: bool = False,
        stdin: str = "",
        target: str | None = None,
        yes: bool = True,
        config: str | None = None,
        mp: pytest.MonkeyPatch | None = None,
    ) -> Run:
        actor = ["--actor", "human:tester" if human else "auto:tester"]
        extra: dict[str, Any] = {} if self.git_runner is None else {"git_runner": self.git_runner}
        rt = Runtime(
            stdin_isatty=tty,
            me_name="alice",
            gateway=Gateway(scanner=self.scanner, **extra),
        )
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        flags = ["--yes"] if yes else []
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(
                    ["--config", config or self.config(), "--json", *actor, "remove",
                     target or self.rid, *flags, *argv],
                    runtime=rt,
                )  # fmt: skip
        finally:
            sys.stdin = old_stdin
        return Run(code, out.getvalue(), err.getvalue())

    def removed(self, *argv: str, **kw: Any) -> dict[str, Any]:
        r = self.rm(*argv, **kw)
        assert r.code == 0, (r.out, r.err)
        data = r.json()
        validate("wtctl/cli/remove.json", data)
        return data

    def refused(self, *argv: str, code: int = 20, **kw: Any) -> dict[str, Any]:
        r = self.rm(*argv, **kw)
        assert r.code == code, (r.code, r.out, r.err)
        obj = r.json()
        validate("wtctl/cli/error.json", obj)
        return obj["error"]

    def state_of(self) -> str:
        return self.rows("SELECT state FROM records WHERE id = ?", self.rid)[0]["state"]

    def intact(self) -> bool:
        return self.wt.is_dir() and self.admin.is_dir() and self.state_of() == "active"

    def audit_records(self) -> list[dict[str, Any]]:
        import json

        p = self.home / ".local" / "state" / "wtctl" / "audit.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def branches(self) -> list[str]:
        out = self.g(["for-each-ref", "--format=%(refname:short)", "refs/heads/"])
        return out.split()


def world(tmp_path: Path, home: Path) -> RR:
    return RR(tmp_path, home)
