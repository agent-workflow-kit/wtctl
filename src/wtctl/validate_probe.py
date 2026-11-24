"""The live checks of ``wtctl validate --probe`` (issue #95).

Three families, each over the configured ``repos.<id>`` and ``hosts.<host>`` entries only:

- ``clones``: a configured ``repos.<id>.clone`` is a clone whose remote names the repo (the check
  ``locate`` runs, with hardened git, nothing written);
- ``tokens``: a ``hosts.<host>`` token source (``token_env``, ``token_command``) yields a token that
  would be accepted for a header; the token is never printed, stored or returned;
- ``remotes``: the repo's HTTPS fetch URL answers ``git ls-remote -- <url> HEAD`` (hardened git
  outside any repository, the host's token as a header scoped to that host, redirects off).

Nothing here writes ``state.db``, the audit or the log, takes a lease or prompts. The only files
are one private directory below wtctl's own tmp area, removed on exit. A failure is never an
error: it is a ``failed`` entry and a warning (the command still exits 0). Remote URLs and hosts
are untrusted config text: they are normalized as the other commands do, ssh and local remotes are
skipped (a token never goes over ssh, and a probe makes no ssh connection), and git's output is
classified, never shown.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from . import urls
from .context import Context
from .credentials import (
    TOKEN_COMMAND_TIMEOUT,
    NetworkKind,
    fetch_target,
    prepare_network_auth,
    resolve_token,
    timeout_seconds,
    token_header,
)
from .errors import WtctlError
from .gitsetup import private_dir, tmp_root
from .hardgit import FetchKind, Git, classify_fetch
from .locate import NotAClone, Resolver, verify_clone
from .output import escape_control

MAX_ITEMS = 20  # entries examined per family; more is a warning
MAX_TOTAL_S = 300.0  # wall clock for the whole probe; what is left then is skipped
MAX_CALL_S = 60.0  # one network call, whatever network_timeout says (it is capped, not raised)
LOCAL_GIT_S = 15.0  # a local git call on a clone
MIN_CALL_S = 1.0  # below this much budget left, nothing new is started
MAX_SHOWN = 200

OK, FAILED, SKIPPED = "ok", "failed", "skipped"

Item = dict[str, str]
Warning_ = dict[str, str]


class _BoundedGit(Git):
    """``Git`` whose local calls end after ``LOCAL_GIT_S`` unless the caller says otherwise."""

    def run(self, args: Any, *, timeout: float | None = None, **kw: Any) -> Any:
        return super().run(args, timeout=LOCAL_GIT_S if timeout is None else timeout, **kw)


def _shown(text: str) -> str:
    text = escape_control(text)
    return text if len(text) <= MAX_SHOWN else text[:MAX_SHOWN] + "..."


@dataclass
class _Probe:
    ctx: Context
    git: Git
    work: str  # a fresh private directory: cwd of the calls outside any repository
    started: float
    warnings: list[Warning_]

    def remaining(self) -> float:
        return MAX_TOTAL_S - (self.ctx.gateway.clock.monotonic() - self.started)

    def warn(self, code: str, key: str, message: str) -> None:
        self.warnings.append({"code": code, "key": key, "message": _shown(message)})


def _item(fields: Item, status: str, reason: str | None = None) -> Item:
    item = {**fields, "status": status}
    if reason is not None:
        item["reason"] = reason
    return item


def _guarded(fn: Callable[[], Item], fields: Item) -> Item:
    """One check that crashes is a failed entry, never a crashed ``validate``."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a doctor check must report, not die
        return {**fields, "status": FAILED, "reason": "internal_error"}


# ---------------------------------------------------------------- clones


def _check_clone(probe: _Probe, repo_id: str) -> Item:
    fields = {"repo": repo_id}
    cfg = probe.ctx.config
    s = cfg.repo(repo_id)
    assert s.clone is not None
    try:
        st = os.stat(s.clone)
    except (FileNotFoundError, NotADirectoryError):
        return _item(fields, SKIPPED, "missing")
    except (OSError, ValueError):
        return _item(fields, FAILED, "not_a_clone")
    if not stat.S_ISDIR(st.st_mode):
        return _item(fields, FAILED, "not_a_clone")
    try:
        verify_clone(probe.git, s.clone, repo_id, remote=s.remote, clone_url=s.clone_url)
    except (NotAClone, WtctlError):
        return _item(fields, FAILED, "not_a_clone")
    return _item(fields, OK)


# ---------------------------------------------------------------- tokens


def _check_token(probe: _Probe, host: str) -> Item:
    fields = {"host": host}
    cfg = probe.ctx.config
    hc = cfg.host(host)
    if hc.token_env is None and hc.token_command is None:
        return _item(fields, SKIPPED, "no_token_source")
    notes: list[str] = []
    token = resolve_token(
        cfg,
        host,
        environ=os.environ,
        gateway=probe.ctx.gateway,
        warn=notes.append,
        command_timeout=max(MIN_CALL_S, min(TOKEN_COMMAND_TIMEOUT, probe.remaining())),
    )
    if token is None:
        return _item(fields, FAILED, "token_unavailable")
    try:
        token_header(host, hc.token_user, token)
    except ValueError:
        return _item(fields, FAILED, "token_invalid")
    return _item(fields, OK)


# ---------------------------------------------------------------- remotes

_CAUSES = {"timeout", "network_unreachable", "repository_not_found", "rate_limited"}


class _EmbeddedCredentials(Exception):
    pass


def _remote_url(probe: _Probe, repo_id: str) -> str:
    """The repo's fetch URL as ``clone`` would use it (``WtctlError`` for an unusable config)."""
    cfg = probe.ctx.config
    s = cfg.repo(repo_id)
    if s.clone_url is not None:
        raw, what = s.clone_url, f"repos.{repo_id}.clone_url"
    else:
        host = urls.repo_host(repo_id)
        raw = urls.render_clone_url(repo_id, template=cfg.host(host).clone_url)
        what = f"hosts.{host}.clone_url"
    if urls.local_path(raw) is None and urls.url_scheme(raw) == "https":
        if urls.strip_userinfo(raw) != raw:
            raise _EmbeddedCredentials(what)
    return Resolver._plain_url(raw, what)  # noqa: SLF001 - the one place that normalizes it


def _check_remote(probe: _Probe, repo_id: str) -> Item:
    cfg = probe.ctx.config
    fields = {"repo": repo_id, "host": urls.repo_host(repo_id)}
    try:
        url = _remote_url(probe, repo_id)
        target = fetch_target(url)
    except _EmbeddedCredentials:
        return _item(fields, FAILED, "embedded_credentials")
    except (WtctlError, ValueError):
        return _item(fields, FAILED, "invalid_url")
    if target.scheme == "ssh":
        return _item(fields, SKIPPED, "ssh_not_probed")
    if target.scheme != "https":
        return _item(fields, SKIPPED, "local_remote")
    fields["host"] = target.hostport or fields["host"]
    budget = probe.remaining()
    if budget < MIN_CALL_S:
        return _item(fields, SKIPPED, "deadline")
    notes: list[str] = []
    auth = prepare_network_auth(
        cfg,
        url,
        environ=os.environ,
        gateway=probe.ctx.gateway,
        warn=notes.append,
        command_timeout=max(MIN_CALL_S, min(TOKEN_COMMAND_TIMEOUT, budget)),
    )
    budget = probe.remaining()
    if budget < MIN_CALL_S:
        return _item(fields, SKIPPED, "deadline")
    timeout = min(timeout_seconds(cfg, NetworkKind.NETWORK), MAX_CALL_S, budget)
    result = probe.git.run(
        [
            "ls-remote",
            "--",
            url,
            "HEAD",
        ],  # HEAD only: the answer stays tiny whatever the server says
        private_dir=probe.work,
        timeout=timeout,
        scheme=auth.scheme,
        tool_owned=True,  # a fresh private directory, no repository config to read
        extra_config=auth.extra_config,
    )
    outcome = classify_fetch(result)  # git's text is classified, never shown: nothing to scrub
    if outcome.ok:
        return _item(fields, OK)
    if outcome.kind is FetchKind.AUTH_REQUIRED:
        return _item(fields, FAILED, "token_rejected" if auth.has_token else "auth_required")
    cause = outcome.cause if outcome.cause in _CAUSES else "git_failed"
    return _item(fields, FAILED, cause)


# ---------------------------------------------------------------- the whole probe


_FAILURE_CODE = {
    "clones": ("probe_clone_failed", "repos"),
    "tokens": ("probe_token_failed", "hosts"),
    "remotes": ("probe_remote_failed", "repos"),
}


def _family(
    probe: _Probe,
    name: str,
    ids: list[str],
    check: Callable[[_Probe, str], Item],
    field: str,
) -> list[Item]:
    items: list[Item] = []
    if len(ids) > MAX_ITEMS:
        probe.warn(
            "probe_truncated",
            name,
            f"{len(ids)} {name} entries; only the first {MAX_ITEMS} were probed",
        )
    code, section = _FAILURE_CODE[name]
    for ident in ids[:MAX_ITEMS]:
        if probe.remaining() < MIN_CALL_S:
            items.append({field: ident, "status": SKIPPED, "reason": "deadline"})
            continue
        item = _guarded(lambda ident=ident: check(probe, ident), {field: ident})
        items.append(item)
        if item["status"] == FAILED:
            probe.warn(
                code,
                f"{section}.{ident}",
                f"{_shown(ident)}: {item['reason'].replace('_', ' ')}",
            )
    return items


def run_probe(ctx: Context, git_bin: str) -> tuple[dict[str, Any], list[Warning_]]:
    """The ``probe`` object and its warnings. Never raises for a failing check."""
    cfg = ctx.config
    warnings: list[Warning_] = []
    clone_ids = sorted(i for i, r in cfg.repos.items() if r.clone is not None)
    repo_ids = sorted(cfg.repos)
    hosts = sorted(cfg.hosts)
    stack = ExitStack()
    try:
        work = stack.enter_context(private_dir())  # wtctl's own tmp area; removed on exit
        home = str(tmp_root())
    except (WtctlError, OSError):
        warnings.append(
            {
                "code": "probe_unavailable",
                "key": "probe",
                "message": "wtctl's private temporary directory is not usable; nothing probed",
            }
        )
        skipped = "workdir_unavailable"
        return {
            "clones": [{"repo": i, "status": SKIPPED, "reason": skipped} for i in clone_ids],
            "tokens": [{"host": h, "status": SKIPPED, "reason": skipped} for h in hosts],
            "remotes": [{"repo": i, "status": SKIPPED, "reason": skipped} for i in repo_ids],
        }, warnings
    try:
        git = _BoundedGit(ctx.gateway, git_bin, home)
        probe = _Probe(ctx, git, work, ctx.gateway.clock.monotonic(), warnings)
        out = {
            "clones": _family(probe, "clones", clone_ids, _check_clone, "repo"),
            "tokens": _family(probe, "tokens", hosts, _check_token, "host"),
            "remotes": _family(probe, "remotes", repo_ids, _check_remote, "repo"),
        }
        if probe.remaining() < MIN_CALL_S:
            probe.warn(
                "probe_deadline",
                "probe",
                f"the probe ran out of its {int(MAX_TOTAL_S)} second budget; the rest was skipped",
            )
        return out, warnings
    finally:
        stack.close()
