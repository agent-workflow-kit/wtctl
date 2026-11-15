"""The mirror: one bare, treeless, wtctl-configured repository per clone.

Rules are in docs/interface.md (Mirror, Reachability) and docs/design.md (Mirror implementation
notes). The mirror is the only repository wtctl's network git fetches into for a clone, and the
only place "is this commit pushed?" is answered: the user's clone is an agent-writable repository
whose refs, replace refs, grafts, commit-graph and object store prove nothing, so the clone is
never asked.

Everything here that touches the mirror holds the per-clone lease (callers pass the
:class:`~wtctl.state.Lease`, or use :meth:`Mirror.refresh_locked`). The mirror's config is written
byte for byte by this module (:func:`config_text`) and rewritten whenever the file differs, so a
planted ``http.proxy``, ``url.*.insteadOf``, include or filter in it cannot survive the next use.

TODO(#10): the mirror directory and its config are opened and replaced with plain no-follow opens
(``locate.open_dir_nofollow``, ``O_NOFOLLOW`` file opens); fsafe's anchored primitives (which also
refuse mount points) replace them when #10 lands.
TODO(#14): the base-commit transfer (tree fetch, ``pack-objects --stdout``, install into the clone)
is built on :meth:`Mirror.ensure` and :meth:`Mirror.refresh` and needs a stdout file option in
``gateway.run_git`` (stdout is buffered in memory).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import urls
from .config import Config
from .credentials import (
    NetworkKind,
    prepare_network_auth,
    rejected_token_error,
    run_network_git,
)
from .errors import WtctlError
from .gateway import Gateway, GitResult
from .gitsetup import private_dir
from .hardgit import Git, classify_fetch, fetch_with_retries, outcome_error
from .locate import open_dir_nofollow
from .state import Conflict, Lease, State, _prepare_dir, clone_lease, state_path

# The keys of the mirror's config, in ``git config --list`` spelling. Nothing else is ever there.
OWNED_KEYS = frozenset(
    {
        "core.repositoryformatversion",
        "core.bare",
        "core.logallrefupdates",
        "extensions.partialclone",
        "gc.auto",
        "remote.origin.url",
        "remote.origin.promisor",
        "remote.origin.partialclonefilter",
    }
)
FETCH_REFSPEC = "+refs/heads/*:refs/heads/*"
MAX_CONFIG_BYTES = 64 * 1024
# Tips that contain the commit are tried in this order; a repository whose first MAX_CANDIDATES
# containing tips all fail verification gets "unpushed" (fail-safe), never a guess.
MAX_CANDIDATES = 20

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_TIP_LINE = re.compile(r"([0-9a-f]{40}) (refs/heads/[^\n]+)\Z")
_SYMREF_LINE = re.compile(r"ref: (refs/heads/[^\t\n]+)\tHEAD\Z")
_OPEN_NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC

# Entries in the mirror that change how git reads it. Their presence makes the mirror unusable
# (the refresh fails, so there are no tips and nothing is pushed): none of them is ever created by
# wtctl or by the fetches it runs.
FORBIDDEN_ENTRIES = (
    "shallow",
    "commondir",
    "info/grafts",
    "objects/info/alternates",
    "objects/info/http-alternates",
)


# --------------------------------------------------------------------------------------------
# Location and configuration
# --------------------------------------------------------------------------------------------


def mirror_key(common_dir: str) -> str:
    """First 16 hex digits of the SHA-256 of the realpath of the clone's common git dir."""
    real = os.path.realpath(common_dir)
    return hashlib.sha256(os.fsencode(real)).hexdigest()[:16]


def mirrors_root() -> Path:
    return state_path().parent / "mirrors"


def mirror_path(common_dir: str) -> str:
    return str(mirrors_root() / f"{mirror_key(common_dir)}.git")


def mirror_url(fetch_url: str) -> str:
    """The URL written to the mirror: an https URL ends in ``.git`` (redirects are not followed,
    and some forges answer a URL without it with a redirect)."""
    if urls.url_scheme(fetch_url) == "https" and not fetch_url.endswith(".git"):
        return fetch_url.rstrip("/") + ".git"
    return fetch_url


def _quote(value: str) -> str:
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise ValueError("control character in a config value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def config_text(url: str) -> bytes:
    """The mirror's complete config file for fetch URL ``url`` (already ``mirror_url``'d)."""
    text = (
        "[core]\n"
        "\trepositoryformatversion = 1\n"
        "\tbare = true\n"
        "\tlogAllRefUpdates = false\n"
        "[extensions]\n"
        "\tpartialClone = origin\n"
        "[gc]\n"
        "\tauto = 0\n"
        '[remote "origin"]\n'
        f"\turl = {_quote(url)}\n"
        "\tpromisor = true\n"
        "\tpartialclonefilter = tree:0\n"
    )
    return text.encode("utf-8")


# --------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Tips:
    """The mirror's ``refs/heads/*`` after a successful refresh. Only :meth:`Mirror.refresh`
    makes one; without one nothing is pushed."""

    refs: Mapping[str, str]

    @property
    def shas(self) -> frozenset[str]:
        return frozenset(self.refs.values())


@dataclass(frozen=True)
class Refresh:
    tips: Tips
    default_branch: str | None
    created: bool  # this call made the mirror directory


@dataclass(frozen=True)
class Pushed:
    pushed: bool
    # None when pushed; else missing, not_a_commit, not_ancestor, no_tips, invalid_sha,
    # ancestry_unverified or check_failed
    detail: str | None = None


# --------------------------------------------------------------------------------------------
# Content-verified ancestry (pure)
# --------------------------------------------------------------------------------------------


def commit_parents(content: bytes) -> list[str] | None:
    """The ``parent`` shas of a commit object's content, or None if the header is malformed.

    Only header lines are read (up to the first blank line, continuation lines of a multi-line
    header skipped), so a message that mentions ``parent`` changes nothing.
    """
    header, sep, _ = content.partition(b"\n\n")
    if not sep and not content.endswith(b"\n"):
        return None
    parents: list[str] = []
    for line in header.split(b"\n"):
        if line.startswith(b"parent "):
            sha = line[7:].decode("ascii", "replace")
            if not _SHA.fullmatch(sha):
                return None
            parents.append(sha)
    return parents


def object_sha1(kind: bytes, content: bytes) -> str:
    return hashlib.sha1(b"%s %d\0" % (kind, len(content)) + content).hexdigest()  # noqa: S324


def parse_cat_file_batch(data: bytes) -> dict[str, tuple[str, bytes] | None]:
    """``cat-file --batch`` output: sha -> (type, content), or None for ``<sha> missing``.

    A malformed stream raises ``ValueError``. The sha is the one git echoed, so a caller compares
    it with what it asked for and with the hash of the content.
    """
    out: dict[str, tuple[str, bytes] | None] = {}
    pos = 0
    while pos < len(data):
        eol = data.find(b"\n", pos)
        if eol < 0:
            raise ValueError("truncated cat-file header")
        fields = data[pos:eol].split(b" ")
        pos = eol + 1
        if len(fields) == 2 and fields[1] == b"missing":
            out[fields[0].decode("ascii", "replace")] = None
            continue
        if len(fields) != 3 or not fields[2].isdigit():
            raise ValueError("malformed cat-file header")
        size = int(fields[2])
        content = data[pos : pos + size]
        if len(content) != size or data[pos + size : pos + size + 1] != b"\n":
            raise ValueError("truncated cat-file object")
        pos += size + 1
        out[fields[0].decode("ascii", "replace")] = (fields[1].decode("ascii", "replace"), content)
    return out


def verify_path(path: list[str], objects: Mapping[str, tuple[str, bytes] | None]) -> bool:
    """Whether ``path`` (tip first, target last) is a chain of genuine commits.

    Every commit on it must be present, a commit, and hash to its own name; each hop's ``parent``
    line must name the next commit in the path. The last commit's content is checked as well.
    """
    for i, sha in enumerate(path):
        entry = objects.get(sha)
        if entry is None or entry[0] != "commit":
            return False
        content = entry[1]
        if object_sha1(b"commit", content) != sha:
            return False
        if i + 1 < len(path):
            parents = commit_parents(content)
            if parents is None or path[i + 1] not in parents:
                return False
    return True


def find_path(edges: Mapping[str, list[str]], tip: str, target: str) -> list[str] | None:
    """Shortest parent path ``tip -> ... -> target`` in ``edges`` (child -> parents), or None."""
    if tip == target:
        return [tip]
    prev: dict[str, str] = {}
    queue = [tip]
    seen = {tip}
    for node in queue:  # appended to while iterating: a breadth-first walk
        for parent in edges.get(node, ()):
            if parent in seen:
                continue
            seen.add(parent)
            prev[parent] = node
            if parent == target:
                path = [target]
                while path[-1] != tip:
                    path.append(prev[path[-1]])
                return path[::-1]
            if parent in edges:
                queue.append(parent)
    return None


# --------------------------------------------------------------------------------------------
# The mirror
# --------------------------------------------------------------------------------------------


@dataclass
class Mirror:
    """The mirror of one clone. ``fetch_url`` is the clone fetch URL recorded by locate
    (userinfo stripped, verified on every resolution); ``clone_path`` names the per-clone lease
    and ``common_dir`` the mirror's path."""

    git: Git
    cfg: Config
    state: State
    gateway: Gateway
    repo: str
    clone_path: str
    common_dir: str
    fetch_url: str
    environ: Mapping[str, str]
    warn: Callable[[str], None] = field(default=lambda message: None)
    path: str = field(init=False)

    def __post_init__(self) -> None:
        self.path = mirror_path(self.common_dir)

    # -- helpers ------------------------------------------------------------------------------

    @property
    def lease_name(self) -> str:
        return clone_lease(self.clone_path)

    def _require_lease(self, lease: Lease) -> None:
        if lease.name != self.lease_name:
            raise ValueError("the mirror needs the per-clone lease of its own clone")

    def _fail(self, message: str) -> WtctlError:
        return WtctlError("fetch_failed", message)

    def _run(self, args: list[str], *, stdin: bytes | None = None, **kw: object) -> GitResult:
        """A hardened, protocol-less git command in the mirror (reachability queries)."""
        return self.git.run(args, git_dir=self.path, cwd=self.path, stdin=stdin, **kw)  # type: ignore[arg-type]

    # -- creation -----------------------------------------------------------------------------

    def ensure(self, lease: Lease) -> bool:
        """Create the mirror if needed and write its config; True when it was created.

        The path is recorded in ``state.db`` first. A directory that exists and was not recorded
        for this clone must be empty (any other one is never reused: ``fetch_failed``).
        """
        self._require_lease(lease)
        row = self.state.get_clone(self.repo) or {}
        recorded = row.get("mirror_path") == self.path
        created = self._prepare_directory(recorded)
        try:
            self.state.set_mirror(self.repo, mirror_path=self.path)
        except Conflict as e:
            raise self._fail(f"mirror path {self.path} is already stored by another clone") from e
        if created:
            with private_dir() as p:
                r = self.git.run(
                    ["init", "-q", "--bare", "--template=", "--", self.path],
                    private_dir=p,
                    timeout=60,
                )
            if r.returncode != 0:
                raise self._fail(f"git init of the mirror failed: {r.err.strip()[:200]}")
        self._write_config()
        return created

    def _prepare_directory(self, recorded: bool) -> bool:
        """Check the mirror path and create it (0700) if absent; True when a git init is due."""
        _prepare_dir(mirrors_root())
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            os.mkdir(self.path, 0o700)
            return True
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise self._fail(f"mirror path {self.path} is not a plain directory")
        if st.st_uid != os.geteuid():
            raise self._fail(f"mirror path {self.path} is not owned by the current user")
        entries = os.listdir(self.path)
        if not entries:
            os.chmod(self.path, 0o700)
            return True
        if not recorded:
            raise self._fail(f"mirror path {self.path} exists and is not empty")
        return False

    def _write_config(self) -> None:
        """Rewrite the config file unless it already holds exactly what wtctl writes."""
        want = config_text(mirror_url(self.fetch_url))
        try:
            fd = open_dir_nofollow(self.path)
        except OSError as e:
            raise self._fail(f"cannot open the mirror {self.path}: {e.strerror}") from e
        try:
            if self._read_config(fd) == want:
                return
            tmp = f"config.wtctl-{secrets.token_hex(6)}"
            try:
                out = os.open(
                    tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
                )
                try:
                    os.write(out, want)
                    os.fsync(out)
                finally:
                    os.close(out)
                os.rename(tmp, "config", src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            except OSError as e:
                with contextlib.suppress(OSError):
                    os.unlink(tmp, dir_fd=fd)
                raise self._fail(f"cannot write the mirror config: {e.strerror}") from e
        finally:
            os.close(fd)

    @staticmethod
    def _read_config(fd: int) -> bytes | None:
        try:
            f = os.open("config", os.O_RDONLY | _OPEN_NOFOLLOW, dir_fd=fd)
        except OSError:
            return None  # absent, or a symlink: rewritten either way
        try:
            if not stat.S_ISREG(os.fstat(f).st_mode):
                return None
            return os.read(f, MAX_CONFIG_BYTES + 1)
        finally:
            os.close(f)

    def check_layout(self) -> None:
        """Refuse a mirror holding an entry that changes how git reads it (see FORBIDDEN_ENTRIES)."""
        for sub in ("objects", "refs"):
            try:
                st = os.lstat(os.path.join(self.path, sub))
            except OSError as e:
                raise self._fail(f"the mirror has no {sub} directory") from e
            if not stat.S_ISDIR(st.st_mode):
                raise self._fail(f"the mirror's {sub} is not a plain directory")
        for name in FORBIDDEN_ENTRIES:
            if os.path.lexists(os.path.join(self.path, name)):
                raise self._fail(f"the mirror holds an unexpected {name}; it is not used")

    # -- refresh ------------------------------------------------------------------------------

    def refresh_locked(self) -> Refresh:
        """Take the per-clone lease and :meth:`refresh`."""
        with self.state.lease(self.lease_name, wait=self.cfg.lease_wait_s) as lease:
            return self.refresh(lease)

    def refresh(self, lease: Lease) -> Refresh:
        """Fetch the remote's branches (commits only), learn its default branch, return the tips.

        Raises ``fetch_failed`` (``details.cause`` ``timeout`` or ``prune_incomplete`` where it
        applies), ``auth_required`` or ``remote_mismatch`` (an unusable URL).
        """
        self._require_lease(lease)
        created = self.ensure(lease)
        self.check_layout()
        url = mirror_url(self.fetch_url)
        auth = prepare_network_auth(
            self.cfg, url, environ=self.environ, gateway=self.gateway, warn=self.warn
        )
        first = not self._read_tips().refs
        with private_dir() as p:

            def network(args: list[str], kind: NetworkKind) -> GitResult:
                return run_network_git(
                    self.git, args, cfg=self.cfg, auth=auth, kind=kind, tool_owned=True,
                    owned_keys=OWNED_KEYS, git_dir=self.path, cwd=p,
                )  # fmt: skip

            fetch_kind = NetworkKind.CLONE if first else NetworkKind.NETWORK
            result, outcome = fetch_with_retries(
                lambda: network(
                    ["fetch", "--no-tags", "--prune", "--filter=tree:0", "--", "origin",
                     FETCH_REFSPEC],
                    fetch_kind,
                ),
                retries=self.cfg.remove.fetch_retries,
            )  # fmt: skip
            err = rejected_token_error(auth, outcome_error(outcome, result))
            if err is not None:
                raise err
            listing = network(["ls-remote", "--symref", "--", url, "HEAD"], NetworkKind.NETWORK)
            err = rejected_token_error(
                auth, outcome_error(classify_fetch(listing), listing)
            )
            if err is not None:
                raise err
        tips = self._read_tips()
        branch = self._default_branch(listing.stdout, tips)
        if branch is not None:
            r = self._run(["symbolic-ref", "HEAD", f"refs/heads/{branch}"])
            if r.returncode != 0:
                raise self._fail(f"cannot point the mirror's HEAD at {branch}")
        self.state.set_mirror(self.repo, default_branch=branch)
        return Refresh(tips, branch, created)

    def _read_tips(self) -> Tips:
        r = self._run(["for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/"])
        if r.returncode != 0 or r.timed_out:
            raise self._fail("cannot list the mirror's branches")
        refs: dict[str, str] = {}
        for line in r.out.split("\n"):
            if not line:
                continue
            m = _TIP_LINE.fullmatch(line)
            if m is None:
                raise self._fail("unexpected branch listing in the mirror")
            refs[m.group(2)] = m.group(1)
        return Tips(refs)

    def _default_branch(self, listing: bytes, tips: Tips) -> str | None:
        """The branch the remote's HEAD points at, from ``ls-remote --symref`` output.

        The output is the server's text: only a ``ref: refs/heads/<name>\\tHEAD`` line whose name is
        valid UTF-8, a valid ref name, and a branch the mirror now holds is believed. Anything else
        (an empty or detached remote included) gives no default branch.
        """
        for raw in listing.split(b"\n"):
            if not raw.startswith(b"ref: "):
                continue
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                self.warn("the remote's default branch is not valid UTF-8; ignoring it")
                return None
            m = _SYMREF_LINE.fullmatch(line)
            if m is None:
                continue  # e.g. a symref of another name
            ref = m.group(1)
            if len(ref) > 255 or ref not in tips.refs:
                self.warn("the remote's default branch is not a branch it advertises; ignoring it")
                return None
            check = self._run(["check-ref-format", ref])
            if check.returncode != 0:
                self.warn("the remote's default branch is not a valid ref name; ignoring it")
                return None
            return ref.removeprefix("refs/heads/")
        return None

    # -- the pushed check ---------------------------------------------------------------------

    def is_pushed(self, commit: str, tips: Tips | None) -> Pushed:
        """Whether ``commit`` is reachable from a tip of the remote, with content-verified ancestry.

        ``tips`` come from :meth:`refresh` in the same command; without them nothing is pushed.
        Runs in the mirror with protocols ``none`` and ``GIT_NO_LAZY_FETCH=1`` (the hardened
        invocation's defaults for a command without a scheme). Any failure to establish the answer
        is "unpushed".
        """
        if tips is None or not tips.refs:
            return Pushed(False, "no_tips")
        if not _SHA.fullmatch(commit):
            return Pushed(False, "invalid_sha")
        head = self._run(["cat-file", "--batch-check"], stdin=f"{commit}\n".encode())
        if head.returncode != 0 or head.timed_out:
            return Pushed(False, "check_failed")
        fields = head.out.split()
        if len(fields) < 2 or fields[0] != commit or fields[1] == "missing":
            return Pushed(False, "missing")
        if fields[1] != "commit":
            return Pushed(False, "not_a_commit")
        containing = self._run(
            ["for-each-ref", "--contains", commit, "--format=%(objectname)", "refs/heads/"]
        )
        if containing.returncode != 0 or containing.timed_out:
            return Pushed(False, "check_failed")
        candidates = sorted(set(containing.out.split()) & tips.shas)[:MAX_CANDIDATES]
        verdict = "not_ancestor"
        for tip in candidates:
            anc = self._run(["merge-base", "--is-ancestor", commit, tip])
            if anc.returncode != 0 or anc.timed_out:
                continue  # 1: not an ancestor; 128: an object is missing. Both: not through here
            if self._verified(commit, tip):
                return Pushed(True)
            verdict = "ancestry_unverified"
        return Pushed(False, verdict)

    def _verified(self, commit: str, tip: str) -> bool:
        """Find a parent path tip -> commit and re-hash every commit on it."""
        if tip == commit:
            path = [tip]
        else:
            r = self._run(["rev-list", "--parents", "--ancestry-path", f"{commit}..{tip}"])
            if r.returncode != 0 or r.timed_out:
                return False
            edges: dict[str, list[str]] = {}
            for line in r.out.split("\n"):
                if not line:
                    continue
                shas = line.split(" ")
                if not all(_SHA.fullmatch(s) for s in shas):
                    return False
                edges[shas[0]] = shas[1:]
            found = find_path(edges, tip, commit)
            if found is None:
                return False
            path = found
        got = self._run(["cat-file", "--batch"], stdin=("\n".join(path) + "\n").encode())
        if got.returncode != 0 or got.timed_out:
            return False
        try:
            objects = parse_cat_file_batch(got.stdout)
        except ValueError:
            return False
        return verify_path(path, objects)

