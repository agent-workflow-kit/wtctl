"""Review round 1 hardening: chmod ordering, mounts, bounds, memory, fd limits, real swap races."""

from __future__ import annotations

import ctypes
import errno
import os
import resource
import stat
import sys
import threading
import time
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fsafe_fakes import FakeFs
from wtctl import fsafe
from wtctl.gateway import MountEntry

needs_user = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permissions")


@pytest.fixture
def base(tmp_path: Path) -> Path:
    b = tmp_path / "base"
    b.mkdir()
    return b


@pytest.fixture
def anchor(base: Path) -> Iterator[fsafe.Anchor]:
    a = fsafe.open_anchor(str(base))
    yield a
    a.close()


def count(p: Path) -> int:
    return sum(len(fs) + len(ds) for _, ds, fs in os.walk(p))


def mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def make_deep(path: Path, depth: int) -> None:
    path.mkdir()
    fd = os.open(path, os.O_RDONLY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            nfd = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nfd
    finally:
        os.close(fd)


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


# ---- R1: no chmod before the identity checks ---------------------------------------------


def test_raced_directory_keeps_its_mode(base: Path, anchor: fsafe.Anchor, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "f0").write_text("precious")
    wt = base / "wt"
    (wt / "d").mkdir(parents=True)
    swapped: list[int] = []

    def hook(stage: str, path: tuple[str, ...]) -> None:
        if stage == "classified" and path == ("d",) and not swapped:
            swapped.append(1)
            os.rename(wt / "d", wt / "d.moved")
            os.rename(other, wt / "d")  # a different directory now stands there
            os.chmod(wt / "d", 0o555)

    try:
        res = fsafe.delete_tree(anchor, anchor.fd, "wt", hook=hook)
        assert swapped and ("d",) in [r.path for r in res.remaining]
        assert mode(wt / "d") == 0o555  # not chmodded: it is not the directory we classified
    finally:
        if (wt / "d").exists():
            os.chmod(wt / "d", 0o755)


def test_wrong_recorded_identity_keeps_the_root_mode(base: Path, anchor: fsafe.Anchor) -> None:
    wt = base / "wt"
    wt.mkdir()
    (wt / "f").write_text("x")
    os.chmod(wt, 0o555)
    try:
        with pytest.raises(fsafe.FsafeError) as e:
            fsafe.delete_tree(anchor, anchor.fd, "wt", expected=fsafe.Identity(1, 2))
        assert e.value.kind == "identity"
        assert mode(wt) == 0o555
    finally:
        os.chmod(wt, 0o755)


# ---- R2: the O_PATH chmod never crosses a mount ------------------------------------------


@needs_user
@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="needs O_PATH")
def test_mode_000_directory_on_another_mount_is_not_chmodded(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    (wt / "locked").mkdir(parents=True)
    st = os.stat(wt / "locked")
    os.chmod(wt / "locked", 0o000)
    a = fsafe.open_anchor(str(tmp_path), fs=FakeFs(dev_by_ino={st.st_ino: st.st_dev + 1}))
    try:
        res = fsafe.delete_tree(a, a.fd, "wt")
        assert [(r.path, r.reason) for r in res.remaining] == [(("locked",), "mount_point")]
        assert mode(wt / "locked") == 0o000
    finally:
        a.close()
        os.chmod(wt / "locked", 0o755)


# ---- R3: mounts inside the tree are skipped and reported, never refuse the anchor --------


def entry(mp: str) -> MountEntry:
    return MountEntry(50, mp, "tmpfs", "none")


def test_mounts_below_are_recorded_and_walk_skips_them_unopened(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "mnt" / "victim").mkdir(parents=True)
    (tree / "plain").mkdir()
    a = fsafe.open_anchor(str(tree), fs=FakeFs(table=[entry(str(tree / "mnt"))]))
    try:
        assert a.mounts_below == (str(tree / "mnt"),)
        got = {e.path: e.kind for e in fsafe.walk_tree(a, a.fd, root_path=str(tree))}
    finally:
        a.close()
    assert got == {("mnt",): "mount", ("plain",): "dir"}


def test_delete_skips_a_mount_below_by_path_and_reports_it(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "mnt" / "victim").mkdir(parents=True)
    (tree / "mnt" / "victim" / "keep").write_text("keep")
    (tree / "plain").mkdir()
    a = fsafe.open_anchor(str(tmp_path), fs=FakeFs(table=[entry(str(tree / "mnt"))]))
    try:
        res = fsafe.delete_tree(a, a.fd, "tree", parent_path=str(tmp_path))
    finally:
        a.close()
    assert [(r.path, r.reason) for r in res.remaining] == [(("mnt",), "mount_point")]
    assert (tree / "mnt" / "victim" / "keep").exists() and not (tree / "plain").exists()


# ---- R4: a real concurrent swap ------------------------------------------------------------


def _renameat2_exchange() -> object | None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameat2
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    return fn


def test_concurrent_exchange_of_a_directory_and_a_symlink_never_escapes(tmp_path: Path) -> None:
    fn = _renameat2_exchange()
    if fn is None or not sys.platform.startswith("linux"):
        pytest.skip("renameat2 unavailable")
    victim = tmp_path / "victim"
    (victim / "sub").mkdir(parents=True)
    for i in range(5):
        (victim / f"f{i}").write_text("precious")
        (victim / "sub" / f"g{i}").write_text("precious")
    before = count(victim)
    deadline = time.monotonic() + 5
    runs = 0
    while runs < 6 and time.monotonic() < deadline:
        base = tmp_path / f"base{runs}"
        wt = base / "wt"
        for i in range(300):
            (wt / f"filler{i}" / "x").mkdir(parents=True)
        (wt / "a" / "inner").mkdir(parents=True)
        (wt / "b").symlink_to(victim)
        wfd = os.open(wt, os.O_RDONLY)
        probe = fn(wfd, b"a", wfd, b"b", 2)  # type: ignore[operator]
        if probe != 0:
            os.close(wfd)
            err = ctypes.get_errno()
            if err in (errno.ENOSYS, errno.EINVAL, errno.EPERM):
                pytest.skip("RENAME_EXCHANGE unsupported here")
            raise OSError(err, "renameat2")
        stop = threading.Event()

        def swapper(wfd: int = wfd, stop: threading.Event = stop) -> None:
            while not stop.is_set():
                if fn(wfd, b"a", wfd, b"b", 2) != 0:  # type: ignore[operator]
                    time.sleep(0.0005)

        t = threading.Thread(target=swapper)
        t.start()
        a = fsafe.open_anchor(str(base))
        try:
            fsafe.delete_tree(a, a.fd, "wt")
        finally:
            stop.set()
            t.join(10)
            a.close()
            os.close(wfd)
        assert count(victim) == before
        runs += 1
    assert runs >= 1


# ---- R5 / R9: bounded listing, dirs_only ----------------------------------------------------


def test_walk_limit_is_enforced_while_the_directory_is_read(
    base: Path, anchor: fsafe.Anchor, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(3000):
        (base / f"f{i}").write_text("")
    pulled = [0]
    real = os.scandir

    class Spy:
        def __init__(self, it: object) -> None:
            self.it = it

        def __enter__(self) -> Spy:
            self.it.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *a: object) -> None:
            self.it.__exit__(*a)  # type: ignore[attr-defined]

        def __iter__(self) -> Spy:
            return self

        def __next__(self) -> object:
            pulled[0] += 1
            return next(self.it)  # type: ignore[call-overload]

    monkeypatch.setattr(os, "scandir", lambda p: Spy(real(p)))
    with pytest.raises(fsafe.FsafeError) as e:
        list(fsafe.walk_tree(anchor, anchor.fd, limit=10))
    assert e.value.kind == "limit"
    assert 0 < pulled[0] <= 11


def test_dirs_only_skips_files_before_counting(base: Path, anchor: fsafe.Anchor) -> None:
    for i in range(20):
        (base / f"f{i}").write_text("")
    (base / "d1" / "d2").mkdir(parents=True)
    (base / "d1" / "file").write_text("")
    (base / "ln").symlink_to("d1")
    got = [(e.path, e.kind) for e in fsafe.walk_tree(anchor, anchor.fd, limit=3, dirs_only=True)]
    assert got == [(("d1",), "dir"), (("d1", "d2"), "dir")]


# ---- R6: the remaining list is capped ------------------------------------------------------


def test_remaining_is_capped_at_100_with_a_total(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    inos = {}
    for i in range(150):
        (wt / f"m{i}").mkdir(parents=True)
        inos[os.stat(wt / f"m{i}").st_ino] = os.stat(wt).st_dev + 1
    a = fsafe.open_anchor(str(tmp_path), fs=FakeFs(dev_by_ino=inos))
    try:
        res = fsafe.delete_tree(a, a.fd, "wt")
    finally:
        a.close()
    assert len(res.remaining) == 100 and res.remaining_total == 150 and not res.complete


# ---- R10: memory on deep trees ---------------------------------------------------------------

DEEP = 5000


def _peak_mb(fn: object) -> float:
    tracemalloc.start()
    try:
        fn()  # type: ignore[operator]
        return tracemalloc.get_traced_memory()[1] / 1e6
    finally:
        tracemalloc.stop()


def _need_fds(n: int) -> None:
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if hard != resource.RLIM_INFINITY and hard < n:
        pytest.skip("hard fd limit too low")


def test_deep_tree_delete_memory_is_linear(base: Path, anchor: fsafe.Anchor) -> None:
    _need_fds(DEEP + 500)
    fsafe.raise_fd_limit()
    make_deep(base / "wt", DEEP)
    peak = _peak_mb(lambda: fsafe.delete_tree(anchor, anchor.fd, "wt"))
    assert not (base / "wt").exists()
    assert peak < 60


def test_deep_tree_walk_memory_is_linear(base: Path, anchor: fsafe.Anchor) -> None:
    _need_fds(DEEP + 500)
    fsafe.raise_fd_limit()
    make_deep(base / "wt", DEEP)
    peak = _peak_mb(lambda: sum(1 for _ in fsafe.walk_tree(anchor, anchor.fd)))
    assert peak < 60


# ---- R12: fd limit ----------------------------------------------------------------------------


def test_delete_raises_the_soft_fd_limit_itself(base: Path) -> None:
    make_deep(base / "wt", 200)
    a = fsafe.open_anchor(str(base))
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if not os.path.isdir("/proc/self/fd") or (hard != resource.RLIM_INFINITY and hard < 1000):
        a.close()
        pytest.skip("needs /proc and a higher hard limit")
    resource.setrlimit(resource.RLIMIT_NOFILE, (open_fds() + 40, hard))
    try:
        res = fsafe.delete_tree(a, a.fd, "wt")
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        a.close()
    assert res.complete and not (base / "wt").exists()


def test_emfile_errno_is_carried(
    base: Path, anchor: fsafe.Anchor, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("needs /proc")
    make_deep(base / "wt", 200)
    monkeypatch.setattr(fsafe, "raise_fd_limit", lambda: None)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (open_fds() + 40, hard))
    try:
        got = [e for e in fsafe.walk_tree(anchor, anchor.fd) if e.kind == "unreadable"]
        res = fsafe.delete_tree(anchor, anchor.fd, "wt")
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert got and got[0].errno == errno.EMFILE
    assert res.remaining and res.remaining[0].errno == errno.EMFILE


# ---- R13: EBUSY on unlink is a mount point -----------------------------------------------------


def test_unlink_ebusy_is_reported_as_mount_point(
    base: Path, anchor: fsafe.Anchor, monkeypatch: pytest.MonkeyPatch
) -> None:
    wt = base / "wt"
    wt.mkdir()
    (wt / "bound").write_text("x")
    real = os.unlink

    def fake(path: str, *, dir_fd: int | None = None) -> None:
        if path == "bound":
            raise OSError(errno.EBUSY, "busy")
        real(path, dir_fd=dir_fd)

    monkeypatch.setattr(fsafe.os, "unlink", fake)
    res = fsafe.delete_tree(anchor, anchor.fd, "wt")
    assert [(r.path, r.reason) for r in res.remaining] == [(("bound",), "mount_point")]
