"""``wtctl validate --probe``: live checks against loopback fixtures only (issue #95).

A loopback HTTPS server speaks just enough smart HTTP for ``git ls-remote`` (the advertisement
comes from a real ``git upload-pack``) and can be told to demand a token, stall, redirect or
send hostile bytes. The gateway the command runs through trusts only the test certificate and
refuses (and records) any git call whose URL is not on loopback, so no test can reach a real host.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import Run
from tests.schemacheck import validate
from tests.vectorlib import GIT, plain_git
from wtctl import validate_probe
from wtctl.cli import main
from wtctl.context import Runtime
from wtctl.gateway import Gateway, GitResult, run_git

TOKEN = "tok-SECRET-0123456789abcdef"
BASIC = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()


@dataclass
class Srv:
    port: int
    cert: Path
    advert: bytes
    mode: str = "open"  # open | token | stall | redirect | garbage | error
    requests: list[dict[str, str]] = field(default_factory=list)
    release: threading.Event = field(default_factory=threading.Event)

    @property
    def repo_id(self) -> str:
        return f"127.0.0.1:{self.port}/org/repo"


def _pkt(text: bytes) -> bytes:
    return b"%04x" % (len(text) + 4) + text


@pytest.fixture
def srv(tmp_path: Path, private_home: Path) -> Iterator[Srv]:
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
        check=True, capture_output=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "OPENSSL_CONF": "/dev/null"},
    )  # fmt: skip
    repo = tmp_path / "served"
    repo.mkdir()
    plain_git(["init", "-q", "-b", "main", str(repo)], cwd=tmp_path, home=private_home)
    plain_git(["commit", "-q", "--allow-empty", "-m", "x"], cwd=repo, home=private_home)
    adv = subprocess.run(
        [GIT, "upload-pack", "--stateless-rpc", "--advertise-refs", str(repo)],
        capture_output=True, check=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": str(private_home), "GIT_CONFIG_NOSYSTEM": "1"},
    ).stdout  # fmt: skip
    cap = Srv(0, cert, _pkt(b"# service=git-upload-pack\n") + b"0000" + adv)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            cap.requests.append(
                {"path": self.path, **{k.lower(): v for k, v in self.headers.items()}}
            )
            if cap.mode == "stall":
                cap.release.wait(20)
                return
            if cap.mode == "redirect":
                self.send_response(302)
                self.send_header("Location", f"https://127.0.0.1:{cap.port}/after-redirect")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if cap.mode == "garbage":
                body = (b"\x1b[31mFAKE\x1b[0m\xe2\x80\xaeremote: password=hunter2\n" * 4000)[
                    :300000
                ]
                self.send_response(200)
                self.send_header("Content-Type", "application/x-git-upload-pack-advertisement")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if cap.mode == "error":
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if cap.mode == "token" and self.headers.get("Authorization") != f"Basic {BASIC}":
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="t"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-git-upload-pack-advertisement")
            self.send_header("Content-Length", str(len(cap.advert)))
            self.end_headers()
            self.wfile.write(cap.advert)

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


class Guard:
    """A gateway that trusts the test certificate and refuses every non-loopback URL."""

    def __init__(self, cert: Path | None) -> None:
        self.cert = cert
        self.urls: list[str] = []
        self.refused: list[str] = []

    def gateway(self) -> Gateway:
        def runner(argv: list[str], *, env: dict[str, str], **kw: Any) -> GitResult:
            if "ls-remote" in argv and "--get-url" not in argv:
                url = argv[argv.index("--") + 1]
                self.urls.append(url)
                if not url.startswith("https://127.0.0.1:"):
                    self.refused.append(url)
                    return GitResult(128, b"", b"fatal: refused by the test guard\n")
            extra = {"GIT_SSL_CAINFO": str(self.cert)} if self.cert else {}
            return run_git(argv, env={**env, **extra}, **kw)

        return Gateway(git_runner=runner)


@pytest.fixture(autouse=True)
def no_real_connections(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """In-process code must not resolve or connect anywhere (git children are guarded above)."""
    seen: list[object] = []
    real_connect = socket.socket.connect

    def spy(self: socket.socket, addr: Any) -> Any:
        seen.append(addr)
        if not (isinstance(addr, tuple) and addr[0] == "127.0.0.1"):
            raise AssertionError(f"non-loopback connection {addr!r}")
        return real_connect(self, addr)

    monkeypatch.setattr(socket.socket, "connect", spy)
    return seen


def config(tmp_path: Path, **keys: object) -> str:
    root = tmp_path / "wt"
    root.mkdir(exist_ok=True)
    path = tmp_path / "cfg.yaml"
    path.write_text(json.dumps({"root": str(root), **keys}))
    return str(path)


def probe(
    capsys: pytest.CaptureFixture[str], cfg: str, guard: Guard, *extra: str, json_out: bool = True
) -> Run:
    rt = Runtime(stdin_isatty=False, me_name="alice", gateway=guard.gateway())
    argv = ["validate", "--probe", "--config", cfg, *extra]
    if json_out:
        argv.append("--json")
    code = main(argv, runtime=rt)
    c = capsys.readouterr()
    return Run(code, c.out, c.err)


def remote_cfg(tmp_path: Path, srv: Srv, **extra: object) -> str:
    return config(
        tmp_path,
        network_timeout="1s" if srv.mode == "stall" else "20s",
        repos={srv.repo_id: {}},
        hosts={f"127.0.0.1:{srv.port}": {"token_env": "PROBE_TOKEN"}},
        **extra,
    )


def only(data: dict[str, Any], family: str) -> dict[str, str]:
    (item,) = data["probe"][family]
    return item


@pytest.fixture(autouse=True)
def clean_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROBE_TOKEN", raising=False)


# ---------------------------------------------------------------- offline default


def test_without_probe_there_is_no_probe_member(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv
) -> None:
    g = Guard(srv.cert)
    rt = Runtime(stdin_isatty=False, me_name="alice", gateway=g.gateway())
    code = main(["validate", "--json", "--config", remote_cfg(tmp_path, srv)], runtime=rt)
    assert code == 0
    assert "probe" not in json.loads(capsys.readouterr().out)
    assert g.urls == [] and srv.requests == []


# ---------------------------------------------------------------- remotes


def test_remote_ok_with_a_token(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv.mode = "token"
    monkeypatch.setenv("PROBE_TOKEN", TOKEN)
    r = probe(capsys, remote_cfg(tmp_path, srv), Guard(srv.cert))
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/validate.json", data)
    assert only(data, "remotes") == {
        "repo": srv.repo_id, "host": f"127.0.0.1:{srv.port}", "status": "ok",
    }  # fmt: skip
    assert only(data, "tokens") == {"host": f"127.0.0.1:{srv.port}", "status": "ok"}
    assert data["warnings"] == []
    assert srv.requests and all(q.get("authorization") == f"Basic {BASIC}" for q in srv.requests)
    assert all(q["host"] == f"127.0.0.1:{srv.port}" for q in srv.requests)
    for secret in (TOKEN, BASIC):
        assert secret not in r.out and secret not in r.err


def test_remote_ok_anonymous_without_any_credentials(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv
) -> None:
    r = probe(capsys, config(tmp_path, repos={srv.repo_id: {}}), Guard(srv.cert))
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/validate.json", data)
    assert only(data, "remotes")["status"] == "ok"
    assert data["probe"]["tokens"] == []
    assert all("authorization" not in q for q in srv.requests)


def test_auth_required_is_a_warning_not_an_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv
) -> None:
    srv.mode = "token"
    r = probe(capsys, remote_cfg(tmp_path, srv), Guard(srv.cert))
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/validate.json", data)
    item = only(data, "remotes")
    assert (item["status"], item["reason"]) == ("failed", "auth_required")
    assert only(data, "tokens")["reason"] == "token_unavailable"
    codes = {w["code"] for w in data["warnings"]}
    assert {"probe_remote_failed", "probe_token_failed"} <= codes
    assert "warning: repos." in r.err


def test_rejected_token_is_reported_without_the_token(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv.mode = "token"
    monkeypatch.setenv("PROBE_TOKEN", "tok-WRONG-0123456789")
    r = probe(capsys, remote_cfg(tmp_path, srv), Guard(srv.cert))
    assert r.code == 0
    assert only(r.json(), "remotes")["reason"] == "token_rejected"
    assert "WRONG" not in r.out + r.err
    assert BASIC not in r.out + r.err


def test_unreachable_server_fails_fast(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    started = time.monotonic()
    r = probe(capsys, config(tmp_path, repos={f"127.0.0.1:{port}/org/repo": {}}), Guard(None))
    assert r.code == 0
    item = only(r.json(), "remotes")
    assert item["status"] == "failed"
    assert item["reason"] in {"network_unreachable", "git_failed"}
    assert time.monotonic() - started < 30


def test_slow_server_hits_the_deadline(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv
) -> None:
    srv.mode = "stall"
    started = time.monotonic()
    r = probe(capsys, remote_cfg(tmp_path, srv), Guard(srv.cert))
    assert r.code == 0
    assert only(r.json(), "remotes")["reason"] == "timeout"
    assert time.monotonic() - started < 20


def test_total_budget_skips_the_rest(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    srv: Srv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    srv.mode = "stall"
    monkeypatch.setattr(validate_probe, "MAX_TOTAL_S", 2.5)
    cfg = config(
        tmp_path,
        network_timeout="30s",
        repos={f"127.0.0.1:{srv.port}/org/a": {}, f"127.0.0.1:{srv.port}/org/b": {}},
    )
    started = time.monotonic()
    r = probe(capsys, cfg, Guard(srv.cert))
    assert r.code == 0
    data = r.json()
    reasons = [i["reason"] for i in data["probe"]["remotes"]]
    assert reasons == ["timeout", "deadline"]
    assert "probe_deadline" in {w["code"] for w in data["warnings"]}
    assert time.monotonic() - started < 20


def test_redirect_is_not_followed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv, monkeypatch: pytest.MonkeyPatch
) -> None:
    srv.mode = "redirect"
    monkeypatch.setenv("PROBE_TOKEN", TOKEN)
    r = probe(capsys, remote_cfg(tmp_path, srv), Guard(srv.cert))
    assert r.code == 0
    assert only(r.json(), "remotes")["status"] == "failed"
    assert not [q for q in srv.requests if "after-redirect" in q["path"]]
    assert TOKEN not in r.out + r.err


@pytest.mark.parametrize("mode", ["garbage", "error"])
def test_hostile_server_output_is_classified_never_shown(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv, mode: str
) -> None:
    srv.mode = mode
    r = probe(capsys, config(tmp_path, repos={srv.repo_id: {}}), Guard(srv.cert))
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/validate.json", data)
    assert only(data, "remotes")["status"] == "failed"
    text = r.out + r.err
    for bad in ("\x1b", "hunter2", "FAKE", "‮"):
        assert bad not in text
    assert r.out.isascii() and len(text) < 5000


# ---------------------------------------------------------------- untrusted config values


def test_unsafe_remote_urls_are_never_contacted(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv
) -> None:
    base = f"127.0.0.1:{srv.port}"
    repos = {
        f"{base}/org/creds": {"clone_url": f"https://user:pw@{base}/org/creds.git"},
        f"{base}/org/ext": {"clone_url": "ext::sh -c id"},
        f"{base}/org/file": {"clone_url": "file:///etc/passwd"},
        f"{base}/org/ssh": {"clone_url": "ssh://git@127.0.0.1:2/org/ssh"},
        f"{base}/org/local": {"clone_url": "/some/local/path"},
        f"{base}/org/opt": {"clone_url": "--upload-pack=touch /tmp/x"},
    }
    g = Guard(srv.cert)
    r = probe(capsys, config(tmp_path, repos=repos), g)
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/validate.json", data)
    got = {
        i["repo"].rsplit("/", 1)[1]: (i["status"], i["reason"]) for i in data["probe"]["remotes"]
    }
    assert got["creds"] == ("failed", "embedded_credentials")
    assert got["ext"][0] == "failed"
    assert got["file"][0] in ("failed", "skipped")
    assert got["ssh"] == ("skipped", "ssh_not_probed")
    assert got["local"] == ("skipped", "local_remote")
    assert got["opt"][0] == "failed"
    assert g.urls == [] and srv.requests == []
    assert "pw@" not in r.out + r.err and ":pw" not in r.out + r.err


def test_too_many_entries_are_truncated(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    repos = {
        f"example.test/org/r{i:02d}": {"clone_url": f"ssh://git@example.test/org/r{i:02d}"}
        for i in range(25)
    }
    r = probe(capsys, config(tmp_path, repos=repos), Guard(None))
    assert r.code == 0
    data = r.json()
    assert len(data["probe"]["remotes"]) == 20
    assert "probe_truncated" in {w["code"] for w in data["warnings"]}
    validate("wtctl/cli/validate.json", data)


# ---------------------------------------------------------------- tokens


def test_token_command_yields_a_token_that_is_never_printed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv
) -> None:
    srv.mode = "token"
    script = tmp_path / "tok.sh"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{TOKEN}'\n")
    script.chmod(0o755)
    cfg = config(
        tmp_path,
        repos={srv.repo_id: {}},
        hosts={f"127.0.0.1:{srv.port}": {"token_command": [str(script), "{host}"]}},
    )
    r = probe(capsys, cfg, Guard(srv.cert))
    assert r.code == 0
    data = r.json()
    assert only(data, "tokens")["status"] == "ok"
    assert only(data, "remotes")["status"] == "ok"
    assert TOKEN not in r.out + r.err


def test_failing_token_command_is_a_warning(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    script = tmp_path / "bad.sh"
    script.write_text("#!/bin/sh\necho oops-secret >&2\nexit 3\n")
    script.chmod(0o755)
    cfg = config(tmp_path, hosts={"example.test": {"token_command": [str(script)]}})
    r = probe(capsys, cfg, Guard(None))
    assert r.code == 0
    data = r.json()
    assert only(data, "tokens")["reason"] == "token_unavailable"
    assert data["warnings"][0]["code"] == "probe_token_failed"
    assert "oops-secret" not in r.out + r.err


def test_host_without_a_token_source_is_skipped(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    r = probe(capsys, config(tmp_path, hosts={"example.test": {}}), Guard(None))
    assert only(r.json(), "tokens") == {
        "host": "example.test", "status": "skipped", "reason": "no_token_source",
    }  # fmt: skip
    assert r.json()["warnings"] == []


# ---------------------------------------------------------------- clones


def test_clone_checks(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, private_home: Path
) -> None:
    good, bad, gone = tmp_path / "good", tmp_path / "bad", tmp_path / "gone"
    for d, url in (
        (good, "https://example.test/org/good.git"),
        (bad, "https://example.test/org/elsewhere.git"),
    ):
        d.mkdir()
        plain_git(["init", "-q", str(d)], cwd=tmp_path, home=private_home)
        plain_git(["remote", "add", "origin", url], cwd=d, home=private_home)
    repos = {
        "example.test/org/good": {"clone": str(good)},
        "example.test/org/bad": {"clone": str(bad)},
        "example.test/org/gone": {"clone": str(gone)},
    }
    g = Guard(None)
    r = probe(capsys, config(tmp_path, repos=repos), g)
    assert r.code == 0
    data = r.json()
    validate("wtctl/cli/validate.json", data)
    got = {
        i["repo"].rsplit("/", 1)[1]: (i["status"], i.get("reason")) for i in data["probe"]["clones"]
    }
    assert got == {
        "good": ("ok", None),
        "bad": ("failed", "not_a_clone"),
        "gone": ("skipped", "missing"),
    }
    assert [w["code"] for w in data["warnings"] if w["code"] == "probe_clone_failed"] == [
        "probe_clone_failed"
    ]
    assert not gone.exists()  # a missing clone is not created
    # these remotes are https on a non-loopback name: the guard must have stopped every call
    assert g.urls and set(g.refused) == set(g.urls)


# ---------------------------------------------------------------- read-only, private, bounded


def _snapshot(base: Path) -> list[tuple[str, int, int]]:
    out = []
    for dirpath, dirs, files in os.walk(base):
        for name in dirs + files:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out.append((p, st.st_mtime_ns, st.st_size))
    return sorted(out)


def test_probe_writes_no_state_and_cleans_its_tmp(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv, private_home: Path
) -> None:
    cfg = remote_cfg(tmp_path, srv)
    before_cfg_tree = _snapshot(tmp_path / "wt")
    r = probe(capsys, cfg, Guard(srv.cert))
    assert r.code == 0
    state = Path(os.environ["XDG_STATE_HOME"])
    files = [p for p in state.rglob("*") if not p.is_dir()]
    assert files == []  # no state.db, audit.jsonl, log.jsonl, lock or leftover temp file
    tmp_dirs = [p for p in state.rglob("*") if p.is_dir() and p.name.startswith("p-")]
    assert tmp_dirs == []
    assert _snapshot(tmp_path / "wt") == before_cfg_tree
    assert not (private_home / "wtctl").exists()


def test_probe_with_unusable_tmp_area_skips_and_warns(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, srv: Srv, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x")  # a file where the state directory should be
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    g = Guard(srv.cert)
    r = probe(capsys, remote_cfg(tmp_path, srv), g)
    assert r.code == 0
    data = r.json()
    assert only(data, "remotes")["reason"] == "workdir_unavailable"
    assert [w["code"] for w in data["warnings"]] == ["probe_unavailable"]
    assert srv.requests == [] and g.urls == []
    validate("wtctl/cli/validate.json", data)


def test_probe_with_nothing_configured_is_empty_and_ok(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    r = probe(capsys, config(tmp_path), Guard(None))
    assert r.code == 0
    data = r.json()
    assert data["probe"] == {"clones": [], "tokens": [], "remotes": []}
    assert data["warnings"] == []
    validate("wtctl/cli/validate.json", data)


def test_human_output_and_quiet(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cfg = config(tmp_path, hosts={"example.test": {}})
    r = probe(capsys, cfg, Guard(None), json_out=False)
    assert r.code == 0
    assert "probe: 0 ok, 0 failed, 1 skipped" in r.out


def test_probe_failure_does_not_change_the_exit_code_of_a_bad_config(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    r = probe(capsys, config(tmp_path, bogus_key=1), Guard(None))
    assert r.code == 2  # config errors still win; nothing was probed
    assert "probe" not in r.json()


def test_git_runs_in_a_clean_hardened_environment(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    srv: Srv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_SSH_COMMAND", "touch /tmp/should-never-exist")
    monkeypatch.setenv("GIT_TRACE", str(tmp_path / "trace"))
    seen: list[tuple[list[str], dict[str, str]]] = []
    g = Guard(srv.cert)
    inner = g.gateway()

    def runner(argv: list[str], **kw: Any) -> GitResult:
        if "ls-remote" in argv:
            seen.append((argv, kw["env"]))
        return inner.git(argv, **kw)

    rt = Runtime(stdin_isatty=False, me_name="alice", gateway=Gateway(git_runner=runner))
    code = main(
        ["validate", "--probe", "--json", "--config", config(tmp_path, repos={srv.repo_id: {}})],
        runtime=rt,
    )
    capsys.readouterr()
    assert code == 0
    ((argv, env),) = seen
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert "GIT_SSH_COMMAND" not in env and "GIT_TRACE" not in env
    assert "http.followRedirects=false" in argv
    assert argv[argv.index("ls-remote") + 1] == "--"
    assert not os.path.exists("/tmp/should-never-exist")


def test_dry_run_is_still_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["validate", "--probe", "--dry-run"], runtime=Runtime(stdin_isatty=False))
    capsys.readouterr()
    assert code == 2
