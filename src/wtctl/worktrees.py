"""Git-level steps of ``create`` for worktrees (docs/interface.md, Create order).

Everything runs through the hardened :class:`~wtctl.hardgit.Git`; nothing here contacts a remote.
Names that reach git as arguments are checked first and are never option-like: a branch is
validated by ``check-ref-format --branch`` and never starts with ``-``, a start commit is a 40-hex
id, and a path is absolute. Every call also puts ``--`` before its positional arguments.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .errors import UsageError, WtctlError
from .gitsetup import private_dir
from .hardgit import Git, PorcelainError, WorktreeEntry, parse_config_list, parse_worktree_list

SHA = re.compile(r"[0-9a-f]{40}\Z")
_HEAD_LINE = re.compile(r"([0-9a-f]{40}) (refs/heads/[^\n]+)\Z")
ADMIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

# Config keys of an honored filter driver (git-lfs) that make it fetch from, or run, something
# the repository chose (docs/interface.md, Hardened git > Driver config check).
UNSAFE_LFS_KEYS = re.compile(
    r"lfs\.customtransfer\..+|lfs\.standalonetransferagent|lfs\..+\.standalonetransferagent"
    r"|lfs\.extension\..+|lfs\.url|lfs\..+\.access|remote\..+\.lfsurl",
    re.IGNORECASE,
)


def create_failed(message: str, **details: object) -> WtctlError:
    return (
        WtctlError("create_failed", message, details=details)
        if details
        else WtctlError("create_failed", message)
    )


# -- names ------------------------------------------------------------------------------------


def check_ref_name(git: Git, name: str, *, what: str) -> None:
    """``name`` is a valid branch name by git's own rules, spelled exactly as given (a usage error
    otherwise). ``--branch`` expands ``@{-1}`` and the like, so the expansion must equal the
    input; the name also never starts with ``-`` and is never ``HEAD`` or ``@``."""
    problem = None
    if not name:
        problem = "is empty"
    elif name.startswith("-"):
        problem = "starts with '-'"
    elif name in ("HEAD", "@"):
        problem = "is reserved"
    elif any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        problem = "contains a control character"
    if problem is None:
        with private_dir() as p:
            r = git.run(
                ["check-ref-format", "--branch", name],
                private_dir=p,
                timeout=30,
            )
        if r.returncode != 0 or r.timed_out:
            problem = "is not a valid branch name"
        elif r.stdout.rstrip(b"\n") != name.encode("utf-8", "surrogateescape"):
            problem = "is not spelled in its canonical form"
    if problem is not None:
        raise UsageError(f"{what} {name!r} {problem}")


# -- reading the clone ------------------------------------------------------------------------


def local_heads(git: Git, common_dir: str) -> dict[str, str]:
    """The clone's ``refs/heads/*`` as ``{full ref name: sha}``."""
    r = git.run(
        ["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/"],
        git_dir=common_dir,
        cwd=common_dir,
        timeout=60,
    )
    if r.returncode != 0 or r.timed_out:
        raise create_failed(f"cannot list the branches of {common_dir}")
    heads: dict[str, str] = {}
    for line in r.stdout.decode("utf-8", "surrogateescape").split("\n"):
        if not line:
            continue
        m = _HEAD_LINE.fullmatch(line)
        if m is None:
            raise create_failed("unexpected branch listing in the clone")
        heads[m.group(2)] = m.group(1)
    return heads


def worktree_entries(git: Git, common_dir: str) -> list[WorktreeEntry]:
    """Every worktree of the clone (the main one first), cross-checked against the number of
    admin entries (a path with a newline can forge entries)."""
    admin = os.path.join(common_dir, "worktrees")
    try:
        with os.scandir(admin) as it:
            n = sum(1 for e in it if e.is_dir(follow_symlinks=False))
    except FileNotFoundError:
        n = 0
    r = git.run(["worktree", "list", "--porcelain"], git_dir=common_dir, cwd=common_dir, timeout=60)
    if r.returncode != 0 or r.timed_out:
        raise create_failed("cannot list the worktrees of the clone")
    try:
        entries = parse_worktree_list(r.stdout, expected=None)
    except PorcelainError as exc:
        raise WtctlError("internal", f"unexpected worktree listing: {exc}") from exc
    bare = bool(entries) and entries[0].bare
    if len(entries) != n + (0 if bare else 1):
        raise WtctlError(
            "internal",
            f"git worktree list returned {len(entries)} entries for {n} admin entries"
            " (a path with a newline can forge entries)",
        )
    return entries


# -- the driver config check ------------------------------------------------------------------


def unsafe_driver_keys(git: Git, common_dir: str, start: str | None) -> list[str]:
    """Config keys (repository config, and ``.lfsconfig`` of the checkout commit when the clone
    has it) that an honored filter driver must not see."""
    keys: list[str] = []
    r = git.run(["config", "--list", "-z"], git_dir=common_dir, cwd=common_dir, timeout=60)
    if r.returncode != 0 or r.timed_out:
        raise WtctlError("internal", "cannot read the clone's config")
    keys += [k for k, _ in parse_config_list(r.stdout) if UNSAFE_LFS_KEYS.fullmatch(k)]
    if start is not None:
        spec = f"{start}:.lfsconfig"
        present = git.run(["cat-file", "-e", spec], git_dir=common_dir, cwd=common_dir, timeout=60)
        if present.returncode == 0:
            c = git.run(
                ["config", "--no-includes", f"--blob={spec}", "--list", "-z"],
                git_dir=common_dir,
                cwd=common_dir,
                timeout=60,
            )
            if c.returncode != 0 or c.timed_out:
                raise WtctlError("internal", "cannot read .lfsconfig of the checkout commit")
            keys += [k for k, _ in parse_config_list(c.stdout) if UNSAFE_LFS_KEYS.fullmatch(k)]
    return sorted(set(keys))


# -- writing ----------------------------------------------------------------------------------


def write_upstream(git: Git, common_dir: str, branch: str, remote: str) -> None:
    """Explicit upstream for a branch created from a remote-only one (git's own remote-branch
    guessing is never used)."""
    for key, value in (("remote", remote), ("merge", f"refs/heads/{branch}")):
        r = git.run(
            ["config", "--local", f"branch.{branch}.{key}", value],
            git_dir=common_dir,
            cwd=common_dir,
            timeout=60,
        )
        if r.returncode != 0 or r.timed_out:
            raise create_failed(f"cannot write the upstream of {branch}")


def remove_upstream(git: Git, common_dir: str, branch: str) -> None:
    """Best effort: drop the ``branch.<b>`` section this create wrote."""
    git.run(
        ["config", "--local", "--remove-section", f"branch.{branch}"],
        git_dir=common_dir,
        cwd=common_dir,
        timeout=60,
    )


def delete_created_branch(git: Git, common_dir: str, branch: str, sha: str) -> bool:
    """Delete ``refs/heads/<branch>`` only if it still points at ``sha`` (compare-and-swap)."""
    r = git.run(
        ["update-ref", "-d", f"refs/heads/{branch}", sha],
        git_dir=common_dir,
        cwd=common_dir,
        timeout=60,
    )
    return r.returncode == 0 and not r.timed_out


@dataclass(frozen=True)
class AddSpec:
    path: str
    branch: str | None
    start: str | None
    new_branch: bool  # `-b`: the branch is created by `worktree add`


def worktree_add(git: Git, common_dir: str, spec: AddSpec) -> None:
    """``git worktree add`` (protocols ``none``, filters honored per ``create.allow_filters``)."""
    if spec.new_branch:
        assert spec.branch is not None and spec.start is not None
        args = ["worktree", "add", "--no-track", "-b", spec.branch, "--", spec.path, spec.start]
    elif spec.branch is not None:
        args = ["worktree", "add", "--", spec.path, spec.branch]
    else:
        assert spec.start is not None
        args = ["worktree", "add", "--detach", "--", spec.path, spec.start]
    r = git.run(args, git_dir=common_dir, cwd=common_dir, honor_filters=True, timeout=600)
    if r.returncode != 0 or r.timed_out:
        why = "timed out" if r.timed_out else r.err.strip().splitlines()[-1:] or ["failed"]
        raise create_failed(f"git worktree add failed: {why if isinstance(why, str) else why[0]}")


@dataclass(frozen=True)
class Added:
    admin_dir: str
    head: str
    reflog_start_sha: str | None
    stash_ids: list[str]


def inspect_added(git: Git, common_dir: str, path: str) -> Added:
    """Read what the new worktree looks like: its admin entry, HEAD, reflog start and stashes."""
    r = git.run(
        ["rev-parse", "--absolute-git-dir"],
        git_dir=os.path.join(path, ".git"),
        cwd=path,
        timeout=60,
    )
    admin = r.out.strip()
    common_real = os.path.realpath(common_dir)
    if (
        r.returncode != 0
        or os.path.dirname(os.path.realpath(admin)) != os.path.join(common_real, "worktrees")
        or not ADMIN_NAME.fullmatch(os.path.basename(admin))
    ):
        raise create_failed("the new worktree does not point at an admin entry of the clone")
    admin = os.path.join(common_real, "worktrees", os.path.basename(admin))
    h = git.run(["rev-parse", "--verify", "HEAD"], git_dir=admin, cwd=admin, timeout=60)
    head = h.out.strip()
    if h.returncode != 0 or not SHA.fullmatch(head):
        raise create_failed("cannot read HEAD of the new worktree")
    return Added(admin, head, _reflog_last(admin), _stash_ids(git, common_dir))


def _reflog_last(admin: str) -> str | None:
    try:
        fd = os.open(os.path.join(admin, "logs", "HEAD"), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        lines = fh.read(1 << 20).splitlines()
    if not lines:
        return None
    fields = lines[-1].split(b" ", 2)
    new = fields[1].decode("ascii", "replace") if len(fields) > 1 else ""
    return new if SHA.fullmatch(new) else None


def _stash_ids(git: Git, common_dir: str) -> list[str]:
    has = git.run(
        ["rev-parse", "--verify", "-q", "refs/stash"],
        git_dir=common_dir,
        cwd=common_dir,
        timeout=60,
    )
    if has.returncode != 0:
        return []
    r = git.run(
        ["log", "-g", "--format=%H", "refs/stash", "--"],
        git_dir=common_dir,
        cwd=common_dir,
        timeout=60,
    )
    if r.returncode != 0 or r.timed_out:
        raise create_failed("cannot read the clone's stash entries")
    return [ln for ln in r.out.split() if SHA.fullmatch(ln)]
