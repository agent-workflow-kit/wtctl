"""fd-based tree deletion: no-follow, races (SAFE-1, SAFE-8), read-only directories, partial."""

from __future__ import annotations

import os
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


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    o = tmp_path / "outside"
    (o / "sub").mkdir(parents=True)
    for i in range(5):
        (o / f"f{i}").write_text("precious")
        (o / "sub" / f"g{i}").write_text("precious")
    return o


def count(p: Path) -> int:
    return sum(len(fs) + len(ds) for _, ds, fs in os.walk(p))


def test_deletes_a_nested_tree_including_special_entries(base: Path, anchor: fsafe.Anchor) -> None:
    wt = base / "wt"
    (wt / "a" / "b").mkdir(parents=True)
    (wt / "a" / "b" / "f").write_text("x")
    (wt / "top").write_text("x")
    os.mkfifo(wt / "a" / "fifo")
    (wt / "ln").symlink_to("a")
    (wt / "dangling").symlink_to("/nonexistent")
    res = fsafe.delete_tree(anchor, anchor.fd, "wt")
    assert res.complete and res.remaining == []
    assert res.removed == 8  # 3 directories (a, b, wt), f, top, fifo, ln, dangling
    assert not wt.exists()


def test_symlinks_are_removed_not_followed(base: Path, anchor: fsafe.Anchor, outside: Path) -> None:
    wt = base / "wt"
    wt.mkdir()
    (wt / "dirlink").symlink_to(outside)
    (wt / "filelink").symlink_to(outside / "f0")
    fsafe.delete_tree(anchor, anchor.fd, "wt")
    assert not wt.exists()
    assert count(outside) == 11


def test_hard_linked_file_only_loses_its_name(
    base: Path, anchor: fsafe.Anchor, outside: Path
) -> None:
    wt = base / "wt"
    wt.mkdir()
    os.link(outside / "f0", wt / "linked")
    fsafe.delete_tree(anchor, anchor.fd, "wt")
    assert (outside / "f0").read_text() == "precious"


def test_missing_root_is_a_complete_noop(base: Path, anchor: fsafe.Anchor) -> None:
    res = fsafe.delete_tree(anchor, anchor.fd, "nope")
    assert res.complete and res.removed == 0


def test_root_that_is_a_symlink_is_refused_and_target_untouched(
    base: Path, anchor: fsafe.Anchor, outside: Path
) -> None:
    (base / "wt").symlink_to(outside)
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.delete_tree(anchor, anchor.fd, "wt")
    assert e.value.kind in ("symlink", "not_dir")
    assert count(outside) == 11
    assert (base / "wt").is_symlink()


def test_recorded_dir_id_is_checked_before_anything_is_deleted(
    base: Path, anchor: fsafe.Anchor
) -> None:
    wt = base / "wt"
    wt.mkdir()
    (wt / "f").write_text("x")
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.delete_tree(anchor, anchor.fd, "wt", expected=fsafe.Identity(1, 2))
    assert e.value.kind == "identity"
    assert (wt / "f").exists()


def test_owned_read_only_directories_get_u_plus_w(base: Path, anchor: fsafe.Anchor) -> None:
    wt = base / "wt"
    (wt / "ro" / "deeper").mkdir(parents=True)
    (wt / "ro" / "f").write_text("x")
    (wt / "ro" / "deeper" / "g").write_text("x")
    os.chmod(wt / "ro" / "deeper", 0o555)
    os.chmod(wt / "ro", 0o555)
    os.chmod(wt, 0o555)
    res = fsafe.delete_tree(anchor, anchor.fd, "wt")
    assert res.complete, res.remaining
    assert not wt.exists()


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="needs O_PATH")
def test_owned_directory_with_no_permissions_at_all(base: Path, anchor: fsafe.Anchor) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses permissions")
    wt = base / "wt"
    (wt / "locked").mkdir(parents=True)
    (wt / "locked" / "f").write_text("x")
    os.chmod(wt / "locked", 0o000)
    res = fsafe.delete_tree(anchor, anchor.fd, "wt")
    assert res.complete, res.remaining
    assert not wt.exists()


def test_directories_not_owned_are_never_chmodded_and_are_reported(
    base: Path, anchor: fsafe.Anchor
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses permissions")
    wt = base / "wt"
    (wt / "ro").mkdir(parents=True)
    (wt / "ro" / "f").write_text("x")
    os.chmod(wt / "ro", 0o555)
    try:
        res = fsafe.delete_tree(anchor, anchor.fd, "wt", owner_uid=os.geteuid() + 1)  # "not ours"
        assert not res.complete
        assert [r.path for r in res.remaining] == [("ro", "f")]  # causes only, not ancestors
        assert res.remaining[0].reason == "error" and res.remaining[0].errno is not None
        assert os.stat(wt / "ro").st_mode & 0o777 == 0o555
    finally:
        os.chmod(wt / "ro", 0o755)


def test_partial_when_an_entry_cannot_be_removed_lists_it(base: Path, anchor: fsafe.Anchor) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses permissions")
    wt = base / "wt"
    (wt / "a").mkdir(parents=True)
    (wt / "a" / "f").write_text("x")
    (wt / "b").write_text("x")
    # `a` is read-only and "not ours" (owner_uid), so wtctl must not chmod it: f cannot be unlinked
    os.chmod(wt / "a", 0o555)
    res = fsafe.delete_tree(anchor, anchor.fd, "wt", owner_uid=-1)  # -1 never equals an owner
    os.chmod(wt / "a", 0o755)
    assert not res.complete
    assert ("a", "f") in [r.path for r in res.remaining]
    assert not (wt / "b").exists()


def test_deep_tree_does_not_hit_the_recursion_limit(base: Path, anchor: fsafe.Anchor) -> None:
    wt = base / "wt"
    wt.mkdir()
    fd = os.open(wt, os.O_RDONLY)
    try:
        for _ in range(3000):
            os.mkdir("d", dir_fd=fd)
            nfd = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nfd
    finally:
        os.close(fd)
    res = fsafe.delete_tree(anchor, anchor.fd, "wt")
    assert res.complete and not wt.exists()


# ---- SAFE-1 / SAFE-8: deterministic races through the hook seam -------------------------


def swap_for_symlink(path: Path, target: Path) -> None:
    moved = path.with_name(path.name + ".moved")
    os.rename(path, moved)
    os.symlink(target, path)


def test_directory_swapped_for_symlink_before_it_is_opened(
    base: Path, anchor: fsafe.Anchor, outside: Path
) -> None:
    wt = base / "wt"
    (wt / "victim").mkdir(parents=True)
    (wt / "victim" / "own").write_text("x")
    swapped: list[tuple[str, ...]] = []

    def hook(stage: str, path: tuple[str, ...]) -> None:
        if stage == "classified" and path == ("victim",) and not swapped:
            swapped.append(path)
            swap_for_symlink(wt / "victim", outside)

    res = fsafe.delete_tree(anchor, anchor.fd, "wt", hook=hook)
    assert swapped
    assert count(outside) == 11  # SAFE-1: nothing outside the tree was deleted
    assert not (wt / "victim").exists() and not (wt / "victim").is_symlink()  # link removed
    # the attacker's extra entry appeared after the listing: reported, never silently ignored
    assert ((), "not_empty") in [(r.path, r.reason) for r in res.remaining]


def test_directory_swapped_for_symlink_after_it_is_opened(
    base: Path, anchor: fsafe.Anchor, outside: Path
) -> None:
    wt = base / "wt"
    (wt / "victim").mkdir(parents=True)
    (wt / "victim" / "own").write_text("x")
    swapped: list[int] = []

    def hook(stage: str, path: tuple[str, ...]) -> None:
        if stage == "opened" and path == ("victim",) and not swapped:
            swapped.append(1)
            swap_for_symlink(wt / "victim", outside)

    res = fsafe.delete_tree(anchor, anchor.fd, "wt", hook=hook)
    assert swapped
    assert count(outside) == 11
    assert not (wt / "victim.moved" / "own").exists()  # the real directory was emptied via its fd
    assert not res.complete  # rmdir by name hit the symlink and was refused


def test_ancestor_swapped_for_symlink_between_chain_open_and_delete(
    base: Path, anchor: fsafe.Anchor, outside: Path
) -> None:
    (base / "mid" / "wt").mkdir(parents=True)
    (base / "mid" / "wt" / "f").write_text("x")
    mid_fd = fsafe.open_chain(anchor, anchor.fd, ["mid"])
    try:
        swap_for_symlink(base / "mid", outside)  # attacker replaces the ancestor
        res = fsafe.delete_tree(anchor, mid_fd, "wt")  # SAFE-8
    finally:
        os.close(mid_fd)
    assert res.complete
    assert count(outside) == 11
    assert not (base / "mid.moved" / "wt").exists()


def test_swap_between_listing_and_open_with_a_different_directory(
    base: Path, anchor: fsafe.Anchor, outside: Path
) -> None:
    wt = base / "wt"
    (wt / "d").mkdir(parents=True)
    swapped: list[int] = []

    def hook(stage: str, path: tuple[str, ...]) -> None:
        if stage == "classified" and path == ("d",) and not swapped:
            swapped.append(1)
            os.rename(wt / "d", wt / "d.moved")
            os.rename(outside, wt / "d")  # a real directory with the same name now stands there

    res = fsafe.delete_tree(anchor, anchor.fd, "wt", hook=hook)
    assert swapped
    # the different directory is recognized as a swap (identity differs from the lstat) and kept
    assert ("d",) in [r.path for r in res.remaining]
    assert (wt / "d" / "f0").read_text() == "precious"


@pytest.mark.parametrize(
    "name",
    [
        "new\nline",
        "sp ace",
        "-rf",
        "ünï-cødé",
        "a" * 254,
        "\udcff-bad-utf8",
        "--",
        "*",
        ".git.",
        "GIT~1",
    ],
)
def test_hostile_entry_names_are_deleted_by_exact_name(
    base: Path, anchor: fsafe.Anchor, outside: Path, name: str
) -> None:
    wt = base / "wt"
    wt.mkdir()
    fd = os.open(wt, os.O_RDONLY)
    try:
        os.close(os.open(name, os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd))
        os.mkdir(name + "d", dir_fd=fd)
        os.symlink(outside, name + "l", dir_fd=fd)
    finally:
        os.close(fd)
    res = fsafe.delete_tree(anchor, anchor.fd, "wt")
    assert res.complete and not wt.exists()
    assert count(outside) == 11


def test_fsafe_error_round_trips_through_json() -> None:
    e = fsafe.FsafeError("mount_point", "m", path="/x", errno_=5, mounts=["/a"])
    back = fsafe.FsafeError.from_dict(e.to_dict())
    assert (back.kind, back.message, back.path, back.errno, back.mounts) == (
        "mount_point",
        "m",
        "/x",
        5,
        ["/a"],
    )
