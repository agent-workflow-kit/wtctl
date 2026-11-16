"""A world for ``extract`` tests: a source throwaway an "agent" wrote into, and destinations."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tests.helpers import Run
from tests.schemacheck import validate
from tests.throwlib import TW


class XW(TW):
    def __init__(self, tmp: Path, home: Path) -> None:
        super().__init__(tmp, home)
        self.lists_dir = tmp / "lists"
        self.lists_dir.mkdir()
        self.n = 0

    def build(self) -> XW:
        super().build()
        return self

    # -- fixtures ---------------------------------------------------------------------------

    def src(self, *argv: str) -> dict[str, Any]:
        return self.made("--source", "local", *argv)

    def plain_dest(self, name: str = "dest") -> Path:
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        return d

    @staticmethod
    def put(
        rec: dict[str, Any] | Path, rel: str, data: bytes = b"data\n", mode: int = 0o600
    ) -> Path:
        base = Path(rec if isinstance(rec, Path) else rec["path"])
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(mode)
        return p

    # -- running ----------------------------------------------------------------------------

    def listfile(self, entries: list[str | bytes], nul: bool = False) -> str:
        self.n += 1
        raw = [e.encode() if isinstance(e, str) else e for e in entries]
        sep = b"\0" if nul else b"\n"
        f = self.lists_dir / f"list{self.n}"
        f.write_bytes(sep.join(raw) + (sep if raw else b""))
        return str(f)

    def ex(
        self,
        rec: dict[str, Any] | str,
        dest: Path | str,
        files: list[str | bytes] | None = None,
        deletes: list[str | bytes] | None = None,
        *argv: str,
        nul: bool = False,
        human: bool = False,
    ) -> Run:
        target = rec if isinstance(rec, str) else rec["id"]
        args = ["extract", target, "--dest", str(dest)]
        if files is not None:
            args += ["--files-from", self.listfile(files, nul)]
        if deletes is not None:
            args += ["--delete-from", self.listfile(deletes, nul)]
        return self.run(*args, *argv, human=human)

    def extracted(self, *a: Any, **kw: Any) -> dict[str, Any]:
        r = self.ex(*a, **kw)
        assert r.code == 0, (r.out, r.err)
        data = r.json()
        validate("wtctl/cli/extract.json", data)
        return data

    def invalid(self, *a: Any, **kw: Any) -> dict[str, str]:
        """The per-path errors of an ``extract_invalid`` failure (shown path -> error)."""
        r = self.ex(*a, **kw)
        e = self.failed(r, 12)
        assert e["code"] == "extract_invalid", e
        return {p["path"]: p["error"] for p in e["details"]["paths"]}


def tree(root: Path) -> dict[str, str]:
    """Every entry below ``root`` (relative path -> kind or content), without following links."""
    out: dict[str, str] = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            p = Path(dp) / n
            rel = str(p.relative_to(root))
            if p.is_symlink():
                out[rel] = "link:" + os.readlink(p)
            elif p.is_dir():
                out[rel] = "dir"
            else:
                out[rel] = p.read_text(errors="replace")
    return out
