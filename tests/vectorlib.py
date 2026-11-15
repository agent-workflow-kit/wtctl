"""A hostile repository: every planted vector of the hardened-git suite, in real git, no network.

Every payload only appends to a file under ``markers/``. A test passes when that directory is
empty. ``plain_git`` runs *unhardened* git in the same repository (explicit minimal environment,
the private HOME) so a test can prove a vector is live: a vector whose control does not fire
proves nothing about the hardened run.
"""

from __future__ import annotations

import os
import pty
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GIT = "/usr/bin/git" if os.path.exists("/usr/bin/git") else (shutil.which("git") or "/usr/bin/git")

PLAIN_ENV_BASE = {
    "PATH": "/usr/bin:/bin",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def plain_git(
    args: list[str],
    *,
    cwd: Path,
    home: Path,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    tty: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """Unhardened git with an explicit environment (nothing inherited).

    ``tty`` runs it with a pseudo-terminal as stdout: git starts a pager only for a terminal.
    """
    full = {**PLAIN_ENV_BASE, "HOME": str(home), **(env or {})}
    if tty:
        master, slave = pty.openpty()
        try:
            return subprocess.run(
                [GIT, *args],
                cwd=cwd,
                env=full,
                input=stdin,
                stdout=slave,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
        finally:
            os.close(slave)
            os.close(master)
    return subprocess.run(
        [GIT, *args],
        cwd=cwd,
        env=full,
        input=stdin,
        capture_output=True,
        timeout=60,
        check=False,
    )


@dataclass
class Hostile:
    root: Path
    home: Path
    src: Path = field(init=False)
    clone: Path = field(init=False)
    markers: Path = field(init=False)
    payload: Path = field(init=False)
    c1: str = ""
    c2: str = ""
    c3: str = ""
    b3: str = ""

    def __post_init__(self) -> None:
        self.src = self.root / "src"
        self.clone = self.root / "clone"
        self.markers = self.root / "markers"
        self.payload = self.root / "payload.sh"

    @property
    def git_dir(self) -> Path:
        return self.clone / ".git"

    def hits(self) -> list[str]:
        return sorted(p.name for p in self.markers.iterdir())

    def payload_hits(self) -> list[str]:
        """Marker lines written by the payload itself (hooks write theirs to the same file)."""
        hit = self.markers / "hit"
        if not hit.exists():
            return []
        return [ln for ln in hit.read_text().splitlines() if ln.startswith(str(self.payload))]

    def nested_hits(self) -> list[str]:
        """Payload lines from the nested repository's own config (its commands carry ``nested``)."""
        return [ln for ln in self.payload_hits() if " nested" in ln]

    @property
    def sub(self) -> Path:
        """The nested repository of ``build_hostile(..., submodule=True)`` (a gitlink at ``s``)."""
        return self.clone / "s"

    def git(self, args: list[str], cwd: Path | None = None, **kw: object):
        r = plain_git(args, cwd=cwd or self.clone, home=self.home, **kw)  # type: ignore[arg-type]
        assert r.returncode == 0, (args, r.stderr)
        return r.stdout.decode().strip()


def _script(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def build_hostile(root: Path, home: Path, *, submodule: bool = False) -> Hostile:
    h = Hostile(root, home)
    h.markers.mkdir(parents=True)
    _script(h.payload, f'echo "$0 $*" >> {h.markers}/hit\nexit 1')

    # The remote the clone points at.
    h.src.mkdir()
    h.git(["init", "-q", "-b", "main"], cwd=h.src)
    (h.src / "r.txt").write_text("remote\n")
    h.git(["add", "r.txt"], cwd=h.src)
    h.git(["commit", "-q", "-m", "remote"], cwd=h.src)

    # The clone: history first (git is not yet hostile), config and hooks after.
    h.clone.mkdir()
    h.git(["init", "-q", "-b", "main"])
    (h.clone / ".gitattributes").write_text("*.txt filter=evil diff=evil merge=evil\n")
    (h.clone / "a.txt").write_text("one\n")
    h.git(["add", "-A"])
    h.git(["commit", "-q", "-m", "c1"])
    h.c1 = h.git(["rev-parse", "HEAD"])
    (h.clone / "a.txt").write_text("two\n")
    h.git(["commit", "-q", "-am", "c2"])
    # c2 is rewritten with a (fake) signature header so log.showSignature calls gpg.program.
    tree = h.git(["rev-parse", "HEAD^{tree}"])
    commit = (
        f"tree {tree}\nparent {h.c1}\nauthor t <t@example.invalid> 1 +0000\n"
        "committer t <t@example.invalid> 1 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n fake\n -----END PGP SIGNATURE-----\n\nc2\n"
    )
    h.c2 = h.git(["hash-object", "-t", "commit", "-w", "--stdin"], stdin=commit.encode())
    h.git(["update-ref", "refs/heads/main", h.c2])
    h.git(["reset", "-q", "--hard", "main"])
    (h.clone / "c.txt").write_text("three\n")
    h.git(["add", "c.txt"])
    h.git(["commit", "-q", "-m", "c3"])
    h.c3 = h.git(["rev-parse", "HEAD"])
    h.b3 = h.git(["rev-parse", "HEAD:c.txt"])
    h.git(["branch", "lazy", h.c3])
    h.git(["reset", "-q", "--hard", h.c2])
    if submodule:
        _add_gitlink(h)

    p = h.payload
    hooks = h.root / "hookdir"
    hooks.mkdir()
    for name in (
        "post-checkout",
        "post-commit",
        "post-merge",
        "post-index-change",
        "pre-commit",
        "pre-push",
        "pre-auto-gc",
        "reference-transaction",
        "prepare-commit-msg",
        "commit-msg",
        "fsmonitor-watchman",
    ):
        for d in (hooks, h.git_dir / "hooks"):
            d.mkdir(exist_ok=True)
            _script(d / name, f"echo {name} >> {h.markers}/hit\nexit 0")
    extra = h.root / "extra.cfg"
    extra.write_text(f"[core]\n\tfsmonitor = {p}\n[credential]\n\thelper = !{p}\n")
    src = h.src
    cfg = f"""
[user]
	name = t
	email = t@example.invalid
[core]
	repositoryformatversion = 1
	fsmonitor = {p}
	hooksPath = {hooks}
	askPass = {p}
	pager = {p}
	sshCommand = {p}
	editor = {p}
	alternateRefsCommand = {p}
	gitProxy = {p}
	attributesFile = {p}
	useReplaceRefs = true
	commitGraph = true
[extensions]
	partialClone = promo
[credential]
	helper = !{p}
[diff]
	external = {p}
[diff "evil"]
	command = {p}
	textconv = {p}
[merge "evil"]
	driver = {p} %O %A %B
[filter "evil"]
	clean = {p}
	smudge = {p}
	process = {p}
	required = true
[log]
	showSignature = true
[gpg]
	program = {p}
[pager]
	status = {p}
	log = {p}
	diff = {p}
	show = {p}
	branch = {p}
	config = {p}
[remote "origin"]
	url = {src}
	uploadpack = {p}
	receivepack = {p}
	fetch = +refs/heads/*:refs/remotes/origin/*
[remote "file://{src}"]
	uploadpack = {p}
[remote "{src}"]
	uploadpack = {p}
[remote "promo"]
	url = {src}
	promisor = true
	uploadpack = {p}
	partialclonefilter = blob:none
[url "ext::{p} %s"]
	insteadOf = evil:
[protocol]
	allow = always
[protocol "ext"]
	allow = always
[http]
	extraheader = X-Leak: repo
	proxy = http://127.0.0.1:9/
[http "https://example.invalid/"]
	extraheader = X-Leak: scoped
[http "https://*.example.invalid/"]
	ExtraHeader = X-Leak: wildcard
[submodule "s"]
	url = {src}
	update = !{p}
	active = true
[alias]
	status = !{p}
	log = !{p}
[include]
	path = {extra}
"""
    with open(h.git_dir / "config", "a") as f:
        f.write(cfg)
    # Make the lazy object really missing (the promisor config above is what git would use).
    obj = h.git_dir / "objects" / h.b3[:2] / h.b3[2:]
    obj.chmod(0o644)
    obj.unlink()
    # An alternate to make alternateRefsCommand reachable during fetch negotiation.
    info = h.git_dir / "objects" / "info"
    info.mkdir(exist_ok=True)
    (info / "alternates").write_text(str(h.src / ".git" / "objects") + "\n")
    # The hooks written above were created before the hooks dir existed in some orders.
    for m in h.markers.iterdir():  # building must not have fired anything
        m.unlink()
    return h


def _add_gitlink(h: Hostile) -> None:
    """A nested repository at ``s``, staged as a gitlink, then moved on and left dirty.

    Its own config is hostile (a filter, a textconv driver, an fsmonitor) and is only written
    here, after the staging, so building does not run it. The outer repository records ``s1``;
    the nested ``HEAD`` is ``s2`` and its file is modified, so every submodule-aware diff or
    status has something to show.
    """
    p = h.payload
    sub = h.sub
    sub.mkdir()
    h.git(["init", "-q", "-b", "main"], cwd=sub)
    (sub / ".gitattributes").write_text("*.txt filter=nest diff=nest\n")
    (sub / "f.txt").write_text("one\n")
    h.git(["add", "-A"], cwd=sub)
    h.git(["commit", "-q", "-m", "s1"], cwd=sub)
    s1 = h.git(["rev-parse", "HEAD"], cwd=sub)
    h.git(["update-index", "--add", "--cacheinfo", f"160000,{s1},s"])
    (h.clone / ".gitmodules").write_text(f'[submodule "s"]\n\tpath = s\n\turl = {h.src}\n')
    h.git(["add", ".gitmodules"])
    (sub / "f.txt").write_text("two\n")
    h.git(["commit", "-q", "-am", "s2"], cwd=sub)
    (sub / "f.txt").write_text("three\n")
    with open(sub / ".git" / "config", "a") as f:
        f.write(
            f'[filter "nest"]\n\tclean = {p} nested\n\tsmudge = {p} nested\n'
            f'[diff "nest"]\n\ttextconv = {p} nested\n[core]\n\tfsmonitor = {p} nested\n'
        )
