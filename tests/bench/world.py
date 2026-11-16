"""A benchmark world: bare remote, stale clone, private HOME, and the real ``wtctl`` CLI run as a
child process (real git children, real fd-based deletion), timed with wall clock and peak RSS."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.bench import fixture as fx

REPO = "example.test/org/repo"
ACTOR = "human:bench"
ROOT = Path(__file__).resolve().parents[2]
MARKER = ".wtctl-bench"
MIN_FREE_GIB = 15
_FORBIDDEN_TREES = (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"), Path("/run"))


def _within(path: Path, tree: Path) -> bool:
    return path == tree or tree in path.parents


def _forbidden_trees() -> tuple[Path, ...]:
    """Each temporary tree as written and as resolved (``/tmp`` is ``/private/tmp`` on macOS),
    plus the platform's own temporary directory."""
    named = (*_FORBIDDEN_TREES, Path(tempfile.gettempdir()))
    return tuple(dict.fromkeys((*named, *(t.resolve() for t in named))))


def bench_dir() -> Path:
    """The benchmark directory from ``BENCH_DIR`` (default ``.bench`` at the repository root),
    resolved. Refuses ``/``, the home directory, the repository root, any ancestor of those,
    and anything in or equal to a temporary file system; the data is deleted, so a typo must
    never reach a real directory."""
    raw = os.environ.get("BENCH_DIR", str(ROOT / ".bench"))
    if not raw.strip():
        pytest.fail("BENCH_DIR is empty; set it to a dedicated directory on a large disk")
    path = Path(raw).expanduser().resolve()
    home = Path.home().resolve()
    for keep in (ROOT, home):
        if path == keep or path in keep.parents:
            pytest.fail(f"BENCH_DIR {path} is, or contains, {keep}; use a dedicated directory")
    if path == Path("/"):
        pytest.fail("BENCH_DIR resolves to /")
    for tree in _forbidden_trees():
        if _within(path, tree):
            pytest.fail(
                f"BENCH_DIR {path} is under {tree}; use a dedicated directory on a large disk"
            )
    return path


def prepare_bench_dir(base: Path, *, min_free_gib: float = MIN_FREE_GIB) -> None:
    """Make ``base`` an empty benchmark directory holding only the marker. An existing directory
    is cleared only when it is empty or already carries the marker; anything else fails without
    deleting. The free-space check runs first, on the nearest existing parent."""
    probe = base
    while not probe.exists():
        probe = probe.parent
    free_gib = shutil.disk_usage(probe).free / 1024**3
    if free_gib < min_free_gib:
        pytest.skip(f"only {free_gib:.0f} GiB free under {probe}")
    if base.exists():
        if not base.is_dir():
            pytest.fail(f"BENCH_DIR {base} exists and is not a directory")
        if any(base.iterdir()) and not (base / MARKER).exists():
            pytest.fail(f"BENCH_DIR {base} is not empty and has no {MARKER} marker; not deleting")
        shutil.rmtree(base)
    base.mkdir(parents=True)
    (base / MARKER).write_text("created by tests/bench; safe to delete\n")


def cleanup_bench_dir(base: Path) -> None:
    """Remove everything under ``base`` except ``results.json`` and the marker. Does nothing
    unless ``base`` carries the marker, so a wrong path is never emptied."""
    if not (base / MARKER).exists():
        return
    for entry in base.iterdir():
        if entry.name in ("results.json", MARKER):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()


def kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the process group of a child started with ``start_new_session=True`` (its pgid is
    the exact pid we started) and reap it.

    The group is signalled whenever the child has not been reaped yet, even if the leader itself
    already exited: an unreaped (zombie) leader keeps its pid, and so the pgid, reserved, so
    ``killpg`` can only reach our own group. ``poll()`` is deliberately not used first, since it
    would reap the leader and release the pgid for reuse while grandchildren may still run."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


@dataclass
class Timed:
    argv: list[str]
    code: int
    wall: float
    rss_kib: int  # largest resident set of any single process in the command's tree
    out: str
    err: str
    git_calls: list[tuple[float, str]] = field(default_factory=list)  # (seconds, subcommand)

    @property
    def git_total(self) -> float:
        return sum(sec for sec, _ in self.git_calls)

    @property
    def git_slowest(self) -> tuple[float, str]:
        return max(self.git_calls, default=(0.0, "-"))

    def json(self) -> Any:
        return json.loads(self.out)


@dataclass
class Series:
    name: str
    runs: list[Timed] = field(default_factory=list)

    @property
    def walls(self) -> list[float]:
        return [r.wall for r in self.runs]

    @property
    def median(self) -> float:
        return statistics.median(self.walls)


WRAPPER = """#!/bin/bash
# Times every git call wtctl makes: one line of microseconds and the git subcommand.
start=${EPOCHREALTIME/./}
"__GIT__" "$@"
rc=$?
end=${EPOCHREALTIME/./}
sub="?"
skip=0
for a in "$@"; do
  if [ "$skip" = 1 ]; then skip=0; continue; fi
  case "$a" in
    -c|-C|--git-dir|--work-tree) skip=1 ;;
    -*) ;;
    *) sub="$a"; break ;;
  esac
done
printf '%s\\t%s\\n' "$((end - start))" "$sub" >> "__LOG__"
exit $rc
"""


def parse_git_log(text: str) -> list[tuple[float, str]]:
    out = []
    for line in text.splitlines():
        micros, _, sub = line.partition("\t")
        if micros.isdigit():
            out.append((int(micros) / 1e6, sub))
    return out


class World:
    def __init__(self, base: Path, dims: fx.Dims) -> None:
        self.base = base
        self.dims = dims
        self.home = base / "home"
        self.ghome = base / "gh"  # HOME of the fixture generator's git
        self.remote = base / "remote.git"
        self.clone = base / "clone"
        self.root = base / "wt"
        self.config_path = base / "cfg" / "config.yaml"
        self.gitlog = base / "git-calls.log"
        self.top: dict[str, Any] = {"root": str(self.root), "lease_wait": "5s"}
        self.repo_cfg: dict[str, Any] = {}
        self.timeline: dict[str, float] = {}
        self.env_files = 0  # .env.local files the build wrote into the clone

    # -- construction ---------------------------------------------------------------------

    def build(self, *, stale_at: int, copy_env_files: int = 5) -> None:
        d = self.dims
        t = time.monotonic()
        fx.build_remote(self.remote, self.ghome, d, stale_at=stale_at)
        self.timeline["remote_stale"] = time.monotonic() - t
        t = time.monotonic()
        self._git(self.base, "clone", "-q", str(self.remote), str(self.clone))
        self.timeline["clone"] = time.monotonic() - t
        t = time.monotonic()
        fx.finish_remote(self.remote, self.ghome, d, stale_at)
        self.timeline["remote_rest"] = time.monotonic() - t
        for k in range(copy_env_files):
            env = self.clone / fx.file_path(d, k * (d.files // max(copy_env_files, 1)))
            (env.parent / ".env.local").write_text(f"KEY={k}\n")
        self.env_files = copy_env_files
        self.repo_cfg = {
            "clone": str(self.clone),
            "clone_url": str(self.remote),
            "copy_env": ["**/.env.local"],
        }
        self.home.mkdir(parents=True, exist_ok=True)

    def _git(self, cwd: Path, *args: str) -> str:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=fx.git_env(self.ghome),
            capture_output=True,
            check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"git {args}: {r.stderr.decode()}")
        return r.stdout.decode().strip()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        return self._git(cwd or self.clone, *args)

    # -- running --------------------------------------------------------------------------

    def env(self) -> dict[str, str]:
        h = self.home
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(h),
            "XDG_CONFIG_HOME": str(h / ".config"),
            "XDG_STATE_HOME": str(h / ".local" / "state"),
            "XDG_DATA_HOME": str(h / ".local" / "share"),
            "XDG_CACHE_HOME": str(h / ".cache"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }

    def write_config(self) -> None:
        wrapper = self.base / "git-timed.sh"
        if not wrapper.exists():
            real = shutil.which("git") or "/usr/bin/git"
            wrapper.write_text(
                WRAPPER.replace("__GIT__", real).replace("__LOG__", str(self.gitlog))
            )
            wrapper.chmod(0o755)
        data = {**self.top, "git_bin": str(wrapper), "repos": {REPO: self.repo_cfg}}
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(data))

    def run(self, *argv: str, actor: str = ACTOR, timeout: float = 3600) -> Timed:
        """Run ``wtctl --json <argv>``; peak RSS is the largest of the process and every
        descendant it waited for (``wait4``'s ``ru_maxrss``)."""
        self.write_config()
        full = [
            sys.executable,
            "-m",
            "wtctl",
            "--config",
            str(self.config_path),
            "--json",
            "--actor",
            actor,
            *argv,
        ]
        out_path, err_path = self.base / "last.out", self.base / "last.err"
        self.gitlog.write_text("")
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            start = time.monotonic()
            proc = subprocess.Popen(
                full,
                env=self.env(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,  # its own group: a kill reaches git children too
            )
            reaped = False
            try:
                while True:
                    done, status, usage = os.wait4(proc.pid, os.WNOHANG)
                    if done:
                        reaped = True
                        break
                    if time.monotonic() - start > timeout:
                        raise TimeoutError(f"{full} exceeded {timeout}s")
                    time.sleep(0.02)
            finally:
                if not reaped:  # timeout, KeyboardInterrupt or any error: the exact group we made
                    kill_group(proc)
            wall = time.monotonic() - start
        proc.returncode = os.waitstatus_to_exitcode(status)
        return Timed(
            list(argv),
            proc.returncode,
            wall,
            usage.ru_maxrss,
            out_path.read_text(errors="replace"),
            err_path.read_text(errors="replace"),
            parse_git_log(self.gitlog.read_text(errors="replace")),
        )


# -- results ------------------------------------------------------------------------------------


class Results:
    """Named series of timed runs, rendered as a Markdown table (seconds, median of the runs)."""

    def __init__(self) -> None:
        self.series: dict[str, Series] = {}
        self.notes: dict[str, str] = {}

    def add(self, name: str, run: Timed, *, note: str = "") -> Timed:
        self.series.setdefault(name, Series(name)).runs.append(run)
        if note:
            self.notes[name] = note
        return run

    def rows(self) -> list[dict[str, Any]]:
        out = []
        for s in self.series.values():
            out.append(
                {
                    "operation": s.name,
                    "runs": len(s.runs),
                    "median_s": round(s.median, 2),
                    "min_s": round(min(s.walls), 2),
                    "max_s": round(max(s.walls), 2),
                    "peak_rss_mib": max(r.rss_kib for r in s.runs) // 1024,
                    "git_median_s": round(statistics.median(r.git_total for r in s.runs), 2),
                    "git_slowest": max((r.git_slowest for r in s.runs), default=(0.0, "-")),
                    "exit": sorted({r.code for r in s.runs}),
                    "note": self.notes.get(s.name, ""),
                }
            )
        return out

    def markdown(self) -> str:
        lines = [
            "| Operation | Runs | Median (s) | Min-max (s) | Git calls, median total (s) "
            "| Slowest git call (s) | Peak RSS (MiB) | Exit |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for r in self.rows():
            exits = ",".join(str(c) for c in r["exit"])
            lines.append(
                f"| {r['operation']} | {r['runs']} | {r['median_s']} | "
                f"{r['min_s']}-{r['max_s']} | {r['git_median_s']} | "
                f"{r['git_slowest'][0]:.1f} ({r['git_slowest'][1]}) | "
                f"{r['peak_rss_mib']} | {exits} |"
            )
        return "\n".join(lines) + "\n"


def machine_info(path: Path) -> dict[str, str]:
    """CPU count, model, disk type of the file system holding ``path``, git and Python versions."""
    info = {"cpus": str(os.cpu_count()), "python": sys.version.split()[0]}
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                info["cpu_model"] = line.split(":", 1)[1].strip()
                break
        info["memory_gib"] = str(
            round(int(Path("/proc/meminfo").read_text().split()[1]) / 1024 / 1024)
        )
    except (OSError, ValueError, IndexError):
        pass
    scrub = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(path),
        "XDG_CONFIG_HOME": str(path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    git = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, check=False, env=scrub
    )
    info["git"] = git.stdout.strip().removeprefix("git version ")
    fs = subprocess.run(
        ["stat", "-f", "-c", "%T", str(path)],
        capture_output=True,
        text=True,
        check=False,
        env=scrub,
    )
    info["filesystem"] = fs.stdout.strip()
    try:
        st = os.stat(path)
        dev = f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
        sysdir = Path(f"/sys/dev/block/{dev}").resolve()
        for candidate in (sysdir / "queue" / "rotational", sysdir.parent / "queue" / "rotational"):
            if candidate.exists():
                rot = candidate.read_text().strip()
                name = candidate.parent.parent.name
                info["disk"] = (
                    f"{name}, {'rotational' if rot == '1' else 'non-rotational (SSD/NVMe)'}"
                )
                break
    except OSError:
        pass
    return info
