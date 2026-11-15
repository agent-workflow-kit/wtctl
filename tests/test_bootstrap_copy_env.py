"""Bootstrap step 2: copy_env (docs/interface.md, Bootstrap)."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from tests.bootlib import Repo
from wtctl import bootstrapper as b


@pytest.fixture
def r(tmp_path: Path, private_home: Path) -> Repo:
    return Repo(tmp_path, private_home)


def copy(r: Repo, patterns: list[str], **kw) -> tuple[list[b.Copied], list[str]]:  # noqa: ANN003
    warns: list[str] = []
    with r.open() as wt:
        tracked = b.tracked_paths(r.git, wt)
        kw.setdefault("source_root", str(r.main))
        got = b.copy_env_files(wt, patterns=patterns, tracked=tracked, warn=warns.append, **kw)
    return got, warns


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_a_matching_file_is_copied_with_mode_0600_and_hashed(r: Repo) -> None:
    src = r.write(".env", "SECRET=1\n", root=r.main)
    src.chmod(0o777)
    got, warns = copy(r, [".env"])
    assert [(c.path, c.digest) for c in got] == [(".env", sha(b"SECRET=1\n"))]
    dst = r.wt / ".env"
    assert dst.read_text() == "SECRET=1\n"
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert warns == []


def test_globs_and_double_star_match_nested_files(r: Repo) -> None:
    r.write(".env.local", "a", root=r.main)
    r.write("apps/web/.env", "b", root=r.main)
    r.write("apps/web/deep/er/.env", "c", root=r.main)
    r.write("apps/web/other.txt", "no", root=r.main)
    got, _ = copy(r, [".env*", "apps/**/.env"])
    assert sorted(c.path for c in got) == [".env.local", "apps/web/.env", "apps/web/deep/er/.env"]
    assert (r.wt / "apps/web/deep/er/.env").read_text() == "c"
    assert not (r.wt / "apps/web/other.txt").exists()


def test_missing_directories_are_created_private(r: Repo) -> None:
    r.write("conf/prod/.env", "x", root=r.main)
    copy(r, ["conf/prod/.env"])
    assert stat.S_IMODE((r.wt / "conf").stat().st_mode) == 0o700


def test_a_symlinked_source_is_skipped_with_a_warning(r: Repo, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("TOP")
    os.symlink(outside, r.main / ".env")
    got, warns = copy(r, [".env"])
    assert got == [] and not (r.wt / ".env").exists()
    assert any(".env" in w and "symlink" in w for w in warns)


def test_a_hard_linked_source_is_skipped(r: Repo, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("TOP")
    os.link(outside, r.main / ".env")
    got, warns = copy(r, [".env"])
    assert got == [] and not (r.wt / ".env").exists()
    assert any("hard link" in w for w in warns)


def test_a_fifo_source_is_skipped_and_never_opened(r: Repo) -> None:
    os.mkfifo(r.main / ".env")
    got, warns = copy(r, [".env"])
    assert got == [] and warns


def test_a_directory_symlink_is_not_followed_by_a_double_star(r: Repo, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text("TOP")
    os.symlink(outside, r.main / "linkdir")
    got, _ = copy(r, ["**/.env", "linkdir/.env"])
    assert got == []
    assert not (r.wt / "linkdir").exists()


def test_a_huge_source_is_skipped(r: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b, "ENV_FILE_MAX_BYTES", 8)
    r.write(".env", "123456789", root=r.main)
    r.write(".env.small", "1234", root=r.main)
    got, warns = copy(r, [".env*"])
    assert [c.path for c in got] == [".env.small"]
    assert any("too large" in w for w in warns)


def test_the_file_count_is_bounded(r: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(b, "ENV_FILE_MAX_COUNT", 2)
    for i in range(4):
        r.write(f".env.{i}", "x", root=r.main)
    got, warns = copy(r, [".env.*"])
    assert len(got) == 2 and warns


@pytest.mark.parametrize(
    "pattern",
    ["../x", "a/../../x", "/etc/passwd", "a//b", ".", "..", "", "a/", ".git/config", ".GIT/config",
     "**/.git/config", "a\0b", ".git*/x", "[.]git/config", "sub\\..\\x"],
)  # fmt: skip
def test_hostile_patterns_copy_nothing_outside(r: Repo, pattern: str, tmp_path: Path) -> None:
    (tmp_path / "x").write_text("PARENT")
    r.write("a/b", "inner", root=r.main)
    got, _ = copy(r, [pattern])
    assert all(not c.path.startswith((".git", "..", "/")) for c in got)
    assert not (r.wt / "x").exists()
    assert not (tmp_path / "wt-x").exists()
    # nothing was written into the worktree's git pointer or the clone's git dir
    assert (r.wt / ".git").is_file()


def test_a_dotgit_alias_in_the_source_tree_is_never_matched(r: Repo) -> None:
    (r.main / ".GIT").mkdir()
    (r.main / ".GIT" / "secret.env").write_text("s")
    got, _ = copy(r, ["**/secret.env", ".GIT/secret.env"])
    assert got == []


def test_an_existing_untracked_destination_is_never_overwritten(r: Repo) -> None:
    r.write(".env", "NEW", root=r.main)
    r.write(".env", "AGENT", root=r.wt)
    got, warns = copy(r, [".env"])
    assert got == []
    assert (r.wt / ".env").read_text() == "AGENT"
    assert any("exists" in w for w in warns)


def test_a_destination_symlink_is_not_followed(r: Repo, tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("VICTIM")
    r.write(".env", "NEW", root=r.main)
    os.symlink(victim, r.wt / ".env")
    got, _ = copy(r, [".env"])
    assert got == [] and victim.read_text() == "VICTIM"


def test_a_dangling_destination_symlink_is_not_followed(r: Repo, tmp_path: Path) -> None:
    target = tmp_path / "not-yet"
    r.write(".env", "NEW", root=r.main)
    os.symlink(target, r.wt / ".env")
    got, _ = copy(r, [".env"])
    assert got == [] and not target.exists()


def test_a_symlinked_destination_directory_is_not_followed(r: Repo, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    r.write("conf/.env", "NEW", root=r.main)
    os.symlink(outside, r.wt / "conf")
    got, warns = copy(r, ["conf/.env"])
    assert got == [] and list(outside.iterdir()) == []
    assert warns


def test_a_tracked_destination_is_skipped(r: Repo) -> None:
    # src/app.py is tracked in the worktree; the clone has an untracked file of that name pattern
    r.write("src/app.py", "CLONE-VERSION", root=r.main)
    os.unlink(r.wt / "src" / "app.py")  # tracked but absent: still a conflict
    got, warns = copy(r, ["src/app.py"])
    assert got == [] and not (r.wt / "src" / "app.py").exists()
    assert any("tracked" in w for w in warns)


def test_a_tracked_file_in_the_way_of_a_directory_is_skipped(r: Repo) -> None:
    (r.main / "docs").mkdir()
    (r.main / "docs" / ".env").write_text("x")
    r.write("docs", "tracked file named docs")
    r.g(["add", "docs"])
    got, warns = copy(r, ["docs/.env"])
    assert got == [] and warns


def test_a_dotgit_alias_destination_is_refused(r: Repo) -> None:
    # a clone directory named like a .git alias is not walked or copied into
    (r.main / "GIT~1").mkdir()
    (r.main / "GIT~1" / ".env").write_text("x")
    got, _ = copy(r, ["GIT~1/.env"])
    assert got == []


def test_empty_patterns_copy_nothing(r: Repo) -> None:
    got, warns = copy(r, [])
    assert got == [] and warns == []


def test_a_name_that_is_not_utf8_is_not_copied(r: Repo) -> None:
    fd = os.open(os.fsencode(str(r.main)) + b"/.env\xff", os.O_WRONLY | os.O_CREAT)
    os.close(fd)
    got, warns = copy(r, [".env*"])
    assert got == [] and warns


def test_a_source_root_that_is_not_the_clone_copies_nothing(r: Repo, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / ".env").write_text("X")
    got, warns = copy(r, [".env"], source_root=str(other),
                      common_dir=str(other / ".git"), common_id="1:1")  # fmt: skip
    assert got == [] and warns


def test_the_source_root_must_not_be_reached_through_a_symlink(r: Repo, tmp_path: Path) -> None:
    r.write(".env", "x", root=r.main)
    link = tmp_path / "mainlink"
    os.symlink(r.main, link)
    got, warns = copy(r, [".env"], source_root=str(link))
    assert got == [] and warns


def test_unicode_and_newline_names_round_trip(r: Repo) -> None:
    r.write("é/.env\nx", "u", root=r.main)
    got, _ = copy(r, ["é/.env*"])
    assert [c.path for c in got] == ["é/.env\nx"]
    assert (r.wt / "é" / ".env\nx").read_text() == "u"


def test_a_source_root_whose_git_dir_has_another_identity_copies_nothing(r: Repo) -> None:
    r.write(".env", "x", root=r.main)
    got, warns = copy(r, [".env"], common_dir=str(r.main / ".git"), common_id="1:1")
    assert got == [] and any("not the recorded clone" in w for w in warns)


def test_a_source_root_with_the_recorded_identity_copies(r: Repo) -> None:
    r.write(".env", "x", root=r.main)
    st = os.stat(r.main / ".git")
    got, _ = copy(
        r, [".env"], common_dir=str(r.main / ".git"), common_id=f"{st.st_dev}:{st.st_ino}"
    )
    assert [c.path for c in got] == [".env"]
