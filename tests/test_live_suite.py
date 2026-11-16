"""The live suite's guard rails, checked in CI with no network: every live test is marked, is
deselected by default, skips (touching nothing) when its prerequisites are absent, cannot leak a
credential, and uses only documented variables."""

from __future__ import annotations

import ast
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.live import harness, liveenv, scenarios, worlds
from tests.live.harness import LiveWorld, Out, Raw
from tests.live.liveenv import FORGES, GITHUB, GITLAB
from wtctl.gateway import Gateway, GitResult

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "tests" / "live"
HELPERS = ("liveenv.py", "harness.py", "scenarios.py", "worlds.py", "conftest.py")
GUARDED_FILES = (
    *sorted(LIVE_DIR.glob("*.py")),
    *sorted((ROOT / "tests").glob("test_live_*.py")),
    ROOT / "tests" / "live_probe.py",
    ROOT / "docs" / "live-testing.md",
)
TOKEN = "tok-SECRET-0123456789abcdef"


def clean_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """An explicit environment for a pytest subprocess: private HOME, no WTCTL_LIVE_*."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".state"),
        "XDG_DATA_HOME": str(home / ".data"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
        **extra,
    }


def pytest_run(tmp_path: Path, args: list[str], **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        env=clean_env(tmp_path, **extra),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.fixture(scope="module")
def probed(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, object]]:
    """Collect-only over tests/live with the real configuration but every marker selected
    (``-m ""``), so an unmarked test would show up in the rows."""
    tmp = tmp_path_factory.mktemp("probe")
    out = tmp / "items.json"
    r = pytest_run(
        tmp,
        ["tests/live", "--collect-only", "-q", "-m", "", "-p", "tests.live_probe"],
        WTCTL_PROBE_OUT=str(out),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(out.read_text())


# -- marker and default deselection --------------------------------------------------------


def test_the_live_suite_collects_tests(probed: list[dict[str, object]]) -> None:
    assert len(probed) >= 30
    assert all(str(item["id"]).startswith("tests/live/") for item in probed)


def test_every_live_test_is_marked_live_and_declares_its_needs(
    probed: list[dict[str, object]],
) -> None:
    for item in probed:
        assert "live" in item["markers"], item["id"]  # type: ignore[operator]
        assert item["needs"], f"{item['id']} declares no prerequisites"


def test_a_marker_free_collection_finds_nothing_else(tmp_path: Path) -> None:
    """With ``live`` excluded, tests/live collects no test: nothing there escapes the marker."""
    r = pytest_run(
        tmp_path, ["tests/live", "--collect-only", "-q", "-o", "addopts=", "-m", "not live"]
    )
    assert "tests/live/" not in r.stdout, r.stdout
    assert r.returncode == 5, r.stdout + r.stderr  # no tests collected


def test_the_default_run_deselects_the_live_tests(tmp_path: Path) -> None:
    r = pytest_run(tmp_path, ["tests/live", "--collect-only", "-q"])
    assert "tests/live/" not in r.stdout, r.stdout
    assert re.search(r"\d+ deselected", r.stdout), r.stdout


# -- skipping without touching anything ----------------------------------------------------


def test_every_live_test_is_skipped_by_an_empty_environment(
    probed: list[dict[str, object]],
) -> None:
    for item in probed:
        why = liveenv.all_unmet(list(item["needs"]), environ={}, which=lambda _n: None)  # type: ignore[arg-type]
        assert why, f"{item['id']} would run with no configuration"


IMPORT_TIME_CALLS = {
    "subprocess.run": 'import subprocess\nsubprocess.run(["true"])\n',
    "subprocess.Popen": 'import subprocess\nsubprocess.Popen(["true"])\n',
    "os.system": 'import os\nos.system("true")\n',
    "socket": 'import socket\nsocket.socket().connect(("127.0.0.1", 9))\n',
}


@pytest.mark.parametrize("call", sorted(IMPORT_TIME_CALLS))
def test_an_import_time_call_to_the_outside_world_fails_the_probe(
    call: str, tmp_path: Path
) -> None:
    """Mutation check: a test module that starts a process or connects when it is imported makes
    the collect-only probe fail."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "test_mutant.py").write_text(IMPORT_TIME_CALLS[call] + "\ndef test_x():\n    pass\n")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", "-q",
         "-p", "tests.live_probe", str(proj)],
        cwd=proj,
        env=clean_env(tmp_path, PYTHONPATH=str(ROOT), WTCTL_PROBE_OUT=str(tmp_path / "o.json")),
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert r.returncode != 0, r.stdout + r.stderr
    assert "touched the outside world" in r.stdout + r.stderr


SYNTH_CONFTEST = """
from tests.live.conftest import _prerequisites  # noqa: F401
"""

SYNTH_TEST = """
import pytest
from tests.live import harness

def test_body():
    # reaching the body means the gate let it through: it must hit the seam, which fails
    harness.spawn(["true"], env={}, cwd="/", timeout=1)

test_body = pytest.mark.needs("github:public")(test_body)
"""


def synth_run(tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "conftest.py").write_text(SYNTH_CONFTEST)
    seam = "harness.spawn = lambda *a, **k: (_ for _ in ()).throw(AssertionError('SEAM'))\n    "
    (proj / "test_synth.py").write_text(
        SYNTH_TEST.replace("harness.spawn(", seam + "harness.spawn(")
    )
    (proj / "pytest.ini").write_text("[pytest]\nmarkers = needs: x\n")
    env = clean_env(tmp_path, PYTHONPATH=str(ROOT), **extra)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rs", "-q", str(proj)],
        cwd=proj, env=env, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip


def test_the_gate_skips_before_the_body_with_no_variables(tmp_path: Path) -> None:
    r = synth_run(tmp_path)
    assert "1 skipped" in r.stdout, r.stdout + r.stderr
    assert "WTCTL_LIVE_GITHUB_PUBLIC_REPO" in r.stdout
    assert "SEAM" not in r.stdout


def test_the_gate_lets_the_body_run_once_configured(tmp_path: Path) -> None:
    r = synth_run(tmp_path, WTCTL_LIVE_GITHUB_PUBLIC_REPO="owner/name")
    assert "1 failed" in r.stdout and "SEAM" in r.stdout, r.stdout + r.stderr


# -- the environment contract --------------------------------------------------------------


def test_documented_variables_match_the_code() -> None:
    doc = (ROOT / "docs" / "live-testing.md").read_text()
    documented = set(re.findall(r"WTCTL_LIVE_[A-Z0-9_]+", doc))
    assert documented == set(liveenv.VAR_NAMES)


def test_code_uses_only_declared_variables() -> None:
    declared = set(liveenv.VAR_NAMES)
    for path in LIVE_DIR.glob("*.py"):
        for name in re.findall(r"WTCTL_LIVE_[A-Z0-9_]+", path.read_text()):
            assert name in declared, f"{path.name} uses undeclared {name}"


def test_the_docs_table_has_exactly_one_row_per_variable() -> None:
    doc = (ROOT / "docs" / "live-testing.md").read_text()
    rows = re.findall(r"^\| `(WTCTL_LIVE_[A-Z0-9_]+)` \|", doc, re.M)
    assert sorted(rows) == sorted(liveenv.VAR_NAMES)


def _env_touches(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "os" and node.attr in {"environ", "environb", "getenv", "putenv"}:
                lines.append(node.lineno)
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            if any(a.name in {"environ", "environb", "getenv", "putenv"} for a in node.names):
                lines.append(node.lineno)
    return lines


def test_only_liveenv_reads_the_environment() -> None:
    for path in sorted(LIVE_DIR.glob("*.py")):
        if path.name == "liveenv.py":
            continue
        assert not _env_touches(ast.parse(path.read_text())), f"{path.name} reads the environment"
    assert _env_touches(ast.parse("import os\nos.environ['X']"))
    assert _env_touches(ast.parse("from os import getenv"))


def test_literal_forge_variable_suffixes_are_declared() -> None:
    """``forge.var("X")`` and ``project_path(forge, "X")`` with a literal suffix name a variable
    that exists for every forge."""
    seen = 0
    for path in sorted(LIVE_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            idx = {"var": 0, "project_path": 1, "_project": 1}.get(name)
            if idx is None or len(node.args) <= idx:
                continue
            arg = node.args[idx]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                seen += 1
                for forge in FORGES.values():
                    assert forge.var(arg.value) in liveenv.VAR_NAMES, (path.name, arg.value)
    assert seen >= 8


def test_forge_variable_names_are_derived_not_misspelled() -> None:
    names = liveenv.VAR_NAMES
    for f in (GITHUB, GITLAB):
        for suffix in ("PUBLIC_REPO", "PRIVATE_REPO", "TOKEN_ENV", "PUSH_REPO", "PUSH_TOKEN_ENV"):
            assert f.var(suffix) in names


def test_getenv_refuses_an_undocumented_name() -> None:
    with pytest.raises(KeyError):
        liveenv.getenv("WTCTL_LIVE_NOT_A_VARIABLE", {})


@pytest.mark.parametrize(
    ("value", "ok"),
    [("owner/name", True), ("group/sub/name", True), ("a b/c", False), ("../x", False),
     ("owner/name.git", False), ("owner", False), ("owner//x", False), ("o/ñ", False),
     ("-x/y", True), ("o/" + "a" * 500, False)],
)  # fmt: skip
def test_project_path_validation(value: str, ok: bool) -> None:
    assert liveenv.valid_project_path(value) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [("github.com/org/repo", True), ("gitlab.com/g/s/repo", True), ("org/repo", False),
     ("code.example/org/repo", False), ("github.com.evil.example/o/r", False),
     ("evilgithub.com/o/r", False), ("github.com/o", True), ("github.com", False)],
)  # fmt: skip
def test_the_lfs_repository_must_be_on_one_of_the_two_forges(value: str, ok: bool) -> None:
    assert liveenv.valid_repo_id(value) is ok


def test_unmet_messages_name_variables_never_values() -> None:
    env = {GITHUB.var("PRIVATE_REPO"): "owner/name", GITHUB.var("TOKEN_ENV"): "MY_TOK"}
    why = liveenv.unmet("github:private", env, lambda _n: "/bin/x")
    assert why and "MY_TOK" not in why and "owner/name" not in why
    env["MY_TOK"] = TOKEN
    assert liveenv.unmet("github:private", env, lambda _n: "/bin/x") is None


def test_a_token_variable_name_must_be_a_plain_identifier() -> None:
    env = {GITHUB.var("TOKEN_ENV"): "A B; rm -rf", "A B; rm -rf": TOKEN}
    assert liveenv.token_value(GITHUB.var("TOKEN_ENV"), env) is None


@pytest.mark.parametrize(
    "name",
    ["HOME", "PATH", "XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "GIT_ASKPASS", "SSH_AUTH_SOCK",
     "WTCTL_LIVE_GITHUB_TOKEN_ENV", "LC_ALL", "LD_PRELOAD", "PYTHONPATH", "LANG",
     "HTTPS_PROXY", "http_proxy", "NO_PROXY", "all_proxy", "SSL_CERT_FILE", "CURL_CA_BUNDLE",
     "GCM_CREDENTIAL_STORE", "GH_TOKEN", "GH_HOST", "GLAB_CONFIG_DIR"],
)  # fmt: skip
def test_a_token_variable_may_not_name_an_isolation_variable(name: str) -> None:
    env = {GITHUB.var("TOKEN_ENV"): name, name: TOKEN}
    assert liveenv.token_env_name(GITHUB.var("TOKEN_ENV"), env) is None
    assert liveenv.token_value(GITHUB.var("TOKEN_ENV"), env) is None


def test_the_child_environment_keeps_isolation_over_a_token_named_like_it(tmp_path: Path) -> None:
    w = LiveWorld(
        tmp_path / "w", host="forge.example", project="o/r",
        tokens={"HOME": "/elsewhere", "PATH": "/evil", "XDG_CONFIG_HOME": "/x"},
    )  # fmt: skip
    env = w.env()
    assert env["HOME"] == str(w.home)
    assert env["PATH"] == liveenv.PATH_SNAPSHOT
    assert env["XDG_CONFIG_HOME"] == str(w.home / ".config")


@pytest.mark.parametrize(
    ("argv", "ok"),
    [(["gh", "auth", "token", "--hostname", "{host}"], True),
     (["/usr/bin/glab", "auth", "token"], True),
     (["gh", "api", "-X", "DELETE", "/repos/o/r"], False), (["gh"], False),
     (["/opt/bin/gh", "repo", "delete", "o/r"], False), (["glab", "auth", "login"], False),
     (["gh", "token", "auth"], False), (["pass", "show", "forge/token"], True),
     (["sh", "-c", "echo x"], False), (["/bin/bash", "-c", "x"], False),
     (["zsh", "-c", "x"], False), (["dash", "-c", "x"], False),
     (["env", "gh", "auth", "token"], False), (["xargs", "echo"], False),
     (["python3", "-c", "x"], False), (["/usr/bin/python3.12", "x"], False),
     (["perl", "-e", "1"], False), (["ruby", "-e", "1"], False), (["node", "-e", "1"], False),
     (["busybox", "sh"], False)],
)  # fmt: skip
def test_a_forge_cli_token_command_may_only_run_auth_token(argv: list[str], ok: bool) -> None:
    env = {GITHUB.var("TOKEN_COMMAND"): json.dumps(argv)}
    assert (liveenv.token_command(GITHUB, env) == argv) is ok
    assert liveenv.token_command_is_read_only(argv) is ok


def test_the_glab_default_config_directory_is_glab_cli(tmp_path: Path) -> None:
    (tmp_path / "glab-cli").mkdir()
    (tmp_path / "gh").mkdir()
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    assert liveenv.config_dir(GITLAB, env) == str(tmp_path / "glab-cli")
    assert liveenv.config_dir(GITHUB, env) == str(tmp_path / "gh")


def test_token_command_needs_its_binary_and_a_json_argv() -> None:
    base = {GITHUB.var("PRIVATE_REPO"): "o/r", GITHUB.var("TOKEN_ENV"): "T", "T": TOKEN}
    assert (
        liveenv.unmet("github:token_command", base, lambda _n: None)
        == "the gh binary is not on PATH"
    )
    assert liveenv.token_command(GITHUB, base) == list(liveenv.DEFAULT_GITHUB_TOKEN_COMMAND)
    assert liveenv.token_command(GITLAB, base) is None
    bad = {**base, GITHUB.var("TOKEN_COMMAND"): "not json"}
    assert liveenv.token_command(GITHUB, bad) is None
    good = {**base, GITLAB.var("TOKEN_COMMAND"): '["glab", "auth", "token", "{host}"]'}
    assert liveenv.token_command(GITLAB, good) == ["glab", "auth", "token", "{host}"]


HOSTILE_PREFIXES = (
    "",
    "  ",
    "../x",
    "..",
    "-x",
    "a b",
    "x" * 80,
    "main",
    "master",
    "HEAD",
    "trunk",
    "develop",
    "release/",
    "release",
    "refs/heads/x",
    "a//b",
    "a/.b",
    "a.lock/",
    "*.lock",
    ".lock",
    "a/b/c-",
    ":",
    "+",
    "*",
    "/",
    "//",
    "x\ny",
    "a:b",
    "a~1",
    "a^",
    "a?",
    "a[",
    "a\\b",
    "@{",
)


@pytest.mark.parametrize("bad", HOSTILE_PREFIXES)
def test_push_prefix_falls_back_to_the_default_for_an_unsafe_prefix(bad: str) -> None:
    assert liveenv.push_prefix({"WTCTL_LIVE_PUSH_BRANCH_PREFIX": bad}) == "wtctl-live-"
    assert not liveenv.valid_branch_prefix(bad)


def test_a_prefix_with_a_newline_is_invalid() -> None:
    assert not liveenv.valid_branch_prefix("a\n")
    assert not liveenv.valid_branch_prefix("\na")


@pytest.mark.parametrize("good", ["zz-", "wtctl-live-", "team/live-", "live/", "a.b_c-", "x1"])
def test_push_prefix_keeps_a_plain_prefix(good: str) -> None:
    assert liveenv.push_prefix({"WTCTL_LIVE_PUSH_BRANCH_PREFIX": good}) == good


def test_push_prefix_defaults_when_unset() -> None:
    assert liveenv.push_prefix({}) == "wtctl-live-"


@pytest.mark.parametrize("prefix", ["wtctl-live-", "team/live-", "live/", "a.b_c-", "x1"])
def test_an_accepted_prefix_makes_a_ref_name_real_git_accepts(prefix: str, tmp_path: Path) -> None:
    name = scenarios.safe_branch(prefix, "0123456789ab", "main")
    r = subprocess.run(
        ["git", "check-ref-format", "--branch", name],
        env=clean_env(tmp_path), capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert r.returncode == 0, name


@pytest.mark.parametrize("bad", ["a//b", "a/.b", "a.lock/", "*.lock", "refs/heads/x", "release/"])
def test_a_refused_prefix_is_also_refused_by_safe_branch(bad: str) -> None:
    with pytest.raises(ValueError):
        scenarios.safe_branch(bad, "0123456789ab", "main")


def test_the_fork_need_requires_a_full_sha() -> None:
    env = {GITHUB.var("PUBLIC_REPO"): "o/r", GITHUB.var("FORK_PR_HEAD"): "abc123"}
    assert liveenv.unmet("github:fork", env, lambda _n: "x")
    env[GITHUB.var("FORK_PR_HEAD")] = "a" * 40
    assert liveenv.unmet("github:fork", env, lambda _n: "x") is None


def test_the_lfs_need_wants_the_binary_and_all_three_variables() -> None:
    env = {
        "WTCTL_LIVE_LFS_REPO": "github.com/o/r",
        "WTCTL_LIVE_LFS_REF": "b" * 40,
        "WTCTL_LIVE_LFS_PATH": "big.bin",
    }
    assert "git-lfs" in (liveenv.unmet("lfs", env, lambda _n: None) or "")
    assert liveenv.unmet("lfs", env, lambda _n: "/usr/bin/git-lfs") is None
    del env["WTCTL_LIVE_LFS_PATH"]
    assert "WTCTL_LIVE_LFS_PATH" in (liveenv.unmet("lfs", env, lambda _n: "x") or "")


def test_there_is_no_lfs_token_variable() -> None:
    assert not [n for n in liveenv.VAR_NAMES if "LFS_TOKEN" in n]
    assert "LFS_TOKEN" not in (ROOT / "docs" / "live-testing.md").read_text()


def test_the_lfs_world_refuses_a_foreign_host(tmp_path: Path) -> None:
    ok = {"WTCTL_LIVE_LFS_REPO": "gitlab.com/g/r"}
    w = worlds.lfs_world(tmp_path / "a", environ=ok)
    assert (w.host, w.project, w.tokens, w.token_env) == ("gitlab.com", "g/r", {}, None)
    with pytest.raises(KeyError):
        worlds.lfs_world(tmp_path / "b", environ={"WTCTL_LIVE_LFS_REPO": "code.example/o/r"})


# -- the gate is not too permissive: every variable of a need matters ----------------------

FULL_ENVS: dict[str, dict[str, str]] = {}
for _f in (GITHUB, GITLAB):
    _public = {_f.var("PUBLIC_REPO"): "o/r"}
    FULL_ENVS[f"{_f.key}:public"] = _public
    FULL_ENVS[f"{_f.key}:private"] = {
        _f.var("PRIVATE_REPO"): "o/p", _f.var("TOKEN_ENV"): "TOK_A", "TOK_A": TOKEN,
    }  # fmt: skip
    FULL_ENVS[f"{_f.key}:push"] = {
        _f.var("PUSH_REPO"): "o/d", _f.var("PUSH_TOKEN_ENV"): "TOK_B", "TOK_B": TOKEN,
    }  # fmt: skip
    FULL_ENVS[f"{_f.key}:fork"] = {**_public, _f.var("FORK_PR_HEAD"): "c" * 40}
    FULL_ENVS[f"{_f.key}:token_command"] = {
        **FULL_ENVS[f"{_f.key}:private"], _f.var("TOKEN_COMMAND"): f'["{_f.cli}", "auth", "token"]',
    }  # fmt: skip
FULL_ENVS["lfs"] = {
    "WTCTL_LIVE_LFS_REPO": "github.com/o/r",
    "WTCTL_LIVE_LFS_REF": "d" * 40,
    "WTCTL_LIVE_LFS_PATH": "big.bin",
}


@pytest.mark.parametrize("need", sorted(FULL_ENVS))
def test_each_need_is_met_by_its_full_environment(need: str) -> None:
    assert liveenv.unmet(need, FULL_ENVS[need], lambda _n: "/bin/x") is None


@pytest.mark.parametrize(
    ("need", "dropped"),
    [
        (n, k)
        for n, env in sorted(FULL_ENVS.items())
        for k in sorted(env)
        # gh has a default token command, so dropping the variable leaves the need met
        if not (n == "github:token_command" and k == GITHUB.var("TOKEN_COMMAND"))
    ],
)
def test_each_need_fails_when_one_variable_is_dropped(need: str, dropped: str) -> None:
    env = {k: v for k, v in FULL_ENVS[need].items() if k != dropped}
    assert liveenv.unmet(need, env, lambda _n: "/bin/x"), f"{need} still met without {dropped}"


def test_each_need_fails_without_its_binary() -> None:
    for need in ("lfs", "github:token_command", "gitlab:token_command"):
        assert liveenv.unmet(need, FULL_ENVS[need], lambda _n: None)
    assert liveenv.unmet("bin:ssh", {}, lambda _n: None)
    assert liveenv.unmet("bin:ssh", {}, lambda _n: "/usr/bin/ssh") is None


# -- credentials never reach argv, config or output ----------------------------------------


def test_the_token_is_in_the_child_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    b64 = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()

    def fake(argv: list[str], *, env: dict[str, str], cwd: str, timeout: float, stdin=None) -> Raw:  # noqa: ANN001
        seen.update(argv=argv, env=env)
        return Raw(1, f"out {TOKEN}".encode(), f"err {b64} ghp_{'a' * 30}".encode())

    monkeypatch.setattr(harness, "spawn", fake)
    w = LiveWorld(
        tmp_path / "w", host="forge.example", project="o/r", token_env="MY_TOK",
        tokens={"MY_TOK": TOKEN},
    )  # fmt: skip
    out = w.wtctl("clone", w.repo_id)
    assert all(TOKEN not in a for a in seen["argv"])  # type: ignore[union-attr]
    assert seen["env"]["MY_TOK"] == TOKEN  # type: ignore[index]
    assert TOKEN not in w.config_path.read_text()
    assert '"token_env": "MY_TOK"' in w.config_path.read_text()
    assert TOKEN not in out.out + out.err + out.describe()
    assert "ghp_" not in out.err
    assert b64 not in out.err and b64 not in out.describe()
    assert "***" in out.out


@pytest.mark.parametrize("user", ["oauth2", "x-access-token", "a", "ab"])
def test_scrub_removes_the_basic_payload_for_the_configured_user(tmp_path: Path, user: str) -> None:
    w = LiveWorld(
        tmp_path / "w", host="forge.example", project="o/r", token_user=user,
        tokens={"T": TOKEN},
    )  # fmt: skip
    payload = base64.b64encode(f"{user}:{TOKEN}".encode()).decode()
    text = f"Authorization: Basic {payload} and {payload.rstrip('=')}"
    assert payload.rstrip("=") not in w.scrub(text)
    assert "Basic ***" in w.scrub(text)


def test_the_token_user_reaches_the_world_config(tmp_path: Path) -> None:
    w = LiveWorld(
        tmp_path / "w", host="forge.example", project="o/r", token_env="T", token_user="oauth2",
        tokens={"T": TOKEN},
    )  # fmt: skip
    assert w.config_data()["hosts"]["forge.example"]["token_user"] == "oauth2"


def test_net_git_scrubs_what_git_prints_and_keeps_the_token_off_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    def runner(argv: list[str], *, env: dict[str, str], **kw: object) -> GitResult:
        calls.append((argv, env))
        echo = " ".join(v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE"))
        return GitResult(0, b"", f"echo {echo}".encode())

    monkeypatch.setattr(harness, "make_gateway", lambda: Gateway(git_runner=runner))
    w = LiveWorld(
        tmp_path / "w", host="forge.example", project="o/r", token_env="MY_TOK",
        tokens={"MY_TOK": TOKEN},
    )  # fmt: skip
    scratch = tmp_path / "scratch.git"
    subprocess.run(["git", "init", "-q", "--bare", str(scratch)], check=True, timeout=30,
                   env=clean_env(tmp_path))  # fmt: skip
    res = w.net_git(["ls-remote", "--", w.fetch_url], url=w.fetch_url, git_dir=scratch)
    assert calls, "the fake runner was not used"
    argv, env = calls[-1]
    assert all(TOKEN not in a for a in argv)
    b64 = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    reached = any(TOKEN in v or b64 in v for v in env.values())
    assert reached, "the header should reach git through the environment"
    assert TOKEN not in res.out and TOKEN not in res.err
    assert b64 not in res.err
    assert "***" in res.err


def test_world_builders_put_names_in_the_config_and_values_in_the_environment(
    tmp_path: Path,
) -> None:
    env = {
        GITHUB.var("PRIVATE_REPO"): "owner/name",
        GITHUB.var("TOKEN_ENV"): "MY_TOK",
        "MY_TOK": TOKEN,
    }
    w = worlds.private_world(tmp_path / "a", GITHUB, environ=env)
    w.write_config()
    assert TOKEN not in w.config_path.read_text()
    assert w.env()["MY_TOK"] == TOKEN
    bad = worlds.private_world(tmp_path / "b", GITHUB, mode="bad", environ=env)
    assert bad.env()["MY_TOK"] == worlds.BOGUS_TOKEN and TOKEN not in json.dumps(bad.env())
    none = worlds.private_world(tmp_path / "c", GITHUB, mode="none", environ=env)
    assert TOKEN not in json.dumps(none.env()) and "hosts" not in none.config_data()
    pub = worlds.public_world(tmp_path / "d", GITHUB, environ={GITHUB.var("PUBLIC_REPO"): "o/r"})
    assert pub.tokens == {} and "hosts" not in pub.config_data()


def test_the_child_environment_inherits_nothing_but_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent")
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    w = LiveWorld(tmp_path / "w", host="forge.example", project="o/r")
    env = w.env()
    assert not {"SSH_AUTH_SOCK", "GITHUB_TOKEN", "GIT_DIR"} & set(env)
    assert env["HOME"] == str(w.home) and Path(env["HOME"]).is_relative_to(tmp_path)


_DENIED_MODULES = {"logging", "pprint", "warnings"}
_DENIED_FROM = {
    "sys": {"stdout", "stderr"},
    "os": {"write"},
}


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def output_violations(source: str) -> list[str]:
    """What in ``source`` could write output: print, stdout and stderr writes, pprint, traceback
    printing, warnings, ``os.write``, logging (imported or called)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            root = name.split(".")[0]
            if (
                name == "print"
                or root in _DENIED_MODULES
                or root in {"logger", "log"}
                or name.startswith(("sys.stdout", "sys.stderr", "sys.__stdout__", "sys.__stderr__"))
                or name.startswith("traceback.print_")
                or name in {"os.write", "os.fdopen"}
            ):
                found.append(f"{node.lineno}: call {name}")
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in _DENIED_MODULES:
                    found.append(f"{node.lineno}: import {a.name}")
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            names = {a.name for a in node.names}
            if mod in _DENIED_MODULES or (
                mod == "traceback" and any(n.startswith("print_") for n in names)
            ):
                found.append(f"{node.lineno}: from {mod} import")
            if names & _DENIED_FROM.get(mod, set()):
                found.append(f"{node.lineno}: from {mod} import {sorted(names)}")
    return found


@pytest.mark.parametrize(
    "source",
    [
        "print(x)",
        "import sys\nsys.stderr.write(x)",
        "import sys\nsys.stdout.flush()",
        "import pprint\npprint.pprint(x)",
        "import traceback\ntraceback.print_exc()",
        "from traceback import print_exception",
        "import warnings\nwarnings.warn('x')",
        "import os\nos.write(1, b'x')",
        "import logging",
        "from logging import getLogger",
        "from sys import stderr",
        "logger.info(x)",
    ],
)
def test_the_output_guard_catches_every_way_to_write(source: str) -> None:
    assert output_violations(source), source


def test_helper_modules_never_print_or_log() -> None:
    assert not output_violations("import sys\nx = sys.executable")
    for name in HELPERS:
        assert not output_violations((LIVE_DIR / name).read_text()), name


ALLOWED_HOSTS = {
    "github.com",
    "gitlab.com",
    "example.invalid",
    "forge.example",
    "code.example",
    "git-lfs.github.com",  # the LFS pointer spec, not a target
    "127.0.0.1",
}
_URL_HOST_RE = re.compile(r"(?:https?|ssh|git)://([^/\s\"'`)>\]]+)")
_SCP_HOST_RE = re.compile(r"[\w.-]+@([\w.-]+):")


def hosts_in(text: str) -> set[str]:
    """Hosts named by URLs and scp-style addresses, without userinfo or port."""
    found = set()
    for authority in _URL_HOST_RE.findall(text):
        host = authority.rsplit("@", 1)[-1]
        found.add(re.sub(r":\d*$", "", host))
    found |= set(_SCP_HOST_RE.findall(text))
    return {h for h in found if h and not h.startswith("{")}


HOSTILE = "evil" + ".test"  # built from pieces: this file is itself scanned


def test_the_host_guard_sees_every_spelling() -> None:
    assert hosts_in("ssh://git@" + HOSTILE + "/x") == {HOSTILE}
    assert hosts_in("git://" + HOSTILE + ":9418/x") == {HOSTILE}
    assert hosts_in("https://u:p@" + HOSTILE + ":443/x") == {HOSTILE}
    assert hosts_in("git@" + HOSTILE + ":o/r.git") == {HOSTILE}
    assert hosts_in("https://{host}/x") == set()


def test_nothing_in_the_suite_names_a_real_target() -> None:
    """Open-source rule: no org, repo or host of a real system beyond the two public forges, in
    the suite, its unit tests, its probe and its documentation."""
    for path in GUARDED_FILES:
        text = path.read_text()
        assert hosts_in(text) <= ALLOWED_HOSTS, (path.name, hosts_in(text) - ALLOWED_HOSTS)


def test_the_branch_deletion_runs_when_a_step_fails_and_after_success() -> None:
    tree = ast.parse((LIVE_DIR / "scenarios.py").read_text())
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    fn = fns["push_fast_forward"]
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    assert any("_delete_branch" in ast.unparse(h) for h in handlers)
    assert ast.unparse(fn).count("_delete_branch(") == 2  # on failure and on success
    assert ":{ref}" in ast.unparse(fns["_delete_branch"])
    src = ast.unparse(fn) + ast.unparse(fns["_delete_branch"])
    assert "--force" not in src and "+HEAD" not in src


def test_make_live_exists_and_verify_does_not_run_it() -> None:
    mk = (ROOT / "Makefile").read_text()
    assert re.search(r"^live:\n\tuv run pytest -m live", mk, re.M)
    verify = next(ln for ln in mk.splitlines() if ln.startswith("verify:"))
    assert "live" not in verify
    recipe = re.search(r"^test:\n\t(.*)$", mk, re.M)
    assert recipe and recipe.group(1) == "uv run pytest"
    assert "never run in CI" in mk


# -- the output of wtctl is checked against its schemas ------------------------------------


def test_a_successful_locate_output_is_validated_against_its_schema() -> None:
    assert Out(0, '{"repo": 1}', "", False, "wtctl/cli/locate.json").code == 0
    with pytest.raises(AssertionError, match="does not match wtctl/cli/locate.json"):
        Out(0, '{"repo": 1}', "", False, "wtctl/cli/locate.json").json()
    # a failed run is not validated (its output is an error object), nor is an unchecked command
    assert Out(12, '{"error": {"code": "x"}}', "", False, "wtctl/cli/locate.json").error()
    assert Out(0, '{"anything": 1}', "").json() == {"anything": 1}


def test_wtctl_runs_for_the_checked_commands_carry_a_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "spawn", lambda *a, **k: Raw(0, b"{}", b""))
    w = LiveWorld(tmp_path / "w", host="forge.example", project="o/r")
    assert w.wtctl("create", "--repo", w.repo_id).schema == "wtctl/cli/create.json"
    assert w.wtctl("locate", w.repo_id).schema == "wtctl/cli/locate.json"
    assert w.wtctl("status", "x").schema == "wtctl/cli/status.json"
    assert w.wtctl("clone", w.repo_id).schema == "wtctl/cli/clone.json"
    assert w.wtctl("remove", "x", "--yes").schema is None


# -- a token command is run once, in the child's environment -------------------------------


def test_the_token_command_output_becomes_a_scrubbed_secret(tmp_path: Path) -> None:
    w = LiveWorld(
        tmp_path / "w", host="forge.example", project="o/r",
        token_command=["sh", "-c", f"printf '%s\\n' {TOKEN}"],
    )  # fmt: skip
    assert w.secrets == []
    assert w.prime_token_command() == TOKEN
    assert w.secrets == [TOKEN]
    assert w.scrub(f"x {TOKEN} y") == "x *** y"


def test_a_silent_token_command_is_reported_not_swallowed(tmp_path: Path) -> None:
    w = LiveWorld(
        tmp_path / "w", host="forge.example", project="o/r", token_command=["sh", "-c", "exit 3"]
    )
    with pytest.raises(scenarios.TokenCommandSilent, match="printed nothing") as e:
        scenarios.token_command_fetch(w, "WTCTL_LIVE_GITHUB_CONFIG_DIR")
    assert "exit 3" in str(e.value) and "WTCTL_LIVE_GITHUB_CONFIG_DIR" in str(e.value)


def test_the_token_command_does_not_see_the_callers_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    w = LiveWorld(
        tmp_path / "w", host="forge.example", project="o/r",
        token_command=["sh", "-c", 'printf "%s" "${GH_TOKEN:-}"'],
    )  # fmt: skip
    assert w.prime_token_command() == ""


def test_a_failing_secret_scan_leaks_no_secret_under_locals_display(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = LiveWorld(tmp_path / "w", host="forge.example", project="o/r", tokens={"T": TOKEN})
    (w.base / "f.txt").write_text("x")

    def boom(_self: Path) -> bytes:
        raise PermissionError(TOKEN)

    monkeypatch.setattr(Path, "read_bytes", boom)
    with pytest.raises(RuntimeError, match="secret scan failed: PermissionError") as e:
        scenarios._find_leak(w)
    assert TOKEN not in str(e.value) and e.value.__cause__ is None
    assert e.value.__suppress_context__
    frame_locals = repr(e.value.__traceback__.tb_next.tb_frame.f_locals)  # type: ignore[union-attr]
    assert TOKEN not in frame_locals
