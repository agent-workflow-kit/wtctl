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
        "GIT_NO_REPLACE_OBJECTS", "GIT_GRAFT_FILE", "GIT_SSH_VARIANT",
    }  # fmt: skip
    assert env["PATH"] == "/opt/g/bin:/usr/bin:/bin"
    assert env["GIT_ALLOW_PROTOCOL"] == "none"
    assert env["GIT_NO_LAZY_FETCH"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_GRAFT_FILE"] == "/dev/null/none" and env["GIT_NO_REPLACE_OBJECTS"] == "1"
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
        "http.followRedirects=false",
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
    cmd = git.build(["ls-remote", "x"], git_dir=repo / ".git", cwd=repo, scheme="https")
    cs = [cmd.argv[i + 1] for i, a in enumerate(cmd.argv) if a == "-c"]
    assert "http.lowSpeedLimit=1000" in cs and "http.lowSpeedTime=60" in cs
    with pytest.raises(ValueError, match="timeout"):
        git.run(["ls-remote", "x"], git_dir=repo / ".git", cwd=repo, scheme="https")


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
    argv = git.build(["status", "--ignore-submodules=none"], git_dir=repo / ".git", cwd=repo).argv
    assert "--ignore-submodules=all" not in argv


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
        (res(1, "error: cannot lock ref 'refs/heads/a'"), FetchKind.LOCK_CONTENTION, None),
        (res(1, "error: unable to update local ref"), FetchKind.LOCK_CONTENTION, None),
        (res(1, "something else"), FetchKind.FAILED, None),
        (res(128, "fatal: could not read Username for 'https://h'"), FetchKind.AUTH_REQUIRED, None),
        (res(128, "fatal: terminal prompts disabled"), FetchKind.AUTH_REQUIRED, None),
        (res(128, "remote: Authentication failed"), FetchKind.AUTH_REQUIRED, None),
        (res(128, "git@h: Permission denied (publickey)."), FetchKind.AUTH_REQUIRED, None),
        (res(128, "Host key verification failed."), FetchKind.AUTH_REQUIRED, None),
        (res(128, "fatal: unable to access: The requested URL returned error: 401"),
         FetchKind.AUTH_REQUIRED, None),
        (res(128, "fatal: unable to access: Could not resolve host"), FetchKind.FAILED, None),
        (res(2), FetchKind.FAILED, None),
        (res(-9, timed_out=True), FetchKind.FAILED, "timeout"),
        (res(0, "error: x", timed_out=True), FetchKind.FAILED, "timeout"),
    ],
)  # fmt: skip
def test_classify_fetch(result: GitResult, kind: FetchKind, cause: str | None) -> None:
    out = classify_fetch(result)
    assert (out.kind, out.cause) == (kind, cause)


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
    got = parse_worktree_list(r.stdout)
    assert [e.path for e in got] == [str(repo), str(wt)]
    assert got[0].branch == "refs/heads/main" and got[1].detached and got[1].locked == "because"


def test_parse_worktree_list_fails_closed_on_a_newline_in_a_path() -> None:
    data = b"worktree /a\nb\nHEAD abc\nbranch refs/heads/m\n\n"
    with pytest.raises(PorcelainError):
        parse_worktree_list(data)
    with pytest.raises(PorcelainError):
        parse_worktree_list(b"HEAD abc\n\n")


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
