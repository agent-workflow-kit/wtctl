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
import textwrap
from pathlib import Path

import pytest

from tests.live import harness, liveenv, worlds
from tests.live.harness import LiveWorld, Raw
from tests.live.liveenv import GITHUB, GITLAB
from wtctl.gateway import Gateway, GitResult

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "tests" / "live"
HELPERS = ("liveenv.py", "harness.py", "scenarios.py", "worlds.py", "conftest.py")
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
    """Collect-only over tests/live with the real pytest configuration but ``-m live``."""
    tmp = tmp_path_factory.mktemp("probe")
    out = tmp / "items.json"
    r = pytest_run(
        tmp,
        ["tests/live", "--collect-only", "-q", "-m", "live", "-p", "tests.live_probe"],
        WTCTL_PROBE_OUT=str(out),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return json.loads(out.read_text())


# -- marker and default deselection --------------------------------------------------------


def test_the_live_suite_collects_tests(probed: list[dict[str, object]]) -> None:
    assert len(probed) >= 30


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
    probed: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "spawn", _fail)
    monkeypatch.setattr(harness, "make_gateway", _fail)
    for item in probed:
        why = liveenv.all_unmet(list(item["needs"]), environ={}, which=lambda _n: None)  # type: ignore[arg-type]
        assert why, f"{item['id']} would run with no configuration"


def _fail(*a: object, **k: object) -> None:
    raise AssertionError("the outside world was touched")


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


def test_repo_id_needs_a_dotted_host() -> None:
    assert liveenv.valid_repo_id("code.example/org/repo")
    assert not liveenv.valid_repo_id("org/repo")


def test_unmet_messages_name_variables_never_values() -> None:
    env = {GITHUB.var("PRIVATE_REPO"): "owner/name", GITHUB.var("TOKEN_ENV"): "MY_TOK"}
    why = liveenv.unmet("github:private", env, lambda _n: "/bin/x")
    assert why and "MY_TOK" not in why and "owner/name" not in why
    env["MY_TOK"] = TOKEN
    assert liveenv.unmet("github:private", env, lambda _n: "/bin/x") is None


def test_a_token_variable_name_must_be_a_plain_identifier() -> None:
    env = {GITHUB.var("TOKEN_ENV"): "A B; rm -rf", "A B; rm -rf": TOKEN}
    assert liveenv.token_value(GITHUB.var("TOKEN_ENV"), env) is None


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
    good = {**base, GITLAB.var("TOKEN_COMMAND"): '["glab", "x", "{host}"]'}
    assert liveenv.token_command(GITLAB, good) == ["glab", "x", "{host}"]


def test_push_prefix_is_never_empty_or_path_like() -> None:
    assert liveenv.push_prefix({}) == "wtctl-live-"
    for bad in ("", "  ", "../x", "-x", "a b", "x" * 80):
        assert liveenv.push_prefix({"WTCTL_LIVE_PUSH_BRANCH_PREFIX": bad}) == "wtctl-live-"
    assert liveenv.push_prefix({"WTCTL_LIVE_PUSH_BRANCH_PREFIX": "zz-"}) == "zz-"


def test_the_fork_need_requires_a_full_sha() -> None:
    env = {GITHUB.var("PUBLIC_REPO"): "o/r", GITHUB.var("FORK_PR_HEAD"): "abc123"}
    assert liveenv.unmet("github:fork", env, lambda _n: "x")
    env[GITHUB.var("FORK_PR_HEAD")] = "a" * 40
    assert liveenv.unmet("github:fork", env, lambda _n: "x") is None


def test_the_lfs_need_wants_the_binary_and_all_three_variables() -> None:
    env = {
        "WTCTL_LIVE_LFS_REPO": "code.example/o/r",
        "WTCTL_LIVE_LFS_REF": "b" * 40,
        "WTCTL_LIVE_LFS_PATH": "big.bin",
    }
    assert "git-lfs" in (liveenv.unmet("lfs", env, lambda _n: None) or "")
    assert liveenv.unmet("lfs", env, lambda _n: "/usr/bin/git-lfs") is None
    del env["WTCTL_LIVE_LFS_PATH"]
    assert "WTCTL_LIVE_LFS_PATH" in (liveenv.unmet("lfs", env, lambda _n: "x") or "")


# -- credentials never reach argv, config or output ----------------------------------------


def test_the_token_is_in_the_child_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake(argv: list[str], *, env: dict[str, str], cwd: str, timeout: float, stdin=None) -> Raw:  # noqa: ANN001
        seen.update(argv=argv, env=env)
        b64 = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
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
    assert "***" in out.out


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


def test_helper_modules_never_print_or_log() -> None:
    for name in HELPERS:
        tree = ast.parse((LIVE_DIR / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                bad = (isinstance(fn, ast.Name) and fn.id == "print") or (
                    isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id in {"logging", "logger", "sys"}
                    and fn.attr not in {"executable"}
                )
                assert not bad, f"{name}:{node.lineno} writes output"
            if isinstance(node, ast.Import | ast.ImportFrom):
                mods = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                assert "logging" not in mods, f"{name} imports logging"


def test_nothing_in_the_suite_names_a_real_target() -> None:
    """Open-source rule: no org, repo or host of a real system beyond the two public forges."""
    allowed = {
        "github.com",
        "gitlab.com",
        "example.invalid",
        "forge.example",
        "code.example",
        "git-lfs.github.com",
    }  # the last is the LFS pointer spec, not a target
    for path in LIVE_DIR.glob("*.py"):
        for host in re.findall(r"https?://([A-Za-z0-9.-]+)", path.read_text()):
            assert host in allowed or host.startswith(("127.0.0.1", "{")), (path.name, host)


def test_the_branch_deletion_runs_in_a_finally() -> None:
    tree = ast.parse((LIVE_DIR / "scenarios.py").read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "push_fast_forward"
    )
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody]
    assert tries and any(":{ref}" in "\n".join(ast.unparse(x) for x in t.finalbody) for t in tries)
    src = ast.unparse(fn)
    assert "--force" not in src and "+HEAD" not in src


def test_make_live_exists_and_verify_does_not_run_it() -> None:
    mk = (ROOT / "Makefile").read_text()
    assert re.search(r"^live:\n\tuv run pytest -m live", mk, re.M)
    verify = next(ln for ln in mk.splitlines() if ln.startswith("verify:"))
    assert "live" not in verify
    assert textwrap.dedent("never run in CI") in mk
