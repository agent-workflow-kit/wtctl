"""Run inside ``unshare -Urm``: the mount of issue #83 goes away before ``remove`` runs.

Not a test module. Usage: ``python unplaced_userns_unmount_scenario.py <workdir>``; prints JSON.
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
    work = Path(sys.argv[1])
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

    def add(git, common, spec):  # noqa: ANN001, ANN202
        real(git, common, spec)
        subprocess.run(
            ["mount", "-t", "tmpfs", "none", str(target)], check=True, capture_output=True
        )

    worktrees.worktree_add = add
    first = w.run("create", "--repo", REPO, "--name", "one", "--no-bootstrap")
    worktrees.worktree_add = real
    rid = w.records()[0]["id"]
    before = w.records()[0]["state"]
    subprocess.run(["umount", str(target)], check=True, capture_output=True)
    rm = w.run("remove", rid, "--yes", human=True)
    out = {
        "create": first.code,
        "state_before": before,
        "remove": rm.code,
        "state": w.records()[0]["state"],
        "admin": w.admin_entries(),
        "dir_exists": target.exists(),
        "branches": [k for k in w.refs() if k.startswith("refs/heads/wt/")],
    }
    print(json.dumps(out))


main()
