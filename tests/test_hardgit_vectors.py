"""The vector regression suite: planted repository config, hooks and caller environment.

Real git in temp dirs, no network. Every payload only appends to ``markers/``; each test asserts
the directory is empty after a hardened command. The ``control`` tests run the same vector with
unhardened git and require the marker, so a vector that stopped being live (a git change) fails
loudly instead of passing vacuously.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.vectorlib import GIT, Hostile, build_hostile, plain_git
from wtctl.gateway import Gateway
from wtctl.hardgit import Git

pytestmark = pytest.mark.skipif(not os.path.exists(GIT), reason="needs /usr/bin/git")


@pytest.fixture
def hostile(tmp_path: Path, private_home: Path) -> Hostile:
    return build_hostile(tmp_path / "h", private_home)


@pytest.fixture
def git(private_home: Path) -> Git:
    return Git(Gateway(), GIT, str(private_home))


def _priv(h: Hostile) -> Path:
    d = h.root / "priv"
    d.mkdir(exist_ok=True)
    return d


# -- controls: each vector is live against unhardened git -------------------------------------


def _wt_add(h: Hostile) -> list[str]:
    return ["worktree", "add", "-q", "--detach", str(h.root / "wt-control"), h.c2]


CONTROLS: dict[str, Callable[[Hostile], tuple[list[str], dict[str, str]]]] = {
    "fsmonitor": lambda h: (["status"], {}),
    "hooks": lambda h: (_wt_add(h), {}),
    "reference-transaction-hook": lambda h: (["update-ref", "refs/heads/x", h.c2], {}),
    "uploadpack": lambda h: (["ls-remote", "origin"], {}),
    "file-url-uploadpack": lambda h: (["ls-remote", f"file://{h.src}"], {}),
    "insteadof-ext": lambda h: (["ls-remote", "evil:x"], {}),
    "filter-clean": lambda h: (["add", "-A"], {}),
    "external-diff": lambda h: (["diff", h.c1, h.c2], {}),
    "pager": lambda h: (["-c", "core.fsmonitor=", "-p", "status"], {}),
    "show-signature": lambda h: (["log", "-n1", h.c2], {}),
    "ssh-command": lambda h: (["ls-remote", "ssh://127.0.0.1:1/x"], {}),
    "promisor-lazy-fetch": lambda h: (["cat-file", "-p", h.b3], {}),
    "askpass": lambda h: (["credential", "fill"], {}),
}


def _touch_worktree(h: Hostile) -> None:
    (h.clone / "a.txt").write_text("edited\n")


@pytest.mark.parametrize("name", sorted(CONTROLS))
def test_control_vector_is_live(hostile: Hostile, name: str) -> None:
    _touch_worktree(hostile)
    args, env = CONTROLS[name](hostile)
    stdin = b"protocol=https\nhost=example.invalid\n\n" if name == "askpass" else None
    plain_git(args, cwd=hostile.clone, home=hostile.home, env=env, stdin=stdin)
    assert hostile.hits(), f"vector {name} no longer fires; the hardened tests prove nothing"


# -- hardened: the command matrix wtctl runs ---------------------------------------------------

# (id, args builder, needs_worktree, stdin)
Spec = tuple[str, Callable[[Hostile], list[str]], bool, Callable[[Hostile], bytes | None]]


def _none(h: Hostile) -> bytes | None:
    return None


def _spec(name: str, args: Callable[[Hostile], list[str]], wt: bool = False, stdin=_none) -> Spec:
    return (name, args, wt, stdin)


MATRIX: list[Spec] = [
    _spec("rev-parse-head", lambda h: ["rev-parse", "HEAD"]),
    _spec("rev-parse-common-dir", lambda h: ["rev-parse", "--git-common-dir"]),
    _spec("rev-parse-tree", lambda h: ["rev-parse", f"{h.c2}^{{tree}}"]),
    _spec("cat-file-e-missing", lambda h: ["cat-file", "-e", h.b3]),
    _spec("cat-file-p-missing", lambda h: ["cat-file", "-p", h.b3]),
    _spec("cat-file-t-commit", lambda h: ["cat-file", "-t", h.c2]),
    _spec(
        "cat-file-batch-check",
        lambda h: ["cat-file", "--batch-check"],
        stdin=lambda h: f"{h.c2}\n{h.b3}\n".encode(),
    ),
    _spec("rev-list-missing", lambda h: ["rev-list", "--objects", "--missing=print", "lazy"]),
    _spec("rev-list-all", lambda h: ["rev-list", "--all"]),
    _spec("merge-base-is-ancestor", lambda h: ["merge-base", "--is-ancestor", h.c1, h.c2]),
    _spec("for-each-ref", lambda h: ["for-each-ref", "--format=%(objectname)"]),
    _spec("show-ref", lambda h: ["show-ref"]),
    _spec("symbolic-ref", lambda h: ["symbolic-ref", "HEAD"]),
    _spec("branch-list", lambda h: ["branch", "--list"]),
    _spec("config-list", lambda h: ["config", "--list"]),
    _spec("config-get", lambda h: ["config", "--get", "remote.origin.url"]),
    _spec("update-ref", lambda h: ["update-ref", "refs/heads/probe", h.c2]),
    _spec("update-ref-d", lambda h: ["update-ref", "-d", "refs/heads/lazy", h.c3]),
    _spec("ls-tree", lambda h: ["ls-tree", "-r", h.c2]),
    _spec("log-p", lambda h: ["log", "-p", "-n2", h.c2]),
    _spec("log-signature", lambda h: ["log", "--show-signature", "-n1", h.c2]),
    _spec("show", lambda h: ["show", h.c2]),
    _spec("diff-commits", lambda h: ["diff", h.c1, h.c2]),
    _spec("diff-tree-p", lambda h: ["diff-tree", "-p", h.c1, h.c2]),
    _spec(
        "pack-objects",
        lambda h: ["pack-objects", "--revs", "--stdout", "-q"],
        stdin=lambda h: f"{h.c3}\n".encode(),
    ),
    _spec("hash-object", lambda h: ["hash-object", "--stdin"], stdin=lambda h: b"x"),
    _spec("check-ref-format", lambda h: ["check-ref-format", "--branch", "x"]),
    _spec("remote-get-url", lambda h: ["remote", "get-url", "origin"]),
    _spec("ls-remote-get-url", lambda h: ["ls-remote", "--get-url", "evil:x"]),
    _spec("count-objects", lambda h: ["count-objects"]),
    _spec("status", lambda h: ["status", "--porcelain=v2", "-z"], True),
    _spec(
        "status-ignored",
        lambda h: [
            "status",
            "--porcelain=v2",
            "-z",
            "--ignored=traditional",
            "--untracked-files=all",
        ],
        True,
    ),
    _spec("ls-files-v", lambda h: ["ls-files", "-v", "-z"], True),
    _spec("diff-worktree", lambda h: ["diff"], True),
    _spec("diff-cached", lambda h: ["diff", "--cached"], True),
    _spec("diff-index", lambda h: ["diff-index", "-p", "HEAD"], True),
    _spec("diff-files", lambda h: ["diff-files", "-p"], True),
    _spec("update-index-refresh", lambda h: ["update-index", "--refresh"], True),
    _spec("add-all", lambda h: ["add", "-A"], True),
    _spec("write-tree", lambda h: ["write-tree"], True),
    _spec("read-tree", lambda h: ["read-tree", "-u", "--reset", "HEAD"], True),
    _spec("worktree-list", lambda h: ["worktree", "list", "--porcelain"], True),
    _spec(
        "worktree-add-new",
        lambda h: ["worktree", "add", "-b", "probe2", "--no-track", str(h.root / "wt-new"), h.c2],
        True,
    ),
    _spec(
        "worktree-add-detach",
        lambda h: ["worktree", "add", "--detach", str(h.root / "wt-det"), h.c2],
        True,
    ),
    _spec(
        "worktree-add-lazy-commit",
        lambda h: ["worktree", "add", "--detach", str(h.root / "wt-lazy"), h.c3],
        True,
    ),
    # Commands that would contact a remote run with protocols none against the clone.
    _spec("ls-remote-name", lambda h: ["ls-remote", "origin"]),
    _spec("ls-remote-file-url", lambda h: ["ls-remote", f"file://{h.src}"]),
    _spec("ls-remote-plain-path", lambda h: ["ls-remote", str(h.src)]),
    _spec("ls-remote-ext", lambda h: ["ls-remote", "evil:x"]),
    _spec("ls-remote-ssh", lambda h: ["ls-remote", "ssh://127.0.0.1:1/x"]),
    _spec("fetch-origin", lambda h: ["fetch", "origin"]),
    _spec("fetch-promo", lambda h: ["fetch", "promo"]),
    _spec("fetch-file-url", lambda h: ["fetch", f"file://{h.src}", "+refs/heads/*:refs/x/*"]),
    _spec("push-origin", lambda h: ["push", "--no-verify", "origin", "main"]),
]


def _modify(h: Hostile) -> None:
    (h.clone / "a.txt").write_text("edited\n")
    (h.clone / "new.txt").write_text("untracked\n")


def _kwargs(h: Hostile, wt: bool) -> dict[str, object]:
    if wt:
        return {"git_dir": h.git_dir, "work_tree": h.clone}
    return {"git_dir": h.git_dir, "cwd": h.clone}


@pytest.mark.parametrize("spec", MATRIX, ids=[s[0] for s in MATRIX])
def test_hostile_clone_runs_nothing(hostile: Hostile, git: Git, spec: Spec) -> None:
    _name, args, wt, stdin = spec
    _modify(hostile)
    git.run(args(hostile), stdin=stdin(hostile), **_kwargs(hostile, wt))  # type: ignore[arg-type]
    assert hostile.hits() == []


@pytest.mark.parametrize("scheme", ["file"])
def test_network_commands_in_a_clean_repository_ignore_the_clones_config(
    hostile: Hostile, git: Git, scheme: str
) -> None:
    # A network command runs in a tool-owned repository, never the clone: its config is the
    # tool's own, so the clone's planted remote.<file-url>.uploadpack does not apply.
    mirror = hostile.root / "mirror.git"
    plain_git(["init", "-q", "--bare", str(mirror)], cwd=hostile.root, home=hostile.home)
    r = git.run(
        ["ls-remote", f"file://{hostile.src}"],
        git_dir=mirror,
        cwd=_priv(hostile),
        scheme=scheme,
        timeout=30,
    )
    assert r.returncode == 0 and r.out.strip()
    assert hostile.hits() == []


def test_plain_path_remote_ignores_slash_named_remote_config(hostile: Hostile, git: Git) -> None:
    # Even when a network call is allowed the 'file' scheme in the hostile clone, the planted
    # remote.<abs path>.uploadpack never applies (git ignores remote names that begin with '/'),
    # which is why local repositories are addressed by plain path and never as file:// URLs.
    git.run(
        ["ls-remote", str(hostile.src)],
        git_dir=hostile.git_dir,
        cwd=hostile.clone,
        scheme="file",
        timeout=30,
    )
    assert "hit" not in hostile.hits()


def test_file_url_is_the_dangerous_form_when_the_protocol_is_allowed(
    hostile: Hostile, git: Git
) -> None:
    # Documents why: with the file protocol allowed, the URL form runs the planted uploadpack.
    # wtctl never does this against a clone (protocols none); this pins the reason for the rule.
    plain_git(["ls-remote", f"file://{hostile.src}"], cwd=hostile.clone, home=hostile.home)
    assert hostile.hits() == ["hit"]


def test_protocols_none_blocks_the_lazy_fetch_without_the_env_flag(
    hostile: Hostile, git: Git
) -> None:
    # GIT_NO_LAZY_FETCH is honored only from git 2.45.1; protocols none must hold on its own.
    cmd = git.build(["cat-file", "-p", hostile.b3], git_dir=hostile.git_dir, cwd=hostile.clone)
    env = dict(cmd.env, GIT_NO_LAZY_FETCH="0")
    git.gateway.git(cmd.argv, env=env, cwd=cmd.cwd, timeout=30)
    assert hostile.hits() == []


def test_without_protocols_none_the_promisor_would_run(hostile: Hostile, git: Git) -> None:
    # The same command with the protocol allowlist opened (and lazy fetch not blocked): control
    # that the previous test is not vacuous under hardened flags.
    cmd = git.build(["cat-file", "-p", hostile.b3], git_dir=hostile.git_dir, cwd=hostile.clone)
    env = dict(cmd.env, GIT_NO_LAZY_FETCH="0", GIT_ALLOW_PROTOCOL="file")
    git.gateway.git(cmd.argv, env=env, cwd=cmd.cwd, timeout=30)
    assert hostile.hits() == ["hit"]


def test_ssh_scheme_uses_batch_mode_not_the_planted_command(hostile: Hostile, git: Git) -> None:
    mirror = hostile.root / "mirror.git"
    plain_git(["init", "-q", "--bare", str(mirror)], cwd=hostile.root, home=hostile.home)
    r = git.run(
        ["ls-remote", "ssh://127.0.0.1:1/x"],
        git_dir=mirror,
        cwd=_priv(hostile),
        scheme="ssh",
        timeout=30,
    )
    assert r.returncode != 0
    # And in the hostile clone itself the planted core.sshCommand is overridden.
    git.run(
        ["ls-remote", "ssh://127.0.0.1:1/x"],
        git_dir=hostile.git_dir,
        cwd=hostile.clone,
        scheme="ssh",
        timeout=30,
    )
    assert hostile.hits() == []


# -- caller environment ------------------------------------------------------------------------


def _env_vectors(h: Hostile) -> dict[str, str]:
    p = str(h.payload)
    cfg = h.root / "evil-global.cfg"
    cfg.write_text(f"[core]\n\tfsmonitor = {p}\n[diff]\n\texternal = {p}\n")
    xdg = h.root / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "config").write_text(cfg.read_text())
    execdir = h.root / "execdir"
    execdir.mkdir()
    shutil.copy(h.payload, execdir / "git-upload-pack")
    return {
        "GIT_EXTERNAL_DIFF": p,
        "GIT_SSH_COMMAND": p,
        "GIT_SSH": p,
        "GIT_PROXY_COMMAND": p,
        "GIT_ASKPASS": p,
        "SSH_ASKPASS": p,
        "GIT_PAGER": p,
        "PAGER": p,
        "GIT_EDITOR": p,
        "EDITOR": p,
        "VISUAL": p,
        "GIT_EXEC_PATH": str(execdir),
        "GIT_CONFIG_PARAMETERS": f"'core.fsmonitor={p}'",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": p,
        "GIT_CONFIG_GLOBAL": str(cfg),
        "GIT_CONFIG_SYSTEM": str(cfg),
        "XDG_CONFIG_HOME": str(xdg),
        "GIT_TRACE": str(h.markers / "trace"),
        "GIT_TRACE_PACKET": str(h.markers / "trace-packet"),
        "GIT_TRACE_SETUP": str(h.markers / "trace-setup"),
        "GIT_TRACE2": str(h.markers / "trace2"),
        "GIT_TRACE2_EVENT": str(h.markers / "trace2-event"),
        "GIT_DIR": str(h.root / "elsewhere.git"),
        "GIT_WORK_TREE": str(h.root / "elsewhere"),
        "GIT_INDEX_FILE": str(h.root / "elsewhere-index"),
        "GIT_OBJECT_DIRECTORY": str(h.root / "elsewhere-objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(h.root / "elsewhere-objects"),
        "GIT_TEMPLATE_DIR": str(h.root / "elsewhere-template"),
        "GIT_NAMESPACE": "evil",
        "GIT_ALLOW_PROTOCOL": "file:ext",
        "GIT_PROTOCOL_FROM_USER": "1",
        "GIT_REPLACE_REF_BASE": "refs/evil/",
    }


ENV_COMMANDS: list[tuple[list[str], bool]] = [
    (["status", "--porcelain=v2", "-z"], True),
    (["diff"], True),
    (["rev-parse", "HEAD"], False),
    (["log", "-p", "-n2"], False),
    (["ls-remote", "origin"], False),
    (["ls-remote", "ssh://127.0.0.1:1/x"], False),
    (["worktree", "list", "--porcelain"], True),
    (["cat-file", "-p", "HEAD"], False),
]


def test_control_caller_environment_is_live(
    hostile: Hostile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modify(hostile)
    env = _env_vectors(hostile)
    plain_git(["status"], cwd=hostile.clone, home=hostile.home, env=env)
    assert hostile.hits()


@pytest.mark.parametrize("args,wt", ENV_COMMANDS, ids=[" ".join(a[:2]) for a, _ in ENV_COMMANDS])
def test_caller_environment_never_reaches_git(
    hostile: Hostile, git: Git, monkeypatch: pytest.MonkeyPatch, args: list[str], wt: bool
) -> None:
    _modify(hostile)
    vectors = _env_vectors(hostile)
    for k, v in vectors.items():
        monkeypatch.setenv(k, v)
    git.run(args, **_kwargs(hostile, wt))  # type: ignore[arg-type]
    assert hostile.hits() == []


# -- pointer and worktree redirection ---------------------------------------------------------


def test_core_worktree_in_config_does_not_redirect_an_explicit_work_tree(
    hostile: Hostile, git: Git
) -> None:
    other = hostile.root / "other-tree"
    other.mkdir()
    (other / "decoy.txt").write_text("decoy\n")
    with open(hostile.git_dir / "config", "a") as f:
        f.write(f"[core]\n\tworktree = {other}\n")
    r = git.run(
        ["status", "--porcelain=v2", "-z", "--untracked-files=all"],
        git_dir=hostile.git_dir,
        work_tree=hostile.clone,
    )
    assert b"decoy.txt" not in r.stdout
    assert hostile.hits() == []


# -- extra headers and filters -----------------------------------------------------------------


def test_repository_extra_headers_are_blanked_by_exact_name(hostile: Hostile, git: Git) -> None:
    cmd = git.build(
        ["config", "--get-urlmatch", "http.extraheader", "https://x.example.invalid/"],
        git_dir=hostile.git_dir,
        cwd=hostile.clone,
    )
    r = git.gateway.git(cmd.argv, env=cmd.env, cwd=cmd.cwd, timeout=30)
    assert "X-Leak" not in r.out
    # Control: the blanket unscoped blank alone does not clear the scoped and wildcard keys.
    plain = plain_git(
        [
            "-c",
            "http.extraHeader=",
            "config",
            "--get-urlmatch",
            "http.extraheader",
            "https://x.example.invalid/",
        ],
        cwd=hostile.clone,
        home=hostile.home,
    )
    assert b"X-Leak" in plain.stdout


def test_extra_config_comes_after_the_blanking(hostile: Hostile, git: Git) -> None:
    token = ("http.https://example.invalid/.extraHeader", "Authorization: Basic abc")
    cmd = git.build(
        ["config", "--get-urlmatch", "http.extraheader", "https://example.invalid/"],
        git_dir=hostile.git_dir,
        cwd=hostile.clone,
        extra_config=[token],
    )
    r = git.gateway.git(cmd.argv, env=cmd.env, cwd=cmd.cwd, timeout=30)
    assert r.out.strip() == "Authorization: Basic abc"
    # The token is never in argv.
    assert not any("Basic" in a for a in cmd.argv)


def test_planted_filter_cannot_run_even_when_required(hostile: Hostile, git: Git) -> None:
    _modify(hostile)
    r = git.run(["add", "-A"], git_dir=hostile.git_dir, work_tree=hostile.clone)
    assert hostile.hits() == []
    assert r.returncode == 0, r.err  # required=false after blanking: no failure either


def test_honored_driver_uses_the_global_definition_not_the_repository_one(
    hostile: Hostile, private_home: Path
) -> None:
    from wtctl.hardgit import load_filter_policy

    tool = hostile.root / "bin" / "lfs-ish"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\ncat\n")
    tool.chmod(0o755)
    (private_home / ".gitconfig").write_text(
        f'[filter "evil"]\n\tprocess = {tool} filter-process\n\tsmudge = {tool}\n'
        "\trequired = true\n"
    )
    policy = load_filter_policy(
        Gateway(),
        GIT,
        home=str(private_home),
        xdg_config_home=None,
        allow=["evil", "missing"],
        path_env="/usr/bin:/bin",
        private_dir=str(_priv(hostile)),
    )
    assert set(policy.drivers) == {"evil"}
    assert any("missing" in w for w in policy.warnings)
    g = Git(Gateway(), GIT, str(private_home), policy)
    cmd = g.build(
        ["config", "--get", "filter.evil.process"],
        git_dir=hostile.git_dir,
        cwd=hostile.clone,
        honor_filters=True,
    )
    r = g.gateway.git(cmd.argv, env=cmd.env, cwd=cmd.cwd, timeout=30)
    assert r.out.strip() == f"{tool} filter-process"
    # Not honored (a throwaway or any non-create command): blanked even though allowed.
    cmd = g.build(
        ["config", "--get", "filter.evil.process"], git_dir=hostile.git_dir, cwd=hostile.clone
    )
    r = g.gateway.git(cmd.argv, env=cmd.env, cwd=cmd.cwd, timeout=30)
    assert r.out.strip() == ""
    # The honored driver's directory is on PATH only when honored.
    assert (
        str(tool.parent)
        in g.build(["status"], git_dir=hostile.git_dir, cwd=hostile.clone, honor_filters=True).env[
            "PATH"
        ]
    )
    assert (
        str(tool.parent)
        not in g.build(["status"], git_dir=hostile.git_dir, cwd=hostile.clone).env["PATH"]
    )
    assert hostile.hits() == []
