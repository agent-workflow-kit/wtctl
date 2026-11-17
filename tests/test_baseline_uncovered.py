"""Cross-device symlink targets in the baseline: ``/dev/null`` is covered by type, any other
target on another device or mount is an explicit ``symlink_uncovered`` item, never read and never
blocking (docs/interface.md, Baseline, Symlinks; issue #68)."""

from __future__ import annotations

import dataclasses
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.baselib import World
from wtctl import baselinelinks, fsafe, repobaseline

KEY = "symlink-target"
COVERED = "symlink_target"
UNCOVERED = "symlink_uncovered"

pytestmark = pytest.mark.skipif(
    not (os.path.isdir("/dev/shm") and os.path.exists("/dev/null")), reason="needs /dev/shm"
)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> World:
    return World(tmp_path, private_home)


@pytest.fixture
def shm() -> Iterator[Path]:
    """A private directory on another device (skipped when /dev/shm shares the device)."""
    d = Path("/dev/shm") / f"wtctl-unc-{os.getpid()}-{os.urandom(4).hex()}"
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def other_device(w: World, shm: Path) -> None:
    if os.stat("/dev/shm").st_dev == os.stat(w.tmp).st_dev:
        pytest.skip("/dev/shm shares the device")


def link_items(b: repobaseline.Baseline, suffix: str = "") -> list[repobaseline.Item]:
    return [i for i in b.items if i.key == KEY and i.path.endswith(suffix)]


def one(b: repobaseline.Baseline, suffix: str) -> repobaseline.Item:
    got = link_items(b, suffix)
    assert len(got) == 1, [(i.path, i.type, i.detail) for i in b.items if i.key == KEY]
    return got[0]


def uncovered(b: repobaseline.Baseline) -> list[str]:
    return sorted(i.path for i in b.items if i.type == UNCOVERED)


# --- /dev/null -----------------------------------------------------------------------------


def test_dev_null_as_a_hook_is_covered_and_not_blocking(w: World) -> None:
    os.symlink("/dev/null", Path(w.common) / "hooks" / "pre-commit")
    b = w.compute()
    item = one(b, "hooks/pre-commit")
    assert (item.type, item.detail) == (COVERED, None)
    assert not b.has_detail() and not uncovered(b)


def test_dev_null_as_claude_md_is_covered(w: World) -> None:
    os.symlink("/dev/null", w.wt / "CLAUDE.md")
    b = w.compute()
    assert (one(b, "CLAUDE.md").type, one(b, "CLAUDE.md").detail) == (COVERED, None)
    assert not b.has_detail() and not uncovered(b)


def test_dev_null_through_a_chain_is_covered_and_retargeting_changes_the_digest(w: World) -> None:
    os.symlink("/dev/null", w.tmp / "mid")
    os.symlink(w.tmp / "mid", w.wt / "CLAUDE.md")
    a = w.compute()
    assert not a.has_detail() and not uncovered(a)
    os.unlink(w.wt / "CLAUDE.md")
    (w.tmp / "plain").write_text("x")
    os.symlink(w.tmp / "plain", w.wt / "CLAUDE.md")
    assert w.digest() != a.digest


def test_a_link_from_dev_null_to_a_file_is_not_dev_null_and_changes_the_digest(w: World) -> None:
    os.symlink("/dev/null", w.wt / "CLAUDE.md")
    a = w.digest()
    os.unlink(w.wt / "CLAUDE.md")
    os.symlink("/dev/zero", w.wt / "CLAUDE.md")
    assert w.digest() != a


def test_dev_zero_is_uncovered_and_never_read(w: World) -> None:
    os.symlink("/dev/zero", w.wt / "CLAUDE.md")  # endless if it were read
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED
    assert not b.has_detail()


def test_a_path_under_dev_null_is_not_dev_null(w: World) -> None:
    os.symlink("/dev/null/x", w.wt / "CLAUDE.md")
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED


def test_a_regular_file_named_null_elsewhere_is_not_dev_null(w: World, shm: Path) -> None:
    (shm / "null").write_text("x")
    os.symlink(shm / "null", w.wt / "CLAUDE.md")
    assert one(w.compute(), "CLAUDE.md").type == UNCOVERED


def test_a_symlink_named_null_in_another_directory_is_not_dev_null(w: World, shm: Path) -> None:
    (shm / "dev").mkdir()
    os.symlink("/etc/hostname", shm / "dev" / "null")
    os.symlink(shm / "dev" / "null", w.wt / "CLAUDE.md")
    assert one(w.compute(), "CLAUDE.md").type == UNCOVERED


def test_a_chain_through_another_device_stops_at_the_first_crossing(w: World, shm: Path) -> None:
    os.symlink("/dev/null", shm / "n")
    os.symlink(shm / "n", w.wt / "CLAUDE.md")
    b = w.compute()
    # the first hop leaves the device, so the chain stops there: the remaining hops are unseen
    assert one(b, "CLAUDE.md").type == UNCOVERED


class _Rdev(fsafe.Fs):
    def __init__(self, rdev: tuple[int, int] | None) -> None:
        self.rdev = rdev

    def char_rdev_at(self, dir_fd: int, name: str) -> tuple[int, int] | None:
        return self.rdev


def test_dev_null_is_matched_by_device_numbers_not_by_name(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.symlink("/dev/null", w.wt / "CLAUDE.md")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", _Rdev((1, 4)))  # a character device named null
    assert one(w.compute(), "CLAUDE.md").type == UNCOVERED


def test_the_null_device_numbers_do_not_make_another_path_covered(
    w: World, shm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.symlink(shm / "whatever", w.wt / "CLAUDE.md")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", _Rdev((1, 3)))
    assert one(w.compute(), "CLAUDE.md").type == UNCOVERED


def test_without_null_device_numbers_the_platform_falls_back_to_uncovered(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.symlink("/dev/null", w.wt / "CLAUDE.md")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", _Rdev(None))
    assert one(w.compute(), "CLAUDE.md").type == UNCOVERED


# --- other cross-device targets ------------------------------------------------------------


def test_a_file_on_another_device_is_uncovered_and_not_blocking(w: World, shm: Path) -> None:
    (shm / "dots.md").write_text("elsewhere")
    os.symlink(shm / "dots.md", w.wt / "CLAUDE.md")
    b = w.compute()
    item = one(b, "CLAUDE.md")
    assert (item.type, item.detail) == (UNCOVERED, None)
    assert not b.has_detail()
    assert uncovered(b) == ["CLAUDE.md"]


def test_retargeting_an_uncovered_link_changes_the_digest(w: World, shm: Path) -> None:
    (shm / "a.md").write_text("a")
    (shm / "b.md").write_text("a")  # same content: only the target text differs
    os.symlink(shm / "a.md", w.wt / "CLAUDE.md")
    first = w.digest()
    os.unlink(w.wt / "CLAUDE.md")
    os.symlink(shm / "b.md", w.wt / "CLAUDE.md")
    assert w.digest() != first


def test_content_behind_an_uncovered_link_is_not_detected(w: World, shm: Path) -> None:
    """The documented residual gap: nothing behind a cross-device target is read."""
    f = shm / "dots.md"
    f.write_text("one")
    os.symlink(f, w.wt / "CLAUDE.md")
    first = w.digest()
    f.write_text("two, quite different")
    os.chmod(f, 0o755)
    assert w.digest() == first


def test_the_type_behind_a_crossing_is_not_looked_at(w: World, shm: Path) -> None:
    """The walk stops at the first entry on the other device (``/dev/shm`` here): what lies
    below it, its type included, is not resolved. The path text is what is recorded."""
    os.symlink(shm / "t", w.wt / "CLAUDE.md")
    (shm / "t").write_text("x")
    as_file = w.digest()
    os.unlink(shm / "t")
    (shm / "t").mkdir()
    assert w.digest() == as_file


def test_the_type_of_a_final_entry_on_another_device_is_part_of_the_item(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = _SwapDevice()
    t = w.tmp / "t"
    os.symlink(t, w.wt / "CLAUDE.md")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", fs)
    digests = []
    for make in (lambda: t.write_text("x"), lambda: (t.unlink(), t.mkdir()),
                 lambda: (t.rmdir(), os.mkfifo(t))):  # fmt: skip
        make()
        fs.inodes.add(os.lstat(t).st_ino)
        digests.append(w.digest())
    assert len(set(digests)) == 3


def test_a_dangling_cross_device_target_is_uncovered_not_missing(w: World, shm: Path) -> None:
    os.symlink(shm / "later.md", w.wt / "CLAUDE.md")
    assert one(w.compute(), "CLAUDE.md").type == UNCOVERED


def test_a_directory_on_another_device_is_uncovered_and_never_listed(
    w: World, shm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (shm / "d").mkdir()
    (shm / "d" / "secret").write_text("x")
    os.symlink(shm / "d", w.wt / "CLAUDE.md")
    seen: list[str] = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p=".": seen.append(str(p)) or real(p))
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED
    assert not b.has_detail()
    before = b.digest
    (shm / "d" / "secret").write_text("changed")
    (shm / "d" / "more").write_text("new")
    assert w.digest() == before
    assert not any(str(shm) in s for s in seen)


def test_a_hooks_directory_on_another_device_blocks(w: World, shm: Path) -> None:
    (shm / "hooks").mkdir()
    shutil.rmtree(Path(w.common) / "hooks")
    os.symlink(shm / "hooks", Path(w.common) / "hooks")
    b = w.compute()
    assert (one(b, "/hooks").type, one(b, "/hooks").detail) == (COVERED, "mount_point")
    assert not uncovered(b)


def test_a_chain_that_crosses_a_device_in_the_middle(w: World, shm: Path) -> None:
    (shm / "d").mkdir()
    os.symlink("elsewhere", shm / "d" / "inner")
    os.symlink(shm / "d" / "inner", w.tmp / "mid")
    os.symlink(w.tmp / "mid", w.wt / "CLAUDE.md")
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED and not b.has_detail()
    first = b.digest
    os.unlink(w.tmp / "mid")  # the same-device first hop is still part of the item
    os.symlink(shm / "d" / "inner", w.tmp / "mid2")
    os.symlink(w.tmp / "mid2", w.tmp / "mid")
    assert w.digest() != first


def test_the_path_beyond_a_crossing_is_recorded_as_text(w: World, shm: Path) -> None:
    os.symlink(shm / "d" / "x", w.wt / "CLAUDE.md")
    a = w.digest()
    os.unlink(w.wt / "CLAUDE.md")
    os.symlink(shm / "d" / "y", w.wt / "CLAUDE.md")
    assert w.digest() != a


def test_nothing_on_the_other_device_is_opened_or_read(w: World, shm: Path) -> None:
    target = shm / f"huge-{os.urandom(4).hex()}"
    target.write_bytes(b"x" * 1000)
    os.symlink(target, w.wt / "CLAUDE.md")
    os.mkfifo(shm / "pipe")
    os.symlink(shm / "pipe", w.wt / "AGENTS.md")
    opened: list[str] = []
    armed = [True]

    def hook(event: str, args: tuple) -> None:  # type: ignore[type-arg]
        if armed[0] and event == "open" and args and isinstance(args[0], str):
            opened.append(args[0])

    sys.addaudithook(hook)
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED
    armed[0] = False
    assert opened, "the audit hook saw nothing"
    assert not any(n in opened for n in (target.name, "pipe")), opened


def test_a_fifo_on_another_device_is_uncovered(w: World, shm: Path) -> None:
    os.mkfifo(shm / "pipe")
    os.symlink(shm / "pipe", w.wt / "CLAUDE.md")
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED and not b.has_detail()
    assert stat.S_ISFIFO(os.lstat(shm / "pipe").st_mode)


def test_links_inside_a_followed_directory_that_leave_the_device_are_uncovered(
    w: World, shm: Path
) -> None:
    real = w.tmp / "claude-real"
    real.mkdir()
    (shm / "x.md").write_text("x")
    os.symlink(shm / "x.md", real / "CLAUDE.md")
    os.symlink(real, w.wt / ".claude")
    b = w.compute()
    assert one(b, ".claude/CLAUDE.md").type == UNCOVERED
    assert one(b, ".claude").type == COVERED
    assert not b.has_detail()


def test_a_link_retargeted_between_the_two_resolutions_is_incomplete(
    w: World, shm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = w.wt / "CLAUDE.md"
    os.symlink(shm / "a.md", link)
    real = baselinelinks.resolve
    calls: list[int] = []

    def wrapped(anchor, path, charge, **kw):  # noqa: ANN001, ANN003, ANN202
        out = real(anchor, path, charge, **kw)
        if path.endswith("CLAUDE.md") and not calls:
            calls.append(1)
            os.unlink(link)
            os.symlink(shm / "b.md", link)
        return out

    monkeypatch.setattr(baselinelinks, "resolve", wrapped)
    assert one(w.compute(), "CLAUDE.md").detail == "incomplete"


class _SwapDevice(fsafe.Fs):
    """The inodes in ``inodes`` report another device (the fake-device seam)."""

    def __init__(self) -> None:
        self.inodes: set[int] = set()

    def adjust(self, st: fsafe.FdStat) -> fsafe.FdStat:
        return dataclasses.replace(st, dev=st.dev + 7) if st.ino in self.inodes else st


def test_the_fake_device_seam_makes_a_file_uncovered(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = _SwapDevice()
    (w.tmp / "plain").write_text("x")
    fs.inodes.add(os.stat(w.tmp / "plain").st_ino)
    os.symlink(w.tmp / "plain", w.wt / "CLAUDE.md")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", fs)
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED and not b.has_detail()


def test_the_fake_device_seam_makes_a_directory_uncovered(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = _SwapDevice()
    (w.tmp / "vol").mkdir()
    (w.tmp / "vol" / "f.md").write_text("x")
    fs.inodes.add(os.stat(w.tmp / "vol").st_ino)
    os.symlink(w.tmp / "vol" / "f.md", w.wt / "CLAUDE.md")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", fs)
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED and not b.has_detail()


def test_a_bind_mount_on_the_same_device_is_uncovered(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the mount id tells (the existing seam)."""
    from wtctl import gateway

    (w.tmp / "bound").mkdir()
    (w.tmp / "bound" / "f.md").write_text("x")
    os.symlink(w.tmp / "bound" / "f.md", w.wt / "CLAUDE.md")
    real = gateway.fd_mount_id

    def fake(fd: int) -> int | None:
        base = real(fd)
        try:
            where = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return base
        return (base or 0) + 4242 if where.startswith(str(w.tmp / "bound")) else base

    if real(os.open(w.tmp, os.O_RDONLY)) is None:
        pytest.skip("no mount ids on this platform")
    monkeypatch.setattr(gateway, "fd_mount_id", fake)
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED and not b.has_detail()


# --- positions whose content must be parsed stay blocking ----------------------------------


def test_an_include_on_another_device_cannot_be_parsed_and_blocks(w: World, shm: Path) -> None:
    (shm / "inc.cfg").write_text("[core]\n\thooksPath = /tmp/x\n")
    link = w.tmp / "link.cfg"
    os.symlink(shm / "inc.cfg", link)
    w.append_cfg(f"[include]\n\tpath = {link}\n")
    b = w.compute()
    assert b.has_detail()
    assert one(b, "link.cfg").detail == "mount_point"
    assert one(b, "link.cfg").type == COVERED  # never the non-blocking uncovered item


def test_a_submodule_git_dir_on_another_device_blocks(w: World, shm: Path) -> None:
    (shm / "sub").mkdir()
    (Path(w.common) / "modules").mkdir()
    os.symlink(shm / "sub", Path(w.common) / "modules" / "sub")
    b = w.compute()
    assert not any(i.type == UNCOVERED for i in b.items)
    assert one(b, "modules/sub").detail == "mount_point"


# --- unchanged behavior --------------------------------------------------------------------


def test_the_digest_of_a_tree_without_cross_device_links_is_unchanged(w: World) -> None:
    (w.tmp / "AGENTS.md").write_text("rules")
    os.symlink(w.tmp / "AGENTS.md", w.wt / "CLAUDE.md")
    b = w.compute()
    assert one(b, "CLAUDE.md").type == COVERED
    assert repobaseline.digest_of(b.items) == b.digest
    # the item line is the one #67 produced: type, hash and no detail
    assert one(b, "CLAUDE.md").detail is None


def test_the_legacy_collector_never_follows_a_cross_device_link(w: World, shm: Path) -> None:
    (shm / "f").write_text("x")
    os.symlink(shm / "f", w.wt / "CLAUDE.md")
    b = w.compute(links=False)
    assert not link_items(b) and not b.has_detail()


def test_a_legacy_mount_point_link_item_never_compares_equal_to_the_new_item(
    w: World, shm: Path
) -> None:
    """No recorded baseline can hold a ``mount_point`` link item (create and accept refuse it),
    but if one were forged or hand-edited, the new collector's item must not read as unchanged."""
    (shm / "f").write_text("x")
    os.symlink(shm / "f", w.wt / "CLAUDE.md")
    cur = w.compute()
    new = one(cur, "CLAUDE.md")
    old = repobaseline.Item("claude", new.path, KEY, COVERED, repobaseline.EMPTY, "mount_point")
    rows = repobaseline.compare([old], cur.items)
    assert [r["path"] for r in rows if r["key"] == KEY] == ["CLAUDE.md"]
    # and as a stored row (without its detail), the new item is still a change
    stored = repobaseline.Item("claude", new.path, KEY, COVERED, repobaseline.EMPTY)
    assert [r["path"] for r in repobaseline.compare([stored], cur.items) if r["key"] == KEY]


def test_routine_changes_leave_the_digest_with_uncovered_links_present(w: World, shm: Path) -> None:
    (shm / "f").write_text("x")
    os.symlink(shm / "f", w.wt / "CLAUDE.md")
    a = w.digest()
    w.repo.g(["config", "branch.feat.remote", "origin"])
    w.repo.g(["config", "branch.feat.merge", "refs/heads/feat"])
    w.repo.g(["branch", "side"], w.repo.main)
    assert w.digest() == a


# --- a real tmpfs --------------------------------------------------------------------------

INNER = "UNCOVERED_INNER_RUN"


def test_a_real_tmpfs_target_in_a_private_mount_namespace(tmp_path: Path) -> None:
    """Re-runs ``_inner_tmpfs`` under ``unshare -Urm`` where a tmpfs can be mounted."""
    if os.environ.get(INNER):
        pytest.skip("this is the inner run")
    if not shutil.which("unshare"):
        pytest.skip("no unshare")
    probe = subprocess.run(
        ["unshare", "-Urm", "sh", "-c", f"mount -t tmpfs tmpfs {tmp_path}"],
        capture_output=True,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip("cannot mount a tmpfs in a user namespace here")
    env = {**os.environ, INNER: "1"}
    r = subprocess.run(
        ["unshare", "-Urm", sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
         f"{__file__}::test__inner_tmpfs"],
        env=env, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr


def test__inner_tmpfs(w: World) -> None:
    if not os.environ.get(INNER):
        pytest.skip("run by test_a_real_tmpfs_target_in_a_private_mount_namespace")
    vol = w.tmp / "vol"
    vol.mkdir()
    subprocess.run(["mount", "-t", "tmpfs", "tmpfs", str(vol)], check=True, timeout=30)
    (vol / "dots.md").write_text("one")
    os.symlink(vol / "dots.md", w.wt / "CLAUDE.md")
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED and not b.has_detail()
    first = b.digest
    (vol / "dots.md").write_text("two")
    assert w.digest() == first  # the documented gap
    os.unlink(w.wt / "CLAUDE.md")
    os.symlink(vol / "other.md", w.wt / "CLAUDE.md")
    assert w.digest() != first


class _DevOnTheAnchorDevice(fsafe.Fs):
    """``/dev`` reports the anchor's device while its nodes ``null`` and ``zero`` report another
    one, and every character device claims the null device's numbers: only the resolved path can
    tell ``/dev/null`` from ``/dev/zero`` (the final entry on another device, reached without
    crossing a directory)."""

    def __init__(self, home_dev: int) -> None:
        self.home_dev = home_dev
        self.dev_ino = os.stat("/dev").st_ino
        self.dev_dev = os.stat("/dev").st_dev
        self.nodes = {os.stat("/dev/null").st_ino, os.stat("/dev/zero").st_ino}

    def adjust(self, st: fsafe.FdStat) -> fsafe.FdStat:
        if st.ino == self.dev_ino and st.dev == self.dev_dev:
            return dataclasses.replace(st, dev=self.home_dev)
        if st.ino in self.nodes and st.dev == self.dev_dev:
            return dataclasses.replace(st, dev=self.home_dev + 9)
        return st

    def char_rdev_at(self, dir_fd: int, name: str) -> tuple[int, int] | None:
        return (1, 3)

    def mount_id(self, fd: int) -> int | None:
        return 1


def test_a_final_special_node_is_type_only_whatever_its_device(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: a final node that is not a file or a directory is recorded by type, never uncovered
    (the format-2 shape from before cross-device links were uncovered)."""
    if os.stat("/dev").st_ino == os.stat("/dev/null").st_ino:
        pytest.skip("odd /dev")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", _DevOnTheAnchorDevice(os.stat(w.common).st_dev))
    os.symlink("/dev/null", w.wt / "CLAUDE.md")
    os.symlink("/dev/zero", w.wt / ".mcp.json")
    b = w.compute()
    assert one(b, "CLAUDE.md").type == COVERED
    assert one(b, ".mcp.json").type == COVERED
    assert not b.has_detail() and not uncovered(b)


# --- the position policy (R1): blocking by default, uncovered only on the allow-list --------


def blocks(b: repobaseline.Baseline, suffix: str) -> None:
    got = [i for i in link_items(b, suffix) if i.detail]
    assert [(i.type, i.detail) for i in got] == [(COVERED, "mount_point")], [
        (i.path, i.type, i.detail) for i in b.items if i.key == KEY
    ]
    assert not uncovered(b)


def test_a_hook_entry_on_another_device_blocks(w: World, shm: Path) -> None:
    (shm / "h").write_text("#!/bin/sh\n")
    os.symlink(shm / "h", Path(w.common) / "hooks" / "pre-commit")
    blocks(w.compute(), "hooks/pre-commit")


def test_a_hook_entry_inside_a_followed_directory_blocks(w: World, shm: Path) -> None:
    real = w.tmp / "hookdir"
    real.mkdir()
    (shm / "h").write_text("x")
    os.symlink(shm / "h", real / "pre-push")
    shutil.rmtree(Path(w.common) / "hooks")
    os.symlink(real, Path(w.common) / "hooks")
    blocks(w.compute(), "hooks/pre-push")


def test_an_external_hooks_path_entry_blocks(w: World, shm: Path) -> None:
    ext = w.tmp / "exthooks"
    ext.mkdir()
    (shm / "h").write_text("x")
    os.symlink(shm / "h", ext / "pre-commit")
    w.append_cfg(f"[core]\n\thooksPath = {ext}\n")
    blocks(w.compute(), "exthooks/pre-commit")


def test_an_external_hooks_path_link_to_another_device_blocks(w: World, shm: Path) -> None:
    (shm / "hd").mkdir()
    link = w.tmp / "hooklink"
    os.symlink(shm / "hd", link)
    w.append_cfg(f"[core]\n\thooksPath = {link}\n")
    blocks(w.compute(), "hooklink")


def test_a_submodule_config_and_hooks_on_another_device_block(w: World, shm: Path) -> None:
    (shm / "c").write_text("[core]\n")
    (shm / "h").write_text("x")
    sub = Path(w.common) / "modules" / "sub"
    (sub / "hooks").mkdir(parents=True)
    (sub / "HEAD").write_text("ref: refs/heads/x\n")
    os.symlink(shm / "c", sub / "config")
    os.symlink(shm / "h", sub / "hooks" / "pre-commit")
    b = w.compute()
    assert {i.path.rsplit("modules/", 1)[-1] for i in link_items(b) if i.detail} == {
        "sub/config",
        "sub/hooks/pre-commit",
    }
    assert not uncovered(b)


def test_the_config_file_on_another_device_blocks(w: World, shm: Path) -> None:
    (shm / "cfg").write_text("[core]\n")
    os.unlink(Path(w.common) / "config")
    os.symlink(shm / "cfg", Path(w.common) / "config")
    blocks(w.compute(), "/config")


def test_a_symlinked_git_dir_across_a_non_final_crossing_blocks(w: World, shm: Path) -> None:
    """R2: ``/dev/shm`` is crossed in the middle of the path (kind ``unresolved``)."""
    (Path(w.common) / "modules").mkdir()
    os.symlink(shm / "no" / "such", Path(w.common) / "modules" / "sub")
    b = w.compute()
    assert [(i.type, i.detail) for i in link_items(b, "modules/sub")] == [(COVERED, "mount_point")]


def test_the_admin_gitdir_link_on_another_device_blocks(w: World, shm: Path) -> None:
    (shm / "g").write_text("x")
    gitdir = Path(w.repo.admin_dir) / "gitdir"
    gitdir.unlink()
    os.symlink(shm / "g", gitdir)
    blocks(w.compute(), "/gitdir")


def test_claude_settings_and_mcp_links_on_another_device_block(w: World, shm: Path) -> None:
    (shm / "s").write_text("{}")
    (w.wt / ".claude").mkdir()
    os.symlink(shm / "s", w.wt / ".claude" / "settings.json")
    os.symlink(shm / "s", w.wt / ".mcp.json")
    b = w.compute()
    assert {i.path for i in link_items(b) if i.detail} == {".claude/settings.json", ".mcp.json"}
    assert not uncovered(b)


def test_a_claude_directory_link_to_another_device_blocks(w: World, shm: Path) -> None:
    (shm / "d").mkdir()
    os.symlink(shm / "d", w.wt / ".claude")
    blocks(w.compute(), ".claude")


def test_claude_md_inside_the_claude_dir_is_uncovered_but_its_neighbours_block(
    w: World, shm: Path
) -> None:
    (shm / "x").write_text("x")
    (w.wt / ".claude").mkdir()
    os.symlink(shm / "x", w.wt / ".claude" / "claude.MD")
    os.symlink(shm / "x", w.wt / ".claude" / "agent.md")
    b = w.compute()
    assert uncovered(b) == [".claude/claude.MD"]
    assert [i.path for i in link_items(b) if i.detail] == [".claude/agent.md"]


def test_a_deep_claude_md_named_link_under_claude_blocks(w: World, shm: Path) -> None:
    """T1: commands and agents are executable prompts, whatever their file name."""
    (shm / "x").write_text("x")
    for sub in ("commands", "agents", "commands/deep"):
        (w.wt / ".claude" / sub).mkdir(parents=True, exist_ok=True)
        os.symlink(shm / "x", w.wt / ".claude" / sub / "claude-x.md")
    b = w.compute()
    assert not uncovered(b)
    assert sorted(i.path for i in link_items(b) if i.detail == "mount_point") == [
        ".claude/agents/claude-x.md",
        ".claude/commands/claude-x.md",
        ".claude/commands/deep/claude-x.md",
    ]


def test_claude_md_outside_claude_and_directly_inside_it_is_uncovered(w: World, shm: Path) -> None:
    (shm / "x").write_text("x")
    (w.wt / "sub").mkdir()
    (w.wt / ".claude").mkdir()
    os.symlink(shm / "x", w.wt / "CLAUDE.md")
    os.symlink(shm / "x", w.wt / "sub" / "CLAUDE.md")
    os.symlink(shm / "x", w.wt / ".claude" / "CLAUDE.md")
    b = w.compute()
    assert uncovered(b) == [".claude/CLAUDE.md", "CLAUDE.md", "sub/CLAUDE.md"]
    assert not [i for i in link_items(b) if i.detail]


def test_a_cross_device_file_link_below_modules_is_ignored_like_a_same_device_one(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2: only a directory target is a submodule git dir; a file target is not collected."""
    fs = _SwapDevice()
    (w.tmp / "plain").write_text("x")
    fs.inodes.add(os.stat(w.tmp / "plain").st_ino)
    mods = Path(w.common) / "modules"
    mods.mkdir()
    os.symlink(w.tmp / "plain", mods / "x")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", fs)
    b = w.compute()
    assert not link_items(b, "modules/x")
    assert not uncovered(b) and not b.has_detail()


def test_info_exclude_alternates_and_excludes_file_are_uncovered(w: World, shm: Path) -> None:
    (shm / "x").write_text("x")
    (Path(w.common) / "info").mkdir(exist_ok=True)
    (Path(w.common) / "objects" / "info").mkdir(parents=True, exist_ok=True)
    for p in (Path(w.common) / "info" / "exclude", Path(w.common) / "objects/info/alternates"):
        p.unlink(missing_ok=True)
        os.symlink(shm / "x", p)
    os.symlink(shm / "x", w.tmp / "ignore")
    w.append_cfg(f"[core]\n\texcludesFile = {w.tmp / 'ignore'}\n")
    b = w.compute()
    assert not b.has_detail()
    assert {p.rsplit("/", 1)[-1] for p in uncovered(b)} == {"exclude", "alternates", "ignore"}


def test_dev_null_is_covered_at_every_position(w: World) -> None:
    os.symlink("/dev/null", Path(w.common) / "hooks" / "pre-commit")
    (w.wt / ".claude").mkdir()
    os.symlink("/dev/null", w.wt / ".claude" / "settings.json")
    os.symlink("/dev/null", w.wt / ".mcp.json")
    b = w.compute()
    assert not b.has_detail() and not uncovered(b)


def test_a_wrong_device_number_at_dev_null_blocks_in_a_hook(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6(f): only the numbers tell /dev/null; at a blocking position it is a refusal."""
    os.symlink("/dev/null", Path(w.common) / "hooks" / "pre-commit")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", _Rdev((1, 4)))
    blocks(w.compute(), "hooks/pre-commit")


# --- R3: a bind-mounted regular file on the same device ------------------------------------


def _bind_file(w: World, monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    from wtctl import gateway

    real = gateway.fd_mount_id
    if real(os.open(w.tmp, os.O_RDONLY)) is None:
        pytest.skip("no mount ids on this platform")

    def fake(fd: int) -> int | None:
        base = real(fd)
        try:
            where = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return base
        return (base or 0) + 4242 if where == str(target) else base

    monkeypatch.setattr(gateway, "fd_mount_id", fake)


def test_a_bind_mounted_file_is_uncovered_at_claude_md_and_blocks_in_a_hook(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = w.tmp / "bound.md"
    f.write_text("x")
    os.symlink(f, w.wt / "CLAUDE.md")
    os.symlink(f, Path(w.common) / "hooks" / "pre-commit")
    _bind_file(w, monkeypatch, f)
    b = w.compute()
    assert uncovered(b) == ["CLAUDE.md"]
    assert [i.path for i in link_items(b) if i.detail] == [f"{w.common}/hooks/pre-commit"]


def test_a_bind_mounted_file_found_in_the_mount_table_is_uncovered(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = w.tmp / "bound2.md"
    f.write_text("x")
    os.symlink(f, w.wt / "CLAUDE.md")
    real = fsafe.open_anchor

    def anchor(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        got = real(*a, **kw)
        got.mounts_below = (*got.mounts_below, str(f))
        return got

    monkeypatch.setattr(fsafe, "open_anchor", anchor)
    assert uncovered(w.compute()) == ["CLAUDE.md"]


# --- R6(a): the link target is the crossing directory itself --------------------------------


def test_a_link_whose_target_is_the_crossing_directory_itself_is_uncovered(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = _SwapDevice()
    (w.tmp / "vol").mkdir()
    fs.inodes.add(os.stat(w.tmp / "vol").st_ino)
    os.symlink(w.tmp / "vol", w.wt / "CLAUDE.md")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", fs)
    b = w.compute()
    assert one(b, "CLAUDE.md").type == UNCOVERED and not b.has_detail()
