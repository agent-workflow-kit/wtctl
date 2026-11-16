"""The live scenarios: plain functions over a :class:`LiveWorld`.

The ``test_live_*`` modules call these with a world configured for a real forge; the offline unit
tests (``tests/test_live_scenarios_offline.py``) call the same functions with a world whose remote
is a local bare repository, so the logic of every scenario is exercised in CI without a network.
A scenario raises ``AssertionError`` with scrubbed, bounded output and returns what the test may
record.
"""

from __future__ import annotations

import contextlib
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tests.live.harness import FRESH_KEYS, LiveWorld, Out

SHA_RE = re.compile(r"[0-9a-f]{40}")
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"
MAX_SCAN_BYTES = 8 * 1024 * 1024


def ok(out: Out, what: str) -> Out:
    """Fail with the scrubbed output (cut to a bounded size) unless the command exited 0."""
    if out.code != 0 or out.timed_out:
        raise AssertionError(f"{what}: {out.describe()[:3000]}")
    return out


def error_code(out: Out) -> str | None:
    try:
        return out.error().get("code")
    except ValueError:
        return None


def head_of(record: dict[str, Any]) -> str:
    """The sha a detached ``.git/HEAD`` holds."""
    return (Path(record["path"]) / ".git" / "HEAD").read_text().strip()


def create_throwaway(
    w: LiveWorld, sha: str, *, depth: int | None, source: str = "remote", timeout: float = 900.0
) -> Out:
    argv = ["create", "--throwaway", "--repo", w.repo_id, "--ref", sha, "--source", source]
    if depth is not None:
        argv += ["--depth", str(depth)]
    return w.wtctl(*argv, timeout=timeout)


@contextlib.contextmanager
def throwaway(
    w: LiveWorld, sha: str, *, depth: int | None, source: str = "remote"
) -> Iterator[dict[str, Any]]:
    """A throwaway that is removed on the way out, whatever happens inside."""
    out = ok(create_throwaway(w, sha, depth=depth, source=source), f"create throwaway at {sha[:8]}")
    rec = out.json()["record"]
    try:
        yield rec
    except BaseException:
        w.wtctl("remove", rec["id"], "--yes", timeout=300)
        raise
    ok(w.wtctl("remove", rec["id"], "--yes", timeout=300), "remove throwaway")


# -- anonymous fetches -----------------------------------------------------------------------


def tip_by_sha(w: LiveWorld, depth: int = 1) -> str:
    """Fetch the advertised tip of the default branch by sha, as a consumer pinning a commit."""
    tip = w.tip("HEAD")
    with throwaway(w, tip, depth=depth) as rec:
        assert head_of(rec) == tip, "the throwaway's HEAD is not the requested sha"
    return tip


def unadvertised_sha(w: LiveWorld, skip: int = 5) -> str:
    """A commit that is no branch tip, fetched by sha at depth 1 and depth 50.

    The commit is found in a shallow throwaway of the tip (``skip`` commits back), then asked for
    from the forge by full sha: servers differ in whether they serve a sha they do not advertise.
    """
    tip = w.tip("HEAD")
    with throwaway(w, tip, depth=skip + 5) as rec:
        path = Path(rec["path"])
        res = w.git.run(
            ["rev-list", "--max-count=1", f"--skip={skip}", "HEAD"],
            git_dir=path / ".git",
            work_tree=path,
            cwd=path,
            allow_shallow=True,
            timeout=60,
        )
        old = res.out.strip()
    assert SHA_RE.fullmatch(old), f"the public repository needs more than {skip + 1} commits"
    assert old != tip
    for depth in (1, 50):
        with throwaway(w, old, depth=depth) as rec:
            assert head_of(rec) == old, f"depth {depth}: HEAD is not the requested old sha"
    return old


def full_history_strict(w: LiveWorld) -> str:
    """Full history (``--depth 0``) with ``fetch.fsckObjects=true``: legitimate old history must
    not be refused by git's fsck."""
    tip = w.tip("HEAD")
    with throwaway(w, tip, depth=0) as rec:
        assert head_of(rec) == tip
        assert not (Path(rec["path"]) / ".git" / "shallow").exists(), "a full fetch is shallow"
        assert rec["depth"] == 0
    return tip


def fetch_sha(w: LiveWorld, sha: str, *, depth: int = 1) -> None:
    """An exact sha (a fork's pull or merge request head) fetched without any ref."""
    with throwaway(w, sha, depth=depth) as rec:
        assert head_of(rec) == sha


def observe_sha(w: LiveWorld, sha: str) -> str:
    """The outcome of asking for a sha that is in no branch, PR or MR head: ``fetched`` or
    ``ref_not_found``. Anything else is a failure."""
    out = create_throwaway(w, sha, depth=1)
    if out.code == 0:
        w.wtctl("remove", out.json()["record"]["id"], "--yes", timeout=300)
        return "fetched"
    if out.code == 12 and error_code(out) == "ref_not_found":
        return "ref_not_found"
    raise AssertionError(f"unexpected outcome: {out.describe()[:2000]}")


def ssh_batchmode(w: LiveWorld, sha: str, expect: str | None = None) -> str:
    """A fetch over ssh never prompts: with no usable key it fails fast as ``auth_required``.

    The child has no agent socket and a private HOME; ssh itself reads keys and ``known_hosts``
    from the passwd home, so a maintainer with an unlocked key may see ``ok``.
    """
    out = create_throwaway(w, sha, depth=1, timeout=180.0)
    assert not out.timed_out, f"the ssh fetch hung (a prompt?): {out.describe()[:1500]}"
    if out.code == 0:
        w.wtctl("remove", out.json()["record"]["id"], "--yes", timeout=300)
        outcome = "ok"
    elif out.code == 12 and error_code(out) == "auth_required":
        outcome = "auth_required"
    else:
        raise AssertionError(f"unexpected ssh outcome: {out.describe()[:2000]}")
    if expect:
        assert outcome == expect, f"expected {expect}, got {outcome}"
    return outcome


def redirect_outcome(w: LiveWorld, sha: str) -> str:
    """Fetch through a URL the forge redirects (no ``.git`` suffix): with ``http.followRedirects``
    off wtctl must either fetch directly or fail; it must not hang and never leak a token."""
    out = create_throwaway(w, sha, depth=1, timeout=300.0)
    assert not out.timed_out, out.describe()[:1500]
    if out.code == 0:
        w.wtctl("remove", out.json()["record"]["id"], "--yes", timeout=300)
        return "fetched"
    assert out.code == 12, out.describe()[:2000]
    return f"refused:{error_code(out)}"


# -- tokens ----------------------------------------------------------------------------------


def assert_secret_absent(w: LiveWorld) -> None:
    """No credential value is in any file wtctl or git wrote under the world (mirror and clone
    config, ``FETCH_HEAD``, state, logs)."""
    needles = [s.encode() for s in w.secrets]
    if not needles:
        return
    for path in w.base.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_SCAN_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        for needle in needles:
            assert needle not in data, f"a credential was written to {path.relative_to(w.base)}"


def token_fetch(w: LiveWorld, *, depth: int = 1) -> str:
    """The token reaches the private repository (``token_env`` or ``token_command``, whatever the
    world configures) and is stored nowhere."""
    tip = w.tip("HEAD")
    with throwaway(w, tip, depth=depth) as rec:
        assert head_of(rec) == tip
    assert_secret_absent(w)
    return tip


def no_token_is_refused(w: LiveWorld, sha: str) -> None:
    out = create_throwaway(w, sha, depth=1, timeout=300.0)
    assert out.code == 12 and error_code(out) == "auth_required", out.describe()[:2000]


def bad_token_message(w: LiveWorld, sha: str) -> str:
    """401 after a header was sent: ``auth_required`` naming the rejected token, not its
    absence, and no anonymous fallback."""
    out = create_throwaway(w, sha, depth=1, timeout=300.0)
    assert out.code == 12 and error_code(out) == "auth_required", out.describe()[:2000]
    message = out.error().get("message", "")
    assert "rejected the configured token" in message, message[:500]
    return message


# -- mirror, default branch, local strict transfer -----------------------------------------


def mirror_flow(w: LiveWorld) -> dict[str, str]:
    """Clone, a first (treeless) mirror refresh through ``create``, default-branch detection,
    an incremental refresh, the pushed check at removal, and a strict local full-history
    throwaway from the clone."""
    default = w.default_branch()
    cloned = ok(w.wtctl("clone", w.repo_id), "clone").json()
    assert cloned["cloned"] is True
    ids: list[str] = []
    try:
        first = ok(
            w.wtctl("create", "--repo", w.repo_id, "--name", "live-one", "--no-bootstrap"),
            "create (first mirror refresh)",
        ).json()["record"]
        ids.append(first["id"])
        assert first["base"] == f"refs/heads/{default}", (first["base"], default)
        assert SHA_RE.fullmatch(first["base_sha"])
        located = ok(w.wtctl("locate", w.repo_id), "locate").json()
        assert located["default_branch"] == default, located["default_branch"]
        second = ok(
            w.wtctl("create", "--repo", w.repo_id, "--name", "live-two", "--no-bootstrap"),
            "create (incremental refresh)",
        ).json()["record"]
        ids.append(second["id"])
        status = ok(w.wtctl("status", first["id"]), "status").json()
        assert status["checks"]["unpushed_head"]["status"] == "pass", status["checks"]
        assert status["would_remove"] is True, status
        with throwaway(w, first["base_sha"], depth=0, source="local") as rec:
            assert head_of(rec) == first["base_sha"]
    finally:
        for rid in ids:
            w.wtctl("remove", rid, "--yes", timeout=300)
    assert_secret_absent(w)
    return {"default_branch": default, "base_sha": first["base_sha"]}


# -- push ------------------------------------------------------------------------------------


def safe_branch(prefix: str, suffix: str, default_branch: str) -> str:
    """A disposable branch name: never empty-prefixed, never the default branch."""
    if not prefix or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", prefix):
        raise ValueError("the branch prefix must be a non-empty plain name")
    if not re.fullmatch(r"[0-9a-f]{8,32}", suffix):
        raise ValueError("the branch suffix must be random hex")
    name = f"{prefix}{suffix}"
    if name == default_branch or name.endswith(".lock") or ".." in name:
        raise ValueError("refusing a branch name that could be a real branch")
    return name


def _porcelain(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln and ln[0] in " +-*=!"]


def push_fast_forward(w: LiveWorld, depth: int, prefix: str) -> str:
    """Create a disposable branch from a shallow throwaway, then fast-forward it from the same
    shallow throwaway; the branch is deleted in ``finally``. Never forces, never touches the
    default branch."""
    default = w.default_branch()
    branch = safe_branch(prefix, secrets.token_hex(6), default)
    ref = f"refs/heads/{branch}"
    url = w.fetch_url
    base_tip = w.tip(f"refs/heads/{default}")
    try:
        with throwaway(w, base_tip, depth=depth) as rec:
            path = Path(rec["path"])
            gd = path / ".git"
            owned = FRESH_KEYS | {
                "core.logallrefupdates", "core.symlinks", "core.ignorecase",
                "core.precomposeunicode", "fetch.fsckobjects", "user.name", "user.email",
            }  # fmt: skip

            def run(args: list[str]) -> str:
                res = w.git.run(
                    args, git_dir=gd, work_tree=path, cwd=path, allow_shallow=True, timeout=120
                )
                if res.returncode != 0:
                    raise AssertionError(f"git {args[0]}: {w.scrub(res.err)[:800]}")
                return res.out.strip()

            run(["config", "--local", "user.name", "Live Test"])
            run(["config", "--local", "user.email", "live@example.invalid"])
            for step in (1, 2):
                (path / f"{branch.replace('/', '_')}-{step}.txt").write_text(f"step {step}\n")
                run(["add", "-A", "--", "."])
                run(["commit", "-q", "-m", f"live test {step}"])
                head = run(["rev-parse", "HEAD"])
                res = w.net_git(
                    ["push", "--porcelain", "--", url, f"HEAD:{ref}"],
                    url=url, git_dir=gd, work_tree=path, owned_keys=owned,
                )  # fmt: skip
                assert res.returncode == 0, f"push step {step}: {res.err[:1500]}"
                assert not any(ln.startswith("!") for ln in _porcelain(res.out)), res.out[:800]
                assert w.tip(ref) == head, f"step {step}: the remote branch is not at the pushed commit"
    finally:
        # The deletion needs no objects: a bare empty repository is enough, and it works even
        # when the throwaway could not be created.
        w.net_git(["push", "--porcelain", "--", url, f":{ref}"], url=url, git_dir=w.scratch())
    assert [r for r in w.ls_remote(ref) if r[1] == ref] == [], "the disposable branch is still there"
    return branch


# -- git-lfs ---------------------------------------------------------------------------------

LFS_PLANTED_KEYS = (
    ("lfs.url", "https://127.0.0.1:9/planted"),
    ("lfs.customtransfer.planted.path", "/bin/false"),
)


def install_lfs_driver(w: LiveWorld) -> None:
    """Define ``filter.lfs.*`` in the world's private global git config (what ``git lfs
    install`` writes), the only place wtctl reads an honored driver from."""
    ok(w.plain([w.git_bin, "lfs", "install", "--skip-repo"]), "git lfs install")


def lfs_content(w: LiveWorld, sha: str, rel_path: str) -> tuple[bytes, bytes]:
    """The file at ``rel_path`` in a worktree of ``sha`` with git-lfs honored, and in one with
    no honored driver."""
    got: list[bytes] = []
    for filters, name in ((["lfs"], "live-lfs-on"), ([], "live-lfs-off")):
        w.allow_filters = filters
        rec = ok(
            w.wtctl(
                "create", "--repo", w.repo_id, "--detach", "--ref", sha, "--name", name,
                "--no-bootstrap",
            ),
            f"create worktree ({name})",
        ).json()["record"]  # fmt: skip
        try:
            got.append((Path(rec["path"]) / rel_path).read_bytes())
        finally:
            w.wtctl("remove", rec["id"], "--yes", "--force", timeout=300)
    return got[0], got[1]


def lfs_planted_key_refused(w: LiveWorld, sha: str, key: str, value: str) -> None:
    """A transfer-redirecting key planted in the clone's config is refused when git-lfs is
    honored."""
    clone = Path(ok(w.wtctl("locate", w.repo_id), "locate").json()["clone_path"])
    w.allow_filters = ["lfs"]
    ok(w.plain([w.git_bin, "-C", str(clone), "config", "--local", key, value]), "plant key")
    try:
        out = w.wtctl(
            "create", "--repo", w.repo_id, "--detach", "--ref", sha, "--name", "live-planted",
            "--no-bootstrap",
        )  # fmt: skip
        assert out.code == 12 and error_code(out) == "unsafe_repo_config", out.describe()[:2000]
        assert key.lower() in [k.lower() for k in out.error().get("details", {}).get("keys", [])]
    finally:
        w.plain([w.git_bin, "-C", str(clone), "config", "--local", "--unset-all", key])
