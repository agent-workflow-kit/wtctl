"""A world for throwaway tests: ``CW`` plus helpers for throwaways, hostile objects and prune."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sqlite3
import zlib
from pathlib import Path
from typing import Any

from tests.createlib import CW, REPO
from tests.helpers import Run
from tests.removallib import FakeScanner
from tests.schemacheck import validate
from tests.vectorlib import plain_git
from wtctl.cli import main
from wtctl.context import Runtime
from wtctl.gateway import Gateway


class TW(CW):
    """``CW`` with throwaway helpers. The remote is ``self.remote``, the clone ``self.clone``."""

    scanner: FakeScanner | None = None
    git_runner: Any = None

    # -- running ----------------------------------------------------------------------------

    def run(self, *argv: str, human: bool = False, gateway: Gateway | None = None) -> Run:
        """Any command in-process, with the fake scanner when the test set one."""
        actor = ["--actor", "human:tester" if human else "auto:tester"]
        extra: dict[str, Any] = {}
        if self.scanner is not None:
            extra["scanner"] = self.scanner
        if self.git_runner is not None:
            extra["git_runner"] = self.git_runner
        gw = gateway or Gateway(**extra)
        out, err = io.StringIO(), io.StringIO()
        rt = Runtime(stdin_isatty=False, me_name="alice", gateway=gw)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--config", self.config(), "--json", *actor, *argv], runtime=rt)
        return Run(code, out.getvalue(), err.getvalue())

    def throw(self, *argv: str, ref: str | None = None, **kw: Any) -> Run:
        return self.run("create", "--throwaway", "--repo", REPO, "--ref", ref or self.shas["c3"],
                        *argv, **kw)  # fmt: skip

    def made(self, *argv: str, **kw: Any) -> dict[str, Any]:
        r = self.throw(*argv, **kw)
        assert r.code == 0, (r.out, r.err)
        data = r.json()
        validate("wtctl/cli/create.json", data)
        return data["record"]

    def failed(self, r: Run, code: int) -> dict[str, Any]:
        assert r.code == code, (r.code, r.out, r.err)
        obj = r.json()
        validate("wtctl/cli/error.json", obj)
        return obj["error"]

    def rm(self, target: str, *argv: str, **kw: Any) -> Run:
        return self.run("remove", target, "--yes", *argv, **kw)

    def removed(self, target: str, *argv: str, **kw: Any) -> dict[str, Any]:
        r = self.rm(target, *argv, **kw)
        assert r.code == 0, (r.out, r.err)
        data = r.json()
        validate("wtctl/cli/remove.json", data)
        return data

    def prune(self, *argv: str, **kw: Any) -> Run:
        return self.run("prune", "--throwaways", *argv, **kw)

    def pruned(self, *argv: str, **kw: Any) -> dict[str, Any]:
        r = self.prune(*argv, **kw)
        assert r.code == 0, (r.out, r.err)
        data = r.json()
        validate("wtctl/cli/prune.json", data)
        return data

    # -- inspection -------------------------------------------------------------------------

    @property
    def troot(self) -> Path:
        return self.root.resolve() / "throwaway"

    def state_of(self, rid: str) -> str:
        return self.rows("SELECT state FROM records WHERE id = ?", rid)[0]["state"]

    def tg(self, path: str | Path, *args: str, check: bool = True) -> str:
        """Unhardened git against the throwaway at ``path`` (an explicit git dir)."""
        p = Path(path)
        r = plain_git(["--git-dir", str(p / ".git"), "--work-tree", str(p), *args], cwd=p,
                      home=self.home)  # fmt: skip
        if check:
            assert r.returncode == 0, (args, r.stderr.decode())
        return r.stdout.decode().strip()

    def age(self, rid: str, days: float) -> None:
        """Pretend the record was created ``days`` ago."""
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE records SET created = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) WHERE id = ?",
            (f"-{days} days", rid),
        )
        conn.commit()
        conn.close()

    def log_events(self) -> list[dict[str, Any]]:
        import json

        f = self.home / ".local" / "state" / "wtctl" / "log.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def audit_records(self) -> list[dict[str, Any]]:
        import json

        f = self.home / ".local" / "state" / "wtctl" / "audit.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def leases(self) -> list[str]:
        return [r["name"] for r in self.rows("SELECT name FROM leases")]


# -- raw git objects (hostile content that porcelain would refuse to build) ---------------------


def write_object(git_dir: Path, kind: str, body: bytes) -> str:
    """Write a loose object directly: no validation, so any tree can be planted."""
    raw = f"{kind} {len(body)}".encode() + b"\0" + body
    sha = hashlib.sha1(raw).hexdigest()  # noqa: S324 - git object ids
    d = git_dir / "objects" / sha[:2]
    d.mkdir(parents=True, exist_ok=True)
    f = d / sha[2:]
    if not f.exists():
        f.write_bytes(zlib.compress(raw))
        f.chmod(0o444)
    return sha


def tree_body(entries: list[tuple[str, str, str]]) -> bytes:
    """``(mode, name, sha)`` entries, sorted the way git sorts them (directories with a slash)."""

    def key(e: tuple[str, str, str]) -> bytes:
        return e[1].encode() + (b"/" if e[0] == "40000" else b"")

    out = b""
    for mode, name, sha in sorted(entries, key=key):
        out += f"{mode} {name}".encode() + b"\0" + bytes.fromhex(sha)
    return out


def commit_of(git_dir: Path, tree: str, parent: str | None = None, msg: str = "hostile") -> str:
    lines = [f"tree {tree}"]
    if parent:
        lines.append(f"parent {parent}")
    ident = "t <t@example.invalid> 1700000000 +0000"
    lines += [f"author {ident}", f"committer {ident}", "", msg, ""]
    return write_object(git_dir, "commit", "\n".join(lines).encode())


def blob(git_dir: Path, data: bytes) -> str:
    return write_object(git_dir, "blob", data)


def ref(git_dir: Path, name: str, sha: str) -> None:
    p = git_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(sha + "\n")


def leaks(root: Path) -> list[str]:
    """Names below ``root`` (a directory the test watches for stray writes)."""
    if not root.exists():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def markers(path: Path) -> list[str]:
    return sorted(os.listdir(path)) if path.exists() else []


def secret_free(text: str, *secrets: str) -> bool:
    return not any(s in text for s in secrets)
