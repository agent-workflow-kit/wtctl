"""Run inside ``unshare -Urm``: a real mount over the directory of a crashed ``creating`` record
(issue #83); ``reconcile`` must settle it ``partial`` without monkeypatching the mount probe.

Not a test module. Usage: ``python unplaced_userns_reconcile_scenario.py <workdir>``; prints JSON.
"""

from __future__ import annotations

import json
import os
import sqlite3
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
    # what a crash leaves: creating, no dir_id, the admin entry still on disk, the mount in place
    conn = sqlite3.connect(w.db)
    conn.execute(
        "UPDATE records SET state = 'creating', admin_dir = NULL, head_at_create = NULL "
        "WHERE id = ?",
        [rid],
    )
    conn.commit()
    conn.close()
    rec = w.run("reconcile")
    row = w.records()[0]
    print(
        json.dumps(
            {
                "create": first.code,
                "changes": [(c["id"], c["to"], c["action"]) for c in rec.json()["changes"]],
                "rid": rid,
                "state": row["state"],
                "dir_id": row["dir_id"],
                "admin_dir": row["admin_dir"],
                "admin": w.admin_entries(),
            }
        )
    )


main()
