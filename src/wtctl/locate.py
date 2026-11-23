"""Clone resolution: config, recorded clone, discovery, auto-clone, and the explicit clone.

The rules are in docs/interface.md (Clone resolution). The clone being examined is untrusted: an
agent may have written its config, hooks and directory layout. So every git call goes through the
hardened runner with ``GIT_ALLOW_PROTOCOL=none`` and an explicit ``--git-dir`` (no repository
discovery), a clone's ``.git`` must be a real directory (never a symlink), and the identity of the
common dir is read through a no-follow chain and compared with the one recorded for the repo.

TODO(#10): the no-follow chain (:func:`open_dir_nofollow`), the discovery walk and the removal of a
failed clone's temporary directory use plain ``os.open`` with ``O_NOFOLLOW`` here; replace them
with fsafe's anchored walks (which also refuse mount points) when #10 lands.
Network git (the clone) takes its credentials from ``credentials.prepare_network_auth``.
``default_branch`` is the value stored in the repo's ``clones`` row, which a mirror refresh
(``mirror.Mirror.refresh``) writes from the remote's HEAD; it is null until the first refresh. The
mirror's config follows a changed fetch URL at its next use (``Mirror.ensure``), not here.
The human ``clone`` re-accept (and its ``clone_accept`` audit record) is in ``commands/clone.py``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import secrets
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from . import urls
from .credentials import (
    NetworkAuth,
    NetworkKind,
    prepare_network_auth,
    rejected_token_error,
    run_network_git,
)
from .errors import WtctlError
from .gitsetup import private_dir
from .hardgit import Git, classify_fetch, outcome_error, parse_config_list
from .state import State, repo_lease

MAX_DISCOVERY_DIRS = 200_000
MAX_DISCOVERY_DEPTH = 64
DISCOVERY_SECONDS = 60.0
TRUNCATED_NOTE = "discovery stopped early (too many directories or too slow)"
NOT_FOUND_HINT = "set repos.<id>.clone, add a directory to search_roots, or run `wtctl clone <id>`"

_OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


# --------------------------------------------------------------------------------------------
# Identities and mounts
# --------------------------------------------------------------------------------------------


def identity_str(st: os.stat_result) -> str:
    """``<st_dev>:<st_ino>``, the recorded form of an anchor identity (schemas/defs.json)."""
    return f"{st.st_dev}:{st.st_ino}"


def open_dir_nofollow(path: str) -> int:
    """Open a directory through a chain of no-follow opens from ``/`` (the caller closes it).

    ``path`` must be absolute and without ``.``/``..`` components. A symlink anywhere in the
    chain is an error (``OSError``, ``ELOOP`` or ``ENOTDIR``).
    TODO(#10): fsafe.open_anchor (also refuses mount points at or below the anchor).
    """
    parts = [p for p in path.split("/") if p]
    if not path.startswith("/") or any(p in (".", "..") for p in parts):
        raise ValueError(f"not a normalized absolute path: {path!r}")
    fd = os.open("/", _OPEN_DIR)
    try:
        for part in parts:
            nxt = os.open(part, _OPEN_DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _unescape_mount(text: str) -> str:
    out = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 4 <= len(text) and text[i + 1 : i + 4].isdigit():
            out.append(chr(int(text[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def describe_mount(path: str, st: os.stat_result) -> dict[str, str]:
    """File-system type, mount source and mount point of the mount holding ``path`` (untrusted).

    Linux: ``/proc/self/mountinfo`` entries with the directory's device number, the longest
    mount point that is a prefix of the path. Unknown values are reported as ``unknown``.
    TODO(#10): fsafe.describe_mount (macOS ``statfs``, mount id of the opened fd).
    """
    best: tuple[int, dict[str, str]] | None = None
    dev = f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        head, sep, tail = line.partition(" - ")
        fields = head.split(" ")
        rest = tail.split(" ")
        if not sep or len(fields) < 5 or len(rest) < 2 or fields[2] != dev:
            continue
        point = _unescape_mount(fields[4])
        if path != point and not path.startswith(point.rstrip("/") + "/"):
            continue
        if best is None or len(point) > best[0]:
            best = (
                len(point),
                {
                    "fs_type": rest[0],
                    "mount_source": _unescape_mount(rest[1]),
                    "mount_point": point,
                },
            )
    if best is None:
        return {"fs_type": "unknown", "mount_source": "", "mount_point": "/"}
    info = best[1]
    if not info["fs_type"]:
        info["fs_type"] = "unknown"
    return info


def is_fuse(fs_type: str) -> bool:
    return fs_type == "fuse" or fs_type.startswith("fuse.")


def anchor_change(common_dir: str, recorded: str, current: str) -> dict[str, Any]:
    """An ``anchor_change`` object (schemas/defs.json) for a clone's common dir."""
    try:
        st = os.stat(common_dir)
        mount = describe_mount(common_dir, st)
    except OSError:
        mount = {"fs_type": "unknown", "mount_source": "", "mount_point": "/"}
    return {
        "anchor": "common_dir",
        "path": common_dir,
        "recorded": recorded,
        "current": current,
        **mount,
        "changed": recorded != current,
    }


# --------------------------------------------------------------------------------------------
# Verifying one clone
# --------------------------------------------------------------------------------------------


class NotAClone(Exception):
    """A directory that is not a usable clone of the repo (``cause`` is ``no_remote`` or None)."""

    def __init__(self, message: str, cause: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


@dataclass(frozen=True)
class Verified:
    clone_path: str  # realpath of the clone directory
    common_dir: str  # <clone_path>/.git
    fetch_url: str  # fetch form (ssh user kept), normalizes to the repo id, unchanged by ls-remote
    common_id: str  # "<dev>:<ino>" of the common dir, from a no-follow open
    object_format: str  # "sha1" or "sha256"


def _lstat_gitdir(gitdir: str) -> os.stat_result:
    try:
        st = os.lstat(gitdir)
    except OSError as exc:
        raise NotAClone("not a git clone (no .git directory)") from exc
    if not stat.S_ISDIR(st.st_mode):
        # A symlink, or a file (a linked worktree or submodule): not a clone wtctl will use.
        kind = "a symlink" if stat.S_ISLNK(st.st_mode) else "not a directory"
        raise NotAClone(f".git is {kind}; only a clone with a .git directory is used")
    return st


MAX_CLONE_CONFIG = 1 << 20


def _lstat_at(fd: int, name: str) -> os.stat_result | None:
    """``lstat`` of ``name`` below the directory ``fd`` (None when absent)."""
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NotAClone(f"cannot examine .git/{name}: {exc.strerror}") from exc


def _check_layout(fd: int, *, allow_includes: bool) -> None:
    """Refuse a ``.git`` that borrows state from elsewhere or can hang a config read.

    Every check is a no-follow ``lstat`` below the opened ``.git`` directory: a ``commondir``
    file (a planted ``.git`` pointing at another repository's config and objects), a ``config``
    or ``HEAD`` that is not a regular file, ``objects`` or ``refs`` that are not real
    directories, and ``objects/info/alternates``. With ``allow_includes`` false (discovery,
    where the candidate is somebody else's directory) a config whose text mentions ``include``
    is refused too, since git would open the included path (a FIFO blocks until the config
    read times out).
    """
    if _lstat_at(fd, "commondir") is not None:
        raise NotAClone(".git has a commondir file; only a self-contained clone is used")
    for name in ("config", "HEAD"):
        st = _lstat_at(fd, name)
        if st is None or not stat.S_ISREG(st.st_mode):
            raise NotAClone(f".git/{name} is not a regular file")
    for name in ("objects", "refs"):
        st = _lstat_at(fd, name)
        if st is None or not stat.S_ISDIR(st.st_mode):
            raise NotAClone(f".git/{name} is not a directory")
    info = _lstat_at(fd, "objects/info")
    if info is not None:
        if not stat.S_ISDIR(info.st_mode):
            raise NotAClone(".git/objects/info is not a directory")
        if _lstat_at(fd, "objects/info/alternates") is not None:
            raise NotAClone(".git/objects/info/alternates exists; the clone borrows objects")
    if not allow_includes:
        _check_includes_safe(fd)


def _read_config(fd: int) -> bytes:
    """The bytes of ``.git/config`` (at most ``MAX_CLONE_CONFIG``), opened without blocking."""
    try:
        rfd = os.open("config", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except OSError as exc:
        raise NotAClone(f"cannot read .git/config: {exc.strerror}") from exc
    try:
        if not stat.S_ISREG(os.fstat(rfd).st_mode):
            raise NotAClone(".git/config is not a regular file")
        data = os.read(rfd, MAX_CLONE_CONFIG + 1)
    finally:
        os.close(rfd)
    if len(data) > MAX_CLONE_CONFIG:
        raise NotAClone(".git/config is too large")
    return data


def _check_includes_safe(fd: int) -> None:
    """For a discovered candidate: fail closed on any config that could make git open another
    path. Config syntax (quoting, continuations) is not parsed here; a config whose raw text
    contains ``include`` anywhere (case-insensitive) or a NUL byte is refused."""
    data = _read_config(fd)
    if b"\x00" in data:
        raise NotAClone("the clone's config contains a NUL byte")
    if b"include" in data.lower():
        raise NotAClone("the clone's config mentions include")


def verify_clone(
    git: Git,
    path: str,
    repo_id: str,
    *,
    remote: str,
    clone_url: str | None,
    allow_includes: bool = True,
) -> Verified:
    """Check that ``path`` is a clone of ``repo_id`` and describe it (raises :class:`NotAClone`).

    The remote URL is read with hardened git and reduced by ``urls.fetch_url`` (https userinfo
    and any password dropped, a plain ssh user kept); it must normalize to the repo id (or match
    the configured ``clone_url``). Because the clone URL is also the push target, no
    ``url.*.insteadOf`` / ``pushInsteadOf`` prefix may match it and the remote may have no
    ``pushurl``; the fetch-form URL must also come back unchanged from ``ls-remote --get-url``.
    The raw URL (it may hold a password) is never passed to git as an argument.

    ``allow_includes`` false (discovery) refuses a clone whose config mentions include.
    """
    real = os.path.realpath(path)
    if "\n" in real or "\0" in real:
        raise NotAClone("the clone path contains a newline")
    gitdir = os.path.join(real, ".git")
    before = _lstat_gitdir(gitdir)

    def open_checked() -> os.stat_result:
        try:
            fd = open_dir_nofollow(gitdir)
        except OSError as exc:
            raise NotAClone(f"cannot open .git without following links: {exc.strerror}") from exc
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise NotAClone("the clone changed while it was being verified")
            _check_layout(fd, allow_includes=allow_includes)
            return opened
        finally:
            os.close(fd)

    open_checked()

    # TODO(#10): these git runs address the clone by path (--git-dir); a swap of a parent
    # directory between the checks above and the run needs fsafe's anchored walks to close.
    def run(args: list[str], **kw: Any) -> Any:
        try:
            return git.run(args, git_dir=gitdir, **kw)
        except WtctlError as exc:  # a config listing that fails closed: not usable
            if exc.code != "internal":
                raise
            raise NotAClone(f"cannot read the clone's config: {exc.message}"[:300]) from exc

    r = run(["config", "--no-includes", "-z", "--get-all", f"remote.{remote}.url"])
    if r.returncode == 1:
        raise NotAClone(f"the clone has no remote {remote!r}", "no_remote")
    if r.returncode != 0:
        raise NotAClone("not a usable git repository")
    values = [v for v in r.stdout.split(b"\0") if v]
    if len(values) != 1:
        raise NotAClone(f"remote {remote!r} has {len(values)} URLs; exactly one is required")
    try:
        raw = values[0].decode("utf-8")
    except UnicodeDecodeError:
        raise NotAClone(f"remote {remote!r} has a URL that is not UTF-8") from None
    if not urls.matches_repo(raw, repo_id, clone_url=clone_url):
        if urls.ssh_user_refused(raw):
            raise NotAClone(f"remote {remote!r} has an SSH user that is not a plain user name")
        raise NotAClone(f"remote {remote!r} does not point at {repo_id}")
    stripped = urls.fetch_url(raw)
    if not urls.matches_repo(stripped, repo_id, clone_url=clone_url):
        raise NotAClone(f"remote {remote!r} does not point at {repo_id}")
    push = run(["config", "--no-includes", "-z", "--get-all", f"remote.{remote}.pushurl"])
    if push.returncode == 0:
        raise NotAClone(f"remote {remote!r} has a pushurl; pushes would go elsewhere")
    if push.returncode != 1:
        raise NotAClone("not a usable git repository")
    rewrites = run(
        ["config", "--no-includes", "-z", "--get-regexp", r"^url\..*\.(instead|pushinstead)of$"]
    )
    if rewrites.returncode not in (0, 1):
        raise NotAClone("not a usable git repository")
    for key, value in parse_config_list(rewrites.stdout):
        prefix = value or ""
        if any(u.startswith(prefix) for u in (raw, stripped)):
            kind = "pushInsteadOf" if key.endswith("pushinsteadof") else "insteadOf"
            raise NotAClone(f"the URL of remote {remote!r} is rewritten by git config ({kind})")
    g = run(["ls-remote", "--get-url", stripped])
    if g.returncode != 0 or g.out.rstrip("\n") != stripped:
        raise NotAClone(f"the URL of remote {remote!r} is rewritten by git config (insteadOf)")
    fmt = run(["config", "--no-includes", "--get", "extensions.objectformat"])
    object_format = fmt.out.strip().lower() if fmt.returncode == 0 else "sha1"
    # The directory must still be the one examined, and the identity comes from an opened fd.
    opened = open_checked()
    after = _lstat_gitdir(gitdir)
    if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
        raise NotAClone("the clone changed while it was being verified")
    return Verified(real, gitdir, stripped, identity_str(opened), object_format)


# --------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------


@dataclass
class Resolution:
    repo: str
    clone_path: str
    common_dir: str
    source: str  # "config" | "discovered" | "cloned"
    fetch_url: str
    default_branch: str | None
    object_format: str
    common_id: str  # the common dir's current identity
    recorded_id: str | None  # clones.common_id (None when nothing is recorded: dry run)
    cloned: bool = False  # this call cloned it
    warnings: list[str] = field(default_factory=list)

    @property
    def anchor_changed(self) -> bool:
        return self.recorded_id is not None and self.recorded_id != self.common_id

    def anchors(self) -> list[dict[str, Any]]:
        if not self.anchor_changed or self.recorded_id is None:
            return []
        return [anchor_change(self.common_dir, self.recorded_id, self.common_id)]

    def require_sha1(self, command: str = "create") -> None:
        """For ``create`` and ``adopt``: a SHA-256 repository is refused (exit 12)."""
        if self.object_format != "sha1":
            raise WtctlError(
                "create_failed",
                f"{self.clone_path} is a {self.object_format} repository; only sha1 is supported",
                details={"cause": "object_format"},
            )

    def require_anchor(self) -> None:
        """For ``create`` and ``adopt``: a changed common-dir identity is refused (exit 12)."""
        if self.anchor_changed:
            raise WtctlError(
                "create_failed",
                "the clone's directory is not the one wtctl recorded for this repo; verify the "
                f"mount, then run `wtctl clone {self.repo}` as a human to re-accept the clone",
                details={"cause": "anchor_changed", "anchors": self.anchors()},
            )

    def to_json(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "clone_path": self.clone_path,
            "common_dir": self.common_dir,
            "source": self.source,
            "fetch_url": self.fetch_url,
            "default_branch": self.default_branch,
        }

    def stderr_warnings(self) -> list[str]:
        out = list(self.warnings)
        if self.object_format != "sha1":
            out.append(
                f"{self.clone_path} is a {self.object_format} repository; "
                "worktrees cannot be created for it (only sha1 is supported)"
            )
        if self.anchor_changed:
            out.append(
                f"the directory of {self.clone_path}/.git differs from the one recorded for "
                f"{self.repo}; create and adopt will refuse until a human runs "
                f"`wtctl clone {self.repo}`"
            )
        return out


# --------------------------------------------------------------------------------------------
# The resolver
# --------------------------------------------------------------------------------------------


class Resolver:
    """Resolution of repo ids for one invocation (config, state and hardened git injected)."""

    def __init__(self, config: Any, state: State, git: Git, *, readonly: bool = False) -> None:
        self.config = config
        self.state = state
        self.git = git
        self.readonly = readonly  # dry run: nothing is stored or created
        self.notes: list[str] = []  # warnings of the search that a failure message also carries

    # -- helpers --------------------------------------------------------------------------

    def _verify(self, path: str, repo_id: str, *, allow_includes: bool = True) -> Verified:
        s = self.config.repo(repo_id)
        return verify_clone(
            self.git, path, repo_id, remote=s.remote, clone_url=s.clone_url,
            allow_includes=allow_includes,
        )  # fmt: skip

    def _result(
        self, repo_id: str, v: Verified, source: str, *, cloned: bool = False
    ) -> Resolution:
        """Store (or compare with) the repo's row and describe the clone."""
        row = self.state.get_clone(repo_id)
        recorded = row["common_id"] if row else None
        if self.readonly:
            return Resolution(
                repo_id, v.clone_path, v.common_dir, source, v.fetch_url,
                row["default_branch"] if row else None, v.object_format, v.common_id,
                recorded, cloned,
            )  # fmt: skip
        if not recorded:
            # One transaction: if another process recorded first, its identity stands.
            row = self.state.record_first_clone(
                repo_id, path=v.clone_path, common_dir=v.common_dir, common_id=v.common_id,
                source=source, fetch_url=v.fetch_url,
            )  # fmt: skip
            recorded = row["common_id"]
        elif recorded == v.common_id:
            keep = (
                row["source"] if row and row["path"] == v.clone_path and row["source"] else source
            )
            self.state.put_clone(
                repo_id, path=v.clone_path, common_dir=v.common_dir, source=keep,
                fetch_url=v.fetch_url,
            )  # fmt: skip
        # else: a different directory than the recorded one changes nothing stored.
        row = self.state.get_clone(repo_id)
        return Resolution(
            repo_id, v.clone_path, v.common_dir, source, v.fetch_url,
            row["default_branch"] if row else None, v.object_format, v.common_id,
            recorded, cloned,
        )  # fmt: skip

    # -- the sources ----------------------------------------------------------------------

    def _from_config(self, repo_id: str) -> Resolution | None:
        """Step 1. None only when the configured directory does not exist."""
        configured = self.config.repo(repo_id).clone
        assert configured is not None
        try:
            st = os.stat(configured)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise WtctlError(
                "refused", f"cannot examine {configured}: {exc.strerror}", reason="path_exists"
            ) from exc
        if not stat.S_ISDIR(st.st_mode):
            raise WtctlError(
                "refused", f"{configured} exists and is not a directory", reason="path_exists"
            )
        try:
            v = self._verify(configured, repo_id)
        except NotAClone as exc:
            details = {"cause": exc.cause} if exc.cause else None
            msg = f"{configured} is not a clone of {repo_id}: {exc.message}"
            if details:
                raise WtctlError("remote_mismatch", msg, details=details) from exc
            raise WtctlError("remote_mismatch", msg) from exc
        return self._result(repo_id, v, "config")

    def _from_record(self, repo_id: str) -> Resolution | None:
        """Step 2 and the cache of step 3: the repo's row, re-verified on every use."""
        row = self.state.get_clone(repo_id)
        if not row or row["source"] not in ("cloned", "discovered") or not row["path"]:
            return None
        try:
            v = self._verify(row["path"], repo_id)
        except NotAClone:
            return None  # skipped as a source; the row and its recorded identity stay
        return self._result(repo_id, v, "cloned" if row["source"] == "cloned" else "discovered")

    def _discover(self, repo_id: str) -> Resolution | None:
        """Step 3: walk ``search_roots``; several matches are ambiguous (exit 21).

        A candidate is somebody else's directory: it is verified without honoring config
        includes, and no candidate is verified after the time budget is spent. When the walk was
        cut short the note is kept in ``self.notes`` and added to the not-found and ambiguous
        messages (an error object has no warnings field).
        """
        roots = list(self.config.search_roots)
        if not roots:
            return None
        depth = min(int(self.config.search_depth), MAX_DISCOVERY_DEPTH)
        clock = self.git.gateway.clock
        found: dict[str, Verified] = {}
        budget = _Budget(MAX_DISCOVERY_DIRS, clock.monotonic() + DISCOVERY_SECONDS)
        for root in roots:
            for candidate in walk_for_clones(root, depth, budget, clock):
                if clock.monotonic() > budget.deadline:
                    budget.truncated = True
                    break
                try:
                    v = self._verify(candidate, repo_id, allow_includes=False)
                except NotAClone:
                    continue
                found.setdefault(v.clone_path, v)
            if budget.truncated:
                break
        suffix = ""
        if budget.truncated:
            self.notes.append(TRUNCATED_NOTE)
            suffix = f" ({TRUNCATED_NOTE})"
        if not found:
            return None
        if len(found) > 1:
            raise WtctlError(
                "ambiguous",
                f"several clones of {repo_id} were found{suffix}; add repos.{repo_id}.clone "
                "to choose",
                candidates=[{"id": None, "path": p} for p in sorted(found)],
            )
        (v,) = found.values()
        res = self._result(repo_id, v, "discovered")
        res.warnings.extend(self.notes)
        return res

    def find(self, repo_id: str) -> tuple[Resolution | None, bool]:
        """Steps 1 to 3. ``(resolution, configured_missing)``.

        A configured directory that does not exist is "not found" and ends the search: the user
        said where the clone belongs, so no other clone stands in for it (auto-clone, or
        ``clone``, creates it there).
        """
        urls.validate_repo_id(repo_id)
        if self.config.repo(repo_id).clone:
            res = self._from_config(repo_id)
            return res, res is None
        return (self._from_record(repo_id) or self._discover(repo_id)), False

    # -- the whole of `locate` -------------------------------------------------------------

    def locate(self, repo_id: str, *, auto_clone: bool | None = None) -> Resolution:
        res, _missing = self.find(repo_id)
        if res is not None:
            return res
        settings = self.config.repo(repo_id)
        if settings.auto_clone if auto_clone is None else auto_clone:
            res = self.clone(repo_id)
            res.warnings.extend(self.notes)
            return res
        note = f" ({self.notes[0]})" if self.notes else ""
        raise WtctlError("not_found", f"no clone of {repo_id} found{note}; {NOT_FOUND_HINT}")

    # -- cloning --------------------------------------------------------------------------

    def clone_url(self, repo_id: str) -> str:
        """The URL to clone from: ``repos.<id>.clone_url``, else ``hosts.<host>.clone_url``.

        Either way the result is a plain local path or an https/ssh URL in fetch form (an ssh URL
        keeps a plain user name): the configured text may hold a password, which must reach
        neither git's argv nor the new clone's ``.git/config``.
        """
        s = self.config.repo(repo_id)
        if s.clone_url is not None:
            return self._plain_url(s.clone_url, f"repos.{repo_id}.clone_url")
        host = urls.repo_host(repo_id)
        template = self.config.host(host).clone_url
        rendered = urls.render_clone_url(repo_id, template=template)
        return self._plain_url(rendered, f"hosts.{host}.clone_url")

    @staticmethod
    def _plain_url(raw: str, what: str) -> str:
        local = urls.local_path(raw)
        if local is not None:
            return local  # a plain path, never a file:// URL (hardened git rule)
        stripped = urls.fetch_url(raw)
        if urls.normalize_url(stripped) is None:
            if urls.ssh_user_refused(raw):
                raise WtctlError(
                    "remote_mismatch", f"{what} has an SSH user that is not a plain user name"
                )
            raise WtctlError("remote_mismatch", f"{what} is not an https, ssh or local path URL")
        return stripped

    def destination(self, repo_id: str) -> tuple[str, list[str], str]:
        """``(base, parts, name)``: the parent chain below ``base`` and the clone's directory name.

        A configured ``repos.<id>.clone`` is used as is (``parts`` empty); otherwise the
        destination is ``{clone_root}/{host}/{path}``.
        """
        configured = self.config.repo(repo_id).clone
        if configured:
            parent, name = os.path.split(configured.rstrip("/"))
            return parent or "/", [], name
        parts = [urls.host_dirname(urls.repo_host(repo_id)), *urls.repo_path(repo_id).split("/")]
        return self.config.effective_clone_root, parts[:-1], parts[-1]

    def dest_path(self, repo_id: str) -> str:
        base, parts, name = self.destination(repo_id)
        return os.path.join(base, *parts, name)

    def _auth(self, url: str) -> NetworkAuth:
        """Credentials of a network git call for ``url`` (an unusable URL: ``remote_mismatch``)."""
        return prepare_network_auth(
            self.config, url, environ=os.environ, gateway=self.git.gateway, warn=self.notes.append
        )

    def clone(self, repo_id: str) -> Resolution:
        """Clone ``repo_id`` (auto-clone and ``clone``) under the per-repo lease.

        An existing destination that holds a clone of this repo is recorded and reused;
        any other non-empty destination is refused; otherwise the clone is made in a sibling
        ``<dest>.wtctl-tmp-<random>`` and renamed into place. The temporary directory is removed
        on any failure and a leftover one (a crash) is never reused.
        """
        urls.validate_repo_id(repo_id)
        url = self.clone_url(repo_id)
        dest = self.dest_path(repo_id)
        if "\n" in dest or "\0" in dest:
            raise WtctlError("clone_failed", "the clone path contains a newline")
        if self.readonly:
            return self._dry_run_clone(repo_id, url, dest)
        with self.state.lease(repo_lease(repo_id), wait=self.config.lease_wait_s):
            # Another process may have cloned while this one waited for the lease.
            existing = self._reuse_existing(repo_id, dest)
            if existing is not None:
                return existing
            return self._clone_locked(repo_id, url)

    def _dry_run_clone(self, repo_id: str, url: str, dest: str) -> Resolution:
        existing = self._reuse_existing(repo_id, dest)
        if existing is not None:
            return existing
        # Nothing exists to compare: "0:0" is a placeholder, never an identity to warn about.
        return Resolution(
            repo_id, dest, os.path.join(dest, ".git"), "cloned", urls.fetch_url(url),
            None, "sha1", "0:0", None, True,
        )  # fmt: skip

    def _reuse_existing(self, repo_id: str, dest: str) -> Resolution | None:
        """The destination if it already holds a clone of this repo; refuse other contents."""
        try:
            st = os.lstat(dest)
        except FileNotFoundError:
            return None
        if stat.S_ISDIR(st.st_mode) and not os.listdir(dest):
            return None  # an empty directory is replaced by the rename
        try:
            if not stat.S_ISDIR(st.st_mode):
                raise NotAClone("not a directory")
            v = self._verify(dest, repo_id)
            row = self.state.get_clone(repo_id)
            recorded_here = (
                row is not None and row["path"] == v.clone_path and row["common_id"] == v.common_id
            )
            # A clone of an empty remote has no commit: it is accepted only when it is the very
            # clone wtctl recorded (same path and identity), so a second clone is idempotent.
            if not recorded_here:
                head = self.git.run(
                    ["rev-parse", "--verify", "HEAD^{commit}"], git_dir=v.common_dir
                )
                if head.returncode != 0:
                    raise NotAClone("HEAD is not a commit")
        except NotAClone:
            raise WtctlError(
                "refused",
                f"{dest} exists and is not a clone of {repo_id}",
                reason="path_exists",
            ) from None
        return self._result(repo_id, v, "cloned")

    def _clone_locked(self, repo_id: str, url: str) -> Resolution:
        base, parts, name = self.destination(repo_id)
        os.makedirs(base, exist_ok=True)
        pfd, ppath = _open_parent(os.path.realpath(base), parts)
        try:
            tmp = f"{name}.wtctl-tmp-{secrets.token_hex(6)}"
            if len(tmp.encode()) > 255:
                raise WtctlError("clone_failed", f"the directory name {name!r} is too long")
            if "\n" in ppath or "\n" in name:
                raise WtctlError("clone_failed", "the clone path contains a newline")
            tmp_path = os.path.join(ppath, tmp)
            try:
                os.lstat(tmp_path)
            except FileNotFoundError:
                pass
            else:  # a leftover (or planted) name is never reused; the random suffix makes a clash
                raise WtctlError("clone_failed", f"{tmp_path} already exists")
            try:
                self._run_clone(repo_id, url, tmp_path, pfd, ppath)
                v = self._install(pfd, ppath, tmp, name, repo_id)
            except BaseException:
                with contextlib.suppress(OSError):
                    shutil.rmtree(tmp, dir_fd=pfd)  # TODO(#10): fsafe fd-based delete
                raise
        finally:
            os.close(pfd)
        # wtctl made this directory itself: its identity is recorded even over an older row.
        self.state.put_clone(
            repo_id, path=v.clone_path, common_dir=v.common_dir, common_id=v.common_id,
            source="cloned", fetch_url=v.fetch_url,
        )  # fmt: skip
        row = self.state.get_clone(repo_id) or {}
        return Resolution(
            repo_id, v.clone_path, v.common_dir, "cloned", v.fetch_url, row.get("default_branch"),
            v.object_format, v.common_id, v.common_id, True,
        )  # fmt: skip

    def _run_clone(self, repo_id: str, url: str, tmp_path: str, pfd: int, ppath: str) -> None:
        # The parent must still be the directory that was opened without following links.
        if not os.path.samestat(os.fstat(pfd), os.stat(ppath)):
            raise WtctlError("clone_failed", f"{ppath} changed while cloning")
        remote = self.config.repo(repo_id).remote
        auth = self._auth(url)
        with private_dir() as p:
            r = run_network_git(
                self.git,
                [
                    "clone", "--template=", "-c", "protocol.version=2",
                    f"--origin={remote}", "--", url, tmp_path,
                ],
                cfg=self.config,
                auth=auth,
                kind=NetworkKind.CLONE,
                tool_owned=True,
                private_dir=p,
            )  # fmt: skip
        err = rejected_token_error(
            auth, outcome_error(classify_fetch(r), r, failure_code="clone_failed")
        )
        if err is not None:
            raise err

    def _install(self, pfd: int, ppath: str, tmp: str, name: str, repo_id: str) -> Verified:
        """Verify the fresh clone in its temporary directory, rename it into place and check that
        the installed directory is the one verified (same ``(dev, ino)``, same ``.git``)."""
        tmp_path = os.path.join(ppath, tmp)
        try:
            v = self._verify(tmp_path, repo_id)
        except NotAClone as exc:
            raise WtctlError(
                "clone_failed", f"the new clone failed verification: {exc.message}"
            ) from exc
        if not os.path.samestat(os.fstat(pfd), os.stat(ppath)):
            raise WtctlError("clone_failed", f"{ppath} changed while cloning")
        dest = os.path.join(ppath, name)
        try:
            verified = os.stat(tmp, dir_fd=pfd, follow_symlinks=False)
            os.rename(tmp, name, src_dir_fd=pfd, dst_dir_fd=pfd)
        except OSError as exc:
            raise WtctlError(
                "refused",
                f"{dest} appeared while cloning ({exc.strerror})",
                reason="path_exists",
            ) from exc
        # TODO(#10): the verification above addressed the clone by path; parent-chain swaps need
        # fsafe's anchored walks. Here the identity is compared through the parent's fd.
        try:
            installed = os.stat(name, dir_fd=pfd, follow_symlinks=False)
            dfd = os.open(name, _OPEN_DIR, dir_fd=pfd)
            try:
                dot_git = os.stat(".git", dir_fd=dfd, follow_symlinks=False)
            finally:
                os.close(dfd)
        except OSError as exc:
            raise WtctlError(
                "clone_failed", f"{dest} changed after it was installed: {exc.strerror}"
            ) from exc
        if (installed.st_dev, installed.st_ino) != (
            verified.st_dev,
            verified.st_ino,
        ) or identity_str(dot_git) != v.common_id:
            raise WtctlError("clone_failed", f"{dest} changed after it was installed")
        return dataclasses.replace(v, clone_path=dest, common_dir=os.path.join(dest, ".git"))


def _open_parent(base_real: str, parts: list[str]) -> tuple[int, str]:
    """Open ``base_real/parts...`` creating missing directories, never following a symlink."""
    try:
        fd = open_dir_nofollow(base_real)
    except OSError as exc:
        raise WtctlError("clone_failed", f"cannot open {base_real}: {exc.strerror}") from exc
    path = base_real
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o777, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise WtctlError(
                    "clone_failed", f"cannot create {os.path.join(path, part)}: {exc.strerror}"
                ) from exc
            try:
                nxt = os.open(part, _OPEN_DIR, dir_fd=fd)
            except OSError as exc:
                raise WtctlError(
                    "clone_failed",
                    f"{os.path.join(path, part)} is not a plain directory ({exc.strerror})",
                ) from exc
            os.close(fd)
            fd = nxt
            path = os.path.join(path, part)
    except BaseException:
        os.close(fd)
        raise
    return fd, path


# --------------------------------------------------------------------------------------------
# Discovery walk
# --------------------------------------------------------------------------------------------


@dataclass
class _Budget:
    dirs: int
    deadline: float
    truncated: bool = False


def _plain_name(name: str) -> bool:
    if not name or "\n" in name or "\0" in name:
        return False
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:  # undecodable bytes (surrogate escapes)
        return False
    return True


def walk_for_clones(root: str, depth: int, budget: _Budget, clock: Any) -> Iterator[str]:
    """Directories at most ``depth`` levels below ``root`` (itself included) with a ``.git``
    directory. Symlinks are never followed (every step is an ``O_NOFOLLOW`` open), a directory
    on another device is not entered, and a directory with a ``.git`` directory is not descended
    into. A linked worktree (``.git`` is a file) is skipped.
    TODO(#10): fsafe walk (mount points on the same device).
    """
    try:
        real = os.path.realpath(root)
        fd = open_dir_nofollow(real)
    except OSError:
        return
    try:
        dev = os.fstat(fd).st_dev
        yield from _walk(fd, real, 0, depth, dev, budget, clock)
    finally:
        os.close(fd)


def _walk(
    fd: int, path: str, level: int, depth: int, dev: int, budget: _Budget, clock: Any
) -> Iterator[str]:
    if budget.dirs <= 0 or clock.monotonic() > budget.deadline:
        budget.truncated = True
        return
    budget.dirs -= 1
    try:
        names = sorted(os.listdir(fd))
    except OSError:
        return
    if ".git" in names:
        try:
            st = os.stat(".git", dir_fd=fd, follow_symlinks=False)
        except OSError:
            return
        if stat.S_ISDIR(st.st_mode):
            if _plain_name(path):
                yield path
            return
    if level >= depth:
        return
    for name in names:
        if name == ".git" or not _plain_name(name):
            continue
        if budget.dirs <= 0 or clock.monotonic() > budget.deadline:
            budget.truncated = True
            return
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode) or st.st_dev != dev:
                continue
            child = os.open(name, _OPEN_DIR, dir_fd=fd)
        except OSError:
            continue
        try:
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                continue  # swapped between the stat and the open
            yield from _walk(child, os.path.join(path, name), level + 1, depth, dev, budget, clock)
        finally:
            os.close(child)


def resolver_for(ctx: Any, *, readonly: bool = False) -> Resolver:
    """The resolver of a command invocation (config and state load lazily on first use)."""
    from .gitsetup import open_git

    return Resolver(ctx.config, ctx.state, open_git(ctx.config, ctx.gateway), readonly=readonly)
