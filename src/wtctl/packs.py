"""Pack-based object transfer into repositories an agent may have written (D54, spike H).

Objects move into a user's clone, or into a throwaway filled from the clone, only as packs that
wtctl builds and checks itself. No ``fetch``, ``clone`` or ``push`` ever runs against such a
repository, and every git command that reads one runs with ``GIT_ALLOW_PROTOCOL=none`` (the
hardened runner's default for a command without a scheme), so a promisor remote planted in the
clone cannot run its ``uploadpack`` command when git looks for an object that is not there.

Rules are in docs/interface.md (Mirror › Base commits into the clone; Create order step 9) and
docs/design.md (D54, the base-commit and local-throwaway recipes).

* :func:`transfer_base`: the base commit into the clone. The only network step is a fetch of tree
  objects into the mirror; a non-thin pack of the missing range is built there, indexed outside any
  repository (``--fsck-objects``), written into ``<common>/objects/pack`` through a no-follow fd
  chain (fsafe) and checked for completeness before ``worktree add``.
* :func:`fill_local_throwaway`: a fresh throwaway filled with one commit of the clone. The caller
  (the throwaway commands, #21) owns the throwaway's record and its removal after a failure.

Packs and object lists live in files of a private temp directory, never in memory.
"""

from __future__ import annotations

import binascii
import contextlib
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass

from . import fsafe
from .config import Config
from .credentials import (
    NetworkAuth,
    NetworkKind,
    prepare_network_auth,
    rejected_token_error,
    run_network_git,
)
from .errors import WtctlError
from .gateway import GitResult
from .gitsetup import private_dir
from .hardgit import Git, classify_fetch, fetch_with_retries, outcome_error
from .mirror import OWNED_KEYS, Mirror, mirror_url
from .state import Lease

_SHA = re.compile(r"[0-9a-f]{40}\Z")

# A pack larger than this is refused (the pack-objects process is killed once its output file
# passes it). Callers may pass a lower limit; a test passes a tiny one.
MAX_PACK_BYTES = 8 * 1024**3
# Bound on the commits one transfer names and on the size of the object lists written to files.
MAX_RANGE_COMMITS = 2_000_000
MAX_LIST_BYTES = 1024**3
# What a commit's tree may expand to in a throwaway's work tree (a few bytes of pack can name
# gigabytes or an exponential number of files): constants, not configuration.
MAX_TREE_ENTRIES = 500_000
MAX_TREE_BYTES = 4 * 1024**3
MAX_TREE_LIST_BYTES = 256 * 1024**2
# How many of the clone's ref tips bound the range. The list is untrusted and only shrinks the
# pack, so a clone with more refs than this just gets a larger pack.
MAX_PRESENT_TIPS = 1000
MAX_TIPS_BYTES = 64 * 1024**2
COPY_CHUNK = 1024 * 1024
PACK_HEADER = b"PACK\x00\x00\x00\x02"
IDX_HEADER = b"\xfftOc\x00\x00\x00\x02"


def _fail(message: str, **fields: object) -> WtctlError:
    return WtctlError("create_failed", message, **fields)


def _tail(result: GitResult) -> str:
    lines = result.err.strip().splitlines()
    return lines[-1][:200] if lines else "no output"


def _checked(result: GitResult, what: str) -> GitResult:
    """The result of a git command that must have exited 0 within its limits."""
    if result.timed_out:
        raise _fail(f"{what} timed out")
    if result.output_limit:
        raise _fail(f"{what} produced more output than the limit")
    if result.returncode != 0:
        raise _fail(f"{what} failed (exit {result.returncode}): {_tail(result)}")
    return result


def parse_identity(text: str) -> fsafe.Identity:
    """``<dev>:<ino>`` as recorded in ``clones.common_id``."""
    m = re.fullmatch(r"([0-9]+):([0-9]+)", text)
    if m is None:
        raise ValueError(f"not a recorded identity: {text!r}")
    return fsafe.Identity(int(m.group(1)), int(m.group(2)))


def _local_timeout(cfg: Config) -> float:
    """Limit of a local pack operation: as long as a clone may take."""
    return float(min(cfg.clone_timeout_s, 24 * 86400))


def _sha_lines(path: str, *, what: str, limit: int = MAX_RANGE_COMMITS) -> int:
    """Count the lines of an object-id list file; every line must be one 40-hex id."""
    count = 0
    with open(path, "rb") as f:
        for raw in f:
            if not _SHA.fullmatch(raw.rstrip(b"\n").decode("ascii", "replace")):
                raise _fail(f"{what} printed a line that is not an object id")
            count += 1
            if count > limit:
                raise _fail(f"{what} names more than {limit} objects")
    return count


# --------------------------------------------------------------------------------------------
# Index, install, verify
# --------------------------------------------------------------------------------------------


def index_pack(git: Git, pack: str, idx: str, *, p: str, timeout: float) -> str:
    """Index ``pack`` OUTSIDE any repository and return its checksum ``<h>`` (40 hex).

    Runs with cwd ``p`` (a fresh private directory) and ``GIT_CEILING_DIRECTORIES`` its parent, so
    no repository and no config is read. ``--fsck-objects`` rejects objects that fail fsck (a
    ``.gitmodules`` abuse, a malformed tree); ``--strict`` is not used here (it crashes outside a
    repository on git 2.34) and ``--fix-thin`` never: a thin pack is refused, because resolving it
    would need objects of the clone. The name is checked against the pack's own trailer, so a
    forged name cannot reach the install.
    """
    r = _checked(
        git.run(["index-pack", "--fsck-objects", "-o", idx, pack], private_dir=p, timeout=timeout),
        "git index-pack",
    )
    name = r.out.strip()
    if not _SHA.fullmatch(name):
        raise _fail("git index-pack printed no pack checksum")
    with open(pack, "rb") as f:
        head = f.read(len(PACK_HEADER))
        f.seek(-20, os.SEEK_END)
        trailer = f.read(20)
    if head != PACK_HEADER:
        raise _fail("the pack has no version 2 header")
    with open(idx, "rb") as f:
        idx_head = f.read(len(IDX_HEADER))
        f.seek(-40, os.SEEK_END)
        idx_pack_sum = f.read(20)
    if idx_head != IDX_HEADER:
        raise _fail("the pack index has no version 2 header")
    if binascii.hexlify(trailer).decode() != name or idx_pack_sum != trailer:
        raise _fail("the pack checksum does not match its name")
    return name


def _anchor_changed(
    path: str, recorded: fsafe.Identity, exc: fsafe.FsafeError, fs: fsafe.Fs | None
) -> WtctlError:
    """``create_failed`` with ``cause: anchor_changed`` and the anchor table of the schema."""
    # ``current`` is an identity that cannot be the recorded one until the anchor is re-opened:
    # inode 0 is never a real directory, so ``changed: true`` always has current != recorded.
    unknown = "0:0" if (recorded.dev, recorded.ino) != (0, 0) else "0:1"
    entry = {
        "anchor": "common_dir",
        "path": fsafe.escape_for_details(path),
        "recorded": f"{recorded.dev}:{recorded.ino}",
        "current": unknown,
        "changed": True,
        "fs_type": "unknown",
        "mount_source": "",
        "mount_point": "/",
    }
    with contextlib.suppress(fsafe.FsafeError, OSError):
        with fsafe.open_anchor(path, fs=fs, precheck=False) as now:
            entry["current"] = f"{now.identity.dev}:{now.identity.ino}"
            entry["changed"] = now.identity != recorded
            desc = fsafe.describe_mount(now)
            if desc is not None:
                entry["fs_type"] = desc.fstype or "unknown"
                entry["mount_source"] = desc.source
                entry["mount_point"] = fsafe.escape_for_details(desc.mount_point)
    return _fail(
        f"the clone's common git dir is not what was recorded ({exc.message}); "
        "nothing was written to the clone",
        details={"cause": "anchor_changed", "anchors": [entry]},
    )


def open_common_dir(
    common_dir: str, common_id: fsafe.Identity, *, fs: fsafe.Fs | None = None
) -> fsafe.Anchor:
    """The clone's common dir, opened through a no-follow chain and checked against its recorded
    identity (``create_failed``, ``cause: anchor_changed`` when it is not what was recorded)."""
    real = os.path.realpath(common_dir)
    try:
        return fsafe.open_anchor(real, expected=common_id, fs=fs)
    except fsafe.FsafeError as e:
        if e.kind in ("identity", "mount_point", "symlink", "not_dir", "missing"):
            raise _anchor_changed(real, common_id, e, fs) from e
        raise _fail(f"cannot open the clone's common git dir: {e.message}") from e


@dataclass(frozen=True)
class Installed:
    pack_new: bool  # this call created pack-<h>.pack (False: a file of that name was there)
    idx_new: bool


def _copy_to_new_file(
    anchor: fsafe.Anchor, dir_fd: int, tmp: str, src_path: str, *, expected_size: int | None
) -> os.stat_result:
    """Copy ``src_path`` into a new file ``tmp`` (mode 0444) of the directory ``dir_fd``."""
    src = os.open(src_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fd = fsafe.create_new_file(dir_fd, tmp, mode=0o444)
        try:
            anchor.verify_fd(fd, tmp)
            written = 0
            while True:
                chunk = os.read(src, COPY_CHUNK)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                written += len(chunk)
            if expected_size is not None and written != expected_size:
                raise OSError(f"{src_path} changed while it was copied")
            os.fsync(fd)
            return os.fstat(fd)
        finally:
            os.close(fd)
    finally:
        os.close(src)


def _link_new(dir_fd: int, tmp: str, final: str) -> bool:
    """Install ``tmp`` as ``final`` without replacing: False when ``final`` exists already."""
    try:
        os.link(tmp, final, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError:
        return False
    return True


def _size_and_trailer(dir_fd: int, name: str) -> tuple[int, bytes]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size < 20:
            return st.st_size, b""
        return st.st_size, os.pread(fd, 20, st.st_size - 20)
    finally:
        os.close(fd)


def _require_same(dir_fd: int, final: str, tmp: str) -> None:
    """A file already installed under ``final`` (a concurrent install, or a planted file) must be
    the one we hold: same size and same trailing checksum, else ``create_failed``."""
    try:
        same = _size_and_trailer(dir_fd, final) == _size_and_trailer(dir_fd, tmp)
    except OSError:
        same = False
    if not same:
        raise _fail(f"{final} exists in the clone's objects/pack and is not the file to install")


def install_pack(
    *,
    common_dir: str,
    common_id: fsafe.Identity,
    pack: str,
    idx: str,
    name: str,
    fs: fsafe.Fs | None = None,
) -> Installed:
    """Write ``pack-<name>.pack`` and ``pack-<name>.idx`` into ``<common_dir>/objects/pack``.

    ``objects/pack`` is reached by a no-follow fd chain from ``common_dir`` (resolved once; the
    opened fd must have ``common_id`` and every directory and file written must be on its device
    and mount, else ``create_failed``, ``cause: anchor_changed`` when the anchor itself is not
    what was recorded). Both files are first written under temporary names created exclusively
    (``O_CREAT|O_EXCL|O_NOFOLLOW``, mode 0444, fsynced); then the pack, then the index, are
    linked to their final names with ``linkat`` (never replacing: an existing file of that name is
    left as it is) and the temporary names removed. A failure before the first link leaves
    nothing visible; a failure between the links removes the pack this call created. Only new
    files are created below ``objects/pack``; nothing else of the clone is written.
    """
    if not _SHA.fullmatch(name):
        raise _fail("not a pack name")
    real = os.path.realpath(common_dir)
    with open_common_dir(common_dir, common_id, fs=fs) as anchor:
        try:
            dir_fd = fsafe.open_chain(anchor, anchor.fd, ["objects", "pack"])
        except fsafe.FsafeError as e:
            if e.kind == "mount_point":
                raise _anchor_changed(real, common_id, e, fs) from e
            raise _fail(f"cannot open objects/pack of the clone: {e.message}") from e
        try:
            return _install_into(anchor, dir_fd, pack, idx, name)
        except fsafe.FsafeError as e:
            if e.kind == "mount_point":
                raise _anchor_changed(real, common_id, e, fs) from e
            raise _fail(f"cannot write into the clone's objects/pack: {e.message}") from e
        except OSError as e:
            raise _fail(f"cannot write into the clone's objects/pack: {e.strerror or e}") from e
        finally:
            os.close(dir_fd)


def _install_into(anchor: fsafe.Anchor, dir_fd: int, pack: str, idx: str, name: str) -> Installed:
    token = secrets.token_hex(8)
    tmp_pack, tmp_idx = f"tmp_pack_wtctl_{token}", f"tmp_idx_wtctl_{token}"
    created: list[str] = []
    linked_pack: str | None = None
    pack_stat: os.stat_result | None = None
    try:
        created.append(tmp_pack)
        pack_stat = _copy_to_new_file(
            anchor, dir_fd, tmp_pack, pack, expected_size=os.stat(pack).st_size
        )
        created.append(tmp_idx)
        _copy_to_new_file(anchor, dir_fd, tmp_idx, idx, expected_size=os.stat(idx).st_size)
        final_pack, final_idx = f"pack-{name}.pack", f"pack-{name}.idx"
        pack_new = _link_new(dir_fd, tmp_pack, final_pack)
        if pack_new:
            linked_pack = final_pack
        else:
            _require_same(dir_fd, final_pack, tmp_pack)
        idx_new = _link_new(dir_fd, tmp_idx, final_idx)
        if not idx_new:
            _require_same(dir_fd, final_idx, tmp_idx)
        linked_pack = None  # complete (or the index was there already)
        with contextlib.suppress(OSError):
            os.fsync(dir_fd)
        return Installed(pack_new, idx_new)
    finally:
        if linked_pack is not None and pack_stat is not None:
            # the index failed to install: take back the pack this call created, if it is ours
            with contextlib.suppress(OSError):
                now = os.stat(linked_pack, dir_fd=dir_fd, follow_symlinks=False)
                if (now.st_dev, now.st_ino) == (pack_stat.st_dev, pack_stat.st_ino):
                    os.unlink(linked_pack, dir_fd=dir_fd)
        for tmp in created:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)


def missing_objects(
    git: Git, git_dir: str, start: str, *, p: str, timeout: float, max_bytes: int = 4 * 1024**3
) -> list[str]:
    """Objects reachable from ``start`` that the repository lacks (empty when complete).

    ``rev-list --objects --missing=print`` in the repository with protocols ``none``; the listing
    goes to a file of ``p`` and is scanned line by line. At most 100 ids are returned.
    """
    out = os.path.join(p, f"verify-{secrets.token_hex(4)}")
    r = git.run(
        ["rev-list", "--objects", "--missing=print", start],
        git_dir=git_dir,
        cwd=git_dir,
        stdout_path=out,
        max_stdout_bytes=max_bytes,
        timeout=timeout,
    )
    _checked(r, "git rev-list")
    missing: list[str] = []
    with open(out, "rb") as f:
        for raw in f:
            if raw.startswith(b"?"):
                missing.append(raw[1:].split(b" ", 1)[0].strip().decode("ascii", "replace"))
                if len(missing) >= 100:
                    break
    return missing


def clone_has_commit(git: Git, git_dir: str, sha: str) -> bool:
    """Whether the repository holds ``sha`` as a commit (protocols ``none``, no lazy fetch)."""
    if not _SHA.fullmatch(sha):
        raise ValueError("not a full object id")
    r = git.run(
        ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
        git_dir=git_dir,
        cwd=git_dir,
        stdin=f"{sha}\n".encode(),
        timeout=60,
    )
    return r.returncode == 0 and not r.timed_out and r.out.strip() == f"{sha} commit"


# --------------------------------------------------------------------------------------------
# Base commit into the clone
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Transfer:
    start: str
    already_present: bool  # the clone had the commit and everything reachable from it
    pack: str | None  # checksum of the pack written (or found), None when nothing was needed
    pack_new: bool = False
    commits: int = 0  # commits in the range sent
    present_tips: int = 0  # clone tips that bounded the range


def _present_tips(mirror: Mirror, p: str) -> list[str]:
    """The clone's ref tips that the mirror has as commits (untrusted: only shrinks the pack).

    Only branches, remote-tracking branches and tags are listed, newest commit first. The cap is
    applied after the refs that do not name a commit are dropped, so a flood of junk refs cannot
    push the real tips out.
    """
    git, clone = mirror.git, mirror.common_dir
    out = os.path.join(p, "tips")
    r = git.run(
        ["for-each-ref", "--sort=-committerdate", "--format=%(objectname) %(objecttype)",
         "refs/heads", "refs/remotes", "refs/tags"],
        git_dir=clone, cwd=clone, stdout_path=out, max_stdout_bytes=MAX_TIPS_BYTES, timeout=120,
    )  # fmt: skip
    if r.timed_out or (r.returncode != 0 and not r.output_limit):
        raise _fail("git for-each-ref in the clone failed")
    # past the byte limit the file holds the newest refs (sorted before printing); its last line
    # may be cut and then fails the object-id test below
    wanted: dict[str, None] = {}
    with open(out, "rb") as f:
        for raw in f:
            sha, _, kind = raw.decode("ascii", "replace").rstrip("\n").partition(" ")
            if kind == "commit" and _SHA.fullmatch(sha):
                wanted[sha] = None
                if len(wanted) >= MAX_PRESENT_TIPS * 2:
                    break
    if not wanted:
        return []
    c = _checked(
        git.run(
            ["cat-file", "--batch-check=%(objectname) %(objecttype)"],
            git_dir=mirror.path,
            cwd=mirror.path,
            stdin=("\n".join(wanted) + "\n").encode(),
            timeout=120,
        ),
        "git cat-file in the mirror",
    )
    tips = [ln.split(" ")[0] for ln in c.out.split("\n") if ln.endswith(" commit")]
    return [t for t in tips if _SHA.fullmatch(t)][:MAX_PRESENT_TIPS]


class _Network:
    """The network calls of one transfer: the mirror's config is verified before each one."""

    def __init__(self, mirror: Mirror, p: str) -> None:
        self.mirror, self.p = mirror, p
        self._auth: NetworkAuth | None = None

    def fetch(self, args: list[str], *, stdin_path: str | None = None) -> None:
        m = self.mirror
        if self._auth is None:
            self._auth = prepare_network_auth(
                m.cfg,
                mirror_url(m.fetch_url),
                environ=m.environ,
                gateway=m.gateway,
                warn=m.warn,
            )
        auth = self._auth

        def attempt() -> GitResult:
            m.check_config()
            return run_network_git(
                m.git, args, cfg=m.cfg, auth=auth, kind=NetworkKind.CLONE, tool_owned=True,
                owned_keys=OWNED_KEYS, git_dir=m.path, cwd=self.p, stdin_path=stdin_path,
                overrides=[("fetch.negotiationAlgorithm", "noop")],
            )  # fmt: skip

        result, outcome = fetch_with_retries(attempt, retries=m.cfg.remove.fetch_retries)
        err = rejected_token_error(auth, outcome_error(outcome, result))
        if err is not None:
            raise err
        if not classify_fetch(result).ok:  # outcome_error above already covers this
            raise _fail("git fetch into the mirror did not succeed")


def _mirror_has_commit(mirror: Mirror, sha: str) -> bool:
    return clone_has_commit(mirror.git, mirror.path, sha)


def transfer_base(
    mirror: Mirror,
    lease: Lease,
    *,
    common_id: fsafe.Identity,
    start: str,
    max_pack_bytes: int = MAX_PACK_BYTES,
    fs: fsafe.Fs | None = None,
) -> Transfer:
    """Copy commit ``start`` and everything reachable from it from the mirror into the clone.

    Runs under the per-clone ``lease`` (``mirror.lease_name``) and expects the mirror to have been
    refreshed. Steps (interface.md, Base commits into the clone): present tips; the range and its
    root trees; one tree fetch into the mirror (the only network step, with the token gate of
    :func:`~wtctl.credentials.run_network_git`); a non-thin pack in the mirror; indexing outside any
    repository; the no-follow install; the completeness check in the clone.

    Errors: ``fetch_failed`` or ``auth_required`` for the tree fetch; every other failure is
    ``create_failed`` (``cause: anchor_changed`` for a clone whose common dir is not the recorded
    one). Nothing outside ``<common>/objects/pack`` is ever written in the clone.
    """
    if not _SHA.fullmatch(start):
        raise ValueError("start must be a full 40-hex commit id")
    mirror._require_lease(lease)  # noqa: SLF001
    git, cfg = mirror.git, mirror.cfg
    clone = mirror.common_dir
    timeout = _local_timeout(cfg)
    mirror.ensure(lease)
    mirror.check_layout()
    open_common_dir(clone, common_id, fs=fs).close()  # before any git command reads the clone
    with private_dir() as p:
        # Already complete: no network, no write.
        if clone_has_commit(git, clone, start) and not missing_objects(
            git, clone, start, p=p, timeout=timeout
        ):
            return Transfer(start, True, None)

        net = _Network(mirror, p)
        if not _mirror_has_commit(mirror, start):
            # a commit the mirror's branches do not hold: fetched by its id (commits only, the
            # mirror's partial-clone filter applies)
            net.fetch(["fetch", "-q", "--no-tags", "--no-write-fetch-head", "--", "origin", start])
            if not _mirror_has_commit(mirror, start):
                raise WtctlError("fetch_failed", f"the remote did not supply commit {start}")
        present = _present_tips(mirror, p)

        # 2. the commits and the root trees needed (rev-list, never log: no tree is read)
        need = os.path.join(p, "need")
        # rev-list --stdin takes ^<id> for the excluded side (pack-objects --revs takes --not)
        stdin = "\n".join([start, *(f"^{t}" for t in present)]) + "\n"
        revs = "\n".join([start, *(["--not", *present] if present else [])]) + "\n"
        _checked(
            git.run(
                ["rev-list", "--stdin"], git_dir=mirror.path, cwd=mirror.path,
                stdin=stdin.encode(), stdout_path=need, max_stdout_bytes=MAX_LIST_BYTES,
                timeout=timeout,
            ),
            "git rev-list in the mirror",
        )  # fmt: skip
        commits = _sha_lines(need, what="git rev-list")
        listing = os.path.join(p, "commits")
        with open(need, "rb") as src, open(listing, "xb") as dst:
            while chunk := src.read(COPY_CHUNK):
                dst.write(chunk)
            dst.write(b"".join(f"{t}\n".encode() for t in present))
        trees = os.path.join(p, "trees")
        _checked(
            git.run(
                ["rev-list", "--no-walk=unsorted", "--format=%T", "--no-commit-header", "--stdin"],
                git_dir=mirror.path, cwd=mirror.path, stdin_path=listing, stdout_path=trees,
                max_stdout_bytes=MAX_LIST_BYTES, timeout=timeout,
            ),
            "git rev-list of root trees in the mirror",
        )  # fmt: skip
        if _sha_lines(trees, what="git rev-list of root trees") == 0:
            raise _fail("the range has no root tree")

        # 3. one round trip for full trees and blobs (noop negotiation, no filter)
        net.fetch(
            ["fetch", "-q", "--no-tags", "--no-write-fetch-head", "--no-filter", "--stdin",
             "--", "origin"],
            stdin_path=trees,
        )  # fmt: skip

        # 4. a non-thin pack of the range, in the mirror, into a file
        pack = os.path.join(p, "x.pack")
        _checked(
            git.run(
                ["pack-objects", "--revs", "--stdout", "-q"], git_dir=mirror.path, cwd=mirror.path,
                stdin=revs.encode(), stdout_path=pack, max_stdout_bytes=max_pack_bytes,
                timeout=timeout,
            ),
            "git pack-objects in the mirror",
        )  # fmt: skip

        # 5. index it outside any repository
        idx = os.path.join(p, "x.idx")
        name = index_pack(git, pack, idx, p=p, timeout=timeout)

        # 6. install (no git command touches the clone while objects are written)
        done = install_pack(
            common_dir=clone, common_id=common_id, pack=pack, idx=idx, name=name, fs=fs
        )

        # 7. every object reachable from the start commit must be in the clone now
        lacking = missing_objects(git, clone, start, p=p, timeout=timeout)
        if lacking:
            raise _fail(
                f"the clone still lacks {len(lacking)} object(s) reachable from {start} "
                "after the transfer; nothing is checked out"
            )
        return Transfer(start, False, name, done.pack_new, commits, len(present))


# --------------------------------------------------------------------------------------------
# Local-source throwaway
# --------------------------------------------------------------------------------------------


def is_shallow(git_dir: str) -> bool:
    try:
        return os.stat(os.path.join(git_dir, "shallow")).st_size > 0
    except OSError:
        return False


def check_tree_size(git: Git, git_dir: str, *, timeout: float = 600) -> None:
    """Fail closed (``create_failed``, ``details.cause: tree_too_large``) when ``HEAD``'s tree in
    ``git_dir`` has more than ``MAX_TREE_ENTRIES`` entries (files and trees: ``-t`` makes every tree
    visit an entry, so a DAG of empty trees is counted too) or its blobs sum to more than
    ``MAX_TREE_BYTES``, or when the listing itself exceeds ``MAX_TREE_LIST_BYTES``. The listing
    (``ls-tree -r -t -l -z``) goes to a file and is read in chunks, so a hostile tree never sits in
    memory; it runs before ``read-tree`` writes anything into the work tree."""
    too_large = {"cause": "tree_too_large"}
    with private_dir() as p:
        listing = os.path.join(p, "tree")
        r = git.run(
            ["ls-tree", "-r", "-t", "-l", "-z", "HEAD"], git_dir=git_dir, cwd=git_dir,
            allow_shallow=True, stdout_path=listing, max_stdout_bytes=MAX_TREE_LIST_BYTES,
            timeout=timeout,
        )  # fmt: skip
        if r.output_limit:
            raise _fail("the commit's tree is too large to check out", details=too_large)
        _checked(r, "git ls-tree in the throwaway")
        entries = total = 0
        rest = b""
        with open(listing, "rb") as f:
            while chunk := f.read(1 << 20):
                parts = (rest + chunk).split(b"\0")
                rest = parts.pop()
                for rec in parts:
                    entries += 1
                    size = rec.partition(b"\t")[0].split()[-1:]
                    total += int(size[0]) if size and size[0].isdigit() else 0
                    if entries > MAX_TREE_ENTRIES or total > MAX_TREE_BYTES:
                        raise _fail(
                            "the commit's tree is too large to check out", details=too_large
                        )


def _require_prepared(throwaway: str) -> None:
    """The target is a real directory (not a link) holding only the repository the caller
    initialized: nothing is checked out into anything else."""
    try:
        st = os.lstat(throwaway)
        if not stat.S_ISDIR(st.st_mode):
            raise _fail(f"{throwaway} is not a directory")
        if os.listdir(throwaway) != [".git"] or not stat.S_ISDIR(
            os.lstat(os.path.join(throwaway, ".git")).st_mode
        ):
            raise _fail(f"{throwaway} is not an initialized, otherwise empty directory")
    except OSError as exc:
        raise _fail(f"cannot use {throwaway}: {exc.strerror}") from None


def fill_local_throwaway(
    git: Git,
    cfg: Config,
    *,
    clone_dir: str,
    throwaway: str,
    sha: str,
    depth: int = 0,
    honor_filters: bool = False,
    max_pack_bytes: int = MAX_PACK_BYTES,
    verify: Callable[[], None],
) -> None:
    """Fill the throwaway directory ``throwaway`` with commit ``sha`` of the clone (``local``).

    ``depth`` is ``0`` (the commit's full history) or ``1`` (a shallow throwaway); any other
    value is a ``ValueError`` (the CLI reports it as usage, exit 2). The caller owns the
    throwaway's directory and repository: it created the directory, recorded its identity and ran
    ``init --template=`` with the attributes and config of interface.md "Throwaway create order"
    step 7 (``wtctl.throwaway``); this function only fills it. ``verify`` is the caller's
    re-check that the directory is still the one it created (its recorded identity); it is required
    and runs
    before every step that writes into the throwaway by path (the shallow file, the index
    install, the work tree) and once at the end. Every command that
    reads the clone runs with protocols ``none`` (the clone is never fetched from): the object
    list is written to a file by ``rev-list --objects`` (its exit status checked, so a failure can
    never become a truncated pack), packed with ``pack-objects --stdout`` and indexed with
    ``--strict`` in the throwaway. Failures are ``create_failed``; the caller removes the
    throwaway.
    """
    check = verify
    if not _SHA.fullmatch(sha):
        raise ValueError("sha must be a full 40-hex commit id")
    if depth not in (0, 1):
        raise ValueError("a local source copies the full history (0) or depth 1")
    t = os.fspath(throwaway)
    _require_prepared(t)
    timeout = _local_timeout(cfg)
    if not clone_has_commit(git, clone_dir, sha):
        raise _fail(f"the clone does not hold commit {sha}")
    if depth == 0 and is_shallow(clone_dir):
        raise _fail(
            "source repository is shallow: its full history cannot be copied "
            "(use depth 1 or another source)"
        )
    with private_dir() as p:
        listing = os.path.join(p, "list")
        walk = ["rev-list", "--objects", *(["--no-walk"] if depth == 1 else []), sha]
        _checked(
            git.run(
                walk, git_dir=clone_dir, cwd=clone_dir, allow_shallow=True, stdout_path=listing,
                max_stdout_bytes=MAX_LIST_BYTES, timeout=timeout,
            ),
            "git rev-list in the clone",
        )  # fmt: skip
        pack = os.path.join(p, "t.pack")
        _checked(
            git.run(
                ["pack-objects", "--stdout", "-q"], git_dir=clone_dir, cwd=clone_dir,
                allow_shallow=True,
                stdin_path=listing, stdout_path=pack, max_stdout_bytes=max_pack_bytes,
                timeout=timeout,
            ),
            "git pack-objects in the clone",
        )  # fmt: skip
        tgit = os.path.join(t, ".git")
        check()
        if depth == 1:  # before indexing: --strict needs it for the missing parents
            with fsafe.open_anchor(os.path.realpath(tgit)) as a:
                fd = fsafe.create_new_file(a.fd, "shallow", mode=0o644, forbid_git=False)
                try:
                    os.write(fd, f"{sha}\n".encode())
                finally:
                    os.close(fd)
        check()
        _checked(
            git.run(
                ["index-pack", "--stdin", "--strict"], git_dir=tgit, cwd=tgit, allow_shallow=True,
                stdin_path=pack, timeout=timeout,
            ),
            "git index-pack in the throwaway",
        )  # fmt: skip
        check()
        _checked(
            git.run(
                ["update-ref", "--no-deref", "HEAD", sha],
                git_dir=tgit,
                cwd=tgit,
                allow_shallow=True,
                timeout=60,
            ),
            "git update-ref in the throwaway",
        )
        check_tree_size(git, tgit, timeout=timeout)
        check()
        _checked(
            git.run(
                ["read-tree", "-u", "--reset", "HEAD"], git_dir=tgit, work_tree=t, cwd=t,
                allow_shallow=True,
                honor_filters=honor_filters, timeout=timeout,
            ),
            "git read-tree in the throwaway",
        )  # fmt: skip
        head = _checked(
            git.run(["rev-parse", "HEAD"], git_dir=tgit, cwd=tgit, allow_shallow=True, timeout=60),
            "git rev-parse in the throwaway",
        )
        check()
        if head.out.strip() != sha:
            raise _fail("the throwaway's HEAD is not the requested commit")


__all__ = [
    "MAX_PACK_BYTES",
    "Installed",
    "Transfer",
    "check_tree_size",
    "clone_has_commit",
    "fill_local_throwaway",
    "is_shallow",
    "index_pack",
    "install_pack",
    "missing_objects",
    "parse_identity",
    "transfer_base",
]
