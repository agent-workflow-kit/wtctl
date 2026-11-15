"""A world for ``adopt`` and ``forget`` tests: foreign worktrees next to the ``create`` world."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.createlib import CW, REPO
from tests.helpers import Run
from tests.schemacheck import validate


def foreign(w: CW, name: str = "ext", branch: str | None = None) -> Path:
    """A linked worktree made by plain git, outside wtctl's root."""
    base = w.tmp / "foreign"
    base.mkdir(exist_ok=True)
    path = (base / name).resolve()
    w.g(["worktree", "add", "-q", "-b", branch or f"b-{name}", str(path)])
    return path


def adopt(w: CW, capsys, path: Path | str, *argv: str, repo: bool = True, **kw) -> Run:  # noqa: ANN001, ANN003
    extra = ["--repo", REPO] if repo else []
    return w.cli(capsys, "adopt", str(path), *extra, *argv, **kw)


def ok_adopt(r: Run) -> dict:
    assert r.code == 0, (r.out, r.err)
    data = r.json()
    validate("wtctl/cli/adopt.json", data)
    return data


def ok_forget(r: Run) -> dict:
    assert r.code == 0, (r.out, r.err)
    data = r.json()
    validate("wtctl/cli/forget.json", data)
    return data


def err_of(r: Run, code: int, reason: str | None = None) -> dict:
    assert r.code == code, (r.out, r.err)
    validate("wtctl/cli/error.json", r.json())
    err = r.json()["error"]
    if reason is not None:
        assert err["reason"] == reason
    return err


def disk_snapshot(w: CW) -> dict:
    """Everything `forget` must leave alone."""

    def tree(p: Path) -> list[str]:
        out = []
        for root, dirs, files in os.walk(p):
            out += [os.path.join(root, n) for n in sorted(dirs) + sorted(files)]
        return sorted(out)

    return {
        "refs": w.refs(),
        "admin": w.admin_entries(),
        "worktrees": w.worktree_paths(),
        "config": (w.clone / ".git" / "config").read_text(),
        "tree": tree(w.tmp / "foreign") if (w.tmp / "foreign").exists() else [],
        "clone": tree(w.clone / ".git" / "worktrees"),
    }


def tombstone(w: CW, rid: str, state: str) -> None:
    """Set a tombstone state with the removal time `transition` would set."""
    import sqlite3

    conn = sqlite3.connect(w.db)
    conn.execute(
        "UPDATE records SET state = ?, removed_at = '2026-01-01T00:00:00.000Z' WHERE id = ?",
        (state, rid),
    )
    conn.commit()
    conn.close()


def world(tmp_path: Path, home: Path) -> CW:
    return CW(tmp_path, home).build()


def inproc_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the baseline collectors in this process (the child path has its own test)."""
    from wtctl import baselinesvc, repobaseline

    monkeypatch.setattr(baselinesvc, "RUNNER", repobaseline.inproc)
