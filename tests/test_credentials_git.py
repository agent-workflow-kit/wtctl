"""Credentials against real git and a loopback HTTPS capture server.

The server answers every request with 401 (no Authorization) or 404 (any Authorization) and
records the headers it received, so a test sees exactly what git sent and to which Host. TLS uses
a throwaway self-signed certificate that only the test's git child trusts (``GIT_SSL_CAINFO``
added to the hardened environment, nothing else changed).
"""

from __future__ import annotations

import base64
import http.server
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.vectorlib import GIT, plain_git
from wtctl.config import Config, HostConfig
from wtctl.errors import WtctlError
from wtctl.credentials import (
    NetworkKind,
    prepare_network_auth,
    rejected_token_error,
    run_network_git,
)
from wtctl.gateway import Gateway, run_git
from wtctl.hardgit import FetchKind, Git, classify_fetch

TOKEN = "tok-SECRET-0123456789abcdef"


@dataclass
class Capture:
    port: int
    cert: Path
    requests: list[dict[str, str]] = field(default_factory=list)
    redirect_to: str | None = None
    stall: bool = False
    always_401: bool = False
    release: threading.Event = field(default_factory=threading.Event)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Capture]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to make a loopback certificate")
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "OPENSSL_CONF": "/dev/null"},
        timeout=60,
    )  # fmt: skip
    cap = Capture(0, cert)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            cap.requests.append(
                {"path": self.path, **{k.lower(): v for k, v in self.headers.items()}}
            )
            if cap.stall:
                cap.release.wait(15)
                return
            if cap.redirect_to and self.path.startswith("/redir"):
                self.send_response(302)
                self.send_header("Location", cap.redirect_to + "/after-redirect")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if "authorization" in self.headers and not cap.always_401:
                self.send_response(404)
            else:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="t"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    cap.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield cap
    finally:
        cap.release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)


@pytest.fixture
def mirror(tmp_path: Path, private_home: Path) -> Path:
    m = tmp_path / "mirror.git"
    plain_git(["init", "-q", "--bare", str(m)], cwd=tmp_path, home=private_home)
    return m


def auth_for(url: str, environ: dict[str, str] | None = None, cfg: Config | None = None):  # noqa: ANN201
    cfg = cfg or Config(
        hosts={f"127.0.0.1:{url.split(':')[2].split('/')[0]}": HostConfig(token_env="T")}
    )
    return cfg, prepare_network_auth(
        cfg, url, environ={"PATH": "/usr/bin:/bin", "T": TOKEN, **(environ or {})},
        gateway=Gateway(), warn=lambda m: None,
    )  # fmt: skip


def trusting_gateway(cert: Path) -> Gateway:
    """The real runner, with the test's certificate added to git's (otherwise fixed) environment."""

    def runner(argv: list[str], *, env: dict[str, str], **kw: object):  # noqa: ANN202
        return run_git(argv, env={**env, "GIT_SSL_CAINFO": str(cert)}, **kw)  # type: ignore[arg-type]

    return Gateway(git_runner=runner)


FRESH_KEYS = frozenset({"core.repositoryformatversion", "core.filemode", "core.bare"})


def ls_remote(  # noqa: ANN201
    git: Git, mirror: Path, cfg: Config, auth, url: str, owned_keys: frozenset[str] = FRESH_KEYS  # noqa: ANN001
):
    return run_network_git(
        git, ["ls-remote", "--", url], cfg=cfg, auth=auth, kind=NetworkKind.NETWORK,
        tool_owned=True, git_dir=mirror, owned_keys=owned_keys,
    )  # fmt: skip


def expected_basic(user: str = "x-access-token") -> str:
    return "Basic " + base64.b64encode(f"{user}:{TOKEN}".encode()).decode()


def test_header_goes_to_the_intended_host_only(
    server: Capture, mirror: Path, private_home: Path
) -> None:
    url = f"https://127.0.0.1:{server.port}/org/repo.git"
    cfg, auth = auth_for(url)
    git = Git(trusting_gateway(server.cert), GIT, str(private_home))
    result = ls_remote(git, mirror, cfg, auth, url)
    assert result.returncode != 0
    sent = [r.get("authorization") for r in server.requests]
    assert sent and sent[0] == expected_basic()  # preemptive: git attaches the header at once
    assert set(sent) == {expected_basic()}
    assert all(r["host"] == f"127.0.0.1:{server.port}" for r in server.requests)
    assert TOKEN not in result.err and TOKEN not in result.out


def test_header_scoped_to_one_host_is_not_sent_to_another_name_for_the_same_server(
    server: Capture, mirror: Path, private_home: Path
) -> None:
    # The config entry names 127.0.0.1; the request goes to "localhost" on the same port.
    _, auth = auth_for(f"https://127.0.0.1:{server.port}/x")
    other = f"https://localhost:{server.port}/x"
    git = Git(trusting_gateway(server.cert), GIT, str(private_home))
    ls_remote(git, mirror, Config(), auth, other)
    assert server.requests, "the request must reach the server"
    assert all(r["host"] == f"localhost:{server.port}" for r in server.requests)
    assert all("authorization" not in r for r in server.requests)


def test_header_scoped_to_a_port_is_not_sent_to_another_port(
    server: Capture, mirror: Path, private_home: Path
) -> None:
    _, auth = auth_for(f"https://127.0.0.1:{server.port + 1}/x", cfg=Config(
        hosts={f"127.0.0.1:{server.port + 1}": HostConfig(token_env="T")}
    ))  # fmt: skip
    git = Git(trusting_gateway(server.cert), GIT, str(private_home))
    ls_remote(git, mirror, Config(), auth, f"https://127.0.0.1:{server.port}/x")
    assert server.requests and all("authorization" not in r for r in server.requests)


def test_redirect_is_not_followed_and_carries_no_token(
    server: Capture, mirror: Path, private_home: Path
) -> None:
    server.redirect_to = f"https://localhost:{server.port}"
    url = f"https://127.0.0.1:{server.port}/redir/repo.git"
    cfg, auth = auth_for(url)
    git = Git(trusting_gateway(server.cert), GIT, str(private_home))
    result = ls_remote(git, mirror, cfg, auth, url)
    assert result.returncode != 0
    assert not [r for r in server.requests if "after-redirect" in r["path"]]
    assert TOKEN not in result.err


def test_control_default_follow_redirects_would_send_the_token_on(
    server: Capture, mirror: Path, private_home: Path
) -> None:
    """Proves the redirect vector is live: with initial-redirect following the target is hit."""
    server.redirect_to = f"https://127.0.0.1:{server.port}"
    url = f"https://127.0.0.1:{server.port}/redir/repo.git"
    r = plain_git(
        ["-c", f"http.{url[:-len('redir/repo.git')]}.extraheader=X: 1", "ls-remote", url],
        cwd=mirror, home=mirror.parent,
        env={"GIT_SSL_CAINFO": str(server.cert), "GIT_TERMINAL_PROMPT": "0"},
    )  # fmt: skip
    assert r.returncode != 0
    assert any("after-redirect" in q["path"] for q in server.requests)


def test_no_token_means_anonymous_request_and_auth_required(
    server: Capture, mirror: Path, private_home: Path
) -> None:
    url = f"https://127.0.0.1:{server.port}/org/repo.git"
    cfg = Config()
    _, auth = auth_for(url, cfg=cfg)
    git = Git(trusting_gateway(server.cert), GIT, str(private_home))
    result = ls_remote(git, mirror, cfg, auth, url)
    assert all("authorization" not in r for r in server.requests)
    outcome = classify_fetch(result)
    assert outcome.kind is FetchKind.AUTH_REQUIRED


def test_planted_repository_config_is_inert(
    server: Capture, mirror: Path, private_home: Path, tmp_path: Path
) -> None:
    helper = tmp_path / "helper.sh"
    ask = tmp_path / "ask.sh"
    marker = tmp_path / "markers"
    marker.mkdir()
    helper.write_text(
        f"#!/bin/sh\ntouch {marker}/helper\necho username=planted\necho password=planted\n"
    )
    ask.write_text(f"#!/bin/sh\ntouch {marker}/askpass\necho planted\n")
    helper.chmod(0o755)
    ask.chmod(0o755)
    base = f"https://127.0.0.1:{server.port}/"
    cfg_lines = [
        ("http.extraheader", "X-Planted: wildcard"),
        (f"http.{base}.extraheader", "X-Planted: scoped"),
        (f"http.{base}.followRedirects", "true"),
        ("credential.helper", str(helper)),
        ("core.askPass", str(ask)),
    ]
    for k, v in cfg_lines:
        plain_git(["config", k, v], cwd=mirror, home=private_home)

    url = f"{base}org/repo.git"
    # Control: unhardened git sends the planted header, so the vector is live.
    plain_git(
        ["ls-remote", url], cwd=mirror, home=private_home,
        env={"GIT_SSL_CAINFO": str(server.cert), "GIT_DIR": str(mirror)},
    )  # fmt: skip
    assert any("x-planted" in r for r in server.requests)
    server.requests.clear()
    for f in marker.iterdir():
        f.unlink()

    cfg, auth = auth_for(url)
    git = Git(trusting_gateway(server.cert), GIT, str(private_home))
    # The owned-config check (below) would refuse this repository before any request; it is
    # declared satisfied here so the hardened invocation's own blanking is what is tested.
    every_key = frozenset(
        plain_git(["config", "--list", "--name-only"], cwd=mirror, home=private_home)
        .stdout.decode()
        .split("\n")
    )
    result = ls_remote(git, mirror, cfg, auth, url, owned_keys=every_key)
    assert result.returncode != 0
    assert server.requests
    assert not any("x-planted" in r for r in server.requests)
    assert any(r.get("authorization") == expected_basic() for r in server.requests)
    assert list(marker.iterdir()) == []


def _shadow_bin(tmp_path: Path, log: Path) -> Path:
    """A bin directory whose ``ssh`` only records that it ran; ``git`` forwards to the real one."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(
        f'#!/bin/sh\n{{ echo "sock=$SSH_AUTH_SOCK"; printf "%s\\n" "$@"; }} > {log}\nexit 255\n'
    )
    ssh.chmod(0o755)
    wrapper = bindir / "git"  # hardgit puts the git binary's directory first on PATH
    wrapper.write_text(f'#!/bin/sh\nexec {GIT} "$@"\n')
    wrapper.chmod(0o755)
    return wrapper


def test_caller_environment_cannot_enable_tracing_or_replace_ssh(
    server: Capture,
    mirror: Path,
    private_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh_log = tmp_path / "ssh-ran"
    wrapper = _shadow_bin(tmp_path, ssh_log)
    marker = tmp_path / "caller-ssh-ran"
    caller_ssh = tmp_path / "caller-ssh.sh"
    caller_ssh.write_text(f"#!/bin/sh\ntouch {marker}\nexit 255\n")
    caller_ssh.chmod(0o755)
    trace = tmp_path / "trace.log"
    monkeypatch.setenv("GIT_SSH_COMMAND", str(caller_ssh))
    monkeypatch.setenv("GIT_SSH", str(caller_ssh))
    monkeypatch.setenv("GIT_TRACE", str(trace))
    monkeypatch.setenv("GIT_TRACE_CURL", str(trace))
    monkeypatch.setenv("GIT_CURL_VERBOSE", "1")
    url = f"https://127.0.0.1:{server.port}/org/repo.git"
    cfg, auth = auth_for(url)
    git = Git(trusting_gateway(server.cert), str(wrapper), str(private_home))
    result = ls_remote(git, mirror, cfg, auth, url)
    assert server.requests, "the https call must really have run"
    assert not trace.exists()
    assert TOKEN not in result.err and expected_basic().split()[1] not in result.err
    # And an ssh URL runs the shadowed ssh from PATH, never the caller's GIT_SSH(_COMMAND).
    ssh_url = "ssh://forge.example/x"
    ls_remote(git, mirror, cfg, prepare_network_auth(
        cfg, ssh_url, environ={}, gateway=Gateway(), warn=lambda m: None
    ), ssh_url)  # fmt: skip
    assert ssh_log.exists(), "the PATH-shadowed ssh must be what ran"
    assert not marker.exists()


def test_ssh_runs_in_batch_mode_with_the_agent_socket(
    mirror: Path, private_home: Path, tmp_path: Path
) -> None:
    log = tmp_path / "ssh-argv"
    wrapper = _shadow_bin(tmp_path, log)
    git = Git(Gateway(), str(wrapper), str(private_home))
    cfg = Config()
    auth = prepare_network_auth(
        cfg, "ssh://forge.example/org/r.git", environ={"SSH_AUTH_SOCK": "/run/agent.sock"},
        gateway=Gateway(), warn=lambda m: None,
    )  # fmt: skip
    result = run_network_git(
        git, ["ls-remote", "--", "ssh://forge.example/org/r.git"], cfg=cfg, auth=auth,
        kind=NetworkKind.NETWORK, tool_owned=True, git_dir=mirror,
    )  # fmt: skip
    assert result.returncode != 0
    argv = log.read_text().split("\n")
    assert "-o" in argv and "BatchMode=yes" in argv
    assert "sock=/run/agent.sock" in argv


def test_a_stalled_server_times_out_end_to_end(
    server: Capture, mirror: Path, private_home: Path
) -> None:
    server.stall = True
    url = f"https://127.0.0.1:{server.port}/org/repo.git"
    cfg, auth = auth_for(url)
    cfg = cfg.model_copy(update={"network_timeout": "1s"})
    git = Git(trusting_gateway(server.cert), GIT, str(private_home))
    result = ls_remote(git, mirror, cfg, auth, url)
    assert result.timed_out
    assert classify_fetch(result).cause == "timeout"


# -- #13: separator, owned config, the token-rejected message ---------------------------------------


@pytest.mark.parametrize("sub", ["ls-remote", "fetch", "clone", "push"])
def test_network_git_needs_a_separator_before_the_url(
    sub: str, mirror: Path, private_home: Path
) -> None:
    git = Git(Gateway(), GIT, str(private_home))
    cfg, auth = Config(), prepare_network_auth(
        Config(), "https://forge.example/o/r.git", environ={}, gateway=Gateway(), warn=print
    )  # fmt: skip
    with pytest.raises(ValueError, match="--"):
        run_network_git(
            git, [sub, "https://forge.example/o/r.git"], cfg=cfg, auth=auth,
            kind=NetworkKind.NETWORK, tool_owned=True, git_dir=mirror,
        )  # fmt: skip


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("http.proxy", "http://127.0.0.1:1"),
        ("url.https://evil.example/.insteadOf", "https://127.0.0.1:1/"),
        ("http.sslVerify", "false"),
        ("include.path", "/nonexistent"),
    ],
)
def test_a_token_is_not_sent_from_a_repository_with_foreign_config(
    server: Capture, mirror: Path, private_home: Path, key: str, value: str
) -> None:
    plain_git(["config", key, value], cwd=mirror, home=private_home)
    url = f"https://127.0.0.1:{server.port}/org/repo.git"
    cfg, auth = auth_for(url)
    git = Git(trusting_gateway(server.cert), GIT, str(private_home))
    with pytest.raises(WtctlError) as exc:
        ls_remote(git, mirror, cfg, auth, url)
    assert exc.value.code == "unsafe_repo_config"
    assert server.requests == []


def test_a_token_call_inside_a_repository_must_declare_its_owned_keys(
    server: Capture, mirror: Path, private_home: Path
) -> None:
    url = f"https://127.0.0.1:{server.port}/org/repo.git"
    cfg, auth = auth_for(url)
    git = Git(trusting_gateway(server.cert), GIT, str(private_home))
    with pytest.raises(ValueError, match="checked"):
        run_network_git(
            git, ["ls-remote", "--", url], cfg=cfg, auth=auth, kind=NetworkKind.NETWORK,
            tool_owned=True, git_dir=mirror,
        )  # fmt: skip
    assert server.requests == []


def test_a_rejected_token_is_named_but_a_missing_one_is_not(
    server: Capture, mirror: Path, private_home: Path
) -> None:
    from wtctl.hardgit import outcome_error

    url = f"https://127.0.0.1:{server.port}/org/repo.git"
    git = Git(trusting_gateway(server.cert), GIT, str(private_home))
    server.always_401 = True
    for env, expect in (({}, "credentials wtctl does not have"), (None, "rejected the configured")):
        cfg, auth = auth_for(url) if env is None else (Config(), prepare_network_auth(
            Config(), url, environ={}, gateway=Gateway(), warn=print
        ))  # fmt: skip
        server.requests.clear()
        result = ls_remote(git, mirror, cfg, auth, url)
        err = rejected_token_error(auth, outcome_error(classify_fetch(result), result))
        assert err is not None and err.code == "auth_required"
        assert expect in err.message
    assert rejected_token_error(auth, None) is None
