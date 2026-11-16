"""Run inside ``unshare -Urm``: a worktree-shaped tree with a real file system mounted below it,
deleted by ``removal_delete``. Not a test module. Usage: ``python remove_mount_scenario.py <dir>``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


class StubState:
    def __init__(self, base: Path) -> None:
        st = os.stat(base)
        self.ident = f"{st.st_dev}:{st.st_ino}"

    def anchor_identity(self, path: str) -> str:
        return self.ident


def main() -> None:
    from wtctl import fsafe_child, removal_delete

    work = Path(sys.argv[1])
    base = work / "base"
    wt = base / "wt"
    (wt / "node_modules" / "mnt").mkdir(parents=True)
    (wt / "node_modules" / "plain").mkdir()
    (wt / "node_modules" / "plain" / "f").write_text("x")
    (wt / ".git").write_text("gitdir: x\n")
    subprocess.run(["mount", "-t", "tmpfs", "none", str(wt / "node_modules" / "mnt")], check=True)
    (wt / "node_modules" / "mnt" / "inside").write_text("precious")
    st = os.stat(wt)
    record = {
        "path": str(wt),
        "base_dir": str(base),
        "owned": True,
        "dir_id": f"{st.st_dev}:{st.st_ino}",
    }
    out = removal_delete.delete_worktree(StubState(base), record, fsafe_child.Budget())
    print(
        json.dumps(
            {
                "status": out.status,
                "remaining": out.remaining,
                "inside_kept": (wt / "node_modules" / "mnt" / "inside").read_text(),
                "plain_gone": not (wt / "node_modules" / "plain").exists(),
                "dotgit_kept": (wt / ".git").exists(),
            }
        )
    )


if __name__ == "__main__":
    main()
