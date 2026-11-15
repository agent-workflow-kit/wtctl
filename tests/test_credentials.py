"""Credentials: token resolution, fetch-URL host scoping, header building, timeouts, no leaks."""

from __future__ import annotations

import base64
import copy
import os
import pickle
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.vectorlib import GIT
from wtctl.config import Config, HostConfig
from wtctl.credentials import (
    NetworkAuth,
    NetworkKind,
    Secret,
    fetch_target,
    prepare_network_auth,
    resolve_token,
    run_network_git,
    timeout_seconds,
    token_header,
)
from wtctl.gateway import Gateway, GitResult, TokenCommandResult, run_token_command
from wtctl.hardgit import Git

TOKEN = "tok-SECRET-0123456789abcdef"
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}


def cfg_with(**host: object) -> Config:
    return Config(hosts={"forge.example": HostConfig(**host)})  # type: ignore[arg-type]


def resolve(
    cfg: Config,
    environ: Mapping[str, str] | None = None,
    *,
    timeout: float = 5.0,
    host: str = "forge.example",
) -> tuple[Secret | None, list[str]]:
    warnings: list[str] = []
    token = resolve_token(
        cfg,
        host,
        environ=environ if environ is not None else ENV,
        gateway=Gateway(),
        warn=warnings.append,
        command_timeout=timeout,
    )
    return token, warnings


def sh(script: str) -> list[str]:
    return ["/bin/sh", "-c", script]


# -- Secret --------------------------------------------------------------------------------------


def test_secret_never_shows_itself() -> None:
    s = Secret(TOKEN)
    for text in (repr(s), str(s), f"{s}", f"{s!r}", repr([s]), repr({"k": s})):
        assert TOKEN not in text
    assert s.reveal() == TOKEN
    with pytest.raises(TypeError):
        pickle.dumps(s)
    assert copy.deepcopy(s) is s


# -- token_env -----------------------------------------------------------------------------------


def test_token_env_is_used() -> None:
    token, warnings = resolve(cfg_with(token_env="FORGE_TOKEN"), {**ENV, "FORGE_TOKEN": TOKEN})
    assert token is not None and token.reveal() == TOKEN
    assert warnings == []


def test_token_env_wins_over_command(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    cfg = cfg_with(token_env="FORGE_TOKEN", token_command=sh(f"touch {marker}; echo other"))
    token, _ = resolve(cfg, {**ENV, "FORGE_TOKEN": TOKEN})
    assert token is not None and token.reveal() == TOKEN
    assert not marker.exists()


@pytest.mark.parametrize("value", [None, "", "   ", "\n", "\t \r\n"])
def test_token_env_missing_empty_or_blank_falls_back_to_command(value: str | None) -> None:
    environ = dict(ENV) if value is None else {**ENV, "FORGE_TOKEN": value}
    cfg = cfg_with(token_env="FORGE_TOKEN", token_command=sh("echo from-command"))
    token, _ = resolve(cfg, environ)
    assert token is not None and token.reveal() == "from-command"


def test_token_env_missing_without_command_is_anonymous() -> None:
    token, warnings = resolve(cfg_with(token_env="FORGE_TOKEN"))
    assert token is None and warnings == []


def test_no_host_settings_is_anonymous() -> None:
    token, warnings = resolve(Config())
    assert token is None and warnings == []


@pytest.mark.parametrize(
    "value", ["a b", "line1\nline2", "tok\x00en", "töken", "x" * 5000, "tab\there"]
)
def test_token_env_invalid_value_is_refused_without_fallback(value: str, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    cfg = cfg_with(token_env="FORGE_TOKEN", token_command=sh(f"touch {marker}; echo other"))
    token, warnings = resolve(cfg, {**ENV, "FORGE_TOKEN": value})
    assert token is None
    assert not marker.exists()
    assert len(warnings) == 1
    assert value not in warnings[0] and "forge.example" in warnings[0]


def test_token_env_trailing_newline_is_stripped() -> None:
    token, _ = resolve(cfg_with(token_env="T"), {**ENV, "T": TOKEN + "\r\n"})
    assert token is not None and token.reveal() == TOKEN


# -- token_command -------------------------------------------------------------------------------


@pytest.mark.parametrize("ending", ["\n", "\r\n", "", "\n\n"])
def test_command_output_is_stripped_of_trailing_newlines(ending: str) -> None:
    token, warnings = resolve(cfg_with(token_command=sh(f"printf '%s' '{TOKEN}{ending}'")))
    assert token is not None and token.reveal() == TOKEN
    assert warnings == []


def test_command_host_placeholder_is_substituted_in_whole_arguments_only() -> None:
    cfg = cfg_with(token_command=["/bin/sh", "-c", 'echo "$0|$1"', "{host}", "x{host}"])
    token, _ = resolve(cfg)
    assert token is not None and token.reveal() == "forge.example|x{host}"


def test_command_runs_without_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "m"
    cfg = cfg_with(token_command=["/bin/echo", f"$(touch {marker}); `touch {marker}`; {TOKEN}"])
    token, _ = resolve(cfg)
    assert not marker.exists()
    assert token is None  # spaces and shell syntax make it an invalid token, not an injection


def test_command_stdin_is_dev_null() -> None:
    token, _ = resolve(cfg_with(token_command=sh(f"cat; echo {TOKEN}")), timeout=5)
    assert token is not None and token.reveal() == TOKEN


def test_command_nonzero_exit_gives_no_token_and_leaks_nothing() -> None:
    script = f"echo {TOKEN}; echo stderr-{TOKEN} >&2; exit 3"
    token, warnings = resolve(cfg_with(token_command=sh(script)))
    assert token is None
    assert len(warnings) == 1 and "forge.example" in warnings[0]
    assert "SECRET" not in warnings[0]


def test_command_stderr_is_never_echoed_on_success() -> None:
    token, warnings = resolve(cfg_with(token_command=sh(f"echo err-SECRET >&2; echo {TOKEN}")))
    assert token is not None
    assert warnings == []


def test_command_empty_output() -> None:
    token, warnings = resolve(cfg_with(token_command=sh("true")))
    assert token is None and len(warnings) == 1


def test_command_missing_binary() -> None:
    token, warnings = resolve(cfg_with(token_command=["/nonexistent/wtctl-no-such-bin"]))
    assert token is None and len(warnings) == 1
    assert "nonexistent" not in warnings[0]


def test_command_not_executable(tmp_path: Path) -> None:
    f = tmp_path / "noexec"
    f.write_text("#!/bin/sh\necho x\n")
    token, warnings = resolve(cfg_with(token_command=[str(f)]))
    assert token is None and len(warnings) == 1


def test_command_non_utf8_output() -> None:
    token, warnings = resolve(cfg_with(token_command=sh("printf '\\377\\376abc'")))
    assert token is None and len(warnings) == 1


@pytest.mark.parametrize(
    "script",
    [
        "printf 'abc\\nInjected-Header: 1'",  # newline inside: header injection
        "printf 'abc\\r\\nInjected-Header: 1'",
        "printf 'abc def'",
        "printf ' abc'",
        "printf 'abc\\000def'",
        "printf 'abc\\tdef'",
    ],
)
def test_command_token_with_embedded_whitespace_or_control_is_refused(script: str) -> None:
    token, warnings = resolve(cfg_with(token_command=sh(script)))
    assert token is None and len(warnings) == 1
    assert "Injected" not in warnings[0]


def test_command_huge_output_is_capped_and_killed() -> None:
    start = time.monotonic()
    token, warnings = resolve(
        cfg_with(token_command=sh("yes AAAAAAAAAAAAAAAAAAAA | head -c 200000000")), timeout=10
    )
    assert token is None and len(warnings) == 1
    assert time.monotonic() - start < 8


def test_command_timeout_kills_the_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = f"sleep 300 & echo $! > {pidfile}; echo partial; wait"
    start = time.monotonic()
    token, warnings = resolve(cfg_with(token_command=sh(script)), timeout=1.0)
    assert token is None and len(warnings) == 1 and "timeout" in warnings[0]
    assert time.monotonic() - start < 8
    pid = int(pidfile.read_text())
    try:
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            # A zombie still answers kill(0); it is reaped by init shortly.
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                break
            time.sleep(0.1)
        else:
            pytest.fail("the background child survived the group kill")
    finally:
        try:
            os.kill(pid, 9)  # exact pid recorded by the test's own command
        except ProcessLookupError:
            pass


def test_command_that_closes_stdout_but_keeps_running_times_out() -> None:
    result = run_token_command(
        sh(f"echo {TOKEN}; exec >&-; sleep 300"), env=ENV, timeout=1.0, max_bytes=1024
    )
    assert result.failure == "timeout"


def test_command_result_repr_hides_stdout() -> None:
    result = TokenCommandResult(0, TOKEN.encode())
    assert TOKEN not in repr(result)


def test_command_environment_is_the_given_one(tmp_path: Path) -> None:
    out = run_token_command(
        sh('echo "${ONLY_THIS:-unset}-${HOME:-nohome}"'),
        env={"PATH": "/usr/bin:/bin", "ONLY_THIS": "yes"},
        timeout=5,
        max_bytes=100,
    )
    assert out.stdout.strip() == b"yes-nohome"


# -- the fetch URL's host ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "scheme", "hostport"),
    [
        ("https://Forge.Example/org/repo.git", "https", "forge.example"),
        ("https://forge.example:443/org/repo", "https", "forge.example"),
        ("https://forge.example:8443/org/repo", "https", "forge.example:8443"),
        ("HTTPS://FORGE.EXAMPLE/x", "https", "forge.example"),
        ("https://xn--bcher-kva.example/x", "https", "xn--bcher-kva.example"),
        ("https://127.0.0.1:9/x", "https", "127.0.0.1:9"),
        ("https://forge.example", "https", "forge.example"),
        ("https://forge.example?x=1", "https", "forge.example"),
        ("ssh://git@forge.example/org/repo", "ssh", None),
        ("git@forge.example:org/repo.git", "ssh", None),
        ("forge.example:org/repo", "ssh", None),
        ("/srv/git/repo.git", "file", None),
        ("./rel/repo", "file", None),
        ("file:///srv/git/repo.git", "file", None),
    ],
)
def test_fetch_target(url: str, scheme: str, hostport: str | None) -> None:
    target = fetch_target(url)
    assert (target.scheme, target.hostport) == (scheme, hostport)


@pytest.mark.parametrize(
    "url",
    [
        "https://good.example@evil.example/x",  # userinfo: the host is not the first name
        "https://evil.example@good.example/x",
        "https://user:pw@forge.example/x",
        "https://good.example\\@evil.example/x",
        "https://good.example#@evil.example/x\x7f",
        "https://bücher.example/x",  # IDN must arrive as punycode
        "https://forge.example./x",
        "https://[::1]/x",
        "https://forge.example:/x",
        "https://forge.example:99999999/x",
        "https:///x",
        "https://",
        "https://for ge.example/x",
        "https://forge.example/x\ny",
        "https://forge.example\t/x",
        "http://forge.example/x",  # plaintext: never a token
        "ftp://forge.example/x",
        "ext::sh -c id",
        "",
        "https://.forge.example/x",
        "https://-forge.example/x",
    ],
)
def test_fetch_target_refuses_what_it_cannot_place(url: str) -> None:
    with pytest.raises(ValueError) as exc:
        fetch_target(url)
    assert "pw" not in str(exc.value)


# -- header --------------------------------------------------------------------------------------


def test_token_header_is_url_scoped_basic() -> None:
    key, value = token_header("forge.example", "x-access-token", Secret(TOKEN))
    assert key == "http.https://forge.example/.extraHeader"
    raw = base64.b64decode(value.reveal().removeprefix("Authorization: Basic "))
    assert raw == f"x-access-token:{TOKEN}".encode()
    assert TOKEN not in repr(value)


def test_token_header_keeps_the_port() -> None:
    key, _ = token_header("forge.example:8443", "u", Secret(TOKEN))
    assert key == "http.https://forge.example:8443/.extraHeader"


@pytest.mark.parametrize("user", ["", "a:b", "a b", "a\nb", "ü", "x" * 300])
def test_token_header_rejects_bad_user_without_leaking(user: str) -> None:
    with pytest.raises(ValueError) as exc:
        token_header("forge.example", user, Secret(TOKEN))
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("host", ["", "a/b", "a b", "a\nb", "evil.example/.x", "a.example@b"])
def test_token_header_rejects_bad_host(host: str) -> None:
    with pytest.raises(ValueError):
        token_header(host, "u", Secret(TOKEN))


# -- NetworkAuth ---------------------------------------------------------------------------------


def test_prepare_https_with_token_env() -> None:
    cfg = cfg_with(token_env="T", token_user="oauth2")
    warnings: list[str] = []
    auth = prepare_network_auth(
        cfg,
        "https://Forge.Example/org/r.git",
        environ={**ENV, "T": TOKEN},
        gateway=Gateway(),
        warn=warnings.append,
    )
    assert auth.scheme == "https"
    ((key, value),) = auth.extra_config
    assert key == "http.https://forge.example/.extraHeader"
    assert (
        base64.b64decode(value.removeprefix("Authorization: Basic ")) == f"oauth2:{TOKEN}".encode()
    )
    assert TOKEN not in repr(auth) and TOKEN not in str(auth)
    assert value not in repr(auth)


def test_prepare_uses_the_settings_of_the_url_host_only() -> None:
    cfg = Config(
        hosts={
            "forge.example": HostConfig(token_env="T1"),
            "mirror.example": HostConfig(token_env="T2"),
        }
    )
    environ = {**ENV, "T1": "token-one", "T2": "token-two"}
    auth = prepare_network_auth(
        cfg, "https://mirror.example/x", environ=environ, gateway=Gateway(), warn=lambda m: None
    )
    ((key, value),) = auth.extra_config
    assert key == "http.https://mirror.example/.extraHeader"
    assert base64.b64decode(value.split()[-1]) == b"x-access-token:token-two"


def test_prepare_without_token_is_anonymous() -> None:
    auth = prepare_network_auth(
        Config(), "https://forge.example/x", environ=ENV, gateway=Gateway(), warn=lambda m: None
    )
    assert auth == NetworkAuth("https") and auth.extra_config == ()


def test_prepare_bad_token_user_warns_without_leaking() -> None:
    warnings: list[str] = []
    auth = prepare_network_auth(
        cfg_with(token_env="T", token_user="a:b"),
        "https://forge.example/x",
        environ={**ENV, "T": TOKEN},
        gateway=Gateway(),
        warn=warnings.append,
    )
    assert auth.extra_config == () and len(warnings) == 1 and TOKEN not in warnings[0]


def test_prepare_ssh_passes_the_agent_socket_and_runs_no_command(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    cfg = cfg_with(token_command=sh(f"touch {marker}; echo {TOKEN}"))
    auth = prepare_network_auth(
        cfg,
        "git@forge.example:org/r.git",
        environ={**ENV, "SSH_AUTH_SOCK": "/run/agent.sock"},
        gateway=Gateway(),
        warn=lambda m: None,
    )
    assert auth.scheme == "ssh" and auth.ssh_auth_sock == "/run/agent.sock"
    assert auth.extra_config == () and not marker.exists()


@pytest.mark.parametrize("sock", ["", "relative/sock", "/a\nb", "/a\0b"])
def test_prepare_ssh_ignores_a_bad_agent_socket(sock: str) -> None:
    auth = prepare_network_auth(
        Config(),
        "ssh://forge.example/x",
        environ={**ENV, "SSH_AUTH_SOCK": sock},
        gateway=Gateway(),
        warn=lambda m: None,
    )
    assert auth.ssh_auth_sock is None


def test_prepare_file_url_has_no_credentials(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    cfg = cfg_with(token_command=sh(f"touch {marker}; echo {TOKEN}"))
    auth = prepare_network_auth(
        cfg, "/srv/x.git", environ=ENV, gateway=Gateway(), warn=lambda m: None
    )
    assert auth == NetworkAuth("file") and not marker.exists()


def test_prepare_refuses_userinfo_urls_without_echoing_them() -> None:
    with pytest.raises(ValueError) as exc:
        prepare_network_auth(
            Config(),
            f"https://user:{TOKEN}@forge.example/x",
            environ=ENV,
            gateway=Gateway(),
            warn=lambda m: None,
        )
    assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value)


def test_scrub_replaces_echoes_of_the_credential() -> None:
    auth = prepare_network_auth(
        cfg_with(token_env="T"),
        "https://forge.example/x",
        environ={**ENV, "T": TOKEN},
        gateway=Gateway(),
        warn=lambda m: None,
    )
    header = auth.extra_config[0][1]
    blob = header.removeprefix("Authorization: Basic ")
    raw = GitResult(1, f"out {TOKEN}".encode(), f"remote: {header} {blob} {TOKEN}".encode(), True)
    clean = auth.scrub(raw)
    for data in (clean.stdout, clean.stderr):
        assert TOKEN.encode() not in data and blob.encode() not in data
    assert clean.returncode == 1 and clean.timed_out


# -- timeouts and the runner ---------------------------------------------------------------------


def test_timeouts_come_from_config() -> None:
    cfg = Config(network_timeout="45s", clone_timeout="2m")
    assert timeout_seconds(cfg, NetworkKind.NETWORK) == 45.0
    assert timeout_seconds(cfg, NetworkKind.CLONE) == 120.0
    d = Config()
    assert timeout_seconds(d, NetworkKind.NETWORK) == 120.0
    assert timeout_seconds(d, NetworkKind.CLONE) == 1800.0


def _recording_git(tmp_path: Path, home: Path) -> tuple[Git, list[dict]]:
    calls: list[dict] = []

    def fake(argv: list[str], **kw: object) -> GitResult:
        calls.append({"argv": argv, **kw})
        return GitResult(0, b"", f"echo {TOKEN}".encode())

    return Git(Gateway(git_runner=fake), GIT, str(home)), calls


@pytest.mark.parametrize(("kind", "seconds"), [(NetworkKind.NETWORK, 7), (NetworkKind.CLONE, 90)])
def test_run_network_git_applies_timeout_and_keeps_the_token_out_of_argv(
    tmp_path: Path, private_home: Path, kind: NetworkKind, seconds: int
) -> None:
    git, calls = _recording_git(tmp_path, private_home)
    cfg = cfg_with(token_env="T")
    cfg = cfg.model_copy(update={"network_timeout": "7s", "clone_timeout": "90s"})
    auth = prepare_network_auth(
        cfg, "https://forge.example/x", environ={**ENV, "T": TOKEN}, gateway=Gateway(),
        warn=lambda m: None,
    )  # fmt: skip
    result = run_network_git(
        git, ["ls-remote", "--symref", "--", "https://forge.example/x"], cfg=cfg, auth=auth,
        kind=kind, tool_owned=True, private_dir=tmp_path,
    )  # fmt: skip
    (call,) = calls
    assert call["timeout"] == seconds
    assert TOKEN not in " ".join(call["argv"])
    assert "http.lowSpeedLimit=1000" in call["argv"] and "http.lowSpeedTime=60" in call["argv"]
    assert "http.followRedirects=false" in call["argv"]
    assert "core.sshCommand=ssh -o BatchMode=yes" in call["argv"]
    assert call["env"]["GIT_ALLOW_PROTOCOL"] == "https"
    values = {v for k, v in call["env"].items() if k.startswith("GIT_CONFIG_VALUE_")}
    assert any(v.startswith("Authorization: Basic ") for v in values)
    assert not any(TOKEN in v for v in call["env"].values())  # base64 only
    assert TOKEN not in result.err  # scrubbed


def test_run_network_git_requires_an_explicit_tool_owned_statement(
    tmp_path: Path, private_home: Path
) -> None:
    git, _ = _recording_git(tmp_path, private_home)
    with pytest.raises(ValueError):
        run_network_git(
            git, ["ls-remote", "--", "x"], cfg=Config(), auth=NetworkAuth("https"),
            kind=NetworkKind.NETWORK, tool_owned=False, private_dir=tmp_path,
        )  # fmt: skip


def test_run_network_git_passes_the_agent_socket_for_ssh_only(
    tmp_path: Path, private_home: Path
) -> None:
    git, calls = _recording_git(tmp_path, private_home)
    for auth in (NetworkAuth("ssh", ssh_auth_sock="/run/a.sock"), NetworkAuth("https")):
        run_network_git(
            git, ["ls-remote", "--", "x"], cfg=Config(), auth=auth,
            kind=NetworkKind.NETWORK, tool_owned=True, private_dir=tmp_path,
        )  # fmt: skip
    assert calls[0]["env"]["SSH_AUTH_SOCK"] == "/run/a.sock"
    assert "SSH_AUTH_SOCK" not in calls[1]["env"]
