"""No-follow opens: anchors, chains, files (symlinks, hard links, FIFOs, directories)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from wtctl import fsafe


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "root"
    r.mkdir()
    return r


@pytest.fixture
def anchor(root: Path) -> Iterator[fsafe.Anchor]:
    a = fsafe.open_anchor(str(root))
    yield a
    a.close()


def ident(p: Path) -> fsafe.Identity:
    st = os.stat(p)
    return fsafe.Identity(st.st_dev, st.st_ino)


def test_open_anchor_checks_recorded_identity(root: Path) -> None:
    a = fsafe.open_anchor(str(root), expected=ident(root))
    assert a.identity == ident(root)
    a.close()


def test_open_anchor_refuses_changed_identity(root: Path) -> None:
    other = fsafe.Identity(ident(root).dev, ident(root).ino + 1)
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_anchor(str(root), expected=other)
    assert e.value.kind == "identity"


def test_open_anchor_refuses_symlinked_component(tmp_path: Path, root: Path) -> None:
    (root / "d").mkdir()
    link = tmp_path / "link"
    link.symlink_to(root)
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_anchor(str(link / "d"))
    assert e.value.kind == "symlink"


def test_open_anchor_refuses_symlink_as_last_component(tmp_path: Path, root: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(root)
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_anchor(str(link))
    assert e.value.kind == "symlink"


@pytest.mark.parametrize("bad", ["relative/path", "/a/../b", "/a\0b", ""])
def test_open_anchor_refuses_unsafe_paths(bad: str) -> None:
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_anchor(bad)
    assert e.value.kind == "name"


def test_open_anchor_missing_is_reported(tmp_path: Path) -> None:
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_anchor(str(tmp_path / "nope"))
    assert e.value.kind == "missing"


def test_open_chain_walks_components(root: Path, anchor: fsafe.Anchor) -> None:
    (root / "a" / "b").mkdir(parents=True)
    fd = fsafe.open_chain(anchor, anchor.fd, ["a", "b"], expected=ident(root / "a" / "b"))
    try:
        assert fsafe.identity_of(fd) == ident(root / "a" / "b")
    finally:
        os.close(fd)


def test_open_chain_refuses_symlinked_component_and_does_not_leak_fds(
    root: Path, anchor: fsafe.Anchor, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "a").mkdir()
    (root / "a" / "b").symlink_to(outside)
    before = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else 0
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_chain(anchor, anchor.fd, ["a", "b"])
    assert e.value.kind == "symlink"
    after = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else 0
    assert after == before


def test_open_chain_refuses_swapped_identity(root: Path, anchor: fsafe.Anchor) -> None:
    (root / "a").mkdir()
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_chain(anchor, anchor.fd, ["a"], expected=fsafe.Identity(0, 0))
    assert e.value.kind == "identity"


def test_open_chain_refuses_regular_file_component(root: Path, anchor: fsafe.Anchor) -> None:
    (root / "f").write_text("x")
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_chain(anchor, anchor.fd, ["f"])
    assert e.value.kind == "not_dir"


def test_open_chain_owner_check(root: Path, anchor: fsafe.Anchor) -> None:
    (root / "a").mkdir()
    fd = fsafe.open_chain(anchor, anchor.fd, ["a"], owner_uid=os.geteuid())
    os.close(fd)
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_chain(anchor, anchor.fd, ["a"], owner_uid=os.geteuid() + 12345)
    assert e.value.kind == "owner"


@pytest.mark.parametrize("name", [".git", ".GIT", "git~1", ".git.", ".git "])
def test_open_chain_forbid_git(root: Path, anchor: fsafe.Anchor, name: str) -> None:
    (root / name).mkdir()
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_chain(anchor, anchor.fd, [name], forbid_git=True)
    assert e.value.kind == "name"
    os.close(fsafe.open_chain(anchor, anchor.fd, [name]))  # allowed when not forbidden


def test_open_file_reads_a_regular_file(root: Path, anchor: fsafe.Anchor) -> None:
    (root / "f").write_text("hello")
    fd = fsafe.open_file(anchor, anchor.fd, "f")
    try:
        assert os.read(fd, 10) == b"hello"
    finally:
        os.close(fd)


def test_open_file_refuses_symlink_even_to_regular_file(root: Path, anchor: fsafe.Anchor) -> None:
    (root / "real").write_text("x")
    (root / "ln").symlink_to("real")
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_file(anchor, anchor.fd, "ln")
    assert e.value.kind == "symlink"


def test_open_file_hard_link_refused_when_link_count_matters(
    root: Path, anchor: fsafe.Anchor, tmp_path: Path
) -> None:
    (root / "a").write_text("x")
    os.link(root / "a", root / "b")
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_file(anchor, anchor.fd, "a", single_link=True)
    assert e.value.kind == "hardlink"
    os.close(fsafe.open_file(anchor, anchor.fd, "a"))  # allowed when link count does not matter


def test_open_file_refuses_fifo_without_blocking(root: Path, anchor: fsafe.Anchor) -> None:
    os.mkfifo(root / "fifo")
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_file(anchor, anchor.fd, "fifo")
    assert e.value.kind == "not_regular"


def test_open_file_refuses_directory_and_socket(root: Path, anchor: fsafe.Anchor) -> None:
    import socket

    (root / "d").mkdir()
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_file(anchor, anchor.fd, "d")
    assert e.value.kind == "not_regular"
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(str(root / "sock"))
        with pytest.raises(fsafe.FsafeError) as e2:
            fsafe.open_file(anchor, anchor.fd, "sock")
        assert e2.value.kind == "not_regular"
    finally:
        s.close()


def test_open_file_forbids_git_variants(root: Path, anchor: fsafe.Anchor) -> None:
    (root / ".GIT").write_text("x")
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.open_file(anchor, anchor.fd, ".GIT", forbid_git=True)
    assert e.value.kind == "name"


def test_kind_of_mode() -> None:
    import stat

    assert fsafe.kind_of_mode(stat.S_IFREG) == "file"
    assert fsafe.kind_of_mode(stat.S_IFDIR) == "dir"
    assert fsafe.kind_of_mode(stat.S_IFLNK) == "symlink"
    assert fsafe.kind_of_mode(stat.S_IFIFO) == "fifo"
    assert fsafe.kind_of_mode(stat.S_IFSOCK) == "socket"
    assert fsafe.kind_of_mode(stat.S_IFCHR) == "chardev"
    assert fsafe.kind_of_mode(stat.S_IFBLK) == "blockdev"


def test_walk_records_special_entries_by_type_only(root: Path, anchor: fsafe.Anchor) -> None:
    (root / "d").mkdir()
    (root / "d" / "f").write_text("x")
    (root / "ln").symlink_to("d")
    os.mkfifo(root / "d" / "fifo")
    entries = {e.path: e.kind for e in fsafe.walk_tree(anchor, anchor.fd)}
    assert entries == {
        ("d",): "dir",
        ("d", "f"): "file",
        ("d", "fifo"): "fifo",
        ("ln",): "symlink",
    }


def test_walk_never_follows_directory_symlinks(
    root: Path, anchor: fsafe.Anchor, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    (root / "ln").symlink_to(outside)
    paths = [e.path for e in fsafe.walk_tree(anchor, anchor.fd)]
    assert paths == [("ln",)]


def test_walk_descend_callback_and_limit(root: Path, anchor: fsafe.Anchor) -> None:
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("x")
    (root / "src").mkdir()
    (root / "src" / "a").write_text("x")
    paths = [
        e.path
        for e in fsafe.walk_tree(
            anchor, anchor.fd, descend=lambda e: not fsafe.is_dotgit(e.path[-1])
        )
    ]
    assert (".git",) in paths and (".git", "HEAD") not in paths and ("src", "a") in paths
    with pytest.raises(fsafe.FsafeError) as e:
        list(fsafe.walk_tree(anchor, anchor.fd, limit=2))
    assert e.value.kind == "limit"


def test_walk_handles_very_deep_trees_without_recursion(root: Path, anchor: fsafe.Anchor) -> None:
    fd = os.dup(anchor.fd)
    try:
        for _ in range(3000):
            os.mkdir("d", dir_fd=fd)
            nfd = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nfd
    finally:
        os.close(fd)
    assert sum(1 for _ in fsafe.walk_tree(anchor, anchor.fd)) == 3000
