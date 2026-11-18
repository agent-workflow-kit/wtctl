"""A real terminal: launch `wtctl browse` under a pty, send keys, quit cleanly."""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.listhelpers import seed
from tests.statehelpers import make_state
from wtctl.state import State, state_path

pty = pytest.importorskip("pty")
fcntl = pytest.importorskip("fcntl")
termios = pytest.importorskip("termios")
pytest.importorskip("curses")

HOSTILE = "evil\x1b]0;pwn\x07\x9b2Jend"


class Term:
    def __init__(self, env: dict[str, str], argv: list[str], rows: int = 30, cols: int = 110):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, _winsize(rows, cols))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "wtctl", *argv],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),  # the pty is its terminal
            close_fds=True,
        )
        os.close(slave)
        self.raw = b""

    def resize(self, rows: int, cols: int) -> None:
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, _winsize(rows, cols))
        import signal

        os.kill(self.proc.pid, signal.SIGWINCH)  # our own child, by its exact pid

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def read_until(self, needle: bytes, timeout: float = 20) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if needle in self.raw:
                return True
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(self.master, 65536)
                except OSError:
                    return needle in self.raw
                if not chunk:
                    return needle in self.raw
                self.raw += chunk
        return needle in self.raw

    def finish(self, timeout: float = 60) -> int | None:
        """Wait for exit while draining the pty: a child blocked writing its last bytes into a
        full pty buffer would otherwise look hung (the cause of a load-dependent flake)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            code = self.proc.poll()
            if code is not None:
                return code
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                try:
                    self.raw += os.read(self.master, 65536)
                except OSError:
                    pass
        return None

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()  # the exact Popen we started
            self.proc.wait(timeout=10)
        os.close(self.master)


def _winsize(rows: int, cols: int) -> bytes:
    import struct

    return struct.pack("HHHH", rows, cols, 0, 0)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    # conftest already points HOME and the XDG dirs at a private tree
    s = State(state_path(), make_state(tmp_path / "x").gateway)
    seed(s, "wt-aaaaaaaa", repo="example.com/o/alpha")
    seed(s, "wt-bbbbbbbb", repo="example.com/o/beta", name=HOSTILE)
    s.close()
    keep = ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "PATH")
    out = {k: os.environ[k] for k in keep if k in os.environ}
    out["TERM"] = "xterm"
    out["LANG"] = "C.UTF-8"
    out["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return out


def test_browse_navigates_and_quits_cleanly(env: dict[str, str]) -> None:
    t = Term(env, ["browse"])
    try:
        assert t.read_until(b"wt-aaaaaaaa"), t.raw[-500:]
        assert b"wt-bbbbbbbb" in t.raw
        t.send(b"j")
        t.send(b"\n")  # open the second record
        assert t.read_until(b"never removes"), t.raw[-500:]
        t.send(b"s")  # status runs in a child; the UI stays responsive
        assert t.read_until(b"status"), t.raw[-500:]
        t.send(b"?")
        assert t.read_until(b"read-only")
        t.resize(12, 40)
        t.send(b"\x1b")  # leave help
        t.send(b"q")
        assert t.finish() == 0, t.raw[-500:]
    finally:
        t.close()
    # nothing the record carried reached the terminal as a control sequence
    assert b"\x07" not in t.raw
    assert b"\x1b]0;" not in t.raw
    assert b"\x9b2J" not in t.raw


def test_ctrl_c_quits_cleanly(env: dict[str, str]) -> None:
    t = Term(env, ["browse"])
    try:
        assert t.read_until(b"wt-aaaaaaaa", timeout=60)
        assert t.read_until(b"q quit", timeout=60)  # the whole first screen is drawn
        t.send(b"\x03")
        assert t.finish() == 0, t.raw[-300:]
    finally:
        t.close()


def test_filter_in_a_real_terminal(env: dict[str, str]) -> None:
    t = Term(env, ["browse"])
    try:
        assert t.read_until(b"2/2")
        t.send(b"/beta\n")
        t.send(b"\n")  # leave the filter box ...
        t.send(b"\n")  # ... and open the only match (curses redraws only what changed)
        assert t.read_until(b"id: wt-bbbbbbbb"), t.raw[-500:]
        t.send(b"q")
        assert t.finish() == 0
    finally:
        t.close()


def test_dumb_terminal_exits_2_without_hanging(env: dict[str, str]) -> None:
    t = Term({**env, "TERM": "dumb"}, ["browse"])
    try:
        assert t.finish() == 2
        t.read_until(b"TERM")
        assert b"TERM" in t.raw
    finally:
        t.close()


def test_piped_stdout_exits_2(env: dict[str, str]) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "wtctl", "browse"],
        env=env,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    assert r.returncode == 2 and b"interactive only" in r.stderr


def _add_record(tmp_path: Path, id_: str, **kw: object) -> None:
    s = State(state_path(), make_state(tmp_path / "y").gateway)
    seed(s, id_, **kw)
    s.close()


def test_uninstalled_locale_does_not_crash(env: dict[str, str]) -> None:
    t = Term({**env, "LANG": "xx_YY.UTF-8", "LC_ALL": ""}, ["browse"])
    try:
        assert t.read_until(b"wt-aaaaaaaa", timeout=60), t.raw[-500:]
        t.send(b"q")
        assert t.finish() == 0, t.raw[-500:]
    finally:
        t.close()


def test_c_locale_shows_non_ascii_as_visible_escapes(tmp_path: Path, env: dict[str, str]) -> None:
    _add_record(tmp_path, "wt-cccccccc", repo="example.com/o/gamma", name="caf\u00e9")
    t = Term({**env, "LC_ALL": "C", "LANG": "C"}, ["browse"])
    try:
        assert t.read_until(b"caf\\xe9", timeout=60), t.raw[-500:]
        t.send(b"q")
        assert t.finish() == 0
    finally:
        t.close()


def test_unknown_term_is_a_usage_error(env: dict[str, str]) -> None:
    t = Term({**env, "TERM": "nosuchterm"}, ["browse"])
    try:
        assert t.finish() == 2, t.raw[-300:]
        t.read_until(b"TERM", timeout=5)
        assert b"nosuchterm" in t.raw
    finally:
        t.close()
