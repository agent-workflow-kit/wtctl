"""Run inside ``unshare -Urm``: a create whose clone hook is a symlink into a real tmpfs.

Not a test module. Usage: ``python unwind_userns_scenario.py <workdir>``; prints a JSON report.
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
    from wtctl import baselinesvc, repobaseline

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
    mnt = work / "mnt"
    mnt.mkdir()
    subprocess.run(["mount", "-t", "tmpfs", "none", str(mnt)], check=True, capture_output=True)
    (mnt / "h").write_text("#!/bin/sh\n")
    link = w.clone / ".git" / "hooks" / "post-commit"
    os.symlink(mnt / "h", link)
    first = w.run("create", "--repo", REPO, "--name", "one", "--no-bootstrap")
    out = {
        "code": first.code,
        "error": first.json().get("error"),
        "records": w.count("records"),
        "admin": w.admin_entries(),
        "dir_exists": (w.root / "worktrees" / "example.test" / "org" / "repo" / "one").exists(),
    }
    link.unlink()
    second = w.run("create", "--repo", REPO, "--name", "one", "--no-bootstrap")
    out["retry_code"] = second.code
    print(json.dumps(out))


main()
