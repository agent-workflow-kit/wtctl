"""`.git` name variants: case folding, NTFS and HFS alternate names."""

from __future__ import annotations

import pytest

from wtctl import fsafe


@pytest.mark.parametrize(
    "name",
    [
        ".git",
        ".GIT",
        ".Git",
        ".git.",
        ".git..",
        ".git ",
        ".git. .",
        "GIT~1",
        "git~1",
        "git~2",
        ".git:stream",
        ".git::$INDEX_ALLOCATION",
        ".g\u200cit",  # HFS ignorable code points
        ".gi\ufefft",
        ".\u202agit",
        "\uff0egit",  # fullwidth full stop
        ".\uff47it",  # fullwidth g
    ],
)
def test_dotgit_variants_are_recognized(name: str) -> None:
    assert fsafe.is_dotgit(name)


@pytest.mark.parametrize(
    "name",
    [
        ".gitignore",
        ".gitmodules",
        "git",
        "mygit",
        ".gi",
        "..git",
        ".git.d",
        "git~",
        "xgit~1",
        "",
        "a.git",
    ],
)
def test_non_variants_are_not_flagged(name: str) -> None:
    assert not fsafe.is_dotgit(name)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\0b", "/"])
def test_check_component_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.check_component(name)
    assert e.value.kind == "name"


def test_check_component_forbid_git_only_when_asked() -> None:
    fsafe.check_component(".git")
    with pytest.raises(fsafe.FsafeError) as e:
        fsafe.check_component("GIT~1", forbid_git=True)
    assert e.value.kind == "name"
