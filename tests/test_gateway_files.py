"""run_git with stdin/stdout files and an output-size limit (pack-objects --stdout)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from wtctl.gateway import run_git

ENV = {"PATH": "/usr/bin:/bin"}


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_stdout_goes_to_the_file_not_to_memory(tmp_path: Path) -> None:
    out = tmp_path / "o"
    r = run_git(py("import sys; sys.stdout.write('x'*100000)"), env=ENV, cwd=str(tmp_path),
                stdout_path=str(out))  # fmt: skip
    assert r.returncode == 0 and r.stdout == b"" and out.stat().st_size == 100000
    assert (out.stat().st_mode & 0o777) == 0o600


def test_stdin_comes_from_the_file(tmp_path: Path) -> None:
    src = tmp_path / "i"
    src.write_bytes(b"hello\n" * 3)
    r = run_git(py("import sys; sys.stdout.write(str(len(sys.stdin.read())))"), env=ENV,
                cwd=str(tmp_path), stdin_path=str(src))  # fmt: skip
    assert r.out == "18"


def test_an_existing_output_file_or_symlink_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.write_text("keep")
    with pytest.raises(FileExistsError):
        run_git(py("print(1)"), env=ENV, cwd=str(tmp_path), stdout_path=str(out))
    assert out.read_text() == "keep"
    victim = tmp_path / "victim"
    victim.write_text("keep")
    link = tmp_path / "l"
    link.symlink_to(victim)
    with pytest.raises(OSError):
        run_git(py("print(1)"), env=ENV, cwd=str(tmp_path), stdout_path=str(link))
    assert victim.read_text() == "keep"


def test_a_fifo_as_input_is_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "f"
    os.mkfifo(fifo)
    with pytest.raises(OSError):
        run_git(py("print(1)"), env=ENV, cwd=str(tmp_path), stdin_path=str(fifo))


def test_the_size_limit_kills_the_process(tmp_path: Path) -> None:
    out = tmp_path / "o"
    code = (
        "import sys,time\nwhile True:\n"
        "    sys.stdout.write('x'*65536); sys.stdout.flush(); time.sleep(0.01)\n"
    )
    t0 = time.monotonic()
    r = run_git(
        py(code), env=ENV, cwd=str(tmp_path), stdout_path=str(out),
        max_stdout_bytes=200_000, timeout=60,
    )  # fmt: skip
    assert r.output_limit and not r.timed_out and r.returncode != 0
    assert time.monotonic() - t0 < 30


def test_a_small_output_is_within_the_limit(tmp_path: Path) -> None:
    r = run_git(py("print('ok')"), env=ENV, cwd=str(tmp_path), stdout_path=str(tmp_path / "o"),
                max_stdout_bytes=1000)  # fmt: skip
    assert r.returncode == 0 and not r.output_limit


def test_the_timeout_still_applies_with_a_limit(tmp_path: Path) -> None:
    r = run_git(py("import time; time.sleep(30)"), env=ENV, cwd=str(tmp_path),
                stdout_path=str(tmp_path / "o"), max_stdout_bytes=1000, timeout=0.5)  # fmt: skip
    assert r.timed_out and not r.output_limit


def test_exclusive_options_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_git(py("pass"), env=ENV, cwd=str(tmp_path), stdin=b"x", stdin_path=str(tmp_path))
    with pytest.raises(ValueError):
        run_git(py("pass"), env=ENV, cwd=str(tmp_path), max_stdout_bytes=5)
