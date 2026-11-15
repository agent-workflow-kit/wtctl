"""Bootstrap step 1 and 4: the before/after snapshots (docs/interface.md, Bootstrap)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from tests.bootlib import Repo
from wtctl import bootstrapper as b


@pytest.fixture
def r(tmp_path: Path, private_home: Path) -> Repo:
    return Repo(tmp_path, private_home)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snap(r: Repo) -> b.Snapshot:
    with r.open() as wt:
        return b.take_snapshot(r.git, wt)


def test_an_untouched_worktree_has_an_empty_snapshot(r: Repo) -> None:
    s = snap(r)
    assert s.untracked == {} and s.ignored == {} and s.tracked == set()


def test_untracked_files_are_listed_with_exact_paths_and_content_hashes(r: Repo) -> None:
    r.write("a.txt", "alpha")
    r.write("deep/er/b.bin", b"\x00\x01\x02")
    s = snap(r)
    assert s.untracked == {"a.txt": sha(b"alpha"), "deep/er/b.bin": sha(b"\x00\x01\x02")}


def test_ignored_directories_are_one_entry_and_ignored_files_are_listed(r: Repo) -> None:
    r.write("node_modules/pkg/index.js", "1")
    r.write("node_modules/pkg/deeper/x.js", "2")
    r.write("run.log", "log")
    r.write("src/trace.log", "log")
    s = snap(r)
    assert s.ignored == {"node_modules": True, "run.log": False, "src/trace.log": False}
    assert s.untracked == {}


def test_tracked_changes_are_reported_separately(r: Repo) -> None:
    r.write("README.md", "changed\n")
    s = snap(r)
    assert s.tracked == {"README.md"}
    assert s.untracked == {}


def test_unicode_and_newline_names_are_exact(r: Repo) -> None:
    r.write("café/日本.txt", "u")
    r.write("new\nline.txt", "n")
    r.write("sp ace.txt", "s")
    s = snap(r)
    assert set(s.untracked) == {"café/日本.txt", "new\nline.txt", "sp ace.txt"}
    assert s.untracked["new\nline.txt"] == sha(b"n")


def test_a_non_utf8_name_is_skipped_with_a_count_not_recorded(r: Repo) -> None:
    os.write(os.open(os.fsencode(str(r.wt)) + b"/bad\xff.txt", os.O_WRONLY | os.O_CREAT), b"x")
    r.write("ok.txt", "ok")
    s = snap(r)
    assert set(s.untracked) == {"ok.txt"}
    assert s.skipped == 1


def test_a_symlink_is_recorded_by_its_target_never_followed(r: Repo, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("TOP")
    os.symlink(secret, r.wt / "link")
    s = snap(r)
    assert s.untracked["link"] == "symlink:" + sha(str(secret).encode())
    assert sha(b"TOP") not in s.untracked["link"]


def test_a_fifo_is_never_opened_by_the_snapshot(r: Repo) -> None:
    os.mkfifo(r.wt / "pipe")
    r.write("ok.txt", "ok")
    s = snap(r)  # would hang if the snapshot opened the FIFO for reading
    assert s.untracked.get("pipe", "special:fifo") == "special:fifo"
    assert "ok.txt" in s.untracked


def test_a_file_that_vanishes_between_status_and_hashing_is_skipped(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    r.write("gone.txt", "g")
    r.write("stay.txt", "s")
    real = b._hash_entry

    def flaky(wt, cache, rel, budget):  # noqa: ANN001, ANN202
        if rel == "gone.txt":
            os.unlink(r.wt / "gone.txt")
        return real(wt, cache, rel, budget)

    monkeypatch.setattr(b, "_hash_entry", flaky)
    assert set(snap(r).untracked) == {"stay.txt"}


def test_a_nested_repository_is_a_directory_entry_not_a_hashed_file(r: Repo) -> None:
    nested = r.wt / "vendor" / "dep"
    nested.mkdir(parents=True)
    r.g(["init", "-q"], nested)
    (nested / "f.txt").write_text("f")
    s = snap(r)
    assert s.untracked_dirs == {"vendor/dep"}
    assert not any(p.startswith("vendor/dep") for p in s.untracked)


def test_thousands_of_files(r: Repo) -> None:
    for i in range(3000):
        r.write(f"many/d{i % 30}/f{i}.txt", str(i))
    s = snap(r)
    assert len(s.untracked) == 3000
    assert s.untracked["many/d7/f7.txt"] == sha(b"7")


def test_a_file_larger_than_the_hash_cap_is_not_hashed(
    r: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "HASH_FILE_CAP", 10)
    r.write("big.bin", b"x" * 11)
    r.write("small.bin", b"x" * 10)
    s = snap(r)
    assert s.untracked["big.bin"] == "unhashed:11"
    assert s.untracked["small.bin"] == sha(b"x" * 10)


def test_the_total_hash_budget_is_bounded(r: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b, "HASH_TOTAL_CAP", 25)
    for i in range(5):
        r.write(f"f{i}.bin", b"y" * 10)
    s = snap(r)
    hashed = [d for d in s.untracked.values() if not d.startswith("unhashed:")]
    assert len(hashed) == 2  # 10 + 10 fit in 25; the rest are marked unhashed


def test_status_is_hardened_a_planted_fsmonitor_does_not_run(r: Repo, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\necho hit >> {marker}\n")
    hook.chmod(0o755)
    cfg = Path(r.admin_dir) / "config.worktree"
    cfg.write_text(f"[core]\n\tfsmonitor = {hook}\n")
    r.g(["config", "extensions.worktreeConfig", "true"], r.main)
    r.write("a.txt", "a")
    assert "a.txt" in snap(r).untracked
    assert not marker.exists()


def test_the_worktree_must_have_the_recorded_identity(r: Repo) -> None:
    from wtctl.fsafe import FsafeError

    with pytest.raises(FsafeError) as exc:
        with b.open_worktree(str(r.wt), r.admin_dir, "1:1"):
            pass
    assert exc.value.kind == "identity"


def test_a_symlink_swapped_in_for_the_worktree_is_refused(r: Repo, tmp_path: Path) -> None:
    from wtctl.fsafe import FsafeError

    other = tmp_path / "other"
    other.mkdir()
    moved = tmp_path / "wt-moved"
    os.rename(r.wt, moved)
    os.symlink(other, r.wt)
    with pytest.raises(FsafeError):
        with r.open():
            pass
