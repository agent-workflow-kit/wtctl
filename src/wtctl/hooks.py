"""Hooks: configured command lists, payloads, the environment, and the run/veto/escalate logic.

Rules are in docs/interface.md (Hooks) and design guide section 8. This module runs commands
through ``gateway.run_step`` (own session and process group, timeout, capped output) and never
touches state, the log or the audit trail: it returns what happened (``HookRun`` records,
warnings, decision ids) and the calling command records it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import gateway
from .actor import Actor
from .config import Config, PostHook, PreHook
from .errors import UsageError, WtctlError
from .output import escape_text_lines

PRE_HOOKS = ("pre-create", "pre-remove")
POST_HOOKS = ("post-create", "post-remove", "post-reconcile", "post-baseline-accept")
HOOK_NAMES = (*PRE_HOOKS, *POST_HOOKS)

DEFAULT_TIMEOUT = 30
EXIT_ESCALATE = 13
MAX_STDOUT = 65536  # bytes of hook stdout kept; a larger output is never parsed
MAX_STDERR = 4096  # hook_stderr is cut to this many bytes (interface: Errors)
MAX_DECISION_IDS = 32
DECISION_ID_RE = re.compile(r"[A-Za-z0-9._:@/-]{1,128}")

# The payload fields each hook adds to the common ones (schemas/hooks/<hook>.json).
COMMON_FIELDS = frozenset({"object_type", "object_id", "repo", "labels"})
PAYLOAD_FIELDS: Mapping[str, frozenset[str]] = {
    "pre-create": frozenset(
        {"clone_path", "path", "branch", "branch_action", "base", "ref", "source"}
        | {"actor", "decision_ids"}
    ),
    "post-create": frozenset({"record", "bootstrap"}),
    "pre-remove": frozenset(
        {"record", "checks", "merged_head", "overridden", "actor", "decision_ids"}
    ),
    "post-remove": frozenset({"record", "branch_deleted", "branch_kept_reason", "overridden"}),
    "post-reconcile": frozenset({"changes", "reports"}),
    "post-baseline-accept": frozenset({"record", "items", "actor"}),
}

Runner = Callable[..., gateway.StepResult]
Spawner = Callable[..., int]


@dataclass(frozen=True)
class HookSpec:
    cmd: str
    timeout: int = DEFAULT_TIMEOUT
    is_async: bool = False


@dataclass(frozen=True)
class HookRun:
    """What one hook did; the caller writes it to the log (event ``hook``)."""

    hook: str
    cmd: str
    exit_code: int | None  # negative: killed by that signal; None: did not start / async
    timed_out: bool
    launched_async: bool
    duration: float
    stderr: str  # escaped, at most 4096 bytes; untrusted
    start_error: str | None = None


@dataclass(frozen=True)
class PreHookOutcome:
    runs: tuple[HookRun, ...]
    decision_ids: tuple[str, ...]  # the caller's ids followed by the hooks' (at most 32)
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class PostHookOutcome:
    runs: tuple[HookRun, ...]
    decision_ids: tuple[str, ...]
    warnings: tuple[str, ...]


# --- configuration ---------------------------------------------------------------------------


def configured_hooks(config: Config, name: str, extra: Sequence[str] = ()) -> list[HookSpec]:
    """The configured hooks for ``name`` in config order (drop-ins appended), then ``extra``
    (the ``--hook NAME=CMD`` commands of this invocation, default timeout, never async)."""
    if name not in HOOK_NAMES:
        raise ValueError(f"unknown hook {name!r}")
    entries: list[PreHook] | list[PostHook] = getattr(config.hooks, name.replace("-", "_"))
    specs = [
        HookSpec(e.cmd, e.timeout, e.async_ if isinstance(e, PostHook) else False) for e in entries
    ]
    return specs + [HookSpec(cmd) for cmd in extra]


def parse_hook_flags(values: Sequence[str]) -> dict[str, list[str]]:
    """``--hook NAME=CMD`` values to ``{name: [cmd, ...]}`` in the order given."""
    out: dict[str, list[str]] = {}
    for value in values:
        name, sep, cmd = value.partition("=")
        if not sep or name not in HOOK_NAMES:
            raise UsageError(f"--hook expects NAME=CMD with NAME one of {', '.join(HOOK_NAMES)}")
        if not cmd.strip() or "\x00" in cmd:
            raise UsageError(f"--hook {name}: the command is empty or contains a NUL byte")
        out.setdefault(name, []).append(cmd)
    return out


def hook_env(
    config: Config,
    environ: Mapping[str, str],
    *,
    hook: str,
    object_id: str | None,
    object_type: str,
) -> dict[str, str]:
    """``environ`` minus every ``hosts.*.token_env`` variable, plus the ``WTCTL_*`` hook variables.

    Accident prevention, not a boundary (interface: Residual risks). Only ids and names go in
    the environment; untrusted text travels in the stdin payload.
    """
    tokens = {h.token_env for h in config.hosts.values() if h.token_env}
    env = {k: v for k, v in environ.items() if k not in tokens}
    env["WTCTL_HOOK"] = hook
    env["WTCTL_ID"] = object_id or ""
    env["WTCTL_OBJECT_TYPE"] = object_type
    return env


def build_payload(hook: str, **fields: Any) -> dict[str, Any]:
    """The stdin document: ``tool``, ``hook`` and exactly the fields the hook's schema requires.

    A missing or extra field is a programmer error (``ValueError``); the tests validate the
    result against ``schemas/hooks/<hook>.json``.
    """
    if hook not in PAYLOAD_FIELDS:
        raise ValueError(f"unknown hook {hook!r}")
    want = COMMON_FIELDS | PAYLOAD_FIELDS[hook]
    missing, extra = want - fields.keys(), fields.keys() - want
    if missing:
        raise ValueError(f"{hook} payload is missing {sorted(missing)}")
    if extra:
        raise ValueError(f"{hook} payload has unexpected {sorted(extra)}")
    return {"tool": "wtctl", "hook": hook, **fields}


def _encode(payload: Mapping[str, Any]) -> bytes:
    # ASCII-only JSON: hostile text (controls, bidi, lone surrogates) is escaped on the wire.
    return json.dumps(payload, allow_nan=False).encode("ascii")


# --- decision ids ----------------------------------------------------------------------------


_TRUNC = "stdout too large, truncated; decision ids ignored"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"invalid JSON constant {name}")


def _hook_decision_ids(stdout: bytes, truncated: bool, label: str) -> tuple[list[str], list[str]]:
    """Valid ``decision_id`` strings a hook printed, and warnings about what was dropped."""
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text.startswith("{"):
        return [], ([f"{label}: {_TRUNC}"] if truncated else [])
    if truncated:
        return [], [f"{label}: {_TRUNC}"]
    try:
        doc = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return [], [f"{label}: stdout is not valid JSON; decision ids ignored"]
    if not isinstance(doc, dict) or "decision_id" not in doc:
        return [], []
    raw = doc["decision_id"]
    items = raw if isinstance(raw, list) else [raw]
    ids: list[str] = []
    dropped = 0
    for item in items:
        if isinstance(item, str) and DECISION_ID_RE.fullmatch(item):
            ids.append(item)
        else:
            dropped += 1
    if dropped:
        return ids, [f"{label}: dropped {dropped} invalid decision id value(s)"]
    return ids, []


def merge_decision_ids(
    existing: Sequence[str], new: Sequence[str]
) -> tuple[tuple[str, ...], list[str]]:
    """Append ``new`` ids not already present, keeping at most 32 in total."""
    merged = list(dict.fromkeys(existing))
    warnings: list[str] = []
    overflow = 0
    for item in new:
        if item in merged:
            continue
        if len(merged) >= MAX_DECISION_IDS:
            overflow += 1
        else:
            merged.append(item)
    if overflow:
        warnings.append(f"dropped {overflow} decision id(s) beyond the limit of {MAX_DECISION_IDS}")
    return tuple(merged), warnings


# --- running ---------------------------------------------------------------------------------


def _stderr_text(raw: bytes) -> str:
    text = escape_text_lines(raw.decode("utf-8", errors="replace"))
    return text.encode("utf-8")[:MAX_STDERR].decode("utf-8", errors="ignore")


def _describe(name: str, spec: HookSpec, res: gateway.StepResult) -> str:
    if res.start_error:
        return f"{name} hook did not start: {res.start_error}"
    if res.timed_out:
        return f"{name} hook timed out after {spec.timeout}s"
    if res.signal is not None:
        return f"{name} hook was killed by signal {res.signal}"
    return f"{name} hook exited {res.returncode}"


def _run_one(
    name: str,
    spec: HookSpec,
    body: bytes,
    env: Mapping[str, str],
    cwd: str | None,
    runner: Runner,
    on_tick: Callable[[], None] | None,
) -> tuple[HookRun, gateway.StepResult]:
    res = runner(
        gateway.sh_argv(spec.cmd),
        env=env,
        cwd=cwd,
        stdin=body,
        timeout=spec.timeout,
        max_stdout=MAX_STDOUT,
        max_stderr=MAX_STDERR,
        on_tick=on_tick,
    )
    stderr = _stderr_text(res.stderr)
    if res.start_error:
        stderr = _stderr_text(res.start_error.encode())
    elif res.timed_out:
        stderr = _stderr_text(f"hook timed out after {spec.timeout}s\n".encode()) + stderr
        stderr = stderr[:MAX_STDERR]
    run = HookRun(
        hook=name,
        cmd=spec.cmd,
        exit_code=res.returncode,
        timed_out=res.timed_out,
        launched_async=False,
        duration=res.duration,
        stderr=stderr,
        start_error=res.start_error,
    )
    return run, res


def _attach(err: WtctlError, runs: Sequence[HookRun], warnings: Sequence[str]) -> None:
    """Expose what ran before a veto/escalation so the caller can log it (not serialized)."""
    err.hook_runs = tuple(runs)  # type: ignore[attr-defined]
    err.hook_warnings = tuple(warnings)  # type: ignore[attr-defined]


def run_pre_hooks(
    name: str,
    specs: Sequence[HookSpec],
    payload: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    actor: Actor,
    override_escalation: bool,
    decision_ids: Sequence[str] = (),
    cwd: str | None = None,
    runner: Runner = gateway.run_step,
    on_tick: Callable[[], None] | None = None,
) -> PreHookOutcome:
    """Run a pre-hook list in order (every hook is synchronous, ``async`` is ignored).

    Exit 0 continues; exit 13 escalates and the rest still run; any other exit, a signal, a
    timeout or a failure to start vetoes at once. Raises ``WtctlError`` ``vetoed`` (exit 11) or,
    when nothing vetoed and something escalated, ``escalated`` (exit 13). With
    ``override_escalation`` (human actors only) exit 13 counts as allow; vetoes are unchanged.
    ``decision_ids`` are the caller's (``--decision-id``); hooks' ids are appended.
    """
    if name not in PRE_HOOKS:
        raise ValueError(f"{name!r} is not a pre-hook")
    if override_escalation and not actor.is_human:
        raise UsageError("--override-escalation requires a human actor")
    body = _encode(payload)
    runs: list[HookRun] = []
    warnings: list[str] = []
    ids: tuple[str, ...] = tuple(decision_ids)
    returned = False
    escalated: list[HookRun] = []

    def fields() -> dict[str, Any]:
        return {"decision_ids": list(ids)} if returned else {}

    for spec in specs:
        run, res = _run_one(name, spec, body, env, cwd, runner, on_tick)
        runs.append(run)
        label = f"{name} hook {spec.cmd!r}"
        found, warns = _hook_decision_ids(res.stdout, res.stdout_truncated, label)
        warnings += warns
        if res.output_incomplete:
            warnings.append(f"{label}: output incomplete, a process kept the pipes open")
        if found:
            returned = True
            ids, merge_warns = merge_decision_ids(ids, found)
            warnings += merge_warns
        if res.returncode == 0 and not res.timed_out:
            continue
        if res.returncode == EXIT_ESCALATE and not res.timed_out:
            if override_escalation:
                warnings.append(f"{label}: escalation overridden by {actor.value}")
            else:
                escalated.append(run)
            continue
        veto = WtctlError(
            "vetoed",
            _describe(name, spec, res),
            hook=spec.cmd,
            hook_stderr=run.stderr,
            **fields(),
        )
        _attach(veto, runs, warnings)
        raise veto
    if escalated:
        first = escalated[0]
        esc = WtctlError(
            "escalated",
            f"{name} hook escalated: a human must decide",
            hook=first.cmd,
            hook_stderr=first.stderr,
            escalated_hooks=[r.cmd for r in escalated],
            **fields(),
        )
        _attach(esc, runs, warnings)
        raise esc
    return PreHookOutcome(tuple(runs), ids, tuple(warnings))


def run_post_hooks(
    name: str,
    specs: Sequence[HookSpec],
    payload: Mapping[str, Any],
    *,
    env: Mapping[str, str],
    decision_ids: Sequence[str] = (),
    cwd: str | None = None,
    runner: Runner = gateway.run_step,
    spawner: Spawner = gateway.spawn_detached,
    on_tick: Callable[[], None] | None = None,
) -> PostHookOutcome:
    """Run a post-hook list in order. Failures become warnings, never errors.

    An ``async`` hook is started detached (its timeout still enforced) and its outcome is never
    seen, so it produces no warning.
    """
    if name not in POST_HOOKS:
        raise ValueError(f"{name!r} is not a post-hook")
    body = _encode(payload)
    runs: list[HookRun] = []
    warnings: list[str] = []
    ids: tuple[str, ...] = tuple(decision_ids)
    for spec in specs:
        label = f"{name} hook {spec.cmd!r}"
        if spec.is_async:
            try:
                spawner(
                    gateway.sh_argv(spec.cmd), env=env, cwd=cwd, stdin=body, timeout=spec.timeout
                )
                error = None
            except (OSError, ValueError, OverflowError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                warnings.append(f"{label} did not start: {error}")
            runs.append(
                HookRun(name, spec.cmd, None, False, error is None, 0.0, "", start_error=error)
            )
            continue
        run, res = _run_one(name, spec, body, env, cwd, runner, on_tick)
        runs.append(run)
        found, warns = _hook_decision_ids(res.stdout, res.stdout_truncated, label)
        warnings += warns
        if res.output_incomplete:
            warnings.append(f"{label}: output incomplete, a process kept the pipes open")
        if found:
            ids, merge_warns = merge_decision_ids(ids, found)
            warnings += merge_warns
        if res.returncode != 0 or res.timed_out:
            detail = "" if res.start_error else run.stderr.strip()
            tail = f": {detail}" if detail else ""
            warnings.append(f"{_describe(name, spec, res)}{tail}")
    return PostHookOutcome(tuple(runs), ids, tuple(warnings))
