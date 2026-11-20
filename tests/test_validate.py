"""``wtctl validate``: offline, read-only config and environment check (issue #93)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import Run
from tests.schemacheck import validate
from wtctl.cli import main
from wtctl.context import Runtime
from wtctl.gateway import Gateway, GitResult

GOLDEN = Path(__file__).parent / "golden" / "validate_ok.json"


GIT_CALLS: list[dict[str, Any]] = []


def git_says(text: str, code: int = 0) -> Gateway:
    def runner(argv: list[str], **kwargs: Any) -> GitResult:
        assert argv[1:] == ["--version"]
        GIT_CALLS.append(kwargs)
        return GitResult(code, text.encode(), b"")

    return Gateway(git_runner=runner)


def run_validate(
    capsys: pytest.CaptureFixture[str], *argv: str, gateway: Gateway | None = None
) -> Run:
    rt = Runtime(
        stdin_isatty=False, me_name="alice", gateway=gateway or git_says("git version 2.43.0")
    )
    code = main(["validate", *argv], runtime=rt)
    c = capsys.readouterr()
    return Run(code, c.out, c.err)


def config(tmp_path: Path, **keys: object) -> str:
    path = tmp_path / "cfg.yaml"
    path.write_text(json.dumps(keys))  # JSON is YAML
    return str(path)


@pytest.fixture
def good(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "wt"
    root.mkdir()
    return {"root": str(root)}


def test_valid_config_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    sr = tmp_path / "src"
    sr.mkdir()
    cfg = config(tmp_path, **good, search_roots=[str(sr)])
    r = run_validate(capsys, "--json", "--config", cfg)
    assert r.code == 0
    assert r.err == ""
    data = r.json()
    validate("wtctl/cli/validate.json", data)
    assert data["warnings"] == []
    assert data["checks"]["root"] == {"path": good["root"], "exists": True}
    assert data["checks"]["search_roots"] == [{"path": str(sr), "status": "ok"}]
    assert data["checks"]["git"]["version"] == "2.43.0"
    assert len(r.out.strip().splitlines()) == 1


def test_golden_output(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    cfg = config(tmp_path, root=str(root), search_roots=[str(tmp_path / "gone")])
    r = run_validate(capsys, "--json", "--config", cfg)
    assert r.code == 0
    validate("wtctl/cli/validate.json", r.json())
    text = r.out.replace(str(tmp_path), "<tmp>")
    text = text.replace(r.json()["checks"]["git"]["path"], "<git>")
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(text)
    assert text == GOLDEN.read_text()


def test_human_output(
    capsys: pytest.CaptureFixture[str], good: dict[str, str], tmp_path: Path
) -> None:
    r = run_validate(capsys, "--config", config(tmp_path, **good))
    assert r.code == 0
    assert "usable" in r.out
    assert not r.out.lstrip().startswith("{")


def test_defaults_without_a_config_file(capsys: pytest.CaptureFixture[str]) -> None:
    # HOME is the private home: ~/wtctl does not exist, HOME is writable.
    r = run_validate(capsys, "--json")
    assert r.code == 0
    data = r.json()
    assert data["checks"]["root"]["exists"] is False
    assert not os.path.exists(data["checks"]["root"]["path"])  # read-only: not created


@pytest.mark.parametrize("key", ["search_depth", "bogus_key", "auto_clone"])
def test_invalid_config_names_the_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str], key: str
) -> None:
    cfg = config(tmp_path, **good, **{key: "nope"})
    r = run_validate(capsys, "--json", "--config", cfg)
    assert r.code == 2
    err = r.json()["error"]
    validate("wtctl/cli/error.json", r.json())
    assert err["code"] == "config_invalid"
    assert key in err["message"]


def test_root_that_is_a_file_is_config_invalid(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    f = tmp_path / "file"
    f.write_text("x")
    r = run_validate(capsys, "--json", "--config", config(tmp_path, root=str(f)))
    assert r.code == 2
    assert r.json()["error"]["code"] == "config_invalid"
    assert r.json()["error"]["message"].startswith("root:")


def test_root_below_a_file_is_config_invalid(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    f = tmp_path / "file"
    f.write_text("x")
    r = run_validate(capsys, "--json", "--config", config(tmp_path, root=str(f / "sub")))
    assert r.code == 2
    assert r.json()["error"]["message"].startswith("root:")


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unwritable_root_is_config_invalid(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        for root in (ro, ro / "new"):
            r = run_validate(capsys, "--json", "--config", config(tmp_path, root=str(root)))
            assert r.code == 2, root
            assert r.json()["error"]["message"].startswith("root:")
    finally:
        ro.chmod(0o700)


def test_missing_creatable_root_is_fine_and_not_created(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = tmp_path / "a" / "b"
    r = run_validate(capsys, "--json", "--config", config(tmp_path, root=str(root)))
    assert r.code == 0
    assert r.json()["checks"]["root"]["exists"] is False
    assert not (tmp_path / "a").exists()


def test_search_root_warnings(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    ok = tmp_path / "ok"
    ok.mkdir()
    afile = tmp_path / "afile"
    afile.write_text("x")
    cfg = config(tmp_path, **good, search_roots=[str(ok), str(tmp_path / "missing"), str(afile)])
    r = run_validate(capsys, "--json", "--config", cfg)
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/validate.json", data)
    assert [c["status"] for c in data["checks"]["search_roots"]] == [
        "ok",
        "missing",
        "not_directory",
    ]
    assert [(w["code"], w["key"]) for w in data["warnings"]] == [
        ("search_root_missing", "search_roots[1]"),
        ("search_root_unusable", "search_roots[2]"),
    ]
    assert "warning:" in r.err  # human summary on stderr, stdout stays the JSON object


def test_hostile_search_root_text_is_escaped_and_bounded(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    evil = str(tmp_path / ("\x1b[31m‮" + "x" * 5000))
    r = run_validate(capsys, "--config", config(tmp_path, **good, search_roots=[evil]))
    assert r.code == 0
    assert "\x1b" not in r.err and "‮" not in r.err
    assert "\\x1b" in r.err
    assert max(len(line) for line in r.err.splitlines()) < 500
    j = run_validate(capsys, "--json", "--config", config(tmp_path, **good, search_roots=[evil]))
    validate("wtctl/cli/validate.json", j.json())
    assert j.out.isascii()


def test_too_many_search_roots_are_truncated(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    roots = [str(tmp_path / f"r{i}") for i in range(80)]
    r = run_validate(capsys, "--json", "--config", config(tmp_path, **good, search_roots=roots))
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/validate.json", data)
    assert len(data["checks"]["search_roots"]) == 50
    assert data["warnings"][-1]["code"] == "search_roots_truncated"


def test_git_too_old_is_exit_12_like_every_git_command(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    r = run_validate(
        capsys,
        "--json",
        "--config",
        config(tmp_path, **good),
        gateway=git_says("git version 2.33.9"),
    )
    assert r.code == 12
    validate("wtctl/cli/error.json", r.json())
    assert r.json()["error"]["code"] == "git_too_old"


def test_git_minimum_is_accepted(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    r = run_validate(
        capsys,
        "--json",
        "--config",
        config(tmp_path, **good),
        gateway=git_says("git version 2.34.0"),
    )
    assert r.code == 0


@pytest.mark.parametrize(("text", "code"), [("garbage", 0), ("git version 2.40", 1)])
def test_git_broken_is_internal(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str], text: str, code: int
) -> None:
    r = run_validate(
        capsys, "--json", "--config", config(tmp_path, **good), gateway=git_says(text, code)
    )
    assert r.code == 1
    assert r.json()["error"]["code"] == "internal"


def test_configured_git_bin_that_is_not_executable_names_the_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    cfg = config(tmp_path, **good, git_bin=str(tmp_path / "nogit"))
    r = run_validate(capsys, "--json", "--config", cfg)
    assert r.code == 2
    assert r.json()["error"]["message"].startswith("git_bin:")


def test_config_is_checked_before_git(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    r = run_validate(
        capsys,
        "--json",
        "--config",
        config(tmp_path, bogus=1),
        gateway=git_says("git version 1.0"),
    )
    assert r.code == 2


def test_dry_run_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    r = run_validate(capsys, "--dry-run")
    assert r.code == 2


def test_real_git(capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]) -> None:
    r = run_validate(capsys, "--json", "--config", config(tmp_path, **good), gateway=Gateway())
    assert r.code == 0
    assert r.json()["checks"]["git"]["version"][0].isdigit()


def test_git_is_invoked_hardened(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    GIT_CALLS.clear()
    r = run_validate(capsys, "--json", "--config", config(tmp_path, **good))
    assert r.code == 0
    assert len(GIT_CALLS) == 1
    call = GIT_CALLS[0]
    assert call["cwd"] == "/"
    assert call["timeout"] == 15
    assert call["env"]["HOME"] == "/"


def _tree(base: Path) -> list[tuple[str, int, int]]:
    out = []
    for dirpath, dirs, files in os.walk(base):
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out.append((p, st.st_mtime_ns, st.st_size))
    return sorted(out)


def test_no_network_no_other_process_no_writes(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    good: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock_calls: list[object] = []
    procs: list[list[str]] = []
    real_popen = subprocess.Popen

    def spy_popen(argv: Any, *a: Any, **k: Any) -> Any:
        procs.append(list(argv))
        return real_popen(argv, *a, **k)

    def refuse_sock(*a: Any, **k: Any) -> Any:
        sock_calls.append(a)
        raise AssertionError("validate touched the network")

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    monkeypatch.setattr(socket.socket, "connect", refuse_sock)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_sock)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_sock)

    home = Path(os.environ["HOME"])
    state_home = Path(os.environ["XDG_STATE_HOME"])
    assert not state_home.exists()
    cfg = config(tmp_path, **good, search_roots=[str(tmp_path / "missing")])
    before = _tree(tmp_path)
    r = run_validate(capsys, "--json", "--config", cfg, gateway=Gateway())
    assert r.code == 0
    assert sock_calls == []
    assert len(procs) == 1 and procs[0][1:] == ["--version"]
    assert _tree(tmp_path) == before  # nothing created or changed anywhere
    assert not state_home.exists()  # no state.db, audit or log directory
    assert not (home / "wtctl").exists()


def test_read_only_state_dir_does_not_matter(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    good: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A state directory that cannot be opened must not fail validate: it never looks there.
    monkeypatch.setenv("XDG_STATE_HOME", "/proc/nonexistent-wtctl-state")
    r = run_validate(capsys, "--json", "--config", config(tmp_path, **good))
    assert r.code == 0


NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")


def test_dangling_symlink_root_below_writable_dir_is_fine(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "ro" / "new")
    (tmp_path / "ro").mkdir()
    r = run_validate(capsys, "--json", "--config", config(tmp_path, root=str(link)))
    assert r.code == 0
    assert r.json()["checks"]["root"] == {"path": str(link), "exists": False}


@NOT_ROOT
def test_dangling_symlink_root_below_unwritable_dir_is_config_invalid(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    link = tmp_path / "link"
    link.symlink_to(ro / "new")
    ro.chmod(0o500)
    try:
        r = run_validate(capsys, "--json", "--config", config(tmp_path, root=str(link)))
    finally:
        ro.chmod(0o700)
    assert r.code == 2
    assert r.json()["error"]["message"].startswith("root:")


def test_symlink_to_writable_dir_is_fine(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    link = tmp_path / "link"
    link.symlink_to(good["root"])
    r = run_validate(capsys, "--json", "--config", config(tmp_path, root=str(link)))
    assert r.code == 0
    assert r.json()["checks"]["root"] == {"path": str(link), "exists": True}


def test_read_only_mount_root_is_config_invalid(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    good: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = os.statvfs

    def fake(path: Any) -> Any:
        st = real(path)

        class V:
            f_flag = st.f_flag | os.ST_RDONLY

        return V()

    monkeypatch.setattr(os, "statvfs", fake)
    r = run_validate(capsys, "--json", "--config", config(tmp_path, **good))
    assert r.code == 2
    assert r.json()["error"]["message"].startswith("root:")


def test_statvfs_failure_is_ignored(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    good: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(path: Any) -> Any:
        raise OSError("nope")

    monkeypatch.setattr(os, "statvfs", boom)
    r = run_validate(capsys, "--json", "--config", config(tmp_path, **good))
    assert r.code == 0


@NOT_ROOT
def test_root_without_search_bit_is_config_invalid(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    d = tmp_path / "noexec"
    d.mkdir()
    d.chmod(0o600)
    try:
        r = run_validate(capsys, "--json", "--config", config(tmp_path, root=str(d)))
    finally:
        d.chmod(0o700)
    assert r.code == 2
    assert r.json()["error"]["message"].startswith("root:")


@NOT_ROOT
def test_unreadable_search_root(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    d = tmp_path / "locked"
    d.mkdir()
    d.chmod(0)
    try:
        cfg = config(tmp_path, **good, search_roots=[str(d)])
        r = run_validate(capsys, "--json", "--config", cfg)
    finally:
        d.chmod(0o700)
    assert r.code == 0
    data = r.json()
    assert data["checks"]["search_roots"][0]["status"] == "unreadable"
    assert data["warnings"][0]["code"] == "search_root_unusable"


def test_too_long_search_root_is_missing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, good: dict[str, str]
) -> None:
    long = str(tmp_path / ("x" * 300))
    r = run_validate(capsys, "--json", "--config", config(tmp_path, **good, search_roots=[long]))
    assert r.code == 0
    data = r.json()
    assert data["checks"]["search_roots"][0]["status"] == "missing"
    assert data["warnings"][0]["code"] == "search_root_missing"


def test_shown_cuts_at_exactly_max_shown() -> None:
    from wtctl.commands.validate import MAX_SHOWN, shown

    assert MAX_SHOWN == 200
    assert shown("a" * 500) == "a" * 200 + "..."
    assert shown("a" * 200) == "a" * 200


def test_path_entry_with_newline_is_skipped(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    good: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = tmp_path / "bad\ndir"
    bad.mkdir()
    ok = tmp_path / "okbin"
    ok.mkdir()
    for d in (bad, ok):
        g = d / "git"
        g.write_text("#!/bin/sh\n")
        g.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bad}{os.pathsep}{ok}")
    r = run_validate(capsys, "--json", "--config", config(tmp_path, **good))
    assert r.code == 0
    assert r.json()["checks"]["git"]["path"] == str(ok / "git")
