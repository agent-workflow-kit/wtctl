"""Run inside ``unshare -Urm``: a throwaway-shaped tree with real file systems mounted below it
and on it, deleted by ``removal_delete.delete_throwaway``. Not a test module.
Usage: ``python throwaway_mount_scenario.py <dir>``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def ident(p: Path) -> str:
    st = os.stat(p)
    return f"{st.st_dev}:{st.st_ino}"


def make(work: Path, name: str) -> dict[str, str]:
    root = work / name
    t = root / "wt-abcdefgh"
    (t / "sub" / "mnt").mkdir(parents=True)
    (t / "sub" / "plain").mkdir()
    (t / "sub" / "plain" / "f").write_text("x")
    return {
        "id": "wt-abcdefgh",
        "path": str(t),
        "base_dir": str(root),
        "root_id": ident(root),
        "dir_id": ident(t),
    }


def mount(path: Path) -> None:
    subprocess.run(["mount", "-t", "tmpfs", "none", str(path)], check=True)


def main() -> None:
    from wtctl import fsafe_child, removal_delete

    work = Path(sys.argv[1])
    out: dict[str, object] = {}
    # 1. a mount below the tree: skipped, reported, the rest deleted
    rec = make(work, "below")
    mount(Path(rec["path"]) / "sub" / "mnt")
    (Path(rec["path"]) / "sub" / "mnt" / "inside").write_text("precious")
    res = removal_delete.delete_throwaway(rec, fsafe_child.Budget())
    out["below"] = {
        "status": res.status,
        "remaining": res.remaining,
        "inside_kept": (Path(rec["path"]) / "sub" / "mnt" / "inside").read_text(),
        "plain_gone": not (Path(rec["path"]) / "sub" / "plain").exists(),
    }
    # 2. a mount on the throwaway directory itself: nothing is deleted
    rec = make(work, "on")
    mount(Path(rec["path"]))
    (Path(rec["path"]) / "inside").write_text("precious")
    res = removal_delete.delete_throwaway(rec, fsafe_child.Budget())
    out["on"] = {"status": res.status, "kept": (Path(rec["path"]) / "inside").read_text()}
    # 3. a mount on the throwaway root: the recorded identity no longer matches
    rec = make(work, "root")
    mount(Path(rec["base_dir"]))
    res = removal_delete.delete_throwaway(rec, fsafe_child.Budget())
    out["root"] = {"status": res.status, "kind": res.error.kind if res.error else None}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
