"""Deterministic synthetic "monorepo-sized" repository generator for the timing benchmarks.

Everything is local and offline: ``git fast-import`` writes a bare remote from pure functions of
the dimensions, so two runs with the same ``Dims`` give byte-identical histories (same shas).
The unit tests use tiny dimensions; the ``bench`` tests use ``MONOREPO``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

BASE_TIME = 1_600_000_000
IDENT = "Bench <bench@example.invalid>"


@dataclass(frozen=True)
class Dims:
    packages: int  # top-level directories
    modules: int  # directories per package
    depth: int  # nested directories under each module (a chain: l0/l1/...)
    files_per_leaf: int  # files in the deepest directory of each module
    commits: int  # length of the main history (>= 2)
    files_per_commit: int  # tracked files each commit after the first modifies
    branches: int  # extra branches, one commit each, forked from main
    tags: int  # lightweight tags on main commits
    large_blobs: int  # incompressible files added by the second commit
    large_blob_bytes: int

    @property
    def files(self) -> int:
        return self.packages * self.modules * self.files_per_leaf

    @property
    def dirs(self) -> int:
        return self.packages * self.modules * (1 + self.depth)


TINY = Dims(2, 2, 2, 3, 6, 2, 3, 2, 1, 1024)
# About 200k tracked files, 215k tree entries (the throwaway tree bound is 500k), 20k commits.
MONOREPO = Dims(
    packages=100,
    modules=20,
    depth=5,
    files_per_leaf=100,
    commits=20_000,
    files_per_commit=8,
    branches=500,
    tags=300,
    large_blobs=3,
    large_blob_bytes=16 * 1024 * 1024,
)


def git_env(home: Path) -> dict[str, str]:
    """A private, config-free environment for every git the generator starts."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }


def file_path(d: Dims, n: int) -> str:
    leaf = d.files_per_leaf
    mod_index, f = divmod(n, leaf)
    p, m = divmod(mod_index, d.modules)
    chain = "/".join(f"l{k}" for k in range(d.depth))
    mid = f"{chain}/" if chain else ""
    return f"pkg{p:03d}/mod{m:02d}/{mid}f{f:03d}.ts"


def _filler(seed: str, size: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}/{counter}".encode()).hexdigest().encode() + b"\n"
        counter += 1
    return out[:size]


def base_content(n: int) -> bytes:
    return f"export const value = {n};\n".encode() + _filler(f"base{n}", 96)


def touched(d: Dims, i: int) -> list[int]:
    """The distinct file numbers commit ``i`` (>= 1) modifies."""
    picked: dict[int, None] = {}
    j = 0
    while len(picked) < min(d.files_per_commit, d.files):
        picked[(i * 7919 + j * 104729) % d.files] = None
        j += 1
    return list(picked)


def commit_content(n: int, i: int) -> bytes:
    return f"export const value = {n};\n// edited by commit {i}\n".encode() + _filler(
        f"c{i}/{n}", 280
    )


def large_blob(k: int, size: int) -> bytes:
    out = bytearray()
    block = 0
    while len(out) < size:
        out += hashlib.sha256(f"large{k}/{block}".encode()).digest() * 1024
        block += 1
    return bytes(out[:size])


def _data(payload: bytes) -> Iterator[bytes]:
    yield b"data %d\n" % len(payload)
    yield payload
    yield b"\n"


def _file_cmd(path: str, payload: bytes) -> Iterator[bytes]:
    yield b"M 100644 inline " + path.encode() + b"\n"
    yield from _data(payload)


def _commit_head(
    ref: str, mark: int | None, when: int, message: str, parent: str
) -> Iterator[bytes]:
    yield f"commit {ref}\n".encode()
    if mark is not None:
        yield f"mark :{mark}\n".encode()
    yield f"author {IDENT} {when} +0000\ncommitter {IDENT} {when} +0000\n".encode()
    yield from _data(message.encode())
    if parent:
        yield parent.encode() + b"\n"


def _main_stream(d: Dims, start: int, stop: int) -> Iterator[bytes]:
    for i in range(start, stop):
        parent = "from refs/heads/main^0" if i == start and start > 0 else ""
        if i > start:
            parent = f"from :{i}"  # marks are 1-based commit numbers
        yield from _commit_head("refs/heads/main", i + 1, BASE_TIME + i * 60, f"commit {i}", parent)
        if i == 0:
            for n in range(d.files):
                yield from _file_cmd(file_path(d, n), base_content(n))
            yield from _file_cmd(".gitignore", b"node_modules/\n.env.local\n")
            for p in range(min(d.packages, 50)):
                yield from _file_cmd(f"pkg{p:03d}/CLAUDE.md", f"# package {p}\n".encode())
        elif i == 1:
            for k in range(d.large_blobs):
                yield from _file_cmd(f"assets/blob{k}.bin", large_blob(k, d.large_blob_bytes))
        else:
            for n in touched(d, i):
                yield from _file_cmd(file_path(d, n), commit_content(n, i))
        yield b"\n"


def _fast_import(repo: Path, home: Path, stream: Iterator[bytes]) -> None:
    proc = subprocess.Popen(
        ["git", "fast-import", "--quiet", "--done"],
        cwd=repo,
        env=git_env(home),
        stdin=subprocess.PIPE,
    )
    assert proc.stdin is not None
    for chunk in stream:
        proc.stdin.write(chunk)
    proc.stdin.write(b"done\n")
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("git fast-import failed")


def _git(repo: Path, home: Path, *args: str, stdin: bytes | None = None) -> str:
    r = subprocess.run(
        ["git", *args], cwd=repo, env=git_env(home), input=stdin, capture_output=True, check=False
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr.decode()}")
    return r.stdout.decode().strip()


def init_remote(remote: Path, home: Path) -> None:
    remote.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    _git(remote, home, "init", "-q", "--bare", "-b", "main")


def extend_main(remote: Path, home: Path, d: Dims, start: int, stop: int) -> None:
    """Commits ``start``..``stop-1`` of the main history (``start`` 0 creates the branch)."""
    if not 0 <= start < stop <= d.commits:
        raise ValueError("commit range outside the history")
    _fast_import(remote, home, _main_stream(d, start, stop))


def advance_main(remote: Path, home: Path, d: Dims, have: int, count: int) -> None:
    """``count`` more commits on main, numbered after the ``have`` commits it already holds (a
    remote that moved on since a mirror last refreshed)."""
    _fast_import(remote, home, _main_stream(d, have, have + count))


def add_refs(remote: Path, home: Path, d: Dims) -> None:
    """``d.branches`` one-commit branches forked from spread-out main commits, ``d.tags`` tags."""
    shas = _git(remote, home, "rev-list", "--first-parent", "--reverse", "main").split()

    def at(k: int, count: int) -> str:
        return shas[(k * len(shas)) // max(count, 1) % len(shas)]

    def stream() -> Iterator[bytes]:
        for b in range(d.branches):
            when = BASE_TIME + (d.commits + b) * 60
            yield from _commit_head(
                f"refs/heads/feature/b{b:04d}",
                None,
                when,
                f"branch {b}",
                f"from {at(b, d.branches)}",
            )
            yield from _file_cmd(f"pkg000/branch{b:04d}.txt", f"branch {b}\n".encode())
            yield b"\n"

    if d.branches:
        _fast_import(remote, home, stream())
    if d.tags:
        lines = "".join(f"create refs/tags/v{t:04d} {at(t, d.tags)}\n" for t in range(d.tags))
        _git(remote, home, "update-ref", "--stdin", stdin=lines.encode())


def build_remote(remote: Path, home: Path, d: Dims, *, stale_at: int | None = None) -> None:
    """The whole remote; with ``stale_at`` only the first ``stale_at`` commits (see ``extend``)."""
    init_remote(remote, home)
    extend_main(remote, home, d, 0, d.commits if stale_at is None else stale_at)
    if stale_at is None:
        add_refs(remote, home, d)


def finish_remote(remote: Path, home: Path, d: Dims, stale_at: int) -> None:
    """Complete a remote built with ``stale_at``: the rest of main, the branches and the tags."""
    extend_main(remote, home, d, stale_at, d.commits)
    add_refs(remote, home, d)


def populate_ignored(root: Path, dirs: int, files_per_dir: int) -> int:
    """A ``node_modules`` tree (ignored by the fixture's ``.gitignore``); returns the file count."""
    base = root / "node_modules"
    count = 0
    for k in range(dirs):
        leaf = base / f"dep{k:04d}" / "lib" / "internal"
        leaf.mkdir(parents=True, exist_ok=True)
        for f in range(files_per_dir):
            (leaf / f"m{f:03d}.js").write_bytes(
                f"module.exports = {k * files_per_dir + f};\n".encode()
            )
            count += 1
    return count
