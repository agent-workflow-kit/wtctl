"""The real process scanners behind ``gateway.ProcessScanner`` (interface.md, In-use detection).

Linux and WSL read ``/proc``; macOS runs ``lsof``. Both report every process of the invoking user
with its cwd, the files it has open (descriptors, and on Linux memory maps) and, on Linux, what the
in-use check needs to exclude wtctl's own ancestry. A process of another user, or one whose
entries cannot be read, comes back with ``inspected=False`` (counted by the caller as
``uninspected``). Nothing here decides anything: ``wtctl.inuse`` matches the paths.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from .gateway import ProcessInfo, ScannerUnavailable, run

_MAPS_MAX = 100_000  # lines of /proc/<pid>/maps read per process


def _stat_fields(raw: str) -> list[str] | None:
    """The fields of ``/proc/<pid>/stat`` after the command name (field 3 onward)."""
    # the command (field 2) is in parentheses and may itself contain spaces or ')'
    rest = raw[raw.rfind(")") + 2 :].split()
    return rest if len(rest) >= 20 else None


class ProcScanner:
    """Linux and WSL: ``/proc``."""

    def __init__(self, proc: str = "/proc", uid: int | None = None) -> None:
        self.proc = proc
        self.uid = os.getuid() if uid is None else uid

    def scan(self, target: str | None = None) -> list[ProcessInfo]:
        try:
            names = os.listdir(self.proc)
        except OSError as exc:
            raise ScannerUnavailable(f"cannot read {self.proc}: {exc}") from exc
        if "self" not in names and not any(n.isdigit() for n in names):
            raise ScannerUnavailable(f"{self.proc} lists no processes")
        out: list[ProcessInfo] = []
        for name in names:
            if name.isdigit():
                info = self._one(int(name))
                if info is not None:
                    out.append(info)
        return out

    def _one(self, pid: int) -> ProcessInfo | None:
        base = f"{self.proc}/{pid}"
        try:
            owner = os.stat(base).st_uid
        except FileNotFoundError:
            return None  # gone since the listing
        except OSError:
            return ProcessInfo(pid, "?", None, inspected=False)
        ppid, start = self._stat(base)
        command = self._comm(base)
        if owner != self.uid:
            return ProcessInfo(pid, command, None, ppid=ppid, start=start, inspected=False)
        cwd: str | None = None
        cwd_id: tuple[int, int] | None = None
        ok = True
        try:
            cwd = os.readlink(f"{base}/cwd")
            st = os.stat(f"{base}/cwd")
            cwd_id = (st.st_dev, st.st_ino)
        except FileNotFoundError:
            if not os.path.exists(base):
                return None
            ok = False
        except OSError:
            ok = False
        paths, fds_ok = self._fds(base)
        paths += self._maps(base)
        if not os.path.exists(base):
            return None  # exited while being read
        return ProcessInfo(
            pid, command, cwd, tuple(paths), cwd_id=cwd_id, ppid=ppid, start=start,
            inspected=ok and fds_ok,
        )  # fmt: skip

    @staticmethod
    def _comm(base: str) -> str:
        try:
            with open(f"{base}/comm", "rb") as fh:
                return fh.read(64).decode("utf-8", "replace").rstrip("\n")
        except OSError:
            return "?"

    @staticmethod
    def _stat(base: str) -> tuple[int | None, str | None]:
        try:
            with open(f"{base}/stat", "rb") as fh:
                rest = _stat_fields(fh.read(4096).decode("utf-8", "replace"))
        except OSError:
            return None, None
        if rest is None:
            return None, None
        return int(rest[1]), rest[19]

    @staticmethod
    def _fds(base: str) -> tuple[list[str], bool]:
        paths: list[str] = []
        try:
            fds = os.listdir(f"{base}/fd")
        except FileNotFoundError:
            return paths, True
        except OSError:
            return paths, False
        ok = True
        for fd in fds:
            try:
                target = os.readlink(f"{base}/fd/{fd}")
            except FileNotFoundError:
                continue  # closed since the listing
            except OSError:
                ok = False
                continue
            if target.startswith("/"):  # sockets and pipes read "socket:[1]" and the like
                paths.append(target)
        return paths, ok

    @staticmethod
    def _maps(base: str) -> list[str]:
        """Files mapped into the process (a file whose descriptor was closed after ``mmap``)."""
        paths: set[str] = set()
        try:
            with open(f"{base}/maps", "rb") as fh:
                for n, line in enumerate(fh):
                    if n >= _MAPS_MAX:
                        break
                    fields = line.decode("utf-8", "surrogateescape").rstrip("\n").split(None, 5)
                    if len(fields) == 6 and fields[5].startswith("/"):
                        paths.add(fields[5])
        except OSError:
            pass  # a process without a readable maps file simply shows its descriptors
        return sorted(paths)


_LSOF_FIELDS = re.compile(r"^([pcn])(.*)$", re.S)


def parse_lsof(text: str) -> list[ProcessInfo]:
    """``lsof -F`` output (``pPID``, ``cCOMMAND``, ``nNAME`` lines; ``-F`` joins fields by newline,
    so a name with a newline cannot be told from a following field and is dropped)."""
    procs: dict[int, dict[str, object]] = {}
    pid: int | None = None
    for line in text.split("\n"):
        m = _LSOF_FIELDS.match(line)
        if not m:
            continue
        key, value = m.groups()
        if key == "p":
            pid = int(value) if value.isdigit() else None
            if pid is not None:
                procs.setdefault(pid, {"command": "?", "names": []})
        elif pid is not None and key == "c":
            procs[pid]["command"] = value
        elif pid is not None and key == "n":
            names = procs[pid]["names"]
            assert isinstance(names, list)
            names.append(value)
    return [
        ProcessInfo(p, str(d["command"]), None, tuple(n for n in d["names"] if n.startswith("/")))  # type: ignore[attr-defined, union-attr]
        for p, d in procs.items()
    ]


class LsofScanner:
    """macOS: ``lsof -a -d cwd -Fpcn -u <uid>`` for working directories and
    ``lsof -Fpcn +D <dir>`` for open files below the target (path matching only)."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = run,
        uid: int | None = None,
    ) -> None:
        self.runner = runner
        self.uid = os.getuid() if uid is None else uid

    def _lsof(self, argv: list[str]) -> str:
        try:
            r = self.runner(["lsof", *argv], timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ScannerUnavailable(f"cannot run lsof: {exc}") from exc
        # lsof exits 1 when it lists nothing (and for some unreadable entries); output decides
        if r.returncode not in (0, 1) or (r.returncode == 1 and r.stderr.strip() and not r.stdout):
            if r.returncode in (126, 127):
                raise ScannerUnavailable("lsof is not installed")
        return r.stdout

    def scan(self, target: str | None = None) -> list[ProcessInfo]:
        cwds = parse_lsof(self._lsof(["-a", "-d", "cwd", "-Fpcn", "-u", str(self.uid)]))
        files: list[ProcessInfo] = []
        if target is not None:
            files = parse_lsof(self._lsof(["-Fpcn", "+D", target]))
        by_pid: dict[int, ProcessInfo] = {}
        for c in cwds:
            by_pid[c.pid] = ProcessInfo(c.pid, c.command, c.open_paths[0] if c.open_paths else None)
        for f in files:
            old = by_pid.get(f.pid)
            by_pid[f.pid] = ProcessInfo(f.pid, f.command, old.cwd if old else None, f.open_paths)
        return list(by_pid.values())


def system_scanner() -> ProcScanner | LsofScanner:
    """``/proc`` on Linux, ``lsof`` elsewhere."""
    if sys.platform.startswith("linux") and Path("/proc/self").exists():
        return ProcScanner()
    return LsofScanner()
