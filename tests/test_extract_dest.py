"""``extract`` destinations: --dest restrictions, no-follow, swaps, io_error, throwaway dests."""

from __future__ import annotations

import dataclasses
import errno
import os
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.extractlib import XW, tree
from wtctl import extract_dest, extraction, fsafe, gateway

pytestmark = pytest.mark.usefixtures("inproc")


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> XW:
    return XW(tmp_path, private_home).build()


def one(w: XW, name: str = "a", data: bytes = b"1") -> dict[str, Any]:
    s = w.src()
    w.put(s, name, data)
    return s


# -- --dest restrictions (exit 2) --------------------------------------------------------------


def test_dest_must_exist(w: XW) -> None:
    s = one(w)
    assert w.ex(s, w.tmp / "nope", ["a"]).code == 2


def test_dest_must_be_a_directory(w: XW) -> None:
    s = one(w)
    f = w.tmp / "file"
    f.write_text("x")
    assert w.ex(s, f, ["a"]).code == 2


def test_dest_final_symlink_is_refused(w: XW) -> None:
    s = one(w)
    real = w.plain_dest("real")
    link = w.tmp / "link"
    os.symlink(real, link)
    assert w.ex(s, link, ["a"]).code == 2
    assert tree(real) == {}


def test_dest_with_symlinked_parent_is_resolved_not_followed_at_the_end(w: XW) -> None:
    s = one(w)
    real = w.plain_dest("real")
    (real / "sub").mkdir()
    os.symlink(real, w.tmp / "plink")
    w.extracted(s, w.tmp / "plink" / "sub", ["a"])
    assert (real / "sub" / "a").exists()


def test_dest_inside_dotgit_is_refused(w: XW) -> None:
    s = one(w)
    d = w.plain_dest("repo") / ".git" / "hooks"
    d.mkdir(parents=True)
    assert w.ex(s, d, ["a"]).code == 2
    assert tree(d) == {}


@pytest.mark.parametrize("name", [".GIT", ".git.", "git~1"])
def test_dest_dotgit_aliases_are_refused(w: XW, name: str) -> None:
    s = one(w)
    d = w.plain_dest("repo") / name
    d.mkdir()
    assert w.ex(s, d, ["a"]).code == 2


def test_dest_reaching_dotgit_through_a_symlink_is_refused(w: XW) -> None:
    s = one(w)
    g = w.plain_dest("repo") / ".git"
    g.mkdir()
    os.symlink(g, w.tmp / "innocent")
    # the resolved parent of the final component is checked too
    (g / "sub").mkdir()
    assert w.ex(s, w.tmp / "innocent" / "sub", ["a"]).code == 2
    assert tree(g) == {"sub": "dir"}


def test_dest_inside_the_state_dir_is_refused(w: XW) -> None:
    s = one(w)
    state = w.home / ".local" / "state" / "wtctl"
    state.mkdir(parents=True, exist_ok=True)
    assert w.ex(s, state, ["a"]).code == 2
    sub = state / "sub"
    sub.mkdir(exist_ok=True)
    assert w.ex(s, sub, ["a"]).code == 2


def test_dest_equal_to_or_inside_the_active_config_dir_is_refused(w: XW) -> None:
    s = one(w)
    cfg = Path(w.config()).parent
    assert w.ex(s, cfg, ["a"]).code == 2
    sub = cfg / "sub"
    sub.mkdir()
    assert w.ex(s, sub, ["a"]).code == 2
    assert not (cfg / "a").exists() and not (sub / "a").exists()


def test_dest_in_the_default_config_dir_is_refused_with_another_config(w: XW) -> None:
    s = one(w)
    default_dir = w.home / ".config" / "wtctl"
    sub = default_dir / "sub"
    sub.mkdir(parents=True)
    assert Path(w.config()).parent != default_dir  # --config points elsewhere
    assert w.ex(s, default_dir, ["a"]).code == 2
    assert w.ex(s, sub, ["a"]).code == 2
    assert not (default_dir / "a").exists() and not (sub / "a").exists()


def test_dest_containing_a_protected_directory_is_not_refused(w: XW) -> None:
    s = one(w)
    w.config()
    assert w.extracted(s, w.tmp, ["a"])["written"][0]["path"] == "a"


def test_dest_inside_the_source_throwaway_is_refused(w: XW) -> None:
    s = one(w)
    sub = Path(s["path"]) / "sub"
    sub.mkdir()
    assert w.ex(s, s["path"], ["a"]).code == 2
    assert w.ex(s, sub, ["a"]).code == 2


def test_dest_reaching_the_source_through_a_symlink_is_refused(w: XW) -> None:
    s = one(w)
    os.symlink(s["path"], w.tmp / "viasrc")
    sub = Path(s["path"]) / "sub"
    sub.mkdir()
    assert w.ex(s, w.tmp / "viasrc" / "sub", ["a"]).code == 2


def test_dest_inside_the_source_after_the_throwaway_root_changed_is_refused(w: XW) -> None:
    s = one(w)
    sub = Path(s["path"]) / "sub"
    sub.mkdir()
    other = w.tmp / "other-root"
    other.mkdir()
    w.top["throwaway_root"] = str(other)  # the recorded path is no longer under the root
    assert w.ex(s, sub, ["a"]).code == 2
    assert w.ex(s, s["path"], ["a"]).code == 2
    assert not (sub / "a").exists()


def test_dest_named_dotgit_by_a_symlink_to_a_plain_directory_is_refused(w: XW) -> None:
    s = one(w)
    plain = w.plain_dest("plain")
    (plain / "sub").mkdir()
    os.symlink(plain, w.tmp / ".git")  # the given path says .git, the resolved one does not
    assert w.ex(s, w.tmp / ".git" / "sub", ["a"]).code == 2
    assert tree(plain) == {"sub": "dir"}


def test_dest_in_the_throwaway_root_but_no_record_is_refused(w: XW) -> None:
    s = one(w)
    stray = w.troot / "stray"
    stray.mkdir(exist_ok=True)
    assert w.ex(s, stray, ["a"]).code == 2
    assert w.ex(s, w.troot, ["a"]).code == 2


def test_relative_dest_is_resolved(w: XW, monkeypatch: pytest.MonkeyPatch) -> None:
    s = one(w)
    d = w.plain_dest()
    monkeypatch.chdir(w.tmp)
    data = w.extracted(s, "dest", ["a"])
    assert data["dest"] == str(d.resolve()) and (d / "a").exists()


# -- dest_conflict -----------------------------------------------------------------------------


def test_existing_directory_at_a_file_target(w: XW) -> None:
    s = one(w)
    dest = w.plain_dest()
    (dest / "a").mkdir()
    assert w.invalid(s, dest, ["a"]) == {"a": "dest_conflict"}
    assert tree(dest) == {"a": "dir"}


def test_existing_symlink_at_a_file_target_is_not_written_through(w: XW) -> None:
    s = one(w)
    dest = w.plain_dest()
    victim = w.tmp / "victim"
    victim.write_text("v")
    os.symlink(victim, dest / "a")
    assert w.invalid(s, dest, ["a"]) == {"a": "dest_conflict"}
    assert victim.read_text() == "v" and os.path.islink(dest / "a")


def test_dangling_symlink_at_a_file_target(w: XW) -> None:
    s = one(w)
    dest = w.plain_dest()
    os.symlink(w.tmp / "not-yet", dest / "a")
    assert w.invalid(s, dest, ["a"]) == {"a": "dest_conflict"}
    assert not (w.tmp / "not-yet").exists()


def test_symlinked_directory_component_in_dest(w: XW) -> None:
    s = w.src()
    w.put(s, "d/f", b"1")
    dest = w.plain_dest()
    out = w.tmp / "out"
    out.mkdir()
    os.symlink(out, dest / "d")
    assert w.invalid(s, dest, ["d/f"]) == {"d/f": "dest_conflict"}
    assert tree(out) == {}


def test_file_where_a_directory_is_needed(w: XW) -> None:
    s = w.src()
    w.put(s, "d/f", b"1")
    dest = w.plain_dest()
    (dest / "d").write_text("file")
    assert w.invalid(s, dest, ["d/f"]) == {"d/f": "dest_conflict"}
    assert (dest / "d").read_text() == "file"


def test_fifo_at_target_is_a_conflict(w: XW) -> None:
    s = one(w)
    dest = w.plain_dest()
    os.mkfifo(dest / "a")
    assert w.invalid(s, dest, ["a"]) == {"a": "dest_conflict"}


def test_conflict_for_one_path_stops_all_writes(w: XW) -> None:
    s = w.src()
    w.put(s, "ok", b"1")
    w.put(s, "bad", b"2")
    dest = w.plain_dest()
    (dest / "bad").mkdir()
    assert w.invalid(s, dest, ["ok", "bad"]) == {"bad": "dest_conflict"}
    assert tree(dest) == {"bad": "dir"}


def test_pre_existing_temp_name_does_not_break_anything(w: XW) -> None:
    s = one(w)
    dest = w.plain_dest()
    (dest / ".wtctl-extract-abc").write_text("mine")
    w.extracted(s, dest, ["a"])
    assert (dest / ".wtctl-extract-abc").read_text() == "mine"


# -- swaps between validation and write --------------------------------------------------------


def patch_os(monkeypatch: pytest.MonkeyPatch, name: str, fn: Callable[..., Any]) -> None:
    """Replace ``os.<name>`` (it stays one of the functions that take a ``dir_fd``)."""
    monkeypatch.setattr(os, name, fn)
    monkeypatch.setattr(os, "supports_dir_fd", {*os.supports_dir_fd, fn})


def swap_after_validation(monkeypatch: pytest.MonkeyPatch, action: Callable[[], None]) -> None:
    done: list[bool] = []

    def hook() -> None:
        if not done:
            done.append(True)
            action()

    monkeypatch.setattr(extraction, "AFTER_VALIDATE", hook)


def test_directory_swapped_for_a_symlink_before_the_write(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = w.src()
    w.put(s, "d/f", b"1")
    dest = w.plain_dest()
    (dest / "d").mkdir()
    out = w.tmp / "out"
    out.mkdir()

    def action() -> None:
        os.rmdir(dest / "d")
        os.symlink(out, dest / "d")

    swap_after_validation(monkeypatch, action)
    e = w.failed(w.ex(s, dest, ["d/f"]), 1)
    assert e["code"] == "io_error" and e["details"]["written"] == []
    assert e["details"]["path"] == "d/f" and e["details"]["deleted"] == []
    assert tree(out) == {}


def test_directory_swapped_for_a_symlink_after_some_writes(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = w.src()
    w.put(s, "a1", b"1")
    w.put(s, "d/f", b"2")
    dest = w.plain_dest()
    (dest / "d").mkdir()
    (dest / "gone").write_text("g")
    out = w.tmp / "out"
    out.mkdir()

    def action() -> None:
        os.rmdir(dest / "d")
        os.symlink(out, dest / "d")

    swap_after_validation(monkeypatch, action)
    e = w.failed(w.ex(s, dest, ["a1", "d/f"], ["gone"]), 1)
    assert e["code"] == "io_error"
    assert e["details"] == {"path": "d/f", "written": ["a1"], "deleted": []}
    assert (dest / "a1").read_text() == "1" and (dest / "gone").exists()
    assert tree(out) == {}
    assert not [n for n in tree(dest) if n.startswith(".wtctl")]


def test_dest_directory_itself_replaced_after_validation_does_not_redirect(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = one(w)
    dest = w.plain_dest()
    moved = w.tmp / "moved"
    decoy = w.tmp / "decoy"
    decoy.mkdir()

    def action() -> None:
        os.rename(dest, moved)
        os.symlink(decoy, dest)

    swap_after_validation(monkeypatch, action)
    w.extracted(s, dest, ["a"])
    assert tree(decoy) == {} and (moved / "a").read_text() == "1"


def test_target_replaced_by_a_directory_after_validation(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = one(w)
    dest = w.plain_dest()

    swap_after_validation(monkeypatch, lambda: os.mkdir(dest / "a"))
    e = w.failed(w.ex(s, dest, ["a"]), 1)
    assert e["code"] == "io_error" and e["details"]["path"] == "a"
    assert tree(dest) == {"a": "dir"}


def test_target_replaced_by_a_symlink_after_validation_is_not_followed(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = one(w)
    dest = w.plain_dest()
    victim = w.tmp / "victim"
    victim.write_text("v")
    swap_after_validation(monkeypatch, lambda: os.symlink(victim, dest / "a"))
    r = w.ex(s, dest, ["a"])
    assert victim.read_text() == "v" and r.code in (0, 1)
    if r.code == 0:  # replaced by rename: the link itself is gone, the victim untouched
        assert not os.path.islink(dest / "a")


def test_deletion_target_replaced_by_directory_after_validation(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = w.src()
    dest = w.plain_dest()
    (dest / "x").write_text("1")

    def action() -> None:
        os.unlink(dest / "x")
        os.mkdir(dest / "x")
        (dest / "x" / "inner").write_text("i")

    swap_after_validation(monkeypatch, action)
    e = w.failed(w.ex(s, dest, None, ["x"]), 1)
    assert e["code"] == "io_error" and e["details"]["path"] == "x"
    assert tree(dest) == {"x": "dir", "x/inner": "i"}


def test_deletion_target_replaced_by_symlink_never_deletes_the_target(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = w.src()
    dest = w.plain_dest()
    (dest / "x").write_text("1")
    victim = w.tmp / "victim"
    victim.write_text("v")

    def action() -> None:
        os.unlink(dest / "x")
        os.symlink(victim, dest / "x")

    swap_after_validation(monkeypatch, action)
    w.ex(s, dest, None, ["x"])
    assert victim.read_text() == "v"


def test_source_file_swapped_for_a_symlink_before_it_is_read(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import fsafe

    s = w.src()
    secret = w.tmp / "secret"
    secret.write_text("TOPSECRET")
    w.put(s, "d/f", b"ok")
    real_open = fsafe.open_file
    calls: list[str] = []

    def swapping(anchor: Any, parent_fd: int, name: str, **kw: Any) -> int:
        if name == "f" and not calls:
            calls.append(name)
            os.unlink("f", dir_fd=parent_fd)
            os.symlink(secret, "f", dir_fd=parent_fd)
        return real_open(anchor, parent_fd, name, **kw)

    monkeypatch.setattr(fsafe, "open_file", swapping)
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["d/f"]) == {"d/f": "symlink"}
    assert tree(dest) == {}


def test_source_directory_swapped_for_a_symlink_before_it_is_opened(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import fsafe

    s = w.src()
    outside = w.tmp / "outside"
    outside.mkdir()
    (outside / "f").write_text("TOPSECRET")
    w.put(s, "d/f", b"ok")
    real_open_dir = fsafe.open_dir
    calls: list[str] = []

    def swapping(anchor: Any, parent_fd: int, name: str, **kw: Any) -> int:
        if name == "d" and not calls:
            calls.append(name)
            os.rename("d", "d.old", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.symlink(outside, "d", dir_fd=parent_fd)
        return real_open_dir(anchor, parent_fd, name, **kw)

    monkeypatch.setattr(fsafe, "open_dir", swapping)
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["d/f"]) == {"d/f": "symlink"}
    assert tree(dest) == {}


def test_source_file_growing_past_the_limit_while_it_is_read(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl import fsafe

    w.top["extract"] = {"max_file_bytes": 10}
    s = w.src()
    p = w.put(s, "f", b"12345")
    real_open = fsafe.open_file

    def growing(anchor: Any, parent_fd: int, name: str, **kw: Any) -> int:
        fd = real_open(anchor, parent_fd, name, **kw)
        with p.open("ab") as f:
            f.write(b"x" * 100)
        return fd

    monkeypatch.setattr(fsafe, "open_file", growing)
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["f"]) == {"f": "too_large"}
    assert tree(dest) == {}


# -- io_error ----------------------------------------------------------------------------------


def test_write_failure_reports_exact_progress_and_cleans_up(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = w.src()
    for n in ("a", "b", "c"):
        w.put(s, n, n.encode())
    dest = w.plain_dest()
    (dest / "old").write_text("o")
    real_rename = os.rename
    seen: list[str] = []

    def failing(src: str, dst: str, **kw: Any) -> None:
        seen.append(dst)
        if dst == "c":
            raise OSError(errno.ENOSPC, "No space left on device")
        real_rename(src, dst, **kw)

    patch_os(monkeypatch, "rename", failing)
    e = w.failed(w.ex(s, dest, ["a", "b", "c"], ["old"]), 1)
    assert e["code"] == "io_error"
    assert e["details"] == {"path": "c", "written": ["a", "b"], "deleted": []}
    assert sorted(tree(dest)) == ["a", "b", "old"]  # no temp file, c absent, old not deleted


def test_delete_failure_reports_written_and_deleted(w: XW, monkeypatch: pytest.MonkeyPatch) -> None:
    s = w.src()
    w.put(s, "a", b"1")
    dest = w.plain_dest()
    for n in ("d1", "d2", "d3"):
        (dest / n).write_text(n)
    real_unlink = os.unlink

    def failing(path: str, **kw: Any) -> None:
        if path == "d2":
            raise OSError(errno.EIO, "Input/output error")
        real_unlink(path, **kw)

    patch_os(monkeypatch, "unlink", failing)
    e = w.failed(w.ex(s, dest, ["a"], ["d1", "d2", "d3"]), 1)
    assert e["details"] == {"path": "d2", "written": ["a"], "deleted": ["d1"]}
    assert sorted(tree(dest)) == ["a", "d2", "d3"]


def test_io_error_is_logged_at_error_level_and_lists_stay_valid(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = one(w)
    dest = w.plain_dest()

    def failing(src: str, dst: str, **kw: Any) -> None:
        raise OSError(errno.EROFS, "Read-only file system")

    patch_os(monkeypatch, "rename", failing)
    w.failed(w.ex(s, dest, ["a"]), 1)
    ev = [e for e in w.log_events() if e["event"] == "extract"]
    assert ev and ev[-1]["level"] == "error" and ev[-1]["details"]["code"] == "io_error"
    assert w.leases() == []
    assert not [n for n in tree(dest) if n.startswith(".wtctl")]


def test_unwritable_dest_directory_is_io_error_with_nothing_written(w: XW) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory modes")
    s = one(w)
    dest = w.plain_dest()
    dest.chmod(0o500)
    try:
        e = w.failed(w.ex(s, dest, ["a"]), 1)
    finally:
        dest.chmod(0o700)
    assert e["code"] == "io_error" and e["details"]["written"] == []


# -- a throwaway destination (the clean checkout) -----------------------------------------------


def clean(w: XW) -> dict[str, Any]:
    return w.made("--source", "remote")


def test_extract_into_an_active_throwaway(w: XW) -> None:
    s = w.src()
    w.put(s, "n/new.txt", b"fresh")
    c = clean(w)
    data = w.extracted(s, c["path"], ["n/new.txt"], ["c1.txt"])
    assert (Path(c["path"]) / "n/new.txt").read_text() == "fresh"
    assert not (Path(c["path"]) / "c1.txt").exists()
    assert data["deleted"] == [{"path": "c1.txt", "result": "deleted"}]
    assert w.leases() == []


def test_extract_into_a_subdirectory_of_a_throwaway(w: XW) -> None:
    s = one(w)
    c = clean(w)
    sub = Path(c["path"]) / "sub"
    sub.mkdir()
    w.extracted(s, sub, ["a"])
    assert (sub / "a").exists()


def test_throwaway_destination_must_be_active(w: XW) -> None:
    s = one(w)
    c = clean(w)
    w.set_state(c["id"], "partial")
    assert w.ex(s, c["path"], ["a"]).code == 2
    assert not (Path(c["path"]) / "a").exists()


def test_throwaway_destination_lease_is_held_and_busy_when_taken(w: XW) -> None:
    from wtctl.gateway import Gateway
    from wtctl.state import State, record_lease

    s = one(w)
    c = clean(w)
    st = State(w.db, Gateway())
    lease = st.lease(record_lease(c["id"]), wait=1)
    try:
        e = w.failed(w.ex(s, c["path"], ["a"]), 12)
        assert e["code"] == "busy"
    finally:
        lease.release()
        st.close()
    assert not (Path(c["path"]) / "a").exists()


def test_throwaway_destination_replaced_is_a_dest_conflict(w: XW) -> None:
    s = one(w)
    c = clean(w)
    os.rename(c["path"], w.tmp / "moved")  # keeps the old inode alive: no number reuse
    os.mkdir(c["path"])  # same path, other identity
    assert w.invalid(s, c["path"], ["a"]) == {"a": "dest_conflict"}
    assert tree(Path(c["path"])) == {}


def test_extract_into_itself_is_refused(w: XW) -> None:
    s = one(w)
    assert w.ex(s, s["path"], ["a"]).code == 2


def test_dotgit_of_a_clean_throwaway_can_never_be_written(w: XW) -> None:
    s = w.src()
    c = clean(w)
    git = Path(c["path"]) / ".git"
    before = tree(git)
    assert w.invalid(s, c["path"], [".git/hooks/pre-commit"], [".git/config"]) == {
        ".git/hooks/pre-commit": "git_component",
        ".git/config": "git_component",
    }
    assert tree(git) == before


# -- mount points ------------------------------------------------------------------------------


class MountFs(fsafe.Fs):
    """Pretends every directory named ``mnt`` below ``under`` is the root of another mount."""

    def __init__(self, under: str) -> None:
        self.under = under

    def mount_id(self, fd: int) -> int | None:
        path = os.readlink(f"/proc/self/fd/{fd}")
        return 99 if path.endswith("/mnt") and self.under in path else 1


@pytest.fixture
def mounts(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("needs /proc")
    under = "/throwaway/" if "source" in request.node.name else "/dest/"
    monkeypatch.setattr(fsafe, "DEFAULT_FS", MountFs(under))


def test_destination_component_that_is_a_mount_point_is_a_conflict(w: XW, mounts: None) -> None:
    s = w.src()
    w.put(s, "mnt/f", b"1")
    w.put(s, "ok", b"2")
    dest = w.plain_dest()
    (dest / "mnt").mkdir()
    assert w.invalid(s, dest, ["mnt/f", "ok"]) == {"mnt/f": "dest_conflict"}
    assert tree(dest) == {"mnt": "dir"}


def test_deletion_below_a_destination_mount_point_is_a_conflict(w: XW, mounts: None) -> None:
    s = w.src()
    dest = w.plain_dest()
    (dest / "mnt").mkdir()
    (dest / "mnt" / "f").write_text("keep")
    assert w.invalid(s, dest, None, ["mnt/f"]) == {"mnt/f": "dest_conflict"}
    assert (dest / "mnt" / "f").read_text() == "keep"


def test_source_path_crossing_a_mount_point_is_not_found(w: XW, mounts: None) -> None:
    s = w.src()
    w.put(s, "mnt/f", b"secret")
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["mnt/f"]) == {"mnt/f": "not_found"}
    assert tree(dest) == {}


# -- review round: swaps between lstat and open, growth, identities, temp names ----------------


class SwapFs(fsafe.Fs):
    """After the first ``lstat_at`` of ``name`` the file is replaced by ``make(dir_fd, name)``."""

    def __init__(self, name: str, make: Callable[[int, str], None]) -> None:
        self.name, self.make, self.done = name, make, False

    def lstat_at(self, dir_fd: int, name: str) -> fsafe.FdStat:
        st = super().lstat_at(dir_fd, name)
        if name == self.name and not self.done:
            self.done = True
            os.unlink(name, dir_fd=dir_fd)
            self.make(dir_fd, name)
        return st


def test_source_file_swapped_for_a_symlink_between_lstat_and_open(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = w.src()
    secret = w.tmp / "secret"
    secret.write_text("TOPSECRET")
    w.put(s, "d/f", b"ok")
    fs = SwapFs("f", lambda fd, n: os.symlink(secret, n, dir_fd=fd))
    monkeypatch.setattr(fsafe, "DEFAULT_FS", fs)
    dest = w.plain_dest()
    errs = w.invalid(s, dest, ["d/f"])
    assert fs.done and errs["d/f"] in ("symlink", "not_found")
    assert tree(dest) == {}


def test_source_file_swapped_for_a_fifo_between_lstat_and_open(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = w.src()
    w.put(s, "d/f", b"ok")
    fs = SwapFs("f", lambda fd, n: os.mkfifo(n, 0o600, dir_fd=fd))
    monkeypatch.setattr(fsafe, "DEFAULT_FS", fs)
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["d/f"]) == {"d/f": "not_regular"}
    assert fs.done and tree(dest) == {}


class _OsProxy:
    """``extraction.os`` with ``fstat`` and ``read`` replaceable."""

    def __init__(self, **over: Callable[..., Any]) -> None:
        self.over = over

    def __getattr__(self, name: str) -> Any:
        return self.over.get(name) or getattr(os, name)


def test_file_growing_after_its_size_was_read_is_too_large(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.top["extract"] = {"max_file_bytes": 10}
    s = w.src()
    p = w.put(s, "f", b"12345")
    grown: list[bool] = []

    def fstat(fd: int) -> os.stat_result:
        st = os.fstat(fd)
        if not grown and st.st_ino == p.stat().st_ino:
            grown.append(True)
            with p.open("ab") as f:
                f.write(b"x" * 100)
        return st

    monkeypatch.setattr(extraction, "os", _OsProxy(fstat=fstat))
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["f"]) == {"f": "too_large"}
    assert grown and tree(dest) == {}


def test_file_growing_by_one_byte_after_the_first_read_is_too_large(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.top["extract"] = {"max_file_bytes": 10}
    s = w.src()
    p = w.put(s, "f", b"1234567890")
    grown: list[bool] = []

    def read(fd: int, n: int) -> bytes:
        data = os.read(fd, n)
        if not grown:
            grown.append(True)
            with p.open("ab") as f:
                f.write(b"x")
        return data

    monkeypatch.setattr(extraction, "os", _OsProxy(read=read))
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["f"]) == {"f": "too_large"}
    assert grown and tree(dest) == {}


def test_destination_throwaway_moved_under_another_root_is_a_dest_conflict(w: XW) -> None:
    """The recorded root identity is what notices this: the directory itself is the same inode."""
    from wtctl.gateway import Gateway
    from wtctl.state import State

    c = clean(w)
    st = State(w.db, Gateway())
    rec = st.get_record(c["id"])
    st.close()
    assert rec is not None
    real = str(Path(c["path"]).resolve())
    other = w.tmp / "other-root"
    other.mkdir()
    os.rename(c["path"], other / c["id"])  # same inode, same name, another parent
    moved_root = Path(rec["base_dir"]).rename(w.tmp / "old-root")
    other.rename(rec["base_dir"])
    assert moved_root.exists()
    with pytest.raises(fsafe.FsafeError):
        extract_dest.Dest.open(real, rec)


def test_dotgit_can_never_be_written_directly(w: XW) -> None:
    dest = w.plain_dest()
    with extract_dest.Dest.open(str(dest), None) as d:
        for parts in (["a", ".git", "x"], [".git"], [".git", "x"], ["a", ".GIT"]):
            with pytest.raises((fsafe.FsafeError, extract_dest.DestConflict)):
                d.write(parts, b"")
    assert not any(".git" in n.upper() for n in tree(dest))


def fixed_tokens(monkeypatch: pytest.MonkeyPatch, *tokens: str) -> None:
    it = iter(tokens)
    fake = types.SimpleNamespace(token_hex=lambda n=8: next(it))
    monkeypatch.setattr(extract_dest, "secrets", fake)


def test_temp_name_that_is_a_symlink_is_never_written_through(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = w.plain_dest()
    victim = w.tmp / "victim"  # does not exist: a dangling target
    os.symlink(victim, dest / ".wtctl-extract-aa")
    fixed_tokens(monkeypatch, "aa", "bb")
    with extract_dest.Dest.open(str(dest), None) as d:
        assert d.write(["f"], b"new") == (0o644, True)
    assert not victim.exists()
    assert os.readlink(dest / ".wtctl-extract-aa") == str(victim)
    assert (dest / "f").read_bytes() == b"new"
    assert sorted(tree(dest)) == [".wtctl-extract-aa", "f"]


def test_temp_name_that_is_a_regular_file_is_never_overwritten(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = w.plain_dest()
    (dest / ".wtctl-extract-aa").write_text("mine")
    fixed_tokens(monkeypatch, "aa", "bb")
    with extract_dest.Dest.open(str(dest), None) as d:
        d.write(["f"], b"new")
    assert (dest / ".wtctl-extract-aa").read_text() == "mine"
    assert sorted(tree(dest)) == [".wtctl-extract-aa", "f"]


def test_temp_name_collisions_give_up_after_a_few_tries(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = w.plain_dest()
    (dest / ".wtctl-extract-aa").write_text("mine")
    monkeypatch.setattr(extract_dest, "secrets", types.SimpleNamespace(token_hex=lambda n=8: "aa"))
    with extract_dest.Dest.open(str(dest), None) as d:
        with pytest.raises(fsafe.FsafeError):
            d.write(["f"], b"new")
    assert tree(dest) == {".wtctl-extract-aa": "mine"}


class TableFs(fsafe.Fs):
    """A mount table naming ``points`` as mount points."""

    def __init__(self, points: list[str]) -> None:
        self.points = points

    def mount_table(self) -> list[Any]:
        return [gateway.MountEntry(1, p, "ext4", "x") for p in self.points]


class DevFs(fsafe.Fs):
    """The directory or file with inode ``ino`` reports another device."""

    def __init__(self, ino: int) -> None:
        self.ino = ino

    def adjust(self, st: fsafe.FdStat) -> fsafe.FdStat:
        return dataclasses.replace(st, dev=st.dev + 1) if st.ino == self.ino else st


def test_source_path_listed_in_the_mount_table_is_not_found(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = w.src()
    w.put(s, "d/m", b"secret")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", TableFs([str(Path(s["path"]).resolve() / "d" / "m")]))
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["d/m"]) == {"d/m": "not_found"}
    assert tree(dest) == {}


def test_destination_component_listed_in_the_mount_table_is_a_conflict(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = one(w)
    w.put(s, "sub/f", b"2")
    c = clean(w)
    sub = Path(c["path"]).resolve() / "sub"
    sub.mkdir()
    monkeypatch.setattr(fsafe, "DEFAULT_FS", TableFs([str(sub)]))
    assert w.invalid(s, c["path"], ["a", "sub/f"]) == {"sub/f": "dest_conflict"}
    assert tree(sub) == {}


def test_source_file_on_another_device_is_not_found(w: XW, monkeypatch: pytest.MonkeyPatch) -> None:
    s = w.src()
    p = w.put(s, "f", b"secret")
    monkeypatch.setattr(fsafe, "DEFAULT_FS", DevFs(p.stat().st_ino))
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["f"]) == {"f": "not_found"}
    assert tree(dest) == {}


def test_destination_directory_on_another_device_is_a_conflict(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = w.src()
    w.put(s, "a", b"1")
    w.put(s, "sub/f", b"2")
    dest = w.plain_dest()
    (dest / "sub").mkdir()
    monkeypatch.setattr(fsafe, "DEFAULT_FS", DevFs((dest / "sub").stat().st_ino))
    assert w.invalid(s, dest, ["a", "sub/f"]) == {"sub/f": "dest_conflict"}
    assert tree(dest) == {"sub": "dir"}


def test_destination_record_turning_inactive_before_the_lease_is_busy(
    w: XW, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wtctl.commands import extract as cmd

    s = one(w)
    c = clean(w)
    real = cmd.Extractor.check_dest

    def check_then_change(self: Any, source: Any) -> None:
        real(self, source)
        w.set_state(c["id"], "partial")

    monkeypatch.setattr(cmd.Extractor, "check_dest", check_then_change)
    e = w.failed(w.ex(s, c["path"], ["a"]), 12)
    assert e["code"] == "busy"
    assert not (Path(c["path"]) / "a").exists()


def test_io_error_message_is_escaped(w: XW, monkeypatch: pytest.MonkeyPatch) -> None:
    s = one(w)
    dest = w.plain_dest()

    def failing(src: str, dst: str, **kw: Any) -> None:
        raise OSError(errno.EIO, "bad\x1b[31m\x07 news")

    patch_os(monkeypatch, "rename", failing)
    r = w.ex(s, dest, ["a"])
    e = w.failed(r, 1)
    assert "\x1b" not in r.out + r.err and "\x07" not in r.out + r.err
    assert "\\x1b" in e["message"]
