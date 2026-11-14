"""``cron install`` / ``cron uninstall`` against a fake ``crontab`` (the real one is never run)."""

from __future__ import annotations

import shlex
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import Run
from tests.schemacheck import validate
from wtctl import cron, gateway
from wtctl.cli import build_parser, main
from wtctl.context import Runtime
from wtctl.gateway import Gateway

real_run_crontab = gateway.run_crontab  # captured before the autouse guard replaces it

HEADER = "# wtctl managed"
FOOTER = "# wtctl managed end"


class FakeCrontab:
    """A user's crontab: ``content`` is None when the user has none."""

    def __init__(self, content: bytes | None = None) -> None:
        self.content = content
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.fail: dict[str, tuple[int, bytes]] = {}

    def __call__(
        self, args: list[str], stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(args), stdin))
        op = args[0]
        if op in self.fail:
            code, err = self.fail[op]
            return subprocess.CompletedProcess(args, code, b"", err)
        if op == "-l":
            if self.content is None:
                return subprocess.CompletedProcess(args, 1, b"", b"no crontab for alice\n")
            return subprocess.CompletedProcess(args, 0, self.content, b"")
        if op == "-":
            assert stdin is not None
            self.content = stdin
        elif op == "-r":
            self.content = None
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    @property
    def writes(self) -> list[str]:
        return [a[0] for a, _ in self.calls if a[0] in ("-", "-r")]


@pytest.fixture
def wtctl_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A fake ``wtctl`` executable first on PATH."""
    d = tmp_path / "bin dir"
    d.mkdir()
    exe = d / "wtctl"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:/usr/bin:/bin")
    return str(exe)


def go(
    capsys: pytest.CaptureFixture[str], fake: FakeCrontab, *argv: str, actor: str | None = None
) -> Run:
    rt = Runtime(stdin_isatty=False, me_name="alice", gateway=Gateway(crontab_runner=fake))
    full = [*argv] if actor is None else ["--actor", actor, *argv]
    code = main(full, runtime=rt)
    cap = capsys.readouterr()
    return Run(code, cap.out, cap.err)


def block_of(fake: FakeCrontab) -> list[str]:
    assert fake.content is not None
    lines = fake.content.decode().split("\n")
    return lines[lines.index(HEADER) : lines.index(FOOTER) + 1]


def jobs(fake: FakeCrontab) -> list[str]:
    return block_of(fake)[1:-1]


def command_of(line: str) -> str:
    """The shell script cron would run for a job line (undo the % escaping, unwrap sh -c)."""
    fields = line.split(" ", 5)
    rest = fields[5].replace("\\%", "%")
    argv = shlex.split(rest)
    assert argv[:2] == ["/bin/sh", "-c"]
    return argv[2]


# ---------------------------------------------------------------- install


def test_install_into_empty_crontab(capsys: pytest.CaptureFixture[str], wtctl_bin: str) -> None:
    fake = FakeCrontab(None)
    r = go(capsys, fake, "--json", "cron", "install")
    assert r.code == 0, r.err
    data = r.json()
    validate("wtctl/cli/cron-install.json", data)
    assert data["installed"] is True and data["dry_run"] is False
    assert len(data["entries"]) == 2
    assert data["entries"] == jobs(fake)
    block = block_of(fake)
    assert block[0] == HEADER and block[-1] == FOOTER
    assert fake.content is not None and fake.content.endswith(FOOTER.encode() + b"\n")


def test_install_entries_schedule_commands_and_actor(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(None)
    go(capsys, fake, "cron", "install")
    a, b = jobs(fake)
    assert a.startswith("17 * * * * ")
    assert b.startswith("47 * * * * ")
    exe = shlex.quote(wtctl_bin)
    assert f"exec {exe} reconcile --actor auto:cron" in command_of(a)
    assert f"exec {exe} prune --throwaways --if-configured --actor auto:cron" in command_of(b)


def test_install_sets_explicit_path_inside_the_command(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, private_home: Path
) -> None:
    fake = FakeCrontab(b"MAILTO=me\n* * * * * true\n")
    go(capsys, fake, "cron", "install")
    script = command_of(jobs(fake)[0])
    assert f"PATH={shlex.quote(f'{private_home}/.local/bin:/usr/bin:/bin')}" in (script)
    # no crontab-level assignment that would change the user's own entries
    assert not any(ln.startswith("PATH=") for ln in fake.content.decode().split("\n"))  # type: ignore[union-attr]


def test_macos_path_additions_only_when_they_exist() -> None:
    dirs = {"/opt/homebrew/bin"}
    p = cron.cron_path("/h", macos=True, isdir=lambda d: d in dirs)
    assert p == "/h/.local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    both = cron.cron_path("/h", macos=True, isdir=lambda d: True)
    assert both == "/h/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    linux = cron.cron_path("/h", macos=False, isdir=lambda d: True)
    assert linux == "/h/.local/bin:/usr/bin:/bin"


def test_install_on_macos_adds_homebrew(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cron, "_is_macos", lambda: True)
    monkeypatch.setattr(cron.os.path, "isdir", lambda d: d == "/opt/homebrew/bin")
    fake = FakeCrontab(None)
    go(capsys, fake, "cron", "install")
    assert "/opt/homebrew/bin" in command_of(jobs(fake)[0])
    assert "/usr/local/bin" not in command_of(jobs(fake)[0])


def test_install_preserves_foreign_lines_byte_for_byte(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    before = b"# mine\r\nSHELL=/bin/fish\n\n*/5 * * * * echo \xe2\x9c\x93 \\% \t \n"
    fake = FakeCrontab(before)
    go(capsys, fake, "cron", "install")
    assert fake.content is not None
    assert fake.content.startswith(before)
    go(capsys, fake, "cron", "uninstall")
    assert fake.content == before


def test_install_without_trailing_newline_only_adds_the_separator(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(b"* * * * * true")
    go(capsys, fake, "cron", "install")
    assert fake.content is not None
    assert fake.content.startswith(b"* * * * * true\n" + HEADER.encode() + b"\n")


def test_install_is_idempotent(capsys: pytest.CaptureFixture[str], wtctl_bin: str) -> None:
    fake = FakeCrontab(b"a\n")
    go(capsys, fake, "cron", "install")
    once = fake.content
    n = len(fake.writes)
    r = go(capsys, fake, "--json", "cron", "install")
    assert r.code == 0
    assert fake.content == once
    assert len(fake.writes) == n  # no second write
    assert r.json()["installed"] is True


def test_install_replaces_a_stale_block_in_place(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    old = f"before\n{HEADER}\n0 0 * * * old\n{FOOTER}\nafter\n".encode()
    fake = FakeCrontab(old)
    r = go(capsys, fake, "cron", "install")
    assert r.code == 0
    text = fake.content.decode()  # type: ignore[union-attr]
    assert text.startswith("before\n# wtctl managed\n17 * * * * ")
    assert text.endswith(f"{FOOTER}\nafter\n")
    assert "old" not in text


def test_install_dry_run_writes_nothing(capsys: pytest.CaptureFixture[str], wtctl_bin: str) -> None:
    fake = FakeCrontab(b"x\n")
    r = go(capsys, fake, "--json", "--dry-run", "cron", "install")
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/cron-install.json", data)
    assert data["dry_run"] is True and len(data["entries"]) == 2
    assert fake.writes == [] and fake.content == b"x\n"


def test_install_works_for_any_actor(capsys: pytest.CaptureFixture[str], wtctl_bin: str) -> None:
    fake = FakeCrontab(None)
    assert go(capsys, fake, "cron", "install", actor="auto:cron").code == 0


def test_wtctl_not_found_is_a_usage_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    fake = FakeCrontab(b"keep\n")
    r = go(capsys, fake, "--json", "cron", "install")
    assert r.code == 2
    err = r.json()["error"]
    assert err["code"] == "usage"
    assert "wtctl executable not found; install it on PATH" in err["message"]
    assert fake.writes == []


def test_relative_path_entries_are_not_used_to_find_wtctl(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "wtctl").write_text("#!/bin/sh\n")
    (tmp_path / "wtctl").chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", ".:")
    fake = FakeCrontab(None)
    assert go(capsys, fake, "cron", "install").code == 2
    assert fake.writes == []


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_running_program_is_preferred_over_the_first_on_path(
    capsys: pytest.CaptureFixture[str],
    wtctl_bin: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mine = _exe(tmp_path / "venv" / "bin" / "wtctl")
    monkeypatch.setattr("sys.argv", [str(mine), "cron", "install"])
    fake = FakeCrontab(None)
    go(capsys, fake, "cron", "install")
    assert f"exec {shlex.quote(str(mine))} reconcile" in command_of(jobs(fake)[0])


def test_argv0_is_ignored_unless_its_basename_is_wtctl(
    capsys: pytest.CaptureFixture[str],
    wtctl_bin: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    other = _exe(tmp_path / "bin2" / "pytest")
    monkeypatch.setattr("sys.argv", [str(other)])
    fake = FakeCrontab(None)
    go(capsys, fake, "cron", "install")
    assert f"exec {shlex.quote(wtctl_bin)} reconcile" in command_of(jobs(fake)[0])


def test_bare_argv0_is_not_resolved_against_the_cwd(
    capsys: pytest.CaptureFixture[str],
    wtctl_bin: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _exe(tmp_path / "cwd" / "wtctl")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr("sys.argv", ["wtctl"])
    fake = FakeCrontab(None)
    go(capsys, fake, "cron", "install")
    assert f"exec {shlex.quote(wtctl_bin)} reconcile" in command_of(jobs(fake)[0])


def test_hostile_wtctl_path_is_quoted_and_percent_escaped(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    d = tmp_path / "it's $(touch pwned) `x` ;&|<> 100%"
    d.mkdir()
    exe = d / "wtctl"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}:/usr/bin")
    fake = FakeCrontab(None)
    assert go(capsys, fake, "cron", "install").code == 0
    for line in jobs(fake):
        assert "%" not in line.replace("\\%", "")
        script = command_of(line)
        assert shlex.split(script.split("exec ", 1)[1])[0] == str(exe)
        # the whole line stays one crontab line
        assert "\n" not in line


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\\b", "a\0b"])
def test_wtctl_path_with_unrepresentable_characters_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, bad: str
) -> None:
    with pytest.raises(cron.CronError):
        cron.build_entries(
            wtctl=f"/x/{bad}/wtctl",
            config=None,
            env_file=None,
            path="/usr/bin",
            xdg={},
        )


def test_config_flag_is_carried_into_the_entries(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, tmp_path: Path
) -> None:
    cfg = tmp_path / "my cfg's;dir" / "c%.yaml"
    fake = FakeCrontab(None)
    r = go(capsys, fake, "--config", str(cfg), "cron", "install")
    assert r.code == 0, r.err
    for line in jobs(fake):
        script = command_of(line)
        assert f"--config {shlex.quote(str(cfg))}" in script


def test_config_path_is_made_absolute(
    capsys: pytest.CaptureFixture[str],
    wtctl_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = FakeCrontab(None)
    go(capsys, fake, "--config", "rel.yaml", "cron", "install")
    assert str(tmp_path / "rel.yaml") in command_of(jobs(fake)[0])


def test_entry_argv_parses_with_the_real_parser(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, tmp_path: Path
) -> None:
    fake = FakeCrontab(None)
    go(capsys, fake, "--config", str(tmp_path / "c.yaml"), "cron", "install")
    parser = build_parser()
    for line in jobs(fake):
        argv = shlex.split(command_of(line).split("exec ", 1)[1])[1:]
        ns = parser.parse_args(argv)
        assert ns.actor == "auto:cron"
        assert ns.config == str(tmp_path / "c.yaml")


def test_xdg_variables_are_exported_so_cron_finds_the_same_state(
    capsys: pytest.CaptureFixture[str],
    wtctl_bin: str,
    private_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "relative/ignored")
    fake = FakeCrontab(None)
    go(capsys, fake, "cron", "install")
    script = command_of(jobs(fake)[0])
    assert f"XDG_CONFIG_HOME={shlex.quote(str(private_home / '.config'))}" in script
    assert "XDG_STATE_HOME" not in script


# ---------------------------------------------------------------- env file


def make_env_file(tmp_path: Path, mode: int = 0o600) -> Path:
    d = tmp_path / "envdir"
    d.mkdir(mode=0o700)
    f = d / "my env;file"
    f.write_text("export FOO=bar\n")
    f.chmod(mode)
    return f


def test_env_file_is_sourced_after_path(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, tmp_path: Path
) -> None:
    f = make_env_file(tmp_path)
    fake = FakeCrontab(None)
    r = go(capsys, fake, "cron", "install", "--env-file", str(f))
    assert r.code == 0, r.err
    for line in jobs(fake):
        script = command_of(line)
        assert script.index("export PATH") < script.index(f". {shlex.quote(str(f.resolve()))};")
        assert script.index(". ") < script.index("exec ")


@pytest.mark.parametrize("mode", [0o660, 0o606, 0o666])
def test_env_file_writable_by_others_is_refused(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, tmp_path: Path, mode: int
) -> None:
    f = make_env_file(tmp_path, mode)
    fake = FakeCrontab(b"keep\n")
    r = go(capsys, fake, "--json", "cron", "install", "--env-file", str(f))
    assert r.code == 2
    assert r.json()["error"]["code"] == "usage"
    assert fake.writes == []


def test_env_file_in_writable_directory_is_refused(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, tmp_path: Path
) -> None:
    f = make_env_file(tmp_path)
    f.parent.chmod(0o777)
    r = go(capsys, FakeCrontab(None), "cron", "install", "--env-file", str(f))
    assert r.code == 2


def test_missing_or_non_regular_env_file_is_refused(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, tmp_path: Path
) -> None:
    assert go(capsys, FakeCrontab(None), "cron", "install", "--env-file", "/nope/x").code == 2
    assert go(capsys, FakeCrontab(None), "cron", "install", "--env-file", str(tmp_path)).code == 2


def test_env_file_with_a_newline_in_the_name_is_refused(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, tmp_path: Path
) -> None:
    d = tmp_path / "envdir"
    d.mkdir(mode=0o700)
    f = d / "a\nb"
    f.write_text("x")
    f.chmod(0o600)
    assert go(capsys, FakeCrontab(None), "cron", "install", "--env-file", str(f)).code == 2


def test_env_file_is_checked_in_dry_run_too(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, tmp_path: Path
) -> None:
    f = make_env_file(tmp_path, 0o666)
    r = go(capsys, FakeCrontab(None), "--dry-run", "cron", "install", "--env-file", str(f))
    assert r.code == 2


# ---------------------------------------------------------------- malformed crontabs


@pytest.mark.parametrize(
    "text",
    [
        f"a\n{HEADER}\nx\n",  # unterminated
        f"a\n{FOOTER}\n",  # stray end
        f"{FOOTER}\n{HEADER}\n",  # reversed
        f"{HEADER}\n{FOOTER}\n{HEADER}\n{FOOTER}\n",  # duplicate
        f"{HEADER}\n{HEADER}\n{FOOTER}\n",  # repeated start
    ],
)
@pytest.mark.parametrize("command", ["install", "uninstall"])
def test_malformed_block_is_refused_and_nothing_is_written(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, text: str, command: str
) -> None:
    fake = FakeCrontab(text.encode())
    r = go(capsys, fake, "--json", "cron", command)
    assert r.code == 1
    err = r.json()["error"]
    assert err["code"] == "internal"
    assert "fix" in err["message"]
    assert fake.writes == [] and fake.content == text.encode()


def test_marker_lines_tolerate_trailing_whitespace_and_cr(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(f"a\r\n{HEADER} \t\r\nold\r\n{FOOTER}\r\nb\r\n".encode())
    assert go(capsys, fake, "cron", "uninstall").code == 0
    assert fake.content == b"a\r\nb\r\n"


def test_text_that_merely_contains_a_marker_is_not_a_marker(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    text = f"* * * * * echo '{HEADER}'\n# {HEADER}\n".encode()
    fake = FakeCrontab(text)
    assert go(capsys, fake, "cron", "install").code == 0
    assert fake.content is not None and fake.content.startswith(text)


# ---------------------------------------------------------------- uninstall


def test_uninstall_removes_only_the_block(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(b"before\n")
    go(capsys, fake, "cron", "install")
    installed = jobs(fake)
    fake.content = fake.content + b"after\n"  # type: ignore[operator]
    r = go(capsys, fake, "--json", "cron", "uninstall")
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/cron-uninstall.json", data)
    assert data == {"installed": False, "entries": installed, "dry_run": False}
    assert fake.content == b"before\nafter\n"


def test_uninstall_of_last_lines_removes_the_crontab(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(None)
    go(capsys, fake, "cron", "install")
    go(capsys, fake, "cron", "uninstall")
    assert fake.content is None
    assert fake.writes[-1] == "-r"


def test_uninstall_without_a_block_is_a_noop(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(b"keep")
    r = go(capsys, fake, "--json", "cron", "uninstall")
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/cron-uninstall.json", data)
    assert data["entries"] == [] and data["installed"] is False
    assert fake.writes == [] and fake.content == b"keep"


def test_uninstall_with_no_crontab_at_all_is_a_noop(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(None)
    assert go(capsys, fake, "cron", "uninstall").code == 0
    assert fake.writes == []


def test_uninstall_dry_run_reports_and_changes_nothing(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(None)
    go(capsys, fake, "cron", "install")
    snapshot = fake.content
    r = go(capsys, fake, "--json", "--dry-run", "cron", "uninstall")
    data = r.json()
    validate("wtctl/cli/cron-uninstall.json", data)
    assert data["dry_run"] is True and len(data["entries"]) == 2
    assert fake.content == snapshot


def test_uninstall_is_idempotent(capsys: pytest.CaptureFixture[str], wtctl_bin: str) -> None:
    fake = FakeCrontab(b"x\n")
    go(capsys, fake, "cron", "install")
    go(capsys, fake, "cron", "uninstall")
    n = len(fake.writes)
    go(capsys, fake, "cron", "uninstall")
    assert fake.content == b"x\n" and len(fake.writes) == n


# ---------------------------------------------------------------- crontab failures


def test_crontab_l_failure_other_than_no_crontab_is_an_error(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(b"precious\n")
    fake.fail["-l"] = (1, b"crontab: permission denied\n")
    r = go(capsys, fake, "--json", "cron", "install")
    assert r.code == 1
    assert r.json()["error"]["code"] == "internal"
    assert fake.writes == []  # never treated as empty


def test_missing_crontab_binary_is_an_error(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    def gone(args: list[str], stdin: bytes | None = None) -> Any:
        raise FileNotFoundError("crontab")

    rt = Runtime(stdin_isatty=False, me_name="a", gateway=Gateway(crontab_runner=gone))
    assert main(["--json", "cron", "install"], runtime=rt) == 1
    err = capsys.readouterr().out
    assert "crontab" in err and "not found" in err


@pytest.mark.parametrize("json_out", [False, True])
def test_non_utf8_foreign_bytes_round_trip(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, json_out: bool
) -> None:
    before = b"* * * * * \xff\xfe\n"
    fake = FakeCrontab(before)
    flags = ["--json"] if json_out else []
    assert go(capsys, fake, *flags, "cron", "install").code == 0
    assert fake.content is not None and fake.content.startswith(before)
    assert go(capsys, fake, *flags, "cron", "uninstall").code == 0
    assert fake.content == before


def test_non_utf8_bytes_inside_the_block_do_not_crash_the_output(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(f"{HEADER}\n* * * * * \xff\n{FOOTER}\n".encode("latin-1"))
    r = go(capsys, fake, "cron", "uninstall")
    assert r.code == 0 and "\\udcff" in r.out
    fake2 = FakeCrontab(f"{HEADER}\n* * * * * \xff\n{FOOTER}\n".encode("latin-1"))
    assert go(capsys, fake2, "--json", "cron", "uninstall", "--dry-run").code == 0


def test_failed_write_is_an_error(capsys: pytest.CaptureFixture[str], wtctl_bin: str) -> None:
    fake = FakeCrontab(b"x\n")
    fake.fail["-"] = (1, b"boom\n")
    r = go(capsys, fake, "cron", "install")
    assert r.code == 1 and fake.content == b"x\n"


def test_edit_during_read_modify_write_is_not_lost(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(b"one\n")
    reads = {"n": 0}
    inner = fake.__call__

    def racing(args: list[str], stdin: bytes | None = None) -> Any:
        if args[0] == "-l":
            reads["n"] += 1
            if reads["n"] == 2:  # someone edits between our two reads
                fake.content = b"one\ntwo\n"
        return inner(args, stdin)

    rt = Runtime(stdin_isatty=False, me_name="a", gateway=Gateway(crontab_runner=racing))
    assert main(["cron", "install"], runtime=rt) == 0
    capsys.readouterr()
    assert fake.content is not None and fake.content.startswith(b"one\ntwo\n" + HEADER.encode())


def test_dry_run_still_needs_a_readable_crontab(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(b"x")
    fake.fail["-l"] = (2, b"bad\n")
    assert go(capsys, fake, "--dry-run", "cron", "install").code == 1


# ---------------------------------------------------------------- human output, real runner


def test_human_output_lists_the_entries(capsys: pytest.CaptureFixture[str], wtctl_bin: str) -> None:
    fake = FakeCrontab(None)
    r = go(capsys, fake, "cron", "install")
    assert r.code == 0
    assert "17 * * * *" in r.out and "47 * * * *" in r.out


def test_real_runner_passes_args_and_stdin_to_the_crontab_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "fake"
    bindir.mkdir()
    prog = bindir / "crontab"
    out = tmp_path / "seen"
    prog.write_text(
        f'#!/bin/sh\necho "$@" > {shlex.quote(str(out))}\n/bin/cat >> {shlex.quote(str(out))}\n'
    )
    prog.chmod(prog.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir))
    r = real_run_crontab(["-"], b"hello\xff\n")
    assert r.returncode == 0
    assert out.read_bytes() == b"-\nhello\xff\n"


def test_real_runner_without_crontab_raises_file_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        real_run_crontab(["-l"])


def test_default_gateway_cannot_reach_the_real_crontab_in_tests() -> None:
    with pytest.raises(pytest.fail.Exception, match="real crontab"):
        Gateway().crontab(["-l"])


def test_the_real_crontab_guard_cannot_be_swallowed_by_main() -> None:
    rt = Runtime(stdin_isatty=False, me_name="a", gateway=Gateway())
    with pytest.raises(pytest.fail.Exception, match="real crontab"):
        main(["cron", "uninstall"], runtime=rt)


def test_real_runner_ignores_relative_and_empty_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ran"
    prog = tmp_path / "crontab"
    prog.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    prog.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", ".:")
    with pytest.raises(FileNotFoundError):
        real_run_crontab(["-l"])
    assert not marker.exists()


def test_real_runner_runs_crontab_in_the_c_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prog = tmp_path / "bin" / "crontab"
    prog.parent.mkdir()
    prog.write_text('#!/bin/sh\necho "$LC_ALL:$KEEPME"\n')
    prog.chmod(0o755)
    monkeypatch.setenv("PATH", str(prog.parent))
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("KEEPME", "yes")
    assert real_run_crontab(["-l"]).stdout == b"C:yes\n"


# ---------------------------------------------------------------- review round 1


def test_uninstall_of_an_empty_block_says_it_removed_it(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(f"x\n{HEADER}\n{FOOTER}\n".encode())
    r = go(capsys, fake, "cron", "uninstall")
    assert r.code == 0 and "cron block removed" in r.err and "no cron block" not in r.err
    assert fake.content == b"x\n"
    fake = FakeCrontab(f"x\n{HEADER}\n{FOOTER}\n".encode())
    r = go(capsys, fake, "--dry-run", "cron", "uninstall")
    assert "cron block would be removed" in r.err
    assert fake.writes == []


def test_uninstall_cannot_restore_a_missing_final_newline(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str
) -> None:
    fake = FakeCrontab(b"a")
    go(capsys, fake, "cron", "install")
    go(capsys, fake, "cron", "uninstall")
    assert fake.content == b"a\n"  # the one exception to byte-for-byte preservation


def test_bad_xdg_value_is_skipped_not_fatal(
    capsys: pytest.CaptureFixture[str], wtctl_bin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/a\\b")
    monkeypatch.setenv("XDG_STATE_HOME", "/a\nb")
    fake = FakeCrontab(None)
    assert go(capsys, fake, "cron", "install").code == 0
    script = command_of(jobs(fake)[0])
    assert "XDG_DATA_HOME" not in script and "XDG_STATE_HOME" not in script
    assert "XDG_CONFIG_HOME" in script


def test_unencodable_home_is_a_clean_error() -> None:
    with pytest.raises(cron.CronError):
        cron._home({"HOME": "/h\ud800me"})


def test_unencodable_wtctl_path_is_a_clean_error() -> None:
    with pytest.raises(cron.CronError):
        cron.build_entries(
            wtctl="/x/\ud800/wtctl", config=None, env_file=None, path="/usr/bin", xdg={}
        )


def test_undecodable_but_escapable_bytes_in_the_wtctl_path_are_accepted() -> None:
    lines = cron.build_entries(
        wtctl="/x/\udcff/wtctl", config=None, env_file=None, path="/usr/bin", xdg={}
    )
    assert len(lines) == 2


class _Info:
    def __init__(self, mode: int, uid: int, gid: int) -> None:
        self.st_mode, self.st_uid, self.st_gid = mode, uid, gid


ME, MYGID = 1000, 1000


def _unsafe(mode: int, uid: int, gid: int, immediate: bool, members: list[str]) -> str | None:
    return cron.unsafe_directory(
        _Info(mode, uid, gid),  # type: ignore[arg-type]
        "/d",
        immediate=immediate,
        uid=ME,
        gid=MYGID,
        user="me",
        members=lambda g: members,
    )


def test_directory_rules_group_write() -> None:
    assert _unsafe(0o40775, 0, 0, False, []) is None  # root-owned, any group
    assert _unsafe(0o40775, 0, 0, True, ["x"]) is None
    assert _unsafe(0o40775, ME, MYGID, False, []) is None  # private group
    assert _unsafe(0o40775, ME, MYGID, False, ["me"]) is None
    shared = _unsafe(0o40775, ME, MYGID, False, ["me", "other"])
    assert shared is not None and "chmod g-w /d" in shared
    other_group = _unsafe(0o40775, ME, 50, False, [])
    assert other_group is not None and "chmod g-w" in other_group
    assert _unsafe(0o40755, ME, 50, False, ["a"]) is None


def test_directory_rules_other_write() -> None:
    refused = _unsafe(0o40777, 0, 0, False, [])
    assert refused is not None and "chmod o-w /d" in refused
    assert _unsafe(0o41777, 0, 0, False, []) is None  # sticky ancestor (e.g. /tmp)
    sticky_parent = _unsafe(0o41777, 0, 0, True, [])
    assert sticky_parent is not None and "chmod o-w" in sticky_parent
    assert _unsafe(0o40700, 5, 5, False, []) is not None  # owned by someone else


def test_env_file_in_my_private_group_writable_directory_is_accepted(
    capsys: pytest.CaptureFixture[str],
    wtctl_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = make_env_file(tmp_path)
    f.parent.chmod(0o770)
    monkeypatch.setattr(cron, "_group_members", lambda gid: [])
    r = go(capsys, FakeCrontab(None), "cron", "install", "--env-file", str(f))
    assert r.code == 0, r.err


def test_env_file_in_a_group_writable_directory_shared_with_others_is_refused(
    capsys: pytest.CaptureFixture[str],
    wtctl_bin: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = make_env_file(tmp_path)
    f.parent.chmod(0o770)
    monkeypatch.setattr(cron, "_group_members", lambda gid: ["someone-else"])
    r = go(capsys, FakeCrontab(None), "--json", "cron", "install", "--env-file", str(f))
    assert r.code == 2 and "chmod g-w" in r.json()["error"]["message"]
