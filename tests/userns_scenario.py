"""Run inside ``unshare -Urm``: make real mounts, exercise fsafe, print a JSON report.

Not a test module (no ``test_`` prefix). Usage: ``python userns_scenario.py <workdir>``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def sh(*argv: str) -> None:
    subprocess.run(argv, check=True, capture_output=True)


def main() -> None:
    from wtctl import fsafe

    work = Path(sys.argv[1])
    victim = work / "victim"
    victim.mkdir()
    (victim / "keep").write_text("keep")
    tree = work / "tree"
    (tree / "plain").mkdir(parents=True)
    (tree / "plain" / "f").write_text("x")
    (tree / "tmp").mkdir()
    (tree / "bind").mkdir()
    sh("mount", "-t", "tmpfs", "none", str(tree / "tmp"))
    (tree / "tmp" / "inner").write_text("inner")
    sh("mount", "--bind", str(victim), str(tree / "bind"))

    out: dict[str, object] = {}

    # 1. the pre-check records mounts below the anchor and still opens it (they are skipped later)
    try:
        a0 = fsafe.open_anchor(str(tree))
        out["precheck_kind"] = None
        out["precheck_mounts"] = [Path(m).name for m in a0.mounts_below]
        out["walk_precheck"] = {
            "/".join(e.path): e.kind for e in fsafe.walk_tree(a0, a0.fd, root_path=str(tree))
        }
    except fsafe.FsafeError as e:
        a0 = None
        out["precheck_kind"] = e.kind
        out["precheck_mounts"] = [Path(m).name for m in e.mounts]

    # 2. fd-level checks alone (pre-check off: a mount made after the pre-check)
    a = fsafe.open_anchor(str(tree), precheck=False)
    out["walk"] = {"/".join(e.path): e.kind for e in fsafe.walk_tree(a, a.fd)}
    for name in ("tmp", "bind"):
        try:
            os.close(fsafe.open_chain(a, a.fd, [name]))
            out[f"chain_{name}"] = None
        except fsafe.FsafeError as e:
            out[f"chain_{name}"] = e.kind
    out["bind_same_dev"] = os.stat(tree / "bind").st_dev == os.stat(tree / "plain").st_dev

    # 3. deletion never crosses
    res = fsafe.delete_tree(a, os.open(work, os.O_RDONLY), "tree")
    out["remaining"] = sorted(["/".join(r.path), r.reason] for r in res.remaining)
    out["victim_kept"] = (victim / "keep").read_text() == "keep"
    out["tmp_kept"] = (tree / "tmp" / "inner").read_text() == "inner"
    out["plain_gone"] = not (tree / "plain").exists()
    a.close()
    if a0 is not None:
        res0 = fsafe.delete_tree(a0, os.open(work, os.O_RDONLY), "tree", parent_path=str(work))
        out["remaining_precheck"] = sorted(["/".join(r.path), r.reason] for r in res0.remaining)
        a0.close()

    # 4. an anchor that is itself a mount point
    try:
        fsafe.open_anchor(str(tree / "tmp"))
        out["anchor_on_mount"] = None
    except fsafe.FsafeError as e:
        out["anchor_on_mount"] = e.kind

    print(json.dumps(out))


main()
