"""Run inside ``unshare -Urm``: a real tmpfs placed on the worktree directory after create's
mount-point check (issue #83).

Not a test module. Usage: ``python unplaced_userns_scenario.py <workdir> <variant>``; prints JSON.
Variants: ``after`` (mounted after git added the worktree, so its files are hidden below the
mount), ``before`` (mounted just before the add, so its files live in the mount).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    from tests.createlib import REPO
    from tests.throwlib import TW
    from wtctl import baselinesvc, repobaseline, worktrees

    baselinesvc.RUNNER = repobaseline.inproc
    work, variant = Path(sys.argv[1]), sys.argv[2]
    home = work / "home"
    home.mkdir()
    for k, v in {
        "HOME": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_STATE_HOME": home / ".local" / "state",
        "XDG_DATA_HOME": home / ".local" / "share",
        "XDG_CACHE_HOME": home / ".cache",
    }.items():
        os.environ[k] = str(v)
    w = TW(work, home).build()
    target = w.root / "worktrees" / "example.test" / "org" / "repo" / "one"
    real = worktrees.worktree_add

    def mount() -> None:
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["mount", "-t", "tmpfs", "none", str(target)], check=True, capture_output=True
        )

    def add(git, common, spec):  # noqa: ANN001, ANN202
        if variant == "before":
            mount()
        real(git, common, spec)
        if variant == "after":
            mount()

    worktrees.worktree_add = add
    first = w.run("create", "--repo", REPO, "--name", "one", "--no-bootstrap")
    worktrees.worktree_add = real

    def refs() -> list[str]:
        return [k for k in w.refs() if k.startswith("refs/heads/wt/")]

    def snap(label: str) -> dict:
        row = w.records()[0] if w.records() else None
        return {
            "state": row["state"] if row else None,
            "dir_id": row["dir_id"] if row else None,
            "admin": w.admin_entries(),
            "branches": refs(),
            "mount_entries": sorted(os.listdir(target)) if target.is_dir() else None,
        }

    out: dict = {"create": {"code": first.code, "error": first.json().get("error")}}
    out["after_create"] = snap("create")
    rid = w.records()[0]["id"]
    rec = w.run("reconcile")
    out["reconcile"] = {"code": rec.code, "changes": rec.json()["changes"]}
    out["after_reconcile"] = snap("reconcile")
    rm = w.run("remove", rid, "--yes", human=True)
    out["remove"] = {"code": rm.code, "error": rm.json().get("error")}
    out["after_remove"] = snap("remove")
    rm2 = w.run("remove", rid, "--yes", human=True)
    out["remove_again"] = {"code": rm2.code}
    out["after_remove_again"] = snap("remove_again")
    fg = w.run("forget", rid, human=True)
    out["forget"] = {"code": fg.code}
    out["after_forget"] = snap("forget")
    print(json.dumps(out))


main()
