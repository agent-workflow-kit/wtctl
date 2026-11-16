"""``wtctl extract``: the happy paths, all-or-nothing validation, source rules, record states."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path

import pytest

from tests.extractlib import XW, tree
from wtctl.gateway import ProcessInfo

pytestmark = pytest.mark.usefixtures("inproc")


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> XW:
    return XW(tmp_path, private_home).build()


# -- happy path --------------------------------------------------------------------------------


def test_copies_files_and_reports_them(w: XW) -> None:
    s = w.src()
    w.put(s, "a.txt", b"hello")
    w.put(s, "d/e/f.bin", b"\x00\x01\x02", mode=0o755)
    dest = w.plain_dest()
    data = w.extracted(s, dest, ["a.txt", "d/e/f.bin"])
    assert (dest / "a.txt").read_bytes() == b"hello"
    assert (dest / "d/e/f.bin").read_bytes() == b"\x00\x01\x02"
    assert data["record_id"] == s["id"] and data["dest"] == str(dest.resolve())
    assert data["dry_run"] is False and data["deleted"] == []
    assert data["written"] == [
        {"path": "a.txt", "bytes": 5, "mode": "0644", "created": True},
        {"path": "d/e/f.bin", "bytes": 3, "mode": "0644", "created": True},
    ]


def test_source_modes_are_never_copied(w: XW) -> None:
    s = w.src()
    w.put(s, "x", b"x", mode=0o777)
    dest = w.plain_dest()
    w.extracted(s, dest, ["x"])
    assert stat.S_IMODE((dest / "x").stat().st_mode) == 0o644


def test_missing_directories_get_mode_0755(w: XW) -> None:
    s = w.src()
    w.put(s, "a/b/c", b"x")
    dest = w.plain_dest()
    old = os.umask(0o077)
    try:
        w.extracted(s, dest, ["a/b/c"])
    finally:
        os.umask(old)
    assert stat.S_IMODE((dest / "a").stat().st_mode) == 0o755
    assert stat.S_IMODE((dest / "a/b").stat().st_mode) == 0o755
    assert stat.S_IMODE((dest / "a/b/c").stat().st_mode) == 0o644


def test_existing_file_keeps_its_mode_and_is_replaced(w: XW) -> None:
    s = w.src()
    w.put(s, "x", b"new")
    dest = w.plain_dest()
    t = dest / "x"
    t.write_text("old")
    t.chmod(0o750)
    data = w.extracted(s, dest, ["x"])
    assert t.read_text() == "new" and stat.S_IMODE(t.stat().st_mode) == 0o750
    assert data["written"] == [{"path": "x", "bytes": 3, "mode": "0750", "created": False}]


def test_setuid_bits_of_an_existing_target_are_dropped(w: XW) -> None:
    s = w.src()
    w.put(s, "x", b"new")
    dest = w.plain_dest()
    t = dest / "x"
    t.write_text("old")
    t.chmod(0o4755)
    w.extracted(s, dest, ["x"])
    assert stat.S_IMODE(t.stat().st_mode) == 0o755


def test_an_existing_hard_linked_target_is_replaced_not_written_through(w: XW) -> None:
    s = w.src()
    w.put(s, "x", b"new")
    dest = w.plain_dest()
    other = w.tmp / "other"
    other.write_text("precious")
    os.link(other, dest / "x")
    w.extracted(s, dest, ["x"])
    assert (dest / "x").read_text() == "new" and other.read_text() == "precious"


def test_empty_file_and_unicode_names(w: XW) -> None:
    s = w.src()
    w.put(s, "é/日本.txt", b"")
    dest = w.plain_dest()
    data = w.extracted(s, dest, ["é/日本.txt"])
    assert (dest / "é/日本.txt").read_bytes() == b""
    assert data["written"][0]["bytes"] == 0


def test_nul_separated_lists_allow_newlines_in_names(w: XW) -> None:
    s = w.src()
    w.put(s, "a b", b"1")
    w.put(s, "c", b"2")
    dest = w.plain_dest()
    w.extracted(s, dest, ["a b", "c"], nul=True)
    assert sorted(tree(dest)) == ["a b", "c"]


def test_no_temp_files_are_left(w: XW) -> None:
    s = w.src()
    w.put(s, "d/x", b"1")
    dest = w.plain_dest()
    w.extracted(s, dest, ["d/x"])
    assert sorted(tree(dest)) == ["d", "d/x"]


def test_record_is_selected_by_id_prefix_or_path(w: XW) -> None:
    s = w.src()
    w.put(s, "x", b"1")
    d1, d2 = w.plain_dest("d1"), w.plain_dest("d2")
    w.extracted(s["id"][:8], d1, ["x"])
    w.extracted(s["path"], d2, ["x"])
    assert (d1 / "x").exists() and (d2 / "x").exists()


def test_stdin_list(w: XW, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sys

    s = w.src()
    w.put(s, "x", b"1")
    dest = w.plain_dest()

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"x\n")))
    r = w.run("extract", s["id"], "--dest", str(dest), "--files-from", "-")
    assert r.code == 0, r.err
    assert (dest / "x").read_text() == "1"


def test_both_lists_from_stdin_is_usage(w: XW, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sys

    s = w.src()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"x\n")))  # readable
    r = w.run("extract", s["id"], "--dest", str(w.plain_dest()), "--files-from", "-",
              "--delete-from", "-")  # fmt: skip
    e = w.failed(r, 2)
    assert "only one" in e["message"]


def test_at_least_one_list_is_required(w: XW) -> None:
    s = w.src()
    assert w.run("extract", s["id"], "--dest", str(w.plain_dest())).code == 2


def test_missing_list_file_is_usage(w: XW) -> None:
    s = w.src()
    r = w.run("extract", s["id"], "--dest", str(w.plain_dest()), "--files-from", "/nonexistent")
    assert r.code == 2


def test_list_over_the_size_limit_is_usage(w: XW, monkeypatch: pytest.MonkeyPatch) -> None:
    from wtctl.commands import extract as cmd

    monkeypatch.setattr(cmd, "LIST_LIMIT", 8)
    s = w.src()
    w.put(s, "a", b"1")
    dest = w.plain_dest()
    assert w.ex(s, dest, ["123456789"]).code == 2  # 10 bytes with the newline
    assert w.ex(s, dest, ["1234567"]).code != 2  # exactly 8 bytes is within the limit
    assert tree(dest) == {}


def test_empty_lists_write_nothing_and_succeed(w: XW) -> None:
    s = w.src()
    dest = w.plain_dest()
    data = w.extracted(s, dest, [], [])
    assert data["written"] == [] and data["deleted"] == [] and tree(dest) == {}


# -- deletions ---------------------------------------------------------------------------------


def test_deletes_listed_files_and_reports_absent(w: XW) -> None:
    s = w.src()
    dest = w.plain_dest()
    (dest / "d").mkdir()
    (dest / "d" / "gone").write_text("x")
    (dest / "keep").write_text("k")
    data = w.extracted(s, dest, None, ["d/gone", "d/never", "nodir/never"])
    assert data["deleted"] == [
        {"path": "d/gone", "result": "deleted"},
        {"path": "d/never", "result": "absent"},
        {"path": "nodir/never", "result": "absent"},
    ]
    assert tree(dest) == {"d": "dir", "keep": "k"}  # directories stay, nothing else touched


def test_deleting_a_directory_target_is_a_dest_conflict_and_deletes_nothing(w: XW) -> None:
    s = w.src()
    dest = w.plain_dest()
    (dest / "a").write_text("a")
    (dest / "dir").mkdir()
    (dest / "dir" / "inner").write_text("i")
    errs = w.invalid(s, dest, None, ["a", "dir"])
    assert errs == {"dir": "dest_conflict"}
    assert tree(dest) == {"a": "a", "dir": "dir", "dir/inner": "i"}


def test_deleting_a_symlink_target_is_a_dest_conflict(w: XW) -> None:
    s = w.src()
    dest = w.plain_dest()
    victim = w.tmp / "victim"
    victim.write_text("v")
    os.symlink(victim, dest / "l")
    assert w.invalid(s, dest, None, ["l"]) == {"l": "dest_conflict"}
    assert victim.read_text() == "v" and os.path.islink(dest / "l")


def test_deletion_never_follows_a_symlinked_directory(w: XW) -> None:
    s = w.src()
    dest = w.plain_dest()
    outside = w.tmp / "outside"
    outside.mkdir()
    (outside / "f").write_text("keep")
    os.symlink(outside, dest / "ld")
    assert w.invalid(s, dest, None, ["ld/f"]) == {"ld/f": "dest_conflict"}
    assert (outside / "f").read_text() == "keep"


def test_a_file_in_both_roles_is_a_duplicate(w: XW) -> None:
    s = w.src()
    w.put(s, "x", b"1")
    dest = w.plain_dest()
    assert "x" in w.invalid(s, dest, ["x"], ["x"])
    assert tree(dest) == {}


def test_writes_and_deletes_together(w: XW) -> None:
    s = w.src()
    w.put(s, "new", b"1")
    dest = w.plain_dest()
    (dest / "old").write_text("o")
    data = w.extracted(s, dest, ["new"], ["old"])
    assert tree(dest) == {"new": "1"}
    assert [x["path"] for x in data["written"]] == ["new"]
    assert data["deleted"] == [{"path": "old", "result": "deleted"}]


# -- dry run -----------------------------------------------------------------------------------


def test_dry_run_reports_and_changes_nothing(w: XW) -> None:
    s = w.src()
    w.put(s, "a/new", b"123")
    w.put(s, "old", b"9")
    dest = w.plain_dest()
    (dest / "old").write_text("o")
    (dest / "old").chmod(0o640)
    (dest / "del").write_text("d")
    before = tree(dest)
    n_events = len(w.log_events())
    data = w.extracted(s, dest, ["a/new", "old"], ["del", "absent"], "--dry-run")
    assert tree(dest) == before and data["dry_run"] is True
    assert data["written"] == [
        {"path": "a/new", "bytes": 3, "mode": "0644", "created": True},
        {"path": "old", "bytes": 1, "mode": "0640", "created": False},
    ]
    assert data["deleted"] == [
        {"path": "del", "result": "deleted"},
        {"path": "absent", "result": "absent"},
    ]
    assert len(w.log_events()) == n_events


def test_dry_run_still_fails_on_invalid_input(w: XW) -> None:
    s = w.src()
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["../x"], None, "--dry-run") == {"../x": "invalid_path"}


# -- all or nothing ----------------------------------------------------------------------------


def test_one_bad_entry_writes_nothing_and_deletes_nothing(w: XW) -> None:
    s = w.src()
    w.put(s, "good", b"1")
    dest = w.plain_dest()
    (dest / "victim").write_text("v")
    errs = w.invalid(s, dest, ["good", "missing"], ["victim"])
    assert errs == {"missing": "not_found"}
    assert tree(dest) == {"victim": "v"}


def test_every_problem_is_reported(w: XW) -> None:
    s = w.src()
    w.put(s, "good", b"1")
    w.put(s, "big", b"x" * 20)
    os.symlink("good", Path(s["path"]) / "link")
    os.mkdir(Path(s["path"]) / "adir")
    dest = w.plain_dest()
    errs = w.invalid(
        s, dest, ["good", "../x", ".git/config", "nope", "link", "adir", "big"], None,
        "--max-size", "10",
    )  # fmt: skip
    assert errs == {
        "../x": "invalid_path",
        ".git/config": "git_component",
        "nope": "not_found",
        "link": "symlink",
        "adir": "not_regular",
        "big": "too_large",
    }
    assert tree(dest) == {}


def test_invalid_path_does_not_open_anything(w: XW) -> None:
    s = w.src()
    dest = w.plain_dest()
    errs = w.invalid(s, dest, ["/etc/passwd", "a\\b", "a//b"])
    assert set(errs.values()) == {"invalid_path"}


# -- source rules ------------------------------------------------------------------------------


def test_symlink_source_is_refused_even_when_it_points_inside(w: XW) -> None:
    s = w.src()
    w.put(s, "real", b"1")
    os.symlink("real", Path(s["path"]) / "l")
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["l"]) == {"l": "symlink"}


def test_symlink_to_a_secret_outside_is_never_read(w: XW) -> None:
    s = w.src()
    secret = w.tmp / "secret"
    secret.write_text("TOPSECRET")
    os.symlink(secret, Path(s["path"]) / "l")
    dest = w.plain_dest()
    r = w.ex(s, dest, ["l"])
    assert r.code == 12 and "TOPSECRET" not in r.out + r.err
    assert tree(dest) == {}


def test_symlinked_source_directory_component_is_refused(w: XW) -> None:
    s = w.src()
    outside = w.tmp / "outside"
    outside.mkdir()
    (outside / "f").write_text("outside")
    os.symlink(outside, Path(s["path"]) / "d")
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["d/f"]) == {"d/f": "symlink"}
    assert tree(dest) == {}


def test_hard_linked_source_is_refused(w: XW) -> None:
    s = w.src()
    other = w.tmp / "other"
    other.write_text("x")
    os.link(other, Path(s["path"]) / "h")
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["h"]) == {"h": "hard_link"}


def test_hard_link_inside_the_throwaway_is_refused_too(w: XW) -> None:
    s = w.src()
    p = w.put(s, "a", b"1")
    os.link(p, Path(s["path"]) / "b")
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["a", "b"]) == {"a": "hard_link", "b": "hard_link"}


def test_fifo_socket_and_directory_sources_are_not_regular(w: XW) -> None:
    s = w.src()
    base = Path(s["path"])
    os.mkfifo(base / "fifo")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(base / "sock"))
    sock.close()
    (base / "dir").mkdir()
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["fifo", "sock", "dir"]) == {
        "fifo": "not_regular",
        "sock": "not_regular",
        "dir": "not_regular",
    }


def test_device_source_is_not_regular(w: XW) -> None:
    s = w.src()
    base = Path(s["path"])
    try:
        os.mknod(base / "dev", stat.S_IFCHR | 0o600, os.makedev(1, 3))
    except PermissionError:
        pytest.skip("cannot create device nodes")
    assert w.invalid(s, w.plain_dest(), ["dev"]) == {"dev": "not_regular"}


def test_missing_source_and_file_as_directory(w: XW) -> None:
    s = w.src()
    w.put(s, "f", b"1")
    errs = w.invalid(s, w.plain_dest(), ["nope", "f/x", "nodir/x"])
    assert errs == {"nope": "not_found", "f/x": "not_found", "nodir/x": "not_found"}


def test_planted_dotgit_content_is_not_extractable_and_inert(w: XW) -> None:
    s = w.src()
    base = Path(s["path"])
    hook_marker = w.tmp / "hook-ran"
    hook = base / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {hook_marker}\n")
    hook.chmod(0o755)
    cfg = base / ".git" / "config"
    with cfg.open("a") as f:
        f.write(f"[core]\n\tfsmonitor = touch {hook_marker}\n")
    w.put(s, "ok", b"1")
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["ok", ".git/config", ".git/hooks/post-checkout"]) == {
        ".git/config": "git_component",
        ".git/hooks/post-checkout": "git_component",
    }
    w.extracted(s, dest, ["ok"])
    assert not hook_marker.exists() and tree(dest) == {"ok": "1"}


def test_file_contents_are_data_not_executed_or_interpreted(w: XW) -> None:
    s = w.src()
    marker = w.tmp / "marker"
    w.put(s, "script.sh", f"#!/bin/sh\ntouch {marker}\n".encode(), mode=0o755)
    w.put(s, ".gitattributes", b"* filter=evil\n")
    w.put(s, ".gitmodules", b'[submodule "x"]\n\turl = ext::sh -c touch% ' + str(marker).encode())
    dest = w.plain_dest()
    w.extracted(s, dest, ["script.sh", ".gitattributes", ".gitmodules"])
    assert not marker.exists()
    assert stat.S_IMODE((dest / "script.sh").stat().st_mode) == 0o644


# -- limits ------------------------------------------------------------------------------------


def test_max_size_lowers_the_per_file_limit(w: XW) -> None:
    s = w.src()
    w.put(s, "small", b"x" * 10)
    w.put(s, "big", b"x" * 11)
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["small", "big"], None, "--max-size", "10") == {"big": "too_large"}
    w.extracted(s, dest, ["small"], None, "--max-size", "10")


def test_max_size_cannot_raise_the_configured_limit(w: XW) -> None:
    w.top["extract"] = {"max_file_bytes": 5}
    s = w.src()
    w.put(s, "f", b"123456")
    assert w.invalid(s, w.plain_dest(), ["f"], None, "--max-size", "1000") == {"f": "too_large"}


def test_max_size_must_be_positive(w: XW) -> None:
    s = w.src()
    assert w.ex(s, w.plain_dest(), [], None, "--max-size", "0").code == 2
    assert w.ex(s, w.plain_dest(), [], None, "--max-size", "-1").code == 2


def test_file_at_exactly_the_limit_is_accepted(w: XW) -> None:
    w.top["extract"] = {"max_file_bytes": 4}
    s = w.src()
    w.put(s, "f", b"1234")
    w.extracted(s, w.plain_dest(), ["f"])


def test_total_bytes_limit(w: XW) -> None:
    w.top["extract"] = {"max_file_bytes": 10, "max_total_bytes": 15}
    s = w.src()
    for n in "abc":
        w.put(s, n, b"x" * 8)
    dest = w.plain_dest()
    errs = w.invalid(s, dest, ["a", "b", "c"])
    assert errs == {"b": "limit_exceeded", "c": "limit_exceeded"}
    assert tree(dest) == {}


def test_count_limit(w: XW) -> None:
    w.top["extract"] = {"max_files": 2}
    s = w.src()
    for n in "abc":
        w.put(s, n, b"x")
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["a", "b", "c"]) == {"c": "limit_exceeded"}
    assert tree(dest) == {}


def test_huge_sparse_file_is_too_large_without_being_read(w: XW) -> None:
    s = w.src()
    p = Path(s["path"]) / "sparse"
    with p.open("wb") as f:
        f.truncate(8 * 1024**4)
    assert w.invalid(s, w.plain_dest(), ["sparse"]) == {"sparse": "too_large"}


def test_many_small_files_within_the_limit(w: XW) -> None:
    s = w.src()
    names = [f"d{i % 7}/f{i}" for i in range(300)]
    for n in names:
        w.put(s, n, n.encode())
    dest = w.plain_dest()
    data = w.extracted(s, dest, names)
    assert len(data["written"]) == 300 and (dest / "d3/f10").read_text() == "d3/f10"


# -- record states and actors ------------------------------------------------------------------


def test_worktree_is_usage_error(w: XW) -> None:
    r = w.run("create", "--repo", "example.test/org/repo", "--branch", "x1", "--base", "main")
    assert r.code == 0, r.err
    wt = r.json()["record"]
    assert w.ex(wt, w.plain_dest(), ["a"]).code == 2


def test_unknown_record_is_not_found(w: XW) -> None:
    assert w.ex("wt-aaaaaaaa", w.plain_dest(), ["a"]).code == 10


def test_removed_record_is_not_found_with_details(w: XW) -> None:
    s = w.src()
    w.removed(s["id"])
    e = w.failed(w.ex(s, w.plain_dest(), ["a"]), 10)
    assert e["details"]["state"] == "removed"


@pytest.mark.parametrize("state", ["creating", "partial"])
def test_creating_and_partial_records_are_unlinked(w: XW, state: str) -> None:
    s = w.src()
    w.put(s, "a", b"1")
    w.set_state(s["id"], state)
    e = w.failed(w.ex(s, w.plain_dest(), ["a"]), 20)
    assert e["reason"] == "unlinked"
    assert tree(w.plain_dest()) == {}


def test_vanished_directory_is_unlinked(w: XW) -> None:
    s = w.src()
    import shutil

    shutil.rmtree(s["path"])
    e = w.failed(w.ex(s, w.plain_dest(), ["a"]), 20)
    assert e["reason"] == "unlinked"


def test_vanished_directory_is_unlinked_for_a_delete_only_call(w: XW) -> None:
    import shutil

    s = w.src()
    dest = w.plain_dest()
    (dest / "a").write_text("keep")
    shutil.rmtree(s["path"])
    e = w.failed(w.ex(s, dest, None, ["a"]), 20)
    assert e["reason"] == "unlinked"
    assert (dest / "a").read_text() == "keep"


def test_vanished_directory_is_unlinked_when_every_entry_is_invalid_or_the_lists_are_empty(
    w: XW,
) -> None:
    import shutil

    s = w.src()
    shutil.rmtree(s["path"])
    for files, deletes in ((["../x"], None), (["a", "a"], ["a"]), ([], []), (None, [])):
        e = w.failed(w.ex(s, w.plain_dest(), files, deletes), 20)
        assert e["reason"] == "unlinked"


def test_replaced_directory_is_not_found_for_a_delete_only_call(w: XW) -> None:
    s = w.src()
    os.rename(s["path"], w.tmp / "moved")
    os.mkdir(s["path"])
    dest = w.plain_dest()
    (dest / "a").write_text("keep")
    assert w.invalid(s, dest, None, ["a"]) == {"a": "not_found"}
    assert (dest / "a").read_text() == "keep"


def test_replaced_directory_is_not_found_per_path_and_never_read(w: XW) -> None:
    s = w.src()

    os.rename(s["path"], w.tmp / "moved")  # keeps the old inode alive: no number reuse
    os.mkdir(s["path"])
    (Path(s["path"]) / "a").write_text("impostor")
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["a"]) == {"a": "not_found"}
    assert tree(dest) == {}


def test_symlinked_throwaway_directory_is_not_followed(w: XW) -> None:
    s = w.src()
    real = w.tmp / "real"
    os.rename(s["path"], real)
    os.symlink(real, s["path"])
    (real / "a").write_text("1")
    dest = w.plain_dest()
    r = w.ex(s, dest, ["a"])
    assert r.code in (12, 20) and tree(dest) == {}


def test_in_use_throwaway_is_refused(w: XW) -> None:
    s = w.src()
    w.put(s, "a", b"1")
    w.scanner.procs = [ProcessInfo(4242, "agent", s["path"], ())]
    e = w.failed(w.ex(s, w.plain_dest(), ["a"]), 20)
    assert e["reason"] == "in_use"
    assert tree(w.plain_dest()) == {}


def test_extract_does_not_need_a_human(w: XW) -> None:
    s = w.src()
    w.put(s, "a", b"1")
    assert w.ex(s, w.plain_dest(), ["a"], human=False).code == 0
    assert w.ex(s, w.plain_dest("d2"), ["a"], human=True).code == 0


def test_a_held_lease_is_busy(w: XW) -> None:
    from wtctl.gateway import Gateway
    from wtctl.state import State, record_lease

    s = w.src()
    st = State(w.db, Gateway())
    lease = st.lease(record_lease(s["id"]), wait=1)
    try:
        e = w.failed(w.ex(s, w.plain_dest(), ["a"]), 12)
        assert e["code"] == "busy"
    finally:
        lease.release()
        st.close()


def test_lease_is_released_and_nothing_else_is_left(w: XW) -> None:
    s = w.src()
    w.put(s, "a", b"1")
    w.extracted(s, w.plain_dest(), ["a"])
    assert w.leases() == []
    assert (w.troot / s["id"] / "a").read_text() == "1"  # the source is untouched


def test_extract_runs_no_git(w: XW) -> None:
    from tests.createlib import Spy

    s = w.src()
    w.put(s, "a", b"1")
    spy = Spy()
    w.git_runner = spy
    w.extracted(s, w.plain_dest(), ["a"])
    assert [x for x in spy.subs() if x != "version"] == []


# -- log, secrets ------------------------------------------------------------------------------


def test_success_is_logged_without_contents_and_not_audited(w: XW) -> None:
    s = w.src()
    w.put(s, "a", b"SECRETCONTENT")
    w.extracted(s, w.plain_dest(), ["a"])
    ev = [e for e in w.log_events() if e["event"] == "extract"]
    assert len(ev) == 1 and ev[0]["id"] == s["id"] and ev[0]["level"] == "info"
    assert ev[0]["details"]["written"] == 1 and ev[0]["details"]["deleted"] == 0
    assert "SECRETCONTENT" not in str(w.log_events())
    assert w.audit_records() == []


def test_log_carries_counts_only_never_paths(w: XW, monkeypatch: pytest.MonkeyPatch) -> None:
    s = w.src()
    w.put(s, "uniquewritten", b"1")
    dest = w.plain_dest("uniquedestdir")
    (dest / "uniquedeleted").write_text("x")
    w.extracted(s, dest, ["uniquewritten"], ["uniquedeleted"])
    w.ex(s, dest, ["../uniqueinvalid"])
    text = str(w.log_events())
    for needle in ("uniquewritten", "uniquedeleted", "uniqueinvalid", "uniquedestdir"):
        assert needle not in text
    assert all("dest" not in e["details"] for e in w.log_events() if e["event"] == "extract")


def test_refusal_is_logged(w: XW) -> None:
    s = w.src()
    w.ex(s, w.plain_dest(), ["../x"])
    ev = [e for e in w.log_events() if e["event"] == "extract"]
    assert len(ev) == 1 and ev[0]["level"] == "warn"
    assert ev[0]["details"]["code"] == "extract_invalid"


def test_hostile_names_are_escaped_in_the_error(w: XW) -> None:
    s = w.src()
    r = w.ex(s, w.plain_dest(), [b"a\x1b[31mred\x01", b"\xff\xfe"], nul=True)
    assert r.code == 12
    assert "\x1b" not in r.out + r.err and "\x01" not in r.out + r.err


def test_the_real_child_process_path(w: XW, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reading runs in a child under a deadline when no test runner replaces it."""
    from wtctl import removalfs

    monkeypatch.setattr(removalfs, "RUNNER", None)
    s = w.src()
    w.put(s, "d/a", b"hello")
    os.symlink("a", Path(s["path"]) / "d" / "l")
    dest = w.plain_dest()
    assert w.invalid(s, dest, ["d/a", "d/l"]) == {"d/l": "symlink"}
    w.extracted(s, dest, ["d/a"])
    assert (dest / "d" / "a").read_text() == "hello"


# -- review round: huge lists, display escaping, the total limit ---------------------------------


def test_a_huge_list_is_limit_exceeded_with_bounded_output(w: XW) -> None:
    w.top["extract"] = {"max_files": 10}
    s = w.src()
    for i in range(10):
        w.put(s, f"f{i}", b"1")
    names = [f"f{i}" for i in range(50_000)]
    r = w.ex(s, w.plain_dest(), names)
    e = w.failed(r, 12)
    assert e["code"] == "extract_invalid"
    paths = e["details"]["paths"]
    assert {"path": "f10", "error": "limit_exceeded"} in paths
    assert len(paths) == 1 and len(r.out) < 4096
    assert tree(w.plain_dest()) == {}


def test_per_path_errors_in_the_output_are_capped(w: XW) -> None:
    s = w.src()
    r = w.ex(s, w.plain_dest(), [f"../x{i}" for i in range(1000)])
    e = w.failed(r, 12)
    assert len(e["details"]["paths"]) == 100 and len(r.out) < 64 * 1024


def test_c1_bidi_and_line_separator_characters_are_escaped_in_the_error(w: XW) -> None:
    s = w.src()
    bad = [0x85, 0x9F, 0x202A, 0x202E, 0x2066, 0x2069, 0x2028, 0x2029]
    names = [f"a{chr(c)}b" for c in bad]
    r = w.ex(s, w.plain_dest(), [n.encode() for n in names], nul=True)
    e = w.failed(r, 12)
    shown = [p["path"] for p in e["details"]["paths"]]
    assert len(shown) == len(bad)
    for text in shown:
        assert all(not (0x80 <= ord(c) <= 0x9F or ord(c) in bad) for c in text)
    assert shown[0] == "a\\x85b" and shown[3] == "a\\u202eb"
    assert all(chr(c) not in r.out + r.err for c in bad)


def test_total_bytes_exactly_at_the_limit_is_accepted(w: XW) -> None:
    w.top["extract"] = {"max_file_bytes": 10, "max_total_bytes": 16}
    s = w.src()
    for n in "ab":
        w.put(s, n, b"x" * 8)
    assert len(w.extracted(s, w.plain_dest(), ["a", "b"])["written"]) == 2


def test_total_bytes_one_over_the_limit_is_rejected(w: XW) -> None:
    w.top["extract"] = {"max_file_bytes": 10, "max_total_bytes": 15}
    s = w.src()
    for n in "ab":
        w.put(s, n, b"x" * 8)
    assert w.invalid(s, w.plain_dest(), ["a", "b"]) == {"b": "limit_exceeded"}


def test_nothing_after_the_first_total_overflow_is_read(w: XW) -> None:
    w.top["extract"] = {"max_file_bytes": 10, "max_total_bytes": 15}
    s = w.src()
    w.put(s, "a", b"x" * 8)
    w.put(s, "b", b"x" * 8)
    w.put(s, "empty", b"")
    errs = w.invalid(s, w.plain_dest(), ["a", "b", "empty", "missing"])
    assert errs == {"b": "limit_exceeded", "empty": "limit_exceeded", "missing": "limit_exceeded"}
