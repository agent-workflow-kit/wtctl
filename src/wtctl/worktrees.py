"""Git-level steps of ``create`` for worktrees (docs/interface.md, Create order).

Everything runs through the hardened :class:`~wtctl.hardgit.Git`; nothing here contacts a remote.
Names that reach git as arguments are checked first and are never option-like: a branch is
validated by ``check-ref-format --branch`` and never starts with ``-``, a start commit is a 40-hex
id, and a path is absolute. Every call also puts ``--`` before its positional arguments.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple

from . import sparse, urls
from .errors import UsageError, WtctlError
from .gitsetup import private_dir
from .hardgit import (
    Git,
    PorcelainError,
    WorktreeEntry,
    parse_config_list,
    parse_ls_files_tagged,
    parse_worktree_list,
)

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


_URL_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")


def scrub_urls(text: str) -> str:
    """``text`` with the userinfo (user and password) removed from every URL in it: git echoes
    the URLs it was given, and a clone's remote may carry credentials."""

    def one(m: re.Match[str]) -> str:
        out = urls.strip_userinfo(m.group(0))
        scheme, sep, rest = out.partition("://")
        authority, slash, path = rest.partition("/")
        return f"{scheme}{sep}{authority.rpartition('@')[2]}{slash}{path}"

    return _URL_TOKEN.sub(one, text)


def _admin_pairs(common_dir: str) -> list[tuple[str, str]]:
    """``(admin directory, gitdir file content)`` of every admin entry that has a regular
    ``gitdir`` file."""
    admin = os.path.join(common_dir, "worktrees")
    found: list[tuple[str, str]] = []
    try:
        it = os.scandir(admin)
    except FileNotFoundError:
        return found
    with it:
        for e in it:
            if not e.is_dir(follow_symlinks=False):
                continue
            try:
                fd = os.open(os.path.join(e.path, "gitdir"), os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                continue
            with os.fdopen(fd, "rb") as fh:
                if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                    continue
                text = fh.read(1 << 16).decode("utf-8", "surrogateescape").rstrip("\n")
                found.append((e.path, text))
    return found


def admin_gitdirs(common_dir: str) -> list[str]:
    """The ``gitdir`` file content (the worktree's ``.git`` path) of every admin entry that has
    one; an admin directory without a regular ``gitdir`` file is not an entry."""
    return [g for _, g in _admin_pairs(common_dir)]


def admin_for_path(common_dir: str, path: str) -> str | None:
    """The one admin entry whose ``gitdir`` names ``<path>/.git``, read from the common dir only
    (never from the worktree directory, which a mount may cover). ``None`` for none or for more
    than one (an ambiguity is never guessed at)."""
    mine = os.path.join(path, ".git")
    hits = [
        a
        for a, g in _admin_pairs(common_dir)
        if g == mine and ADMIN_NAME.fullmatch(os.path.basename(a))
    ]
    return hits[0] if len(hits) == 1 else None


def worktree_entries(git: Git, common_dir: str) -> list[WorktreeEntry]:
    """Every worktree of the clone (the main one first), cross-checked against the admin entries
    (a path with a newline can forge entries): their number, and their paths when every
    ``gitdir`` file holds an absolute path."""
    gitdirs = admin_gitdirs(common_dir)
    n = len(gitdirs)
    r = git.run(["worktree", "list", "--porcelain"], git_dir=common_dir, cwd=common_dir, timeout=60)
    if r.returncode != 0 or r.timed_out:
        raise create_failed("cannot list the worktrees of the clone")
    try:
        entries = parse_worktree_list(r.stdout, expected=None)
    except PorcelainError as exc:
        raise WtctlError("internal", f"unexpected worktree listing: {exc}") from exc
    bare = bool(entries) and entries[0].bare
    forged = WtctlError(
        "internal",
        f"git worktree list returned {len(entries)} entries for {n} admin entries"
        " (a path with a newline can forge entries)",
    )
    if len(entries) != n + (0 if bare else 1):
        raise forged
    if all(os.path.isabs(g) and g.endswith("/.git") for g in gitdirs):
        if sorted(g[: -len("/.git")] for g in gitdirs) != sorted(e.path for e in entries[1:]):
            raise forged
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
        ls = git.run(
            ["ls-tree", "-z", start, "--", ".lfsconfig"], git_dir=common_dir, cwd=common_dir,
            timeout=60,
        )  # fmt: skip
        if ls.returncode != 0 or ls.timed_out:
            raise create_failed("cannot read .lfsconfig of the checkout commit")
        if ls.stdout:
            mode = ls.stdout.split(b" ", 1)[0]
            if mode not in (b"100644", b"100755"):
                raise WtctlError(
                    "unsafe_repo_config",
                    ".lfsconfig of the checkout commit is not a regular file",
                    details={"keys": []},
                )
            c = git.run(
                ["config", "--no-includes", f"--blob={spec}", "--list", "-z"],
                git_dir=common_dir,
                cwd=common_dir,
                timeout=60,
            )
            if c.returncode != 0 or c.timed_out:
                raise create_failed("cannot read .lfsconfig of the checkout commit")
            keys += [k for k, _ in parse_config_list(c.stdout) if UNSAFE_LFS_KEYS.fullmatch(k)]
    return sorted(set(keys))


# -- writing ----------------------------------------------------------------------------------


def _local_config(git: Git, common_dir: str) -> dict[str, list[str]]:
    r = git.run(
        ["config", "--local", "--list", "-z"], git_dir=common_dir, cwd=common_dir, timeout=60
    )
    if r.returncode != 0 or r.timed_out:
        raise create_failed("cannot read the clone's config")
    out: dict[str, list[str]] = {}
    for key, value in parse_config_list(r.stdout):
        out.setdefault(key, []).append(value or "")
    return out


def create_branch_ref(
    git: Git, common_dir: str, branch: str, start: str, on_created: Callable[[], None]
) -> None:
    """Create ``refs/heads/<branch>`` at ``start`` only if it does not exist (``update-ref`` with
    the all-zero old value), so a branch this call owns was never anybody else's. A branch that
    exists (also one a case-insensitive file system folds onto) is ``branch_exists``.

    ``on_created`` is called as soon as the ref is known to be ours: on success, and when the
    command timed out or was interrupted but the ref now holds ``start`` (it was create-only, so
    the ref is ours). An "already exists" outcome never calls it."""
    ref = f"refs/heads/{branch}"

    def tip_of() -> str | None:
        have = git.run(
            ["rev-parse", "--verify", "-q", ref], git_dir=common_dir, cwd=common_dir, timeout=60
        )
        tip = have.out.strip()
        return tip if have.returncode == 0 and SHA.fullmatch(tip) else None

    try:
        r = git.run(
            ["update-ref", ref, start, "0" * 40], git_dir=common_dir, cwd=common_dir, timeout=60
        )
    except BaseException:
        try:
            if tip_of() == start:
                on_created()
        except Exception:  # noqa: BLE001 - the interrupt, not this probe, is what propagates
            pass
        raise
    if r.returncode == 0 and not r.timed_out:
        on_created()
        return
    tip = tip_of()
    if r.timed_out:
        if tip == start:
            on_created()
    elif tip is not None:
        raise WtctlError(
            "refused",
            f"branch {branch} already exists",
            reason="branch_exists",
            details={"tip": tip},
        )
    raise create_failed(f"cannot create the branch {branch}")


class UpstreamKey(NamedTuple):
    """One upstream key this call wrote: the value written and the values it had before."""

    value: str
    previous: list[str]


def write_upstream(
    git: Git, common_dir: str, branch: str, remote: str, written: dict[str, UpstreamKey]
) -> None:
    """Explicit upstream for a branch created from a remote-only one (git's own remote-branch
    guessing is never used). ``written`` receives each key this call wrote, with the values it
    had before (empty when it did not exist), so the unwind can put them back."""
    current = _local_config(git, common_dir)
    for var, value in (("remote", remote), ("merge", f"refs/heads/{branch}")):
        key = f"branch.{branch}.{var}"
        written[key] = UpstreamKey(value, list(current.get(key, [])))
        r = git.run(
            ["config", "--local", "--replace-all", key, value],
            git_dir=common_dir,
            cwd=common_dir,
            timeout=60,
        )
        if r.returncode != 0 or r.timed_out:
            raise create_failed(f"cannot write the upstream of {branch}")


def remove_upstream(
    git: Git, common_dir: str, branch: str, written: dict[str, UpstreamKey]
) -> list[str]:
    """Undo :func:`write_upstream`: for each key in ``written`` whose value is still the one
    written, restore the values it had before (unset it when there were none), then drop the
    ``branch.<b>`` section if nothing is left in it. Returns what could not be undone (empty when
    all went well)."""
    problems: list[str] = []
    try:
        current = _local_config(git, common_dir)
    except WtctlError as exc:
        return [f"upstream of {branch}: {exc.message}"]
    unset = False
    for key, (value, previous) in written.items():
        if current.get(key) != [value]:
            continue
        if previous:
            for i, old in enumerate(previous):
                flag = "--replace-all" if i == 0 else "--add"
                r = git.run(
                    ["config", "--local", flag, key, old],
                    git_dir=common_dir,
                    cwd=common_dir,
                    timeout=60,
                )
                if r.returncode != 0 or r.timed_out:
                    problems.append(f"could not restore {key}")
                    break
            continue
        r = git.run(
            ["config", "--local", "--unset-all", key],
            git_dir=common_dir,
            cwd=common_dir,
            timeout=60,
        )
        if r.returncode != 0 or r.timed_out:
            problems.append(f"could not unset {key}")
        else:
            unset = True
    if unset and not problems:
        try:
            left = _local_config(git, common_dir)
        except WtctlError as exc:
            return [f"upstream of {branch}: {exc.message}"]
        if not any(k.startswith(f"branch.{branch}.") for k in left):
            r = git.run(
                ["config", "--local", "--remove-section", f"branch.{branch}"],
                git_dir=common_dir,
                cwd=common_dir,
                timeout=60,
            )
            if r.returncode != 0 or r.timed_out:
                problems.append(f"could not remove the empty section branch.{branch}")
    return problems


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
    branch: str | None  # an existing branch (this call created it, for a new one); None: detached
    start: str | None  # the commit of a detached worktree
    no_checkout: bool = False  # a sparse worktree is populated afterwards by `sparse_checkout`


# `worktree add` may check out a large tree: the longest git call create makes.
ADD_TIMEOUT = 600


def worktree_add(git: Git, common_dir: str, spec: AddSpec) -> None:
    """``git worktree add`` (protocols ``none``, filters honored per ``create.allow_filters``).
    A branch is always the existing-branch form: it was created before, create-only."""
    flags = ["--no-checkout"] if spec.no_checkout else []
    if spec.branch is not None:
        args = ["worktree", "add", *flags, "--", spec.path, spec.branch]
    else:
        assert spec.start is not None
        args = ["worktree", "add", *flags, "--detach", "--", spec.path, spec.start]
    r = git.run(args, git_dir=common_dir, cwd=common_dir, honor_filters=True, timeout=ADD_TIMEOUT)
    if r.returncode != 0 or r.timed_out:
        last = r.err.strip().splitlines()[-1:] or ["failed"]
        why = "timed out" if r.timed_out else scrub_urls(last[0])
        raise create_failed(f"git worktree add failed: {why}")


SPARSE_ON = (("core.sparseCheckout", "true"), ("core.sparseCheckoutCone", "true"))


def sparse_checkout(git: Git, admin_dir: str, path: str, dirs: list[str]) -> None:
    """Populate a worktree added with ``--no-checkout`` as a cone checkout of ``dirs``.

    wtctl writes the pattern file itself (``sparse.pattern_file``, validated directories only,
    never an argv or a shell) into the new admin entry and runs one ``read-tree -mu HEAD`` with
    the sparse settings given for that command alone: nothing is written to any git config, so
    no other worktree of the clone and no other record's baseline is affected.
    """
    info = os.path.join(admin_dir, "info")
    try:
        os.mkdir(info, 0o755)
        fd = os.open(
            os.path.join(info, "sparse-checkout"),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
        )
    except OSError as exc:
        raise create_failed(f"cannot write info/sparse-checkout: {exc.strerror}") from exc
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(sparse.pattern_file(dirs))
    except OSError as exc:
        raise create_failed(f"cannot write info/sparse-checkout: {exc.strerror}") from exc
    r = git.run(
        ["read-tree", "-m", "-u", "HEAD"],
        git_dir=admin_dir,
        work_tree=path,
        honor_filters=True,
        overrides=SPARSE_ON,
        timeout=ADD_TIMEOUT,
    )
    if r.returncode != 0 or r.timed_out:
        last = r.err.strip().splitlines()[-1:] or ["failed"]
        why = "timed out" if r.timed_out else scrub_urls(last[0])
        raise create_failed(f"sparse checkout failed: {why}")


def cone_agrees(git: Git, admin_dir: str, path: str, dirs: list[str]) -> bool | None:
    """Whether the paths git left out (``S`` in ``ls-files -v``) are exactly the tracked paths
    wtctl's own cone excludes; None when that cannot be computed (callers treat it as a
    disagreement)."""
    r = git.run(
        ["ls-files", "-v", "-z"],
        git_dir=admin_dir,
        work_tree=path,
        honor_filters=True,
        timeout=ADD_TIMEOUT,
    )
    if r.returncode != 0 or r.timed_out:
        return None
    try:
        tagged = parse_ls_files_tagged(r.stdout)
    except PorcelainError:
        return None
    cone = sparse.Cone(dirs)
    left_out = {p for tag, p in tagged if tag == "S"}
    want = {p for _tag, p in tagged if not cone.includes(p)}
    return left_out == want


def verify_cone(git: Git, admin_dir: str, path: str, dirs: list[str]) -> None:
    """The paths git left out (``S`` in ``ls-files -v``) must be exactly the tracked paths wtctl's
    own cone excludes. They differ when git matches names more loosely than wtctl (a clone with
    ``core.ignorecase`` or ``core.precomposeunicode``): later checks trust the cone, so a
    checkout that disagrees with it is refused here rather than hide a deleted file."""
    agrees = cone_agrees(git, admin_dir, path, dirs)
    if agrees is None:
        raise create_failed("sparse checkout failed: cannot list the index")
    if not agrees:
        raise create_failed(
            "sparse checkout does not match the requested directories "
            "(case or unicode folding in the clone's git configuration?)"
        )


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


def reflog_first(admin: str) -> str | None:
    """The commit the admin entry's HEAD reflog starts at: where HEAD stood when git created the
    worktree. ``None`` when there is no usable entry or the first line is not the creation entry
    (its old value is not all zeros: the reflog expired or was edited)."""
    return _reflog_last(admin, first=True)


def _reflog_last(admin: str, *, first: bool = False) -> str | None:
    try:
        fd = os.open(os.path.join(admin, "logs", "HEAD"), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        lines = fh.read(1 << 20).splitlines()
    if not lines:
        return None
    fields = lines[0 if first else -1].split(b" ", 2)
    new = fields[1].decode("ascii", "replace") if len(fields) > 1 else ""
    if first and fields[0] != b"0" * 40:
        return None
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


@dataclass(frozen=True)
class Existing:
    branch: str | None  # the checked-out local branch (None when detached)
    head: str
    reflog_start_sha: str | None
    stash_ids: list[str]


def inspect_existing(git: Git, common_dir: str, admin_dir: str | None) -> Existing:
    """Read a worktree wtctl did not create (``adopt``): HEAD, the branch, the HEAD reflog's last
    entry and the clone's stash entries. ``admin_dir`` is None for the main worktree, whose git
    directory is the common dir. An unborn HEAD is refused (``create_failed``)."""
    gdir = admin_dir or common_dir
    h = git.run(["rev-parse", "--verify", "HEAD"], git_dir=gdir, cwd=gdir, timeout=60)
    head = h.out.strip()
    if h.returncode != 0 or not SHA.fullmatch(head):
        raise create_failed("cannot read HEAD (an unborn branch cannot be adopted)")
    s = git.run(["symbolic-ref", "-q", "HEAD"], git_dir=gdir, cwd=gdir, timeout=60)
    ref = s.out.strip() if s.returncode == 0 else ""
    branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else None
    return Existing(branch or None, head, _reflog_last(gdir), _stash_ids(git, common_dir))
