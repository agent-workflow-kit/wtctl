"""Unit tests for the hardened invocation: environment, override set, parsing, classification."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.vectorlib import GIT, plain_git
from wtctl import hardgit
from wtctl.errors import WtctlError
from wtctl.gateway import Gateway, GitResult, run_git
from wtctl.hardgit import (
    FETCH_CAUSES,
    TRANSPORT_CAUSES,
    FetchKind,
    Git,
    PorcelainError,
    UnsafePathError,
    build_env,
    classify_fetch,
    fetch_with_retries,
    outcome_error,
    parse_status_v2,
    parse_worktree_list,
)


def res(code: int, err: str = "", out: bytes = b"", timed_out: bool = False) -> GitResult:
    return GitResult(code, out, err.encode(), timed_out)


@pytest.fixture
def repo(tmp_path: Path, private_home: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    plain_git(["init", "-q", "-b", "main"], cwd=r, home=private_home)
    return r


@pytest.fixture
def git(private_home: Path) -> Git:
    return Git(Gateway(), GIT, str(private_home))


# -- environment ---------------------------------------------------------------------------------


def test_env_is_built_from_scratch(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GIT_DIR", "GIT_SSH_COMMAND", "GIT_TRACE", "SSH_AUTH_SOCK", "GIT_EXEC_PATH"):
        monkeypatch.setenv(name, "x")
    env = build_env(git_bin="/opt/g/bin/git", home="/h")
    assert set(env) == {
        "PATH", "HOME", "LC_ALL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
        "GIT_TERMINAL_PROMPT", "GIT_ALLOW_PROTOCOL", "GIT_NO_LAZY_FETCH",
        "GIT_NO_REPLACE_OBJECTS", "GIT_GRAFT_FILE", "GIT_SSH_VARIANT", "GIT_EDITOR",
        "GIT_SHALLOW_FILE",
    }  # fmt: skip
    assert env["GIT_EDITOR"] == ":"
    assert env["PATH"] == "/opt/g/bin:/usr/bin:/bin"
    assert env["GIT_ALLOW_PROTOCOL"] == "none"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_GRAFT_FILE"] == "/dev/null/none" and env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert env["GIT_SHALLOW_FILE"] == "/dev/null"
    assert "GIT_SHALLOW_FILE" not in build_env(
        git_bin="/opt/g/bin/git", home="/h", allow_shallow=True
    )
    assert env["GIT_SSH_VARIANT"] == "ssh" and env["LC_ALL"] == "C"


def test_network_scheme_sets_protocols_and_ssh_agent_only_for_ssh() -> None:
    assert build_env(git_bin="/u/git", home="/h", scheme="https")["GIT_ALLOW_PROTOCOL"] == "https"
    assert "SSH_AUTH_SOCK" not in build_env(
        git_bin="/u/git", home="/h", scheme="https", ssh_auth_sock="/s"
    )
    assert (
        build_env(git_bin="/u/git", home="/h", scheme="ssh", ssh_auth_sock="/s")["SSH_AUTH_SOCK"]
        == "/s"
    )
    assert "SSH_AUTH_SOCK" not in build_env(git_bin="/u/git", home="/h", ssh_auth_sock="/s")


@pytest.mark.parametrize("scheme", ["ext", "none", "http", "file:ext", "https:ext", "", "git"])
def test_scheme_outside_the_allowlist_is_a_programming_error(scheme: str) -> None:
    with pytest.raises(ValueError):
        build_env(git_bin="/u/git", home="/h", scheme=scheme)


def test_config_count_entries() -> None:
    env = build_env(git_bin="/u/git", home="/h", config=[("a.b", "1"), ("c.d", "")])
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("c.d", "")


def test_filter_dirs_are_on_path_after_the_defaults() -> None:
    env = build_env(git_bin="/u/git", home="/h", extra_path=["/opt/homebrew/bin", "/usr/bin"])
    assert env["PATH"] == "/u:/usr/bin:/bin:/opt/homebrew/bin"


# -- command line --------------------------------------------------------------------------------


def test_command_line_carries_the_full_override_set(git: Git, repo: Path) -> None:
    cmd = git.build(["rev-parse", "HEAD"], git_dir=repo / ".git", cwd=repo)
    assert cmd.argv[:2] == [GIT, "--no-pager"]
    cs = [cmd.argv[i + 1] for i, a in enumerate(cmd.argv) if a == "-c"]
    for expected in (
        "core.fsmonitor=", "core.hooksPath=/dev/null", "core.sshCommand=ssh -o BatchMode=yes",
        "credential.helper=", "core.askPass=", "core.pager=cat", "diff.external=",
        "core.attributesFile=/dev/null", "log.showSignature=false",
        "fetch.recurseSubmodules=false", "submodule.recurse=false", "gc.auto=0",
        "maintenance.auto=false", "core.useReplaceRefs=false", "core.commitGraph=false",
        "http.followRedirects=false", "core.editor=true", "sequence.editor=true",
        "gpg.ssh.defaultKeyCommand=true", "commit.gpgsign=false", "tag.gpgsign=false",
        "push.gpgsign=false", "interactive.diffFilter=", "diff.submodule=short",
        "core.alternateRefsCommand=true", "gpg.program=/bin/false",
    ):  # fmt: skip
        assert expected in cs
    assert f"--git-dir={repo / '.git'}" in cmd.argv
    assert not any(c.startswith("http.lowSpeed") for c in cs)


def test_effective_values_match_the_overrides_in_a_repository(git: Git, repo: Path) -> None:
    for key, value in hardgit.OVERRIDES:
        r = git.run(["config", "--get", key], git_dir=repo / ".git", cwd=repo)
        assert r.returncode == 0, key
        assert r.out.rstrip("\n") == value, key


def test_network_commands_get_timeouts_and_must_have_one(git: Git, repo: Path) -> None:
    cmd = git.build(
        ["ls-remote", "x"], git_dir=repo / ".git", cwd=repo, scheme="https", tool_owned=True
    )
    cs = [cmd.argv[i + 1] for i, a in enumerate(cmd.argv) if a == "-c"]
    assert "http.lowSpeedLimit=1000" in cs and "http.lowSpeedTime=60" in cs
    with pytest.raises(ValueError, match="timeout"):
        git.run(
            ["ls-remote", "x"], git_dir=repo / ".git", cwd=repo, scheme="https", tool_owned=True
        )


@pytest.mark.parametrize(
    "args", [[], ["-c", "x=y", "status"], ["--git-dir=/x", "status"], ["Status"], ["alias-me"],
             ["credential"], ["submodule"], ["rev-parse;ls"]],
)  # fmt: skip
def test_only_known_subcommands_are_run(git: Git, repo: Path, args: list[str]) -> None:
    with pytest.raises(ValueError):
        git.build(args, git_dir=repo / ".git", cwd=repo)


def test_diff_family_gets_no_ext_diff_and_textconv_guards(git: Git, repo: Path) -> None:
    for sub in ("diff", "show", "log", "diff-tree", "diff-index", "diff-files"):
        cmd = git.build([sub], git_dir=repo / ".git", cwd=repo)
        assert "--no-ext-diff" in cmd.argv, sub
    for sub in ("diff", "show", "log"):
        assert "--no-textconv" in git.build([sub], git_dir=repo / ".git", cwd=repo).argv
    assert "--no-ext-diff" not in git.build(["status"], git_dir=repo / ".git", cwd=repo).argv


def test_diff_works_with_the_empty_external_override(git: Git, repo: Path) -> None:
    # With diff.external= set empty, git diff fails unless --no-ext-diff is passed (spike H).
    (repo / "f").write_text("a\n")
    r = git.run(["diff", "--no-index", "/dev/null", str(repo / "f")], git_dir=repo / ".git",
                cwd=repo)  # fmt: skip
    assert r.returncode in (0, 1), r.err
    r = git.run(["diff"], git_dir=repo / ".git", work_tree=repo)
    assert r.returncode == 0, r.err
    plain = plain_git(["-c", "diff.external=", "diff"], cwd=repo, home=Path(os.environ["HOME"]))
    assert plain.returncode != 0 or b"cannot run" in plain.stderr or plain.stdout == b""


def test_status_ignores_submodules_unless_told(git: Git, repo: Path) -> None:
    assert "--ignore-submodules=all" in git.build(["status"], git_dir=repo / ".git", cwd=repo).argv
    argv = git.build(["status", "--ignore-submodules=all"], git_dir=repo / ".git", cwd=repo).argv
    assert "--ignore-submodules=all" in argv


@pytest.mark.parametrize("bad", ["/tmp/a\nb", "relative/path"])
def test_paths_with_newlines_or_relative_are_refused(git: Git, repo: Path, bad: str) -> None:
    with pytest.raises(UnsafePathError):
        git.build(["status"], git_dir=bad, cwd=repo)
    with pytest.raises(UnsafePathError):
        git.build(["status"], git_dir=repo / ".git", work_tree=bad)


def test_no_repository_commands_use_a_private_dir_and_a_ceiling(git: Git, tmp_path: Path) -> None:
    priv = tmp_path / "state" / "tmp" / "p1"
    priv.mkdir(parents=True)
    cmd = git.build(["init", "--template=", str(priv / "x")], private_dir=priv)
    assert cmd.cwd == str(priv)
    assert cmd.env["GIT_CEILING_DIRECTORIES"] == str(priv.parent)
    assert not any(a.startswith("--git-dir") for a in cmd.argv)
    with pytest.raises(ValueError):
        git.build(["init"])  # no repository and no private dir
    with pytest.raises(ValueError):
        git.build(["status"], work_tree=priv)


def test_ancestor_repository_is_not_discovered(
    git: Git, tmp_path: Path, private_home: Path
) -> None:
    anc = tmp_path / "anc"
    priv = anc / "state" / "p"
    priv.mkdir(parents=True)
    plain_git(["init", "-q"], cwd=anc, home=private_home)
    with open(anc / ".git" / "config", "a") as f:
        f.write('[url "https://evil.invalid/"]\n\tinsteadOf = https://good.invalid/\n')
    r = git.run(["config", "--get-regexp", "^url\\."], private_dir=priv)
    assert r.returncode != 0 and "evil" not in r.out
    # Control: without the ceiling git reads the ancestor's config.
    cmd = git.build(["config", "--get-regexp", "^url\\."], private_dir=priv)
    env = {k: v for k, v in cmd.env.items() if k != "GIT_CEILING_DIRECTORIES"}
    r2 = git.gateway.git(cmd.argv, env=env, cwd=cmd.cwd, timeout=30)
    assert "evil" in r2.out


def test_filter_blanking_enumerates_every_driver_name() -> None:
    entries = [
        ("filter.lfs.clean", "x"), ("filter.My.Name.smudge", "y"), ("filter.q.required", None),
        ("core.x", "z"), ("filterx.a.b", "1"),
    ]  # fmt: skip
    assert hardgit.filter_names(entries) == ["lfs", "My.Name", "q"]
    cfg = dict(hardgit.filter_config(["lfs"], {}))
    assert cfg["filter.lfs.process"] == "" and cfg["filter.lfs.required"] == "false"


def test_extra_header_keys_match_case_insensitively_and_scoped() -> None:
    entries = [
        ("http.extraheader", "a"), ("http.https://h/.extraHeader", "b"),
        ("http.https://*.h/.ExtraHeader", "c"), ("http.proxy", "p"), ("remote.o.extraheader", "d"),
    ]  # fmt: skip
    assert hardgit.extra_header_keys(entries) == [
        "http.extraheader", "http.https://h/.extraHeader", "http.https://*.h/.ExtraHeader",
    ]  # fmt: skip


# -- binary and version --------------------------------------------------------------------------


def test_resolve_binary_ignores_relative_and_empty_path_entries(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    tool = good / "git"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    rel = tmp_path / "rel"
    rel.mkdir()
    (rel / "git").write_text("#!/bin/sh\n")
    (rel / "git").chmod(0o755)
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        path = os.pathsep.join(["", "rel", str(good)])
        assert hardgit.resolve_binary("git", path) == str(tool)
        assert hardgit.resolve_binary("git", "rel:") is None
    finally:
        os.chdir(cwd)
    assert hardgit.resolve_binary("git", "", explicit=str(tool)) == str(tool)
    assert hardgit.resolve_binary("git", "", explicit="git") is None
    assert hardgit.resolve_binary("git", "", explicit=str(good / "nope")) is None


@pytest.mark.parametrize(
    "text,expected",
    [("git version 2.34.1\n", (2, 34, 1)), ("git version 2.39.3 (Apple Git-145)", (2, 39, 3)),
     ("git version 2.45", (2, 45, 0))],
)  # fmt: skip
def test_parse_git_version(text: str, expected: tuple[int, int, int]) -> None:
    assert hardgit.parse_git_version(text) == expected


def test_parse_git_version_rejects_garbage() -> None:
    with pytest.raises(PorcelainError):
        hardgit.parse_git_version("not git")


def test_check_version_refuses_old_git(tmp_path: Path) -> None:
    class Old(Gateway):
        pass

    gw = Gateway(git_runner=lambda argv, **kw: res(0, out=b"git version 2.33.9\n"))
    with pytest.raises(WtctlError) as e:
        hardgit.check_version(gw, "/usr/bin/git", "/h", str(tmp_path))
    assert e.value.code == "git_too_old" and e.value.exit_code == 12
    gw = Gateway(git_runner=lambda argv, **kw: res(0, out=b"git version 2.34.0\n"))
    assert hardgit.check_version(gw, "/usr/bin/git", "/h", str(tmp_path)) == (2, 34, 0)


def test_check_version_on_the_installed_git(private_home: Path, tmp_path: Path) -> None:
    v = hardgit.check_version(Gateway(), GIT, str(private_home), str(tmp_path))
    assert v[:2] >= (2, 34)


# -- fetch outcome classification (D15) ----------------------------------------------------------


@pytest.mark.parametrize(
    "result,kind,cause",
    [
        (res(0), FetchKind.OK, None),
        (res(0, "From x\n * branch\n"), FetchKind.OK, None),
        (res(0, "error: unable to delete 'refs/heads/a'\n"), FetchKind.FAILED, "prune_incomplete"),
        (
            res(1, "error: could not delete reference refs/heads/a: x\nFrom x\n - [deleted] a\n"),
            FetchKind.FAILED,
            "prune_incomplete",
        ),
        (
            res(0, "error: could not delete reference refs/heads/a: x\nFrom x\n - [deleted] a\n"),
            FetchKind.FAILED,
            "prune_incomplete",
        ),
        (res(1, "error: could not delete reference refs/heads/a: x"), FetchKind.FAILED, None),
        (res(1, "error: cannot lock ref 'refs/heads/a'"), FetchKind.LOCK_CONTENTION, None),
        (res(1, "error: unable to update local ref"), FetchKind.LOCK_CONTENTION, None),
        (res(1, "something else"), FetchKind.FAILED, None),
        (res(128, "fatal: could not read Username for 'https://h'"), FetchKind.AUTH_REQUIRED, None),
        (res(128, "fatal: terminal prompts disabled"), FetchKind.AUTH_REQUIRED, None),
        (res(128, "fatal: Authentication failed for 'https://h/'"), FetchKind.AUTH_REQUIRED, None),
        (res(128, "remote: Authentication failed"), FetchKind.FAILED, None),
        (res(128, "fatal: remote error: Authentication failed"), FetchKind.FAILED, None),
        (res(128, "fatal: protocol error: terminal prompts disabled"), FetchKind.FAILED, None),
        (res(128, "remote: Host key verification failed.\nfatal: boom"), FetchKind.FAILED, None),
        (res(128, "git@h: Permission denied (publickey)."), FetchKind.AUTH_REQUIRED, None),
        (res(128, "Host key verification failed."), FetchKind.AUTH_REQUIRED, None),
        (res(128, "fatal: unable to access: The requested URL returned error: 401"),
         FetchKind.AUTH_REQUIRED, None),
        (res(2), FetchKind.FAILED, None),
        (res(-9, timed_out=True), FetchKind.FAILED, "timeout"),
        (res(0, "error: x", timed_out=True), FetchKind.FAILED, "timeout"),
    ],
)  # fmt: skip
def test_classify_fetch(result: GitResult, kind: FetchKind, cause: str | None) -> None:
    out = classify_fetch(result)
    assert (out.kind, out.cause) == (kind, cause)


# Transport causes. Texts are captured from git 2.34.1 (curl, OpenSSH), LC_ALL=C, with the hosts
# shortened; `ACCESS` is git's prefix for a curl failure.
ACCESS = "fatal: unable to access 'http://h/a.git/': "
HTTP_ERR = ACCESS + "The requested URL returned error: "
NO_READ = "fatal: Could not read from remote repository.\n\nPlease make sure you have the correct\n"
NOT_FOUND = "fatal: repository 'http://h/a.git/' not found"
NOT_GIT = "fatal: '/x/y.git' does not appear to be a git repository\n"


@pytest.mark.parametrize(
    "stderr,cause",
    [
        (ACCESS + "Could not resolve host: h", "network_unreachable"),
        (
            ACCESS + "Failed to connect to h port 1 after 0 ms: Connection refused",
            "network_unreachable",
        ),
        (ACCESS + "Failed to connect to h port 443: Network is unreachable", "network_unreachable"),
        (
            "ssh: Could not resolve hostname h: Name or service not known\n" + NO_READ,
            "network_unreachable",
        ),
        ("ssh: connect to host h port 1: Connection refused\n" + NO_READ, "network_unreachable"),
        (NOT_FOUND, "repository_not_found"),
        (NOT_GIT + NO_READ, "repository_not_found"),
        (HTTP_ERR + "429", "rate_limited"),
        # a rate limit named before a missing repository wins (fixed order)
        (NOT_FOUND + "\n" + HTTP_ERR + "429", "rate_limited"),
        # other statuses and transport errors stay generic
        (HTTP_ERR + "503", None),
        (HTTP_ERR + "4290", None),
        (HTTP_ERR + "404x", None),
        (ACCESS + "SSL certificate problem: self signed certificate", None),
        # server-chosen text never classifies
        ("remote: Repository not found.\nfatal: remote error: repository 'x' not found", None),
        ("remote: " + HTTP_ERR + "429", None),
        ("fatal: remote error: Could not resolve host: x", None),
        ("fatal: protocol error: " + NOT_FOUND, None),
        # a marker mid-line (inside a ref or path name) is not a line git made
        ("fatal: bad ref name 'ssh: Could not resolve hostname x'", None),
        ("fatal: bad ref name '" + NOT_FOUND + "'", None),
        # authentication wins over any other marker
        (NOT_FOUND + "\nfatal: Authentication failed for 'http://h/'", "AUTH"),
    ],
)
def test_classify_transport_causes(stderr: str, cause: str | None) -> None:
    out = classify_fetch(res(128, stderr))
    if cause == "AUTH":
        assert out.kind is FetchKind.AUTH_REQUIRED and out.cause is None
    else:
        assert (out.kind, out.cause) == (FetchKind.FAILED, cause)


@pytest.mark.parametrize("code", [1, 2, 255])
def test_transport_text_only_classifies_an_exit_128(code: int) -> None:
    assert classify_fetch(res(code, NOT_FOUND)).cause is None


def test_a_line_merely_containing_error_is_not_a_prune_failure() -> None:
    assert classify_fetch(res(0, "warning: no error: here\n")).ok


def test_lock_contention_is_retried_then_fails() -> None:
    sleeps: list[float] = []
    calls = iter([res(1, "cannot lock ref"), res(1, "cannot lock ref"), res(0)])
    r, out = fetch_with_retries(lambda: next(calls), retries=3, sleep=sleeps.append)
    assert out.ok and sleeps == [1.0, 1.0]

    always = res(1, "unable to update local ref")
    n = 0

    def attempt() -> GitResult:
        nonlocal n
        n += 1
        return always

    r, out = fetch_with_retries(attempt, retries=2, sleep=lambda s: None)
    assert (out.kind, out.cause) == (FetchKind.FAILED, None) and n == 3
    _, out = fetch_with_retries(lambda: res(128, "boom"), retries=5, sleep=_no_sleep)
    assert out.kind is FetchKind.FAILED


def test_outcome_errors_map_to_codes_and_causes() -> None:
    assert outcome_error(classify_fetch(res(0)), res(0)) is None
    e = outcome_error(classify_fetch(res(128, "terminal prompts disabled")), res(128))
    assert e is not None and e.code == "auth_required"
    t = res(-9, timed_out=True)
    e = outcome_error(classify_fetch(t), t)
    assert (
        e is not None and e.code == "fetch_failed" and e.fields["details"] == {"cause": "timeout"}
    )
    e = outcome_error(classify_fetch(t), t, failure_code="clone_failed")
    assert (
        e is not None and e.code == "clone_failed" and e.fields["details"] == {"cause": "timeout"}
    )
    p = res(0, "error: x")
    e = outcome_error(classify_fetch(p), p)
    assert e is not None and e.fields["details"] == {"cause": "prune_incomplete"}
    e = outcome_error(classify_fetch(p), p, failure_code="clone_failed")
    assert e is not None and "details" not in e.fields
    for text, cause in (
        ("fatal: repository 'https://h/a.git/' not found", "repository_not_found"),
        (
            "fatal: unable to access 'https://h/': The requested URL returned error: 429",
            "rate_limited",
        ),
        ("fatal: unable to access 'https://h/': Could not resolve host: h", "network_unreachable"),
    ):
        r = res(128, text)
        for code in ("fetch_failed", "clone_failed"):
            e = outcome_error(classify_fetch(r), r, failure_code=code)
            assert e is not None and e.code == code and e.fields["details"] == {"cause": cause}
    e = outcome_error(classify_fetch(res(1, "x")), res(1, "x"))
    assert e is not None and e.code == "fetch_failed"


# -- porcelain parsing ---------------------------------------------------------------------------


def test_parse_status_v2() -> None:
    data = (
        b"# branch.oid abc\0"
        b"1 .M N... 100644 100644 100644 aaa bbb dir/my file.txt\0"
        b"2 R. N... 100644 100644 100644 aaa bbb R100 new name\0old name\0"
        b"u UU N... 1 2 3 4 aaa bbb ccc conflict.txt\0"
        b"? untracked file\0! ignored/\0"
    )
    got = parse_status_v2(data)
    assert [(e.kind, e.path) for e in got] == [
        ("1", "dir/my file.txt"), ("2", "new name"), ("u", "conflict.txt"),
        ("?", "untracked file"), ("!", "ignored/"),
    ]  # fmt: skip
    assert got[1].orig_path == "old name" and got[0].xy == ".M"


@pytest.mark.parametrize(
    "data", [b"1 .M N... 1 2 3 a b\0", b"2 R. N... 1 2 3 a b R1 only\0", b"x junk\0", b"u UU\0"]
)
def test_parse_status_v2_fails_closed(data: bytes) -> None:
    with pytest.raises(PorcelainError):
        parse_status_v2(data)


def test_parse_status_v2_matches_real_git(git: Git, repo: Path, private_home: Path) -> None:
    (repo / "a b.txt").write_text("1\n")
    (repo / "uni\u00e9.txt").write_text("1\n")
    plain_git(["add", "-A"], cwd=repo, home=private_home)
    plain_git(["commit", "-q", "-m", "x"], cwd=repo, home=private_home)
    (repo / "a b.txt").write_text("2\n")
    (repo / "new\nline.txt").write_text("n\n")
    r = git.run(["status", "--porcelain=v2", "-z", "--untracked-files=all"],
                git_dir=repo / ".git", work_tree=repo)  # fmt: skip
    got = {(e.kind, e.path) for e in parse_status_v2(r.stdout)}
    assert got == {("1", "a b.txt"), ("?", "new\nline.txt")}


def test_ls_files_tags_and_hidden_flags() -> None:
    got = hardgit.parse_ls_files_tagged(b"H a\0h b c\0S d\0s e\0")
    assert got == [("H", "a"), ("h", "b c"), ("S", "d"), ("s", "e")]
    assert [hardgit.hidden_index_flag(t) for t, _ in got] == [False, True, True, True]
    with pytest.raises(PorcelainError):
        hardgit.parse_ls_files_tagged(b"Hx\0")


def test_hidden_flags_are_reported_by_real_git(git: Git, repo: Path, private_home: Path) -> None:
    (repo / "a").write_text("1\n")
    (repo / "b").write_text("1\n")
    plain_git(["add", "-A"], cwd=repo, home=private_home)
    plain_git(["commit", "-q", "-m", "x"], cwd=repo, home=private_home)
    plain_git(["update-index", "--assume-unchanged", "a"], cwd=repo, home=private_home)
    plain_git(["update-index", "--skip-worktree", "b"], cwd=repo, home=private_home)
    r = git.run(["ls-files", "-v", "-z"], git_dir=repo / ".git", work_tree=repo)
    tags = dict((p, t) for t, p in hardgit.parse_ls_files_tagged(r.stdout))
    assert hardgit.hidden_index_flag(tags["a"]) and hardgit.hidden_index_flag(tags["b"])


def test_parse_worktree_list_matches_real_git(git: Git, repo: Path, private_home: Path) -> None:
    (repo / "a").write_text("1\n")
    plain_git(["add", "a"], cwd=repo, home=private_home)
    plain_git(["commit", "-q", "-m", "x"], cwd=repo, home=private_home)
    wt = repo.parent / "wt with space"
    plain_git(["worktree", "add", "-q", "--detach", str(wt)], cwd=repo, home=private_home)
    plain_git(["worktree", "lock", "--reason", "because", str(wt)], cwd=repo, home=private_home)
    r = git.run(["worktree", "list", "--porcelain"], git_dir=repo / ".git", work_tree=repo)
    got = parse_worktree_list(r.stdout, expected=2)
    assert [e.path for e in got] == [str(repo), str(wt)]
    assert got[0].branch == "refs/heads/main" and got[1].detached and got[1].locked == "because"


def test_parse_worktree_list_fails_closed_on_a_newline_in_a_path() -> None:
    data = b"worktree /a\nb\nHEAD abc\nbranch refs/heads/m\n\n"
    with pytest.raises(PorcelainError):
        parse_worktree_list(data, expected=None)
    with pytest.raises(PorcelainError):
        parse_worktree_list(b"HEAD abc\n\n", expected=None)


def test_refuse_newline_path() -> None:
    assert hardgit.refuse_newline_path("/a/b") == "/a/b"
    with pytest.raises(UnsafePathError):
        hardgit.refuse_newline_path("/a\nb")


# -- the gateway runner ----------------------------------------------------------------------------


def test_run_git_uses_exactly_the_given_environment(tmp_path: Path) -> None:
    r = run_git(
        [sys.executable, "-c", "import os,sys;sys.stdout.write(','.join(sorted(os.environ)))"],
        env={"ONLY": "1"}, cwd=str(tmp_path),
    )  # fmt: skip
    assert r.returncode == 0 and "ONLY" in r.out and "HOME" not in r.out


def test_timeout_kills_the_process_group_by_exact_pid(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {pidfile}; wait"
    r = run_git(["/bin/sh", "-c", script], env={"PATH": "/usr/bin:/bin"}, cwd=str(tmp_path),
                timeout=1.0)  # fmt: skip
    child = int(pidfile.read_text())
    try:
        assert r.timed_out
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            import time

            time.sleep(0.05)
        else:
            pytest.fail("grandchild survived the group kill")
    finally:
        try:
            os.kill(child, 9)  # exact pid recorded at launch, only if the assertion failed
        except ProcessLookupError:
            pass


def _no_sleep(seconds: float) -> None:
    raise AssertionError("a non-contention failure must not be retried")


# -- review round 1 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sub,args",
    [
        ("diff", ["--ext-diff"]), ("diff", ["--ext"]), ("diff", ["--ex"]), ("log", ["--ext-d"]),
        ("show", ["--no-no-ext-diff"]), ("diff", ["--no-no-textconv"]),
        ("diff", ["--textconv"]), ("diff", ["--textc"]), ("show", ["--textconv"]),
        ("cat-file", ["--textconv", "HEAD:x"]), ("cat-file", ["--textc", "HEAD:x"]),
        ("diff", ["--submodule=diff"]), ("diff", ["--submodule=log"]), ("diff", ["--submodule"]),
        ("diff-tree", ["--submod=diff"]), ("log", ["--submodule=diff"]),
        ("diff", ["--ignore-submodules=none"]), ("diff", ["--ignore-submodules=dirty"]),
        ("diff-files", ["--ignore-submodules=untracked"]), ("diff", ["--ignore-submodules"]),
        ("show", ["--ignore-sub=none"]), ("diff", ["--no-ext-diff", "--ext-diff"]),
        ("diff", ["x", "--ext-diff", "--", "y"]),
        ("status", ["--ignore-submodules=none"]), ("status", ["--ignore-sub=none"]),
        ("status", ["--ig=dirty"]), ("status", ["--ignore-submodules"]),
        ("status", ["--ignore-submodules=untracked"]),
        ("status", ["--recurse-submodules"]), ("status", ["--recurse-sub"]),
        ("status", ["--rec"]), ("status", ["--recurse-submodules=yes"]),
        ("diff", ["--recurse-submodules"]), ("fetch", ["--recurse-submodules=on-demand"]),
        ("ls-files", ["--recurse-submodules"]), ("grep", ["--recurse-sub", "x"]),
    ],
)  # fmt: skip
def test_options_that_undo_the_guards_are_rejected(
    git: Git, repo: Path, sub: str, args: list[str]
) -> None:
    with pytest.raises(ValueError):
        git.build([sub, *args], git_dir=repo / ".git", cwd=repo)


@pytest.mark.parametrize(
    "sub,args",
    [
        ("diff", ["--submodule=short"]), ("diff", ["--ignore-submodules=all"]),
        ("diff", ["--text"]), ("diff", ["--no-ext-diff"]), ("diff", ["--no-textconv"]),
        ("diff", ["--", "--ext-diff"]), ("diff", ["x", "--", "--textconv", "--submodule=log"]),
        ("cat-file", ["--filters", "HEAD:x"]), ("diff", ["--exit-code", "--stat"]),
        ("rev-list", ["--submodule=diff", "x"]), ("status", ["--untracked-files=no"]),
        ("status", ["--ignore-submodules=all"]), ("status", ["--no-recurse-submodules"]),
        ("status", ["--", "--recurse-submodules", "--ignore-submodules=none"]),
    ],
)  # fmt: skip
def test_harmless_options_are_accepted(git: Git, repo: Path, sub: str, args: list[str]) -> None:
    git.build([sub, *args], git_dir=repo / ".git", cwd=repo)


def test_guards_come_first_and_unconditionally(git: Git, repo: Path) -> None:
    for sub in ("diff", "diff-files", "diff-index", "diff-tree", "log", "show"):
        argv = git.build([sub, "--no-ext-diff", "--", "--no-ext-diff"], git_dir=repo / ".git",
                         cwd=repo).argv  # fmt: skip
        i = argv.index(sub)
        assert argv[i + 1 : i + 3] == ["--no-ext-diff", "--ignore-submodules=all"], sub
    argv = git.build(["diff"], git_dir=repo / ".git", cwd=repo).argv
    assert argv[argv.index("diff") + 1 :] == [
        "--no-ext-diff", "--ignore-submodules=all", "--no-textconv",
    ]  # fmt: skip


@pytest.mark.parametrize(
    "extra,expected",
    [([], True), (["-uno"], False), (["-u"], False), (["--untracked-files=no"], False),
     (["--untracked"], False), (["--ignored=no"], True), (["--", "-uno"], True)],
)  # fmt: skip
def test_status_lists_untracked_files_unless_the_caller_chooses(
    git: Git, repo: Path, extra: list[str], expected: bool
) -> None:
    argv = git.build(["status", *extra], git_dir=repo / ".git", cwd=repo).argv
    assert ("--untracked-files=all" in argv) is expected


def test_scheme_needs_a_tool_owned_repository(git: Git, repo: Path) -> None:
    with pytest.raises(ValueError, match="tool_owned"):
        git.build(["ls-remote", "x"], git_dir=repo / ".git", cwd=repo, scheme="https")
    with pytest.raises(ValueError, match="tool_owned"):
        git.run(["ls-remote", "x"], git_dir=repo / ".git", cwd=repo, scheme="file", timeout=5)
    git.build(["ls-remote", "x"], git_dir=repo / ".git", cwd=repo, scheme="file", tool_owned=True)
    # no scheme, no flag needed
    git.build(["ls-remote", "x"], git_dir=repo / ".git", cwd=repo)


@pytest.mark.parametrize("bad", ["git", "", "./git", "bin/git"])
def test_git_bin_must_be_absolute(bad: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        build_env(git_bin=bad, home="/h")
    with pytest.raises(ValueError, match="absolute"):
        Git(Gateway(), bad, "/h")
    assert not any(e == "" for e in build_env(git_bin="/u/git", home="/h")["PATH"].split(":"))


@pytest.mark.parametrize(
    "key,ok",
    [
        ("http.extraheader", True), ("http.https://h/.extraHeader", True),
        ("HTTP.https://h/.EXTRAHEADER", True), ("http.proxy", False),
        ("http.https://h/.proxy", False), ("core.fsmonitor", False),
        ("remote.o.extraheader", False),
        ("nodot", False), ("http.extraheader.x", False), ("filter.a.clean", False),
    ],
)  # fmt: skip
def test_extra_config_takes_only_extra_headers(git: Git, repo: Path, key: str, ok: bool) -> None:
    if ok:
        git.build(["rev-parse", "HEAD"], git_dir=repo / ".git", cwd=repo, extra_config=[(key, "v")])
    else:
        with pytest.raises(ValueError, match="extraheader"):
            git.build(["rev-parse", "HEAD"], git_dir=repo / ".git", cwd=repo,
                      extra_config=[(key, "v")])  # fmt: skip


def test_run_applies_a_default_timeout_that_the_caller_can_override(
    private_home: Path, repo: Path
) -> None:
    seen: list[object] = []

    def runner(argv: list[str], **kw: object) -> GitResult:
        seen.append(kw.get("timeout"))
        return res(0)

    g = Git(Gateway(git_runner=runner), GIT, str(private_home))
    g.run(["rev-parse", "HEAD"], git_dir=repo / ".git", cwd=repo)
    g.run(["rev-parse", "HEAD"], git_dir=repo / ".git", cwd=repo, timeout=7)
    g.run(["ls-remote", "x"], git_dir=repo / ".git", cwd=repo, scheme="file", tool_owned=True,
          timeout=9)  # fmt: skip
    # each run is preceded by the config listing (30 seconds)
    assert seen == [30, hardgit.DEFAULT_TIMEOUT, 30, 7, 30, 9]
    assert hardgit.DEFAULT_TIMEOUT == 300


def test_non_utf8_names_survive_the_round_trip() -> None:
    raw = b"filter.ev\xffil.clean\nx\0http.https://h/\xff.extraheader\nA: b\0"
    entries = hardgit.parse_config_list(raw)
    (name,) = hardgit.filter_names(entries)
    (header,) = hardgit.extra_header_keys(entries)
    assert os.fsencode(name) == b"ev\xffil"
    assert os.fsencode(header) == b"http.https://h/\xff.extraheader"
    env = build_env(
        git_bin="/u/git", home="/h",
        config=[(header, ""), *hardgit.filter_config([name], {})],
    )  # fmt: skip
    assert os.fsencode(env["GIT_CONFIG_KEY_0"]) == b"http.https://h/\xff.extraheader"
    assert os.fsencode(env["GIT_CONFIG_KEY_1"]) == b"filter.ev\xffil.clean"


def test_names_parse_from_real_config_output(git: Git, repo: Path, private_home: Path) -> None:
    for key, value in (
        ("filter.a.b.clean", "x"), ("filter.My.Lfs.smudge", "y"),
        ("http.https://x/.extraHeader", "A: b"), ("http.extraheader", "C: d"),
    ):  # fmt: skip
        assert plain_git(["config", key, value], cwd=repo, home=private_home).returncode == 0
    r = git.run(["config", "--list", "-z"], git_dir=repo / ".git", cwd=repo)
    entries = hardgit.parse_config_list(r.stdout)
    assert hardgit.filter_names(entries) == ["a.b", "My.Lfs"]
    assert hardgit.extra_header_keys(entries) == ["http.https://x/.extraheader", "http.extraheader"]


# -- worktree list (item 10) --------------------------------------------------------------------

FORGED = b"worktree /a\nHEAD a\n\nworktree /v\nHEAD b\nbranch refs/heads/m\n\n"


def test_worktree_list_count_must_match_the_linked_directories() -> None:
    assert len(parse_worktree_list(FORGED, expected=2)) == 2
    with pytest.raises(WtctlError) as e:
        parse_worktree_list(FORGED, expected=1)
    assert e.value.code == "internal"


def test_a_newline_path_cannot_forge_a_worktree_entry_in_real_git(
    git: Git, repo: Path, private_home: Path
) -> None:
    (repo / "a").write_text("1\n")
    plain_git(["add", "a"], cwd=repo, home=private_home)
    plain_git(["commit", "-q", "-m", "x"], cwd=repo, home=private_home)
    evil = repo.parent / "x\nHEAD 0\n\nworktree " / "v"
    r = plain_git(["worktree", "add", "-q", "--detach", str(evil)], cwd=repo, home=private_home)
    assert r.returncode == 0, r.stderr
    out = git.run(["worktree", "list", "--porcelain"], git_dir=repo / ".git", work_tree=repo)
    # Locally the forged text is perfectly valid and shows three entries...
    assert len(parse_worktree_list(out.stdout, expected=None)) == 3
    # ...but there are only two worktrees (main plus one linked directory).
    with pytest.raises(WtctlError) as e:
        parse_worktree_list(out.stdout, expected=2)
    assert e.value.code == "internal"


@pytest.mark.parametrize(
    "data",
    [
        b"worktree /a\nHEAD a\nHEAD b\n\n",  # duplicate attribute
        b"worktree /a\nbranch refs/heads/m\n\n",  # no HEAD
        b"worktree /a\nHEAD a\nbranch main\n\n",  # branch outside refs/
        b"worktree /a\nHEAD a\ndetached\ndetached\n\n",
    ],
)
def test_worktree_list_block_rules(data: bytes) -> None:
    with pytest.raises(PorcelainError):
        parse_worktree_list(data, expected=None)


def test_a_bare_main_worktree_needs_no_head() -> None:
    got = parse_worktree_list(b"worktree /b\nbare\n\n", expected=1)
    assert got[0].bare


# -- filter policy (items 13, 14) -----------------------------------------------------------------


def _global_config(home: Path, text: str) -> None:
    (home / ".gitconfig").write_text(text)


def _policy(home: Path, priv: Path, allow: list[str]) -> hardgit.FilterPolicy:
    return hardgit.load_filter_policy(
        Gateway(), GIT, home=str(home), xdg_config_home=None, allow=allow,
        path_env="/usr/bin:/bin", private_dir=str(priv),
    )  # fmt: skip


def test_valueless_required_is_honored_as_true(private_home: Path, tmp_path: Path) -> None:
    _global_config(private_home, '[filter "x"]\n\tprocess = /bin/sh -c\n\trequired\n\tclean\n')
    policy = _policy(private_home, tmp_path, ["x"])
    assert policy.drivers["x"] == {"process": "/bin/sh -c", "required": "true"}


def test_global_config_is_read_with_a_ceiling_and_no_discovery(
    private_home: Path, tmp_path: Path
) -> None:
    _global_config(private_home, '[filter "x"]\n\tprocess = /bin/sh\n')
    seen: list[dict[str, str]] = []

    def runner(argv: list[str], **kw: object) -> GitResult:
        seen.append(dict(kw["env"]))  # type: ignore[call-overload]
        return GitResult(0, b"filter.x.process\n/bin/sh\0", b"")

    priv = tmp_path / "state" / "p"
    priv.mkdir(parents=True)
    hardgit.load_filter_policy(
        Gateway(git_runner=runner), GIT, home=str(private_home), xdg_config_home=None,
        allow=["x"], path_env="/usr/bin:/bin", private_dir=str(priv),
    )  # fmt: skip
    assert seen and all(e["GIT_CEILING_DIRECTORIES"] == str(priv.parent) for e in seen)


def test_unparsable_process_stays_blanked_with_a_warning(
    private_home: Path, tmp_path: Path
) -> None:
    _global_config(private_home, '[filter "x"]\n\tprocess = "\'unterminated"\n')
    policy = _policy(private_home, tmp_path, ["x"])
    assert policy.drivers == {}
    assert any("unparsable" in w for w in policy.warnings)


def test_process_pointing_at_a_missing_binary_stays_blanked(
    private_home: Path, tmp_path: Path
) -> None:
    _global_config(private_home, '[filter "x"]\n\tprocess = /nonexistent/dir/tool\n')
    policy = _policy(private_home, tmp_path, ["x"])
    assert policy.drivers == {}
    assert any("not found" in w for w in policy.warnings)


# -- fetch classification (item 15) ---------------------------------------------------------------


def test_prune_incomplete_message_uses_the_first_error_line() -> None:
    err = "error: could not delete reference refs/heads/gone: locked\nFrom x\n - [deleted] gone\n"
    r = res(0, err)
    e = outcome_error(classify_fetch(r), r)
    assert e is not None and "could not delete reference" in e.message
    assert e.fields["details"] == {"cause": "prune_incomplete"}


def test_markers_in_remote_lines_do_not_classify_as_auth() -> None:
    r = res(128, "remote: fatal: could not read Username for 'x'\nfatal: unable to access x")
    assert classify_fetch(r).kind is FetchKind.FAILED


def test_prune_blocked_by_packed_refs_lock_in_real_git(
    git: Git, tmp_path: Path, private_home: Path
) -> None:
    # Spike F4: a --prune that cannot delete a ref exits 0 (git 2.34) or 1 (git 2.56) with an
    # `error:` line.
    def pg(args: list[str], cwd: Path) -> None:
        r = plain_git(args, cwd=cwd, home=private_home)
        assert r.returncode == 0, (args, r.stderr)

    rem, work, mirror, priv = (tmp_path / n for n in ("r.git", "w", "m.git", "priv"))
    work.mkdir()
    priv.mkdir()
    pg(["init", "-q", "--bare", "-b", "main", str(rem)], tmp_path)
    pg(["init", "-q", "-b", "main"], work)
    (work / "f").write_text("1")
    pg(["add", "f"], work)
    pg(["commit", "-q", "-m", "1"], work)
    pg(["branch", "gone"], work)
    pg(["push", "-q", str(rem), "main", "gone"], work)
    pg(["init", "-q", "--bare", str(mirror)], tmp_path)
    pg(["remote", "add", "origin", str(rem)], mirror)
    args = ["fetch", "--no-tags", "--prune", "origin", "+refs/heads/*:refs/heads/*"]

    def fetch() -> GitResult:
        return git.run(args, git_dir=mirror, cwd=priv, scheme="file", tool_owned=True, timeout=30)

    assert classify_fetch(fetch()).ok
    pg(["update-ref", "-d", "refs/heads/gone"], rem)
    (mirror / "packed-refs.lock").write_text("")
    r = fetch()
    assert r.returncode in (0, 1) and "error:" in r.err
    outcome = classify_fetch(r)
    assert (outcome.kind, outcome.cause) == (FetchKind.FAILED, "prune_incomplete")
    e = outcome_error(outcome, r)
    assert e is not None and "could not delete reference" in e.message


def test_the_error_module_allows_exactly_the_fetch_causes() -> None:
    from wtctl import errors

    assert errors._CAUSES["fetch_failed"] == set(FETCH_CAUSES)
    assert errors._CAUSES["clone_failed"] == set(FETCH_CAUSES) - {"prune_incomplete"}


# -- review round 1 (#33) ------------------------------------------------------------------------

# Characters `str.splitlines` treats as line ends but git does not: inside a `remote:` line they
# must not start a fake unprefixed line.
_FAKE_BREAKS = ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\r"]
_SERVER_PAYLOADS = [
    ("repository_not_found", "fatal: repository 'x' not found"),
    ("network_unreachable", "fatal: unable to access 'http://h/': Could not resolve host: h"),
    ("rate_limited", "fatal: unable to access 'http://h/': The requested URL returned error: 429"),
    ("AUTH", "fatal: Authentication failed for 'http://h/'"),
]


@pytest.mark.parametrize("brk", _FAKE_BREAKS, ids=[repr(b) for b in _FAKE_BREAKS])
@pytest.mark.parametrize("cause,payload", _SERVER_PAYLOADS)
def test_odd_line_breaks_inside_a_remote_line_do_not_forge_a_line(
    brk: str, cause: str, payload: str
) -> None:
    out = classify_fetch(res(128, "remote: hi" + brk + payload))
    assert (out.kind, out.cause) == (FetchKind.FAILED, None)


def test_a_real_newline_still_separates_lines_and_a_trailing_cr_is_stripped() -> None:
    assert classify_fetch(res(128, "x\r\n" + NOT_FOUND + "\r\n")).cause == "repository_not_found"


@pytest.mark.parametrize("brk", _FAKE_BREAKS, ids=[repr(b) for b in _FAKE_BREAKS])
def test_the_message_line_is_not_split_on_odd_breaks(brk: str) -> None:
    forged = "fatal: unable to access 'http://forged/': The requested URL returned error: 429"
    err = "remote: hi" + brk + forged + "\n" + HTTP_ERR + "429\n" + NO_READ
    r = res(128, err)
    e = outcome_error(classify_fetch(r), r)
    assert e is not None and e.message == f"git failed ({HTTP_ERR}429)"


@pytest.mark.parametrize(
    "stderr,matching",
    [
        (
            "fatal: unable to access 'http://h/a.git/': Could not resolve host: h\n" + NO_READ,
            "fatal: unable to access 'http://h/a.git/': Could not resolve host: h",
        ),
        (
            "ssh: connect to host h port 22: Connection refused\n" + NO_READ,
            "ssh: connect to host h port 22: Connection refused",
        ),
        (
            "ssh: Could not resolve hostname h: Name or service not known\n" + NO_READ,
            "ssh: Could not resolve hostname h: Name or service not known",
        ),
        # the matching line is not the first
        (
            "warning: something first\n" + NOT_FOUND + "\n" + NO_READ,
            NOT_FOUND,
        ),
        (
            "fatal: '/x/y.git' does not appear to be a git repository\n" + NO_READ,
            "fatal: '/x/y.git' does not appear to be a git repository",
        ),
        # a curl line after a `remote:` line
        (
            "remote: Not so fast\n" + HTTP_ERR + "429\n" + NO_READ,
            HTTP_ERR + "429",
        ),
    ],
)
def test_a_transport_failure_message_is_the_matching_line(stderr: str, matching: str) -> None:
    r = res(128, stderr)
    outcome = classify_fetch(r)
    assert outcome.cause in TRANSPORT_CAUSES
    e = outcome_error(outcome, r)
    assert e is not None and e.message == f"git failed ({matching})"
