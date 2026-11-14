"""Mount-point defenses: mountinfo pre-check, st_dev and mount id on every opened fd.

Two kinds of tests: seams (``FakeFs`` reports another ``st_dev`` or mount id for chosen inodes,
which runs everywhere and models a FUSE or bind mount) and real mounts made inside an unprivileged
user + mount namespace (``unshare -Urm``), skipped when the host does not allow that.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fsafe_fakes import FakeFs
from wtctl import fsafe
from wtctl.gateway import MountEntry, MountTableUnavailable


def ino(p: Path) -> int:
    return os.stat(p).st_ino


def entry(mp: str, fstype: str = "tmpfs", mid: int = 50) -> MountEntry:
    return MountEntry(mid, mp, fstype, "none")


def test_mountpoints_under_matches_at_and_below_only() -> None:
    table = [entry("/a/b"), entry("/a/b/c"), entry("/a/bc"), entry("/a"), entry("/")]
    assert fsafe.mountpoints_under("/a/b", table) == ["/a/b", "/a/b/c"]
    assert fsafe.mountpoints_under("/", table) == ["/a/b", "/a/b/c", "/a/bc", "/a", "/"]
    assert fsafe.mountpoints_under("/a/b/c/d", table) == []


def test_precheck_refuses_mount_at_the_anchor(tmp_path: Path) -> None:
    fs = FakeFs(table=[entry(str(tmp_path))])
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_anchor(str(tmp_path), fs=fs)
    assert e.value.kind == "mount_point"


def test_precheck_records_mounts_below_the_anchor_without_refusing(tmp_path: Path) -> None:
    below = str(tmp_path / "x" / "y")
    fs = FakeFs(table=[entry(below)])
    with fsafe.open_anchor(str(tmp_path), fs=fs) as a:
        assert a.mounts_below == (below,)


def test_precheck_allows_mounts_above_and_beside_the_anchor(tmp_path: Path) -> None:
    fs = FakeFs(table=[entry("/"), entry(str(tmp_path.parent)), entry(str(tmp_path) + "-other")])
    fsafe.open_anchor(str(tmp_path), fs=fs).close()


def test_precheck_can_be_skipped_for_unrecorded_walks(tmp_path: Path) -> None:
    fs = FakeFs(table=[entry(str(tmp_path))])
    fsafe.open_anchor(str(tmp_path), fs=fs, precheck=False).close()


def test_unreadable_mount_table_fails_closed(tmp_path: Path) -> None:
    class Broken(FakeFs):
        def mount_table(self) -> list[MountEntry] | None:
            raise MountTableUnavailable("no /proc")

    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_anchor(str(tmp_path), fs=Broken())
    assert e.value.kind == "unavailable"


def test_no_mount_table_platform_relies_on_dev_only(tmp_path: Path) -> None:
    fs = FakeFs(table=None, mnt_default=None)  # macOS-like: no mountinfo, no mount id
    a = fsafe.open_anchor(str(tmp_path), fs=fs)
    assert a.mnt_id is None
    a.close()


def test_anchor_on_a_mount_cannot_present_the_recorded_device(tmp_path: Path) -> None:
    recorded = fsafe.Identity(os.stat(tmp_path).st_dev, ino(tmp_path))
    fs = FakeFs(dev_by_ino={ino(tmp_path): recorded.dev + 77})  # FUSE shows another st_dev
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_anchor(str(tmp_path), expected=recorded, fs=fs, precheck=False)
    assert e.value.kind == "identity"


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    r = tmp_path / "tree"
    (r / "mnt" / "victim").mkdir(parents=True)
    (r / "mnt" / "victim" / "keep").write_text("keep")
    (r / "plain").mkdir()
    (r / "plain" / "f").write_text("x")
    return r


@pytest.mark.parametrize("how", ["dev", "mnt_id"])
def test_chain_open_refuses_a_mount_point_directory(tree: Path, how: str) -> None:
    st = os.stat(tree / "mnt")
    fs = (
        FakeFs(dev_by_ino={st.st_ino: st.st_dev + 1})
        if how == "dev"
        else FakeFs(mnt_by_ino={st.st_ino: 999})
    )
    a = fsafe.open_anchor(str(tree), fs=fs)
    try:
        with pytest.raises(fsafe.FsafeError) as e:
            fsafe.open_chain(a, a.fd, ["mnt", "victim"])
        assert e.value.kind == "mount_point"
        os.close(fsafe.open_chain(a, a.fd, ["plain"]))
    finally:
        a.close()


@pytest.mark.parametrize("how", ["dev", "mnt_id"])
def test_walk_reports_mount_and_never_descends(tree: Path, how: str) -> None:
    st = os.stat(tree / "mnt")
    fs = (
        FakeFs(dev_by_ino={st.st_ino: st.st_dev + 1})
        if how == "dev"
        else FakeFs(mnt_by_ino={st.st_ino: 999})
    )
    a = fsafe.open_anchor(str(tree), fs=fs)
    try:
        got = {e.path: e.kind for e in fsafe.walk_tree(a, a.fd)}
    finally:
        a.close()
    assert got[("mnt",)] == "mount"
    assert ("mnt", "victim") not in got
    assert got[("plain", "f")] == "file"


def test_walk_flags_a_file_mounted_over(tree: Path) -> None:
    st = os.stat(tree / "plain" / "f")
    a = fsafe.open_anchor(str(tree), fs=FakeFs(dev_by_ino={st.st_ino: st.st_dev + 1}))
    try:
        got = {e.path: e.kind for e in fsafe.walk_tree(a, a.fd)}
    finally:
        a.close()
    assert got[("plain", "f")] == "mount"


def test_open_file_refuses_file_on_another_mount(tree: Path) -> None:
    st = os.stat(tree / "plain" / "f")
    a = fsafe.open_anchor(str(tree), fs=FakeFs(mnt_by_ino={st.st_ino: 999}))
    try:
        d = fsafe.open_chain(a, a.fd, ["plain"])
        try:
            with pytest.raises(fsafe.FsafeError) as e:
                fsafe.open_file(a, d, "f")
            assert e.value.kind == "mount_point"
        finally:
            os.close(d)
    finally:
        a.close()


@pytest.mark.parametrize("how", ["dev", "mnt_id"])
def test_delete_skips_mount_and_reports_it(tree: Path, how: str) -> None:
    st = os.stat(tree / "mnt")
    fs = (
        FakeFs(dev_by_ino={st.st_ino: st.st_dev + 1})
        if how == "dev"
        else FakeFs(mnt_by_ino={st.st_ino: 999})
    )
    a = fsafe.open_anchor(str(tree.parent), fs=fs)
    try:
        res = fsafe.delete_tree(a, a.fd, "tree")
    finally:
        a.close()
    assert [(r.path, r.reason) for r in res.remaining] == [(("mnt",), "mount_point")]
    assert (tree / "mnt" / "victim" / "keep").read_text() == "keep"
    assert not (tree / "plain").exists()
    assert tree.exists()  # cannot be removed while the mount point is inside


def test_delete_refuses_a_root_that_is_a_mount(tree: Path) -> None:
    st = os.stat(tree)
    a = fsafe.open_anchor(str(tree.parent), fs=FakeFs(dev_by_ino={st.st_ino: st.st_dev + 1}))
    try:
        res = fsafe.delete_tree(a, a.fd, "tree")
    finally:
        a.close()
    assert [(r.path, r.reason) for r in res.remaining] == [((), "mount_point")]
    assert (tree / "plain" / "f").exists()


def test_describe_mount_by_mount_id_and_fuse_detection(tmp_path: Path) -> None:
    table = [
        MountEntry(1, "/", "ext4", "/dev/sda1"),
        MountEntry(7, str(tmp_path), "fuse.sshfs", "me@host:/"),
    ]
    a = fsafe.open_anchor(str(tmp_path), fs=FakeFs(table=table, mnt_default=7), precheck=False)
    try:
        d = fsafe.describe_mount(a)
    finally:
        a.close()
    assert d is not None and d.fstype == "fuse.sshfs" and d.is_fuse
    assert fsafe.is_fuse_fstype("fuseblk") and fsafe.is_fuse_fstype("macfuse")
    assert not fsafe.is_fuse_fstype("ext4") and not fsafe.is_fuse_fstype("tmpfs")


def test_describe_mount_falls_back_to_longest_prefix_without_mount_ids(tmp_path: Path) -> None:
    table = [
        MountEntry(None, "/", "apfs", "/dev/disk1"),
        MountEntry(None, str(tmp_path.parent), "nfs", "srv:/x"),
    ]
    a = fsafe.open_anchor(str(tmp_path), fs=FakeFs(table=table, mnt_default=None), precheck=False)
    try:
        d = fsafe.describe_mount(a)
    finally:
        a.close()
    assert d is not None and d.fstype == "nfs" and not d.is_fuse


# ---- real mounts in an unprivileged user + mount namespace -------------------------------


def _userns_works(tmp_path: Path) -> bool:
    unshare = shutil.which("unshare")
    mount = shutil.which("mount")
    if not (unshare and mount and sys.platform.startswith("linux")):
        return False
    target = tmp_path / "probe"
    target.mkdir()
    r = subprocess.run(
        [unshare, "-Urm", mount, "-t", "tmpfs", "none", str(target)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return r.returncode == 0


SCENARIO = Path(__file__).with_name("userns_scenario.py")


@pytest.fixture
def userns(tmp_path: Path) -> list[str]:
    if not _userns_works(tmp_path):
        pytest.skip(
            "real mount tests skipped: unprivileged user+mount namespaces are unavailable here "
            "(for example AppArmor restricts them on recent Ubuntu runners); only the FakeFs "
            "seam tests ran"
        )
    return [str(shutil.which("unshare")), "-Urm", sys.executable, str(SCENARIO)]


def _scenario(userns: list[str], tmp_path: Path) -> dict[str, object]:
    work = tmp_path / "ns"
    work.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"]}
    r = subprocess.run(
        [*userns, str(work)], capture_output=True, text=True, timeout=120, env=env, check=False
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_real_tmpfs_and_bind_mounts_are_found_skipped_and_never_crossed(
    userns: list[str], tmp_path: Path
) -> None:
    out = _scenario(userns, tmp_path)
    # the pre-check records both mounts below the anchor and does not refuse it
    assert out["precheck_kind"] is None
    assert sorted(out["precheck_mounts"]) == ["bind", "tmp"]  # type: ignore[arg-type]
    # pre-check on: both are skipped by path without being opened and reported
    assert out["walk_precheck"] == {
        "bind": "mount",
        "tmp": "mount",
        "plain": "dir",
        "plain/f": "file",
    }
    # with the pre-check off (a mount made after it), the fd checks still catch both kinds
    assert out["walk"] == {"bind": "mount", "tmp": "mount", "plain": "dir", "plain/f": "file"}
    assert out["chain_tmp"] == "mount_point"
    assert out["chain_bind"] == "mount_point"  # same st_dev: only the mount id tells
    assert out["bind_same_dev"] is True
    # deletion skipped both and reported them; the victims behind them are untouched
    assert sorted(out["remaining"]) == [["bind", "mount_point"], ["tmp", "mount_point"]]  # type: ignore[arg-type]
    assert sorted(out["remaining_precheck"]) == [["bind", "mount_point"], ["tmp", "mount_point"]]  # type: ignore[arg-type]
    assert out["victim_kept"] is True
    assert out["tmp_kept"] is True
    assert out["plain_gone"] is True
    # a mount point as the anchor itself is refused by the pre-check
    assert out["anchor_on_mount"] == "mount_point"
