"""Atomic no-follow writes: pointer rewrite, exclusive creates, directory creation."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from wtctl import fsafe


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


def test_atomic_write_creates_and_replaces(base: Path, anchor: fsafe.Anchor) -> None:
    fsafe.write_file_atomic(anchor.fd, "gitdir", b"one\n", mode=0o644)
    assert (base / "gitdir").read_bytes() == b"one\n"
    assert stat.S_IMODE(os.stat(base / "gitdir").st_mode) == 0o644  # exact, whatever the umask
    fsafe.write_file_atomic(anchor.fd, "gitdir", b"two\n")
    assert (base / "gitdir").read_bytes() == b"two\n"
    assert os.listdir(base) == ["gitdir"]  # no temp files left behind


def test_atomic_write_replaces_a_symlink_itself_not_its_target(
    base: Path, anchor: fsafe.Anchor, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    (base / ".git").symlink_to(outside)
    fsafe.write_file_atomic(anchor.fd, ".git", b"gitdir: /x\n")
    assert outside.read_text() == "precious"
    assert not (base / ".git").is_symlink()
    assert (base / ".git").read_bytes() == b"gitdir: /x\n"


def test_atomic_write_refuses_a_directory_target(base: Path, anchor: fsafe.Anchor) -> None:
    (base / "d").mkdir()
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.write_file_atomic(anchor.fd, "d", b"x")
    assert e.value.kind == "not_regular"
    assert os.listdir(base) == ["d"]


@pytest.mark.parametrize("name", ["", "a/b", "..", ".", "a\0"])
def test_atomic_write_rejects_unsafe_names(anchor: fsafe.Anchor, name: str) -> None:
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.write_file_atomic(anchor.fd, name, b"x")
    assert e.value.kind == "name"


def test_failed_write_leaves_original_and_no_temp(
    base: Path, anchor: fsafe.Anchor, monkeypatch: pytest.MonkeyPatch
) -> None:
    (base / "gitdir").write_text("old")

    def boom(fd: int) -> None:
        raise OSError(5, "io")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.write_file_atomic(anchor.fd, "gitdir", b"new")
    assert e.value.kind == "io"
    assert (base / "gitdir").read_text() == "old"
    assert os.listdir(base) == ["gitdir"]


def test_temp_file_is_created_exclusively_and_never_followed(
    base: Path, anchor: fsafe.Anchor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("precious")
    monkeypatch.setattr(fsafe.secrets, "token_hex", lambda n: "fixed")
    (base / ".wtctl-fixed.tmp").symlink_to(outside)  # pre-planted temp name
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.write_file_atomic(anchor.fd, "gitdir", b"new")
    assert e.value.kind == "exists"
    assert outside.read_text() == "precious"


def test_rewrite_pointer_writes_through_the_checked_chain(base: Path, anchor: fsafe.Anchor) -> None:
    (base / "worktrees" / "wt1").mkdir(parents=True)
    fsafe.rewrite_pointer(
        anchor, anchor.fd, ["worktrees", "wt1"], "gitdir", b"/p/.git\n", owner_uid=os.geteuid()
    )
    assert (base / "worktrees" / "wt1" / "gitdir").read_bytes() == b"/p/.git\n"


def test_rewrite_pointer_refuses_a_symlinked_component_and_writes_nothing(
    base: Path, anchor: fsafe.Anchor, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "worktrees").mkdir()
    (base / "worktrees" / "wt1").symlink_to(outside)
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.rewrite_pointer(
            anchor, anchor.fd, ["worktrees", "wt1"], "gitdir", b"x", owner_uid=os.geteuid()
        )
    assert e.value.kind == "symlink"
    assert os.listdir(outside) == []


def test_rewrite_pointer_requires_owned_directory(base: Path, anchor: fsafe.Anchor) -> None:
    (base / "wt1").mkdir()
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.rewrite_pointer(
            anchor, anchor.fd, ["wt1"], "gitdir", b"x", owner_uid=os.geteuid() + 1
        )
    assert e.value.kind == "owner"
    assert os.listdir(base / "wt1") == []


def test_write_after_chain_open_goes_to_the_original_directory_even_if_swapped(
    base: Path, anchor: fsafe.Anchor, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "wt1").mkdir()
    fd = fsafe.open_chain(anchor, anchor.fd, ["wt1"])
    try:
        os.rename(base / "wt1", base / "wt1.moved")
        os.symlink(outside, base / "wt1")  # swap the checked component for a symlink
        fsafe.write_file_atomic(fd, "gitdir", b"x")
    finally:
        os.close(fd)
    assert os.listdir(outside) == []
    assert (base / "wt1.moved" / "gitdir").read_bytes() == b"x"


def test_create_new_file_is_exclusive_and_no_follow(
    base: Path, anchor: fsafe.Anchor, tmp_path: Path
) -> None:
    fd = fsafe.create_new_file(anchor.fd, "env")
    os.close(fd)
    assert stat.S_IMODE(os.stat(base / "env").st_mode) == 0o600
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.create_new_file(anchor.fd, "env")
    assert e.value.kind == "exists"
    target = tmp_path / "victim"
    (base / "ln").symlink_to(target)  # dangling link: O_CREAT would create the target
    with pytest.raises(fsafe.FsafeError) as e2:
        fsafe.create_new_file(anchor.fd, "ln")
    assert e2.value.kind == "exists"
    assert not target.exists()


@pytest.mark.parametrize("name", [".git", ".GIT", "git~1", ".git."])
def test_create_new_file_and_ensure_dir_refuse_git_variants(
    anchor: fsafe.Anchor, name: str
) -> None:
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.create_new_file(anchor.fd, name)
    assert e.value.kind == "name"
    with pytest.raises(fsafe.FsafeError) as e2:
        fsafe.ensure_dir(anchor, anchor.fd, name)
    assert e2.value.kind == "name"


def test_ensure_dir_creates_reopens_and_reuses(base: Path, anchor: fsafe.Anchor) -> None:
    fd, created = fsafe.ensure_dir(anchor, anchor.fd, "sub")
    os.close(fd)
    assert created and (base / "sub").is_dir()
    assert stat.S_IMODE(os.stat(base / "sub").st_mode) == 0o700
    fd2, created2 = fsafe.ensure_dir(anchor, anchor.fd, "sub")
    os.close(fd2)
    assert not created2


def test_ensure_dir_refuses_symlink_and_file(
    base: Path, anchor: fsafe.Anchor, tmp_path: Path
) -> None:
    (base / "ln").symlink_to(tmp_path)
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.ensure_dir(anchor, anchor.fd, "ln")
    assert e.value.kind == "symlink"
    (base / "f").write_text("x")
    with pytest.raises(fsafe.FsafeError) as e2:
        fsafe.ensure_dir(anchor, anchor.fd, "f")
    assert e2.value.kind == "not_dir"
