"""Run inside ``unshare -Urm``: real mounts below a tree, scanned by the removal-check child
operations. Not a test module. Usage: ``python removal_scenario.py <workdir>``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    from wtctl import fsafe_child

    tree = Path(sys.argv[1]) / "tree"
    (tree / "plain").mkdir(parents=True)
    (tree / "mnt").mkdir()
    subprocess.run(["mount", "-t", "tmpfs", "none", str(tree / "mnt")], check=True)
    (tree / "mnt" / ".git").mkdir()  # a repository on the mount: never reached
    (tree / "mnt" / "inner").mkdir()
    st = os.stat(tree)
    a = {"anchor": {"path": str(tree), "identity": [st.st_dev, st.st_ino]}, "chain": []}
    scan = fsafe_child.OPS["nested_scan"](dict(a))
    ids = fsafe_child.OPS["dir_ids"](dict(a))
    inner = os.stat(tree / "mnt" / "inner")
    flat: list[int] = ids["ids"]  # type: ignore[assignment]
    pairs = {(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)}
    print(
        json.dumps(
            {
                "mounts": scan["mounts"],
                "gits": scan["gits"],
                "dir_ids_has_mounted_dir": [inner.st_dev, inner.st_ino] in [list(p) for p in pairs],
                "dir_ids_mounts": ids["mounts"],
            }
        )
    )


if __name__ == "__main__":
    main()
