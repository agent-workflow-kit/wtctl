"""The vector regression suite: planted repository config, hooks and caller environment.

Real git in temp dirs, no network. Every payload only appends to ``markers/``; each test asserts
the directory is empty after a hardened command. The ``control`` tests run the same vector with
unhardened git and require the marker, so a vector that stopped being live (a git change) fails
loudly instead of passing vacuously.
"""

from __future__ import annotations

import http.server
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

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
    # Run on a pseudo-terminal (git pages only for a terminal), `log` at the unsigned c1 and no
    # hooks: only the pager can write a payload line. (The earlier `-p status` on a pipe never
    # started a pager; its marker came from the post-index-change hook, and only sometimes.)
    "pager": lambda h: (["-c", "core.hooksPath=/dev/null", "-p", "log", "-n1", h.c1], {}),
    "show-signature": lambda h: (["log", "-n1", h.c2], {}),
    "ssh-command": lambda h: (["ls-remote", "ssh://127.0.0.1:1/x"], {}),
    "promisor-lazy-fetch": lambda h: (["cat-file", "-p", h.b3], {}),
    "askpass": lambda h: (["credential", "fill"], {}),
    "worktree-add-lazy-commit": lambda h: (
        ["worktree", "add", "--detach", str(h.root / "wt-lazy-control"), h.c3],
        {},
    ),
    "update-index": lambda h: (["update-index", "--refresh"], {}),
    "read-tree": lambda h: (["read-tree", "-u", "--reset", "HEAD"], {}),
    "fetch-promo": lambda h: (["fetch", "promo"], {}),
}

# Rows whose control must show a line written by the payload itself: the hostile repository's
# hooks write to the same marker file and could make a control look live by themselves.
PAYLOAD_CONTROLS = {
    "pager",
    "worktree-add-lazy-commit",
    "update-index",
    "read-tree",
    "fetch-promo",
}


def _touch_worktree(h: Hostile) -> None:
    (h.clone / "a.txt").write_text("edited\n")


@pytest.mark.parametrize("name", sorted(CONTROLS))
def test_control_vector_is_live(hostile: Hostile, name: str) -> None:
    _touch_worktree(hostile)
    args, env = CONTROLS[name](hostile)
    stdin = b"protocol=https\nhost=example.invalid\n\n" if name == "askpass" else None
    plain_git(args, cwd=hostile.clone, home=hostile.home, env=env, stdin=stdin, tty=name == "pager")
    assert hostile.hits(), f"vector {name} no longer fires; the hardened tests prove nothing"
    if name in PAYLOAD_CONTROLS:
        assert hostile.payload_hits(), f"vector {name} fires only hooks, not the payload"


# -- hardened: the command matrix wtctl runs ---------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """One matrix row: the command, and what proves it actually ran (not just stayed quiet).

    ``rc`` lists the accepted exit statuses (a refusal such as a blocked transport is a status
    too); ``err`` and ``out`` name a substring the command's stderr or stdout must contain.
    """

    name: str
    args: Callable[[Hostile], list[str]]
    wt: bool = False
    stdin: Callable[[Hostile], bytes | None] = lambda h: None
    rc: tuple[int, ...] = (0,)
    err: str | None = None
    out: Callable[[Hostile], str] | None = None


NOT_ALLOWED = "not allowed"  # fatal: transport 'file' not allowed (GIT_ALLOW_PROTOCOL=none)
LAZY_OFF = "lazy fetching disabled"

MATRIX: list[Spec] = [
    Spec("rev-parse-head", lambda h: ["rev-parse", "HEAD"], out=lambda h: h.c2),
    Spec("rev-parse-common-dir", lambda h: ["rev-parse", "--git-common-dir"], out=lambda h: ".git"),
    Spec("rev-parse-tree", lambda h: ["rev-parse", f"{h.c2}^{{tree}}"], out=lambda h: ""),
    Spec("cat-file-e-missing", lambda h: ["cat-file", "-e", h.b3], rc=(1,), err=LAZY_OFF),
    Spec("cat-file-p-missing", lambda h: ["cat-file", "-p", h.b3], rc=(128,), err=LAZY_OFF),
    Spec("cat-file-t-commit", lambda h: ["cat-file", "-t", h.c2], out=lambda h: "commit"),
    Spec(
        "cat-file-batch-check",
        lambda h: ["cat-file", "--batch-check"],
        stdin=lambda h: f"{h.c2}\n{h.b3}\n".encode(),
        out=lambda h: f"{h.c2} commit",
    ),
    Spec(
        "rev-list-missing",
        lambda h: ["rev-list", "--objects", "--missing=print", "lazy"],
        out=lambda h: f"?{h.b3}",
    ),
    Spec("rev-list-all", lambda h: ["rev-list", "--all"], out=lambda h: h.c2),
    Spec("merge-base-is-ancestor", lambda h: ["merge-base", "--is-ancestor", h.c1, h.c2]),
    Spec(
        "for-each-ref",
        lambda h: ["for-each-ref", "--format=%(objectname)"],
        out=lambda h: h.c2,
    ),
    Spec("show-ref", lambda h: ["show-ref"], out=lambda h: "refs/heads/main"),
    Spec("symbolic-ref", lambda h: ["symbolic-ref", "HEAD"], out=lambda h: "refs/heads/main"),
    Spec("branch-list", lambda h: ["branch", "--list"], out=lambda h: "main"),
    Spec("config-list", lambda h: ["config", "--list"], out=lambda h: "remote.origin.url="),
    Spec(
        "config-get",
        lambda h: ["config", "--get", "remote.origin.url"],
        out=lambda h: str(h.src),
    ),
    Spec("update-ref", lambda h: ["update-ref", "refs/heads/probe", h.c2]),
    Spec("update-ref-d", lambda h: ["update-ref", "-d", "refs/heads/lazy", h.c3]),
    Spec("ls-tree", lambda h: ["ls-tree", "-r", h.c2], out=lambda h: "a.txt"),
    Spec("log-p", lambda h: ["log", "-p", "-n2", h.c2], out=lambda h: "+two"),
    Spec("log-signature", lambda h: ["log", "--show-signature", "-n1", h.c2], out=lambda h: "c2"),
    Spec("show", lambda h: ["show", h.c2], out=lambda h: "+two"),
    Spec("diff-commits", lambda h: ["diff", h.c1, h.c2], out=lambda h: "+two"),
    Spec("diff-tree-p", lambda h: ["diff-tree", "-p", h.c1, h.c2], out=lambda h: "+two"),
    Spec(
        "pack-objects",
        lambda h: ["pack-objects", "--revs", "--stdout", "-q"],
        stdin=lambda h: f"{h.c3}\n".encode(),
        rc=(128,),
        err=LAZY_OFF,
    ),
    Spec("hash-object", lambda h: ["hash-object", "--stdin"], stdin=lambda h: b"x"),
    Spec("check-ref-format", lambda h: ["check-ref-format", "--branch", "x"], out=lambda h: "x"),
    Spec("remote-get-url", lambda h: ["remote", "get-url", "origin"], out=lambda h: str(h.src)),
    Spec("ls-remote-get-url", lambda h: ["ls-remote", "--get-url", "evil:x"]),
    Spec("count-objects", lambda h: ["count-objects"], out=lambda h: "objects"),
    Spec(
        "status",
        lambda h: ["status", "--porcelain=v2", "-z"],
        True,
        out=lambda h: "new.txt",
    ),
    Spec(
        "status-ignored",
        lambda h: [
            "status",
            "--porcelain=v2",
            "-z",
            "--ignored=traditional",
            "--untracked-files=all",
        ],
        True,
        out=lambda h: "new.txt",
    ),
    Spec("ls-files-v", lambda h: ["ls-files", "-v", "-z"], True, out=lambda h: "a.txt"),
    Spec("diff-worktree", lambda h: ["diff"], True, out=lambda h: "+edited"),
    Spec("diff-cached", lambda h: ["diff", "--cached"], True),
    Spec("diff-index", lambda h: ["diff-index", "-p", "HEAD"], True, out=lambda h: "+edited"),
    Spec("diff-files", lambda h: ["diff-files", "-p"], True, out=lambda h: "+edited"),
    Spec("update-index-refresh", lambda h: ["update-index", "--refresh"], True, rc=(1,)),
    Spec("add-all", lambda h: ["add", "-A"], True),
    Spec("write-tree", lambda h: ["write-tree"], True, out=lambda h: ""),
    Spec("read-tree", lambda h: ["read-tree", "-u", "--reset", "HEAD"], True),
    Spec("worktree-list", lambda h: ["worktree", "list", "--porcelain"], True),
    Spec(
        "worktree-add-new",
        lambda h: ["worktree", "add", "-b", "probe2", "--no-track", str(h.root / "wt-new"), h.c2],
        True,
    ),
    Spec(
        "worktree-add-detach",
        lambda h: ["worktree", "add", "--detach", str(h.root / "wt-det"), h.c2],
        True,
    ),
    Spec(
        "worktree-add-lazy-commit",
        lambda h: ["worktree", "add", "--detach", str(h.root / "wt-lazy"), h.c3],
        True,
        rc=(128,),
        err=LAZY_OFF,
    ),
    # Commands that would contact a remote run with protocols none against the clone.
    Spec("ls-remote-name", lambda h: ["ls-remote", "origin"], rc=(128,), err=NOT_ALLOWED),
    Spec(
        "ls-remote-file-url",
        lambda h: ["ls-remote", f"file://{h.src}"],
        rc=(128,),
        err=NOT_ALLOWED,
    ),
    Spec("ls-remote-plain-path", lambda h: ["ls-remote", str(h.src)], rc=(128,), err=NOT_ALLOWED),
    Spec("ls-remote-ext", lambda h: ["ls-remote", "evil:x"], rc=(128,), err=NOT_ALLOWED),
    Spec(
        "ls-remote-ssh",
        lambda h: ["ls-remote", "ssh://127.0.0.1:1/x"],
        rc=(128,),
        err=NOT_ALLOWED,
    ),
    Spec("fetch-origin", lambda h: ["fetch", "origin"], rc=(128,), err=NOT_ALLOWED),
    Spec("fetch-promo", lambda h: ["fetch", "promo"], rc=(128,), err=NOT_ALLOWED),
    Spec(
        "fetch-file-url",
        lambda h: ["fetch", f"file://{h.src}", "+refs/heads/*:refs/x/*"],
        rc=(128,),
        err=NOT_ALLOWED,
    ),
    Spec("commit-hooks", lambda h: ["commit", "-a", "-q", "-m", "x"], True),
    Spec("commit-editor", lambda h: ["commit", "-a", "-q", "-e", "-m", "x"], True),
    Spec(
        "push-origin",
        lambda h: ["push", "--no-verify", "origin", "main"],
        rc=(128,),
        err=NOT_ALLOWED,
    ),
]


def _modify(h: Hostile) -> None:
    (h.clone / "a.txt").write_text("edited\n")
    (h.clone / "new.txt").write_text("untracked\n")


def _kwargs(h: Hostile, wt: bool) -> dict[str, object]:
    if wt:
        return {"git_dir": h.git_dir, "work_tree": h.clone}
    return {"git_dir": h.git_dir, "cwd": h.clone}


@pytest.mark.parametrize("spec", MATRIX, ids=[s.name for s in MATRIX])
def test_hostile_clone_runs_nothing(hostile: Hostile, git: Git, spec: Spec) -> None:
    _modify(hostile)
    r = git.run(spec.args(hostile), stdin=spec.stdin(hostile), **_kwargs(hostile, spec.wt))  # type: ignore[arg-type]
    assert hostile.hits() == []
    # The command really ran: an exit status outside the expected set (a git that died of a
    # config error, say) would also leave no marker.
    assert r.returncode in spec.rc, (r.returncode, r.err)
    if spec.err is not None:
        assert spec.err in r.err, r.err
    if spec.out is not None:
        assert spec.out(hostile) in r.out, r.out


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
        tool_owned=True,  # the mirror is wtctl's own repository
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
        tool_owned=True,  # deliberately attacks with a hostile dir: tests the config overrides
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
        tool_owned=True,  # the mirror is wtctl's own repository
        timeout=30,
    )
    assert r.returncode != 0
    # And in the hostile clone itself the planted core.sshCommand is overridden.
    git.run(
        ["ls-remote", "ssh://127.0.0.1:1/x"],
        git_dir=hostile.git_dir,
        cwd=hostile.clone,
        scheme="ssh",
        tool_owned=True,  # deliberately attacks with a hostile dir: tests the config overrides
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


# -- review round 1: nested repositories (item 1) ----------------------------------------------


@pytest.fixture
def hostile_sub(tmp_path: Path, private_home: Path) -> Hostile:
    """The hostile clone with a nested repository at ``s`` and ``diff.submodule=diff`` planted."""
    h = build_hostile(tmp_path / "hs", private_home, submodule=True)
    with open(h.git_dir / "config", "a") as f:
        f.write("[diff]\n\tsubmodule = diff\n")
    return h


TAME = ["--no-pager", "-c", "core.hooksPath=/dev/null", "-c", "diff.external=", "-c"]


def test_control_planted_diff_submodule_runs_the_nested_repos_filter(hostile_sub: Hostile) -> None:
    _modify(hostile_sub)
    plain_git([*TAME, "log.showSignature=false", "diff", "--no-ext-diff"], cwd=hostile_sub.clone,
              home=hostile_sub.home)  # fmt: skip
    assert hostile_sub.nested_hits(), "the nested repository's config no longer fires"


@pytest.mark.parametrize("args", [["diff"], ["diff-files", "-p"]], ids=["diff", "diff-files"])
def test_nested_repo_config_does_not_run_under_planted_diff_submodule(
    hostile_sub: Hostile, git: Git, args: list[str]
) -> None:
    _modify(hostile_sub)
    r = git.run(args, **_kwargs(hostile_sub, True))  # type: ignore[arg-type]
    assert r.returncode == 0, r.err
    assert "+edited" in r.out  # it ran, and the outer diff is intact
    assert hostile_sub.hits() == []


@pytest.mark.parametrize(
    "extra",
    [["--submodule=diff", "--ignore-submodules=none"], ["--ignore-submodules=none"]],
    ids=["submodule-diff", "ignore-none"],
)
def test_control_caller_flags_would_reopen_the_nested_repo(
    hostile_sub: Hostile, extra: list[str]
) -> None:
    # Control for the rejected caller options: after the guards, these flags win again.
    _modify(hostile_sub)
    plain_git([*TAME, "diff.submodule=short", "diff", "--no-ext-diff", "--ignore-submodules=all"],
              cwd=hostile_sub.clone, home=hostile_sub.home)  # fmt: skip
    assert hostile_sub.nested_hits() == []
    plain_git([*TAME, "diff.submodule=short", "diff", "--no-ext-diff", *extra],
              cwd=hostile_sub.clone, home=hostile_sub.home)  # fmt: skip
    assert hostile_sub.nested_hits(), f"{extra} no longer reopens the nested repo"


RECURSE_CASES = [
    ("status", ["--ignore-sub=none"]),
    ("status", ["--ignore-submodules=none"]),
    ("read-tree", ["-u", "--reset", "--recurse-submodules", "HEAD"]),
]


@pytest.mark.parametrize("sub,extra", RECURSE_CASES, ids=[f"{s}{e[0]}" for s, e in RECURSE_CASES])
def test_control_caller_flags_would_reopen_the_nested_repo_elsewhere(
    hostile_sub: Hostile, sub: str, extra: list[str]
) -> None:
    _modify(hostile_sub)
    plain_git([*TAME, "log.showSignature=false", "status", "--ignore-submodules=all"],
              cwd=hostile_sub.clone, home=hostile_sub.home)  # fmt: skip
    assert hostile_sub.nested_hits() == []
    plain_git([*TAME, "log.showSignature=false", sub, *extra], cwd=hostile_sub.clone,
              home=hostile_sub.home)  # fmt: skip
    assert hostile_sub.nested_hits(), f"{sub} {extra} no longer reopens the nested repo"


@pytest.mark.parametrize("sub,extra", RECURSE_CASES, ids=[f"{s}{e[0]}" for s, e in RECURSE_CASES])
def test_reopening_flags_are_refused_and_run_nothing(
    hostile_sub: Hostile, git: Git, sub: str, extra: list[str]
) -> None:
    _modify(hostile_sub)
    with pytest.raises(ValueError):
        git.run([sub, *extra], **_kwargs(hostile_sub, False))  # type: ignore[arg-type]
    assert hostile_sub.nested_hits() == []
    r = git.run(["status", "--porcelain=v2"], **_kwargs(hostile_sub, False))  # type: ignore[arg-type]
    assert r.returncode == 0, r.err
    assert hostile_sub.hits() == [] and hostile_sub.nested_hits() == []


# -- guards the caller cannot turn off (items 4, 5) ---------------------------------------------


def _plain_diff(h: Hostile, *extra: str) -> None:
    plain_git(
        ["--no-pager", "-c", "core.hooksPath=/dev/null", "-c", "log.showSignature=false",
         "diff", "--no-ext-diff", "--no-textconv", *extra, h.c1, h.c2],
        cwd=h.clone, home=h.home,
    )  # fmt: skip


def test_control_a_later_ext_diff_flag_beats_the_guard(hostile: Hostile) -> None:
    _plain_diff(hostile)
    assert hostile.payload_hits() == []
    _plain_diff(hostile, "--ext-diff", "--textconv")
    assert hostile.payload_hits(), "--ext-diff no longer overrides --no-ext-diff"


def test_control_cat_file_textconv_runs_the_driver(hostile: Hostile) -> None:
    plain_git(["cat-file", "-p", "HEAD:a.txt"], cwd=hostile.clone, home=hostile.home)
    assert hostile.payload_hits() == []
    plain_git(["cat-file", "--textconv", "HEAD:a.txt"], cwd=hostile.clone, home=hostile.home)
    assert hostile.payload_hits(), "cat-file --textconv no longer runs the textconv driver"


def test_pathspec_named_like_a_guard_does_not_suppress_the_guards(
    hostile: Hostile, git: Git
) -> None:
    # A file called --no-ext-diff with a diff driver attached. The old wrapper saw the string in
    # its arguments and skipped the guard, so the external driver ran.
    with open(hostile.clone / ".gitattributes", "a") as f:
        f.write("/--no-ext-diff diff=evil\n")
    blob = hostile.git(["hash-object", "-w", "--no-filters", "--stdin"], stdin=b"one\n")
    # Setup only: the filters are blanked so a racily clean a.txt cannot fire them here.
    keys = ("clean", "smudge", "process", "required")
    quiet = {"GIT_CONFIG_COUNT": str(len(keys))}
    for i, key in enumerate(keys):
        quiet[f"GIT_CONFIG_KEY_{i}"] = f"filter.evil.{key}"
        quiet[f"GIT_CONFIG_VALUE_{i}"] = "false" if key == "required" else ""
    hostile.git(["update-index", "--add", "--cacheinfo", f"100644,{blob},--no-ext-diff"], env=quiet)
    (hostile.clone / "--no-ext-diff").write_text("two\n")
    for m in hostile.markers.iterdir():
        m.unlink()
    plain_git(["--no-pager", "-c", "core.hooksPath=/dev/null", "diff", "--", "--no-ext-diff"],
              cwd=hostile.clone, home=hostile.home)  # fmt: skip
    assert hostile.payload_hits(), "the driver on the planted file no longer fires"
    for m in hostile.markers.iterdir():
        m.unlink()
    r = git.run(["diff", "--", "--no-ext-diff"], **_kwargs(hostile, True))  # type: ignore[arg-type]
    assert r.returncode == 0 and "+two" in r.out, r.err
    assert hostile.hits() == []
    cmd = git.build(["diff", "--", "--no-ext-diff"], git_dir=hostile.git_dir, cwd=hostile.clone)
    argv = cmd.argv
    assert argv.index("--no-ext-diff") < argv.index("--")  # the guard, before the separator


def test_cat_file_filters_stays_blanked(hostile: Hostile, git: Git) -> None:
    plain_git(["cat-file", "--filters", "HEAD:a.txt"], cwd=hostile.clone, home=hostile.home)
    assert hostile.payload_hits(), "cat-file --filters no longer runs the smudge driver"
    for m in hostile.markers.iterdir():
        m.unlink()
    r = git.run(["cat-file", "--filters", "HEAD:a.txt"], **_kwargs(hostile, False))  # type: ignore[arg-type]
    assert r.returncode == 0 and r.out == "two\n", r.err
    assert hostile.hits() == []


# -- non-UTF-8 names (item 2) ---------------------------------------------------------------------


def test_filter_named_with_non_utf8_bytes_is_blanked(hostile: Hostile, git: Git) -> None:
    name = b"ev\xffil"
    with open(hostile.git_dir / "config", "ab") as f:
        f.write(b'[filter "' + name + b'"]\n\tclean = ' + str(hostile.payload).encode() + b"\n")
    with open(hostile.clone / ".gitattributes", "ab") as f:
        f.write(b"*.txt filter=" + name + b"\n")
    _modify(hostile)
    plain_git(["add", "-A"], cwd=hostile.clone, home=hostile.home)
    assert hostile.payload_hits(), "the non-UTF-8 filter no longer fires"
    for m in hostile.markers.iterdir():
        m.unlink()
    r = git.run(["add", "-A"], **_kwargs(hostile, True))  # type: ignore[arg-type]
    assert r.returncode == 0, r.err
    assert hostile.hits() == []


# -- commit: editor, hooks, signing (item 3) --------------------------------------------------


COMMIT_VECTORS = {
    "editor": ("", ["commit", "-a", "-q", "-e", "-m", "x"]),
    "gpg-program": ("[commit]\n\tgpgsign = true\n", ["commit", "-a", "-q", "-m", "x"]),
    "ssh-default-key": (
        '[gpg]\n\tformat = ssh\n[gpg "ssh"]\n\tdefaultKeyCommand = {p}\n',
        ["commit", "-a", "-q", "-S", "-m", "x"],
    ),
}


@pytest.mark.parametrize("name", sorted(COMMIT_VECTORS))
def test_control_commit_vectors_are_live(hostile: Hostile, name: str) -> None:
    cfg, args = COMMIT_VECTORS[name]
    with open(hostile.git_dir / "config", "a") as f:
        f.write(cfg.format(p=hostile.payload))
    _touch_worktree(hostile)
    plain_git(["-c", "core.hooksPath=/dev/null", *args], cwd=hostile.clone, home=hostile.home)
    assert hostile.payload_hits(), f"commit vector {name} no longer fires"


@pytest.mark.parametrize("name", sorted(COMMIT_VECTORS))
def test_commit_runs_no_editor_hook_or_signing_program(
    hostile: Hostile, git: Git, name: str
) -> None:
    cfg, args = COMMIT_VECTORS[name]
    with open(hostile.git_dir / "config", "a") as f:
        f.write(cfg.format(p=hostile.payload))
    _touch_worktree(hostile)
    r = git.run(args, **_kwargs(hostile, True))  # type: ignore[arg-type]
    assert hostile.hits() == []
    if name == "ssh-default-key":
        assert r.returncode != 0  # -S with no usable key: refused, nothing run
    else:
        assert r.returncode == 0, r.err
        assert hostile.git(["rev-parse", "HEAD"]) != hostile.c2  # the commit was made


def test_control_commit_hooks_fire(hostile: Hostile) -> None:
    _touch_worktree(hostile)
    plain_git(["commit", "-a", "-q", "-m", "x"], cwd=hostile.clone, home=hostile.home)
    assert hostile.hits() == ["hit"]


# -- status under showUntrackedFiles=no (item 7) ---------------------------------------------


def test_status_lists_untracked_files_despite_repository_config(hostile: Hostile, git: Git) -> None:
    with open(hostile.git_dir / "config", "a") as f:
        f.write("[status]\n\tshowUntrackedFiles = no\n")
    _modify(hostile)
    plain = plain_git(["status", "--porcelain=v2", "-z"], cwd=hostile.clone, home=hostile.home)
    assert b"new.txt" not in plain.stdout  # control: the repository config hides it
    r = git.run(["status", "--porcelain=v2", "-z"], **_kwargs(hostile, True))  # type: ignore[arg-type]
    assert b"new.txt" in r.stdout


# -- alternate refs during a file fetch (item 12) -------------------------------------------------


def test_control_alternate_refs_command_runs_during_a_file_fetch(hostile: Hostile) -> None:
    plain_git(["fetch", str(hostile.src), "+refs/heads/*:refs/x/*"], cwd=hostile.clone,
              home=hostile.home, env={"GIT_ALLOW_PROTOCOL": "file"})  # fmt: skip
    assert any(ln.endswith("/src/.git") for ln in hostile.payload_hits()), hostile.payload_hits()


def test_file_fetch_ignores_the_alternates_ref_command(hostile: Hostile, git: Git) -> None:
    # A fetch with the file scheme allowed, in the hostile clone: only reachable because the
    # caller says the directory is tool-owned; this deliberately attacks it to test the overrides.
    r = git.run(
        ["fetch", str(hostile.src), "+refs/heads/*:refs/x/*"],
        git_dir=hostile.git_dir,
        cwd=hostile.clone,
        scheme="file",
        tool_owned=True,
        timeout=30,
    )
    assert r.returncode == 0, r.err
    assert hostile.hits() == []
    assert hostile.git(["rev-parse", "refs/x/main"])  # the fetch really happened


# -- redirects (item 12) -----------------------------------------------------------------------


class _Server(http.server.ThreadingHTTPServer):
    seen: list[str]
    target: str | None


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = cast(_Server, self.server)
        server.seen.append(self.path)
        if server.target is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(302)
        self.send_header("Location", server.target + self.path)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def _serve(target: str | None = None) -> _Server:
    srv = _Server(("127.0.0.1", 0), _Handler)
    srv.seen = []
    srv.target = target
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_redirects_are_not_followed(hostile: Hostile, git: Git) -> None:
    second = _serve()
    first = _serve(f"http://127.0.0.1:{second.server_address[1]}")
    try:
        url = f"http://127.0.0.1:{first.server_address[1]}/x.git"
        mirror = hostile.root / "mirror.git"
        plain_git(["init", "-q", "--bare", str(mirror)], cwd=hostile.root, home=hostile.home)
        plain_git(["ls-remote", url], cwd=_priv(hostile), home=hostile.home)
        assert first.seen and second.seen, "control: unhardened git follows the redirect"
        first.seen.clear()
        second.seen.clear()
        cmd = git.build(
            ["ls-remote", url],
            git_dir=mirror,
            cwd=_priv(hostile),
            scheme="https",
            tool_owned=True,  # the mirror is wtctl's own repository
        )
        # The scheme allowlist only admits https/ssh/file; the test server speaks plain http.
        env = dict(cmd.env, GIT_ALLOW_PROTOCOL="http")
        r = git.gateway.git(cmd.argv, env=env, cwd=cmd.cwd, timeout=30)
        assert r.returncode != 0
        assert first.seen, "the request to the first server was made"
        assert second.seen == []
    finally:
        for srv in (first, second):
            srv.shutdown()
            srv.server_close()


# -- forged-history overrides (items 12, 16) --------------------------------------------------


def test_replace_graft_and_commit_graph_are_switched_off(hostile: Hostile, git: Git) -> None:
    # The forged-history vectors themselves (E1a-f) belong to the mirror (#13); here only that
    # the switches are on every command: in the environment and as effective config.
    cmd = git.build(["rev-parse", "HEAD"], git_dir=hostile.git_dir, cwd=hostile.clone)
    assert cmd.env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert cmd.env["GIT_GRAFT_FILE"] == "/dev/null/none"
    for key in ("core.useReplaceRefs", "core.commitGraph"):
        # the hostile config sets both to true; the override must win
        r = git.run(["config", "--get", key], git_dir=hostile.git_dir, cwd=hostile.clone)
        assert r.out.strip() == "false", key
        control = plain_git(["config", "--get", key], cwd=hostile.clone, home=hostile.home)
        assert control.stdout.strip() == b"true", key


# -- allowed filter drivers: negative cases (item 13) ---------------------------------------------


def _honored(
    h: Hostile, home: Path, cfg: str, allow: list[str], key: str, tool: Path | None = None
):
    from wtctl.hardgit import load_filter_policy

    (home / ".gitconfig").write_text(cfg.format(tool=tool))
    policy = load_filter_policy(
        Gateway(), GIT, home=str(home), xdg_config_home=None, allow=allow,
        path_env="/usr/bin:/bin", private_dir=str(_priv(h)),
    )  # fmt: skip
    g = Git(Gateway(), GIT, str(home), policy)
    cmd = g.build(["config", "--get", key], git_dir=h.git_dir, cwd=h.clone, honor_filters=True)
    return policy, g.gateway.git(cmd.argv, env=cmd.env, cwd=cmd.cwd, timeout=30).out.strip()


def _tool(h: Hostile) -> Path:
    tool = h.root / "bin" / "lfs-ish"
    tool.parent.mkdir(exist_ok=True)
    tool.write_text("#!/bin/sh\ncat\n")
    tool.chmod(0o755)
    return tool


@pytest.mark.parametrize("var", ["clean", "smudge"])
def test_planted_clean_or_smudge_stays_blank_without_a_global_process(
    hostile: Hostile, private_home: Path, var: str
) -> None:
    # The repository plants clean/smudge/process for "evil"; the global config defines only
    # clean or smudge for it. Allowed, but no global `process`: not honored, still blanked.
    policy, value = _honored(
        hostile, private_home, '[filter "evil"]\n\t' + var + " = {tool}\n", ["evil"],
        f"filter.evil.{var}", _tool(hostile),
    )  # fmt: skip
    assert policy.drivers == {} and any("evil" in w for w in policy.warnings)
    assert value == ""


def test_dotted_mixed_case_driver_name_is_honored(hostile: Hostile, private_home: Path) -> None:
    tool = _tool(hostile)
    policy, value = _honored(
        hostile, private_home, '[filter "My.Lfs"]\n\tprocess = {tool} filter-process\n',
        ["My.Lfs"], "filter.My.Lfs.process", tool,
    )  # fmt: skip
    assert set(policy.drivers) == {"My.Lfs"}
    assert value == f"{tool} filter-process"


def test_process_at_a_missing_binary_stays_blanked_with_a_warning(
    hostile: Hostile, private_home: Path
) -> None:
    policy, value = _honored(
        hostile, private_home, '[filter "evil"]\n\tprocess = /nonexistent/dir/tool\n', ["evil"],
        "filter.evil.process",
    )  # fmt: skip
    assert policy.drivers == {} and any("not found" in w for w in policy.warnings)
    assert value == ""
