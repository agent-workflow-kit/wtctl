"""Configuration: YAML files, drop-ins, ``WTCTL_<KEY>`` overrides, validation, per-repo resolution.

Rules are in docs/interface.md (Config, Locations, Environment); ``schemas/config.json`` is the
shape and ``config.example.yaml`` the defaults. The loader is deliberately paranoid about its
input: files are size-capped, YAML anchors, aliases, duplicate keys, non-string keys and deep
nesting are refused, drop-in directories and files must not be symlinks, and every failure is a
``config_invalid`` error (exit 2) that names the file and the key.

Seams used by ``context.py`` (exact names): ``load_config(config_path)`` and
``me_name(config_path)``.
"""

from __future__ import annotations

import errno
import getpass
import os
import re
import stat
from dataclasses import dataclass
from typing import Annotated, Any

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    model_validator,
)

from .actor import sanitize_user
from .errors import WtctlError

MAX_FILE_BYTES = 1024 * 1024
MAX_YAML_DEPTH = 64
MAX_YAML_EVENTS = 10_000
MAX_DROPINS = 100
MAX_ERRORS_SHOWN = 5

# ---------------------------------------------------------------- types


def _expand_dir(value: str) -> str:
    """An absolute path or ``~/...`` (expanded once, normalized); anything else is refused."""
    if "\n" in value or "\x00" in value or not (value.startswith("/") or value.startswith("~/")):
        raise ValueError("must be an absolute path or start with ~/ (no newline)")
    if value.startswith("~/"):
        value = os.path.join(_home(), value[2:])
    return os.path.normpath(value)


Dir = Annotated[StrictStr, StringConstraints(min_length=1), AfterValidator(_expand_dir)]
Duration = Annotated[StrictStr, StringConstraints(pattern=r"^[1-9][0-9]*[smhdw]\Z")]
EnvName = Annotated[StrictStr, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*\Z")]
FilterName = Annotated[StrictStr, StringConstraints(pattern=r"^[A-Za-z0-9._-]+\Z")]
NonEmpty = Annotated[StrictStr, StringConstraints(min_length=1)]
Host = Annotated[
    StrictStr,
    StringConstraints(
        pattern=(
            r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
            r"(?::[0-9]{1,5})?\Z"
        )
    ),
]
RepoId = Annotated[
    StrictStr,
    StringConstraints(
        pattern=(
            r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
            r"(?::[0-9]{1,5})?(?:/(?!\.\.?(?:/|\Z))[A-Za-z0-9._-]+)+\Z"
        )
    ),
]
AbsPath = Annotated[StrictStr, StringConstraints(pattern=r"^/")]

DEFAULT_DISPOSABLE = [
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "node_modules/",
    ".venv/",
    "dist/",
    "build/",
    "target/",
    "coverage/",
    ".coverage",
    "*.pyc",
    ".DS_Store",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", regex_engine="python-re", validate_default=True)


class Me(_Model):
    name: Annotated[StrictStr, StringConstraints(pattern=r"^[A-Za-z0-9._@-]{1,64}\Z")] | None = None


class PostHook(_Model):
    model_config = ConfigDict(extra="forbid", regex_engine="python-re", populate_by_name=False)
    cmd: NonEmpty
    timeout: Annotated[StrictInt, Field(ge=1, le=86400)] = 30
    async_: StrictBool = Field(default=False, alias="async")


class PreHook(_Model):
    cmd: NonEmpty
    timeout: Annotated[StrictInt, Field(ge=1, le=86400)] = 30


class Hooks(_Model):
    model_config = ConfigDict(extra="forbid", regex_engine="python-re", populate_by_name=False)
    # Consumed by ``wtctl.hooks.configured_hooks`` (order, veto, timeouts).
    pre_create: list[PreHook] = Field(default_factory=list, alias="pre-create")
    post_create: list[PostHook] = Field(default_factory=list, alias="post-create")
    pre_remove: list[PreHook] = Field(default_factory=list, alias="pre-remove")
    post_remove: list[PostHook] = Field(default_factory=list, alias="post-remove")
    post_reconcile: list[PostHook] = Field(default_factory=list, alias="post-reconcile")
    post_baseline_accept: list[PostHook] = Field(default_factory=list, alias="post-baseline-accept")


class HostConfig(_Model):
    clone_url: NonEmpty = "https://{host}/{path}.git"
    token_env: EnvName | None = None
    token_command: Annotated[list[NonEmpty], Field(min_length=1)] | None = None
    token_user: NonEmpty = "x-access-token"


class CreateGlobal(_Model):
    allow_filters: list[FilterName] = Field(default_factory=list)


class BaselineCfg(_Model):
    ignore_config_keys: list[
        Annotated[
            StrictStr,
            StringConstraints(
                pattern=r"^[A-Za-z][A-Za-z0-9-]*(\.[^\n]+)?\.[A-Za-z][A-Za-z0-9-]*\Z"
            ),
        ]
    ] = Field(default_factory=list)


class ThrowawayCfg(_Model):
    depth: Annotated[StrictInt, Field(ge=0)] = 50
    max_age: Duration | None = None


class RemoveGlobal(_Model):
    delete_branch: StrictBool = True
    disposable: list[NonEmpty] = Field(default_factory=lambda: list(DEFAULT_DISPOSABLE))
    fetch_retries: Annotated[StrictInt, Field(ge=0)] = 3
    nested_scan_limit: Annotated[StrictInt, Field(ge=1)] = 2_000_000


class ExtractCfg(_Model):
    max_file_bytes: Annotated[StrictInt, Field(ge=1)] = 1_048_576
    max_total_bytes: Annotated[StrictInt, Field(ge=1)] = 52_428_800
    max_files: Annotated[StrictInt, Field(ge=1)] = 1000


class PruneCfg(_Model):
    protect_labels: list[
        Annotated[
            StrictStr,
            StringConstraints(
                pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}(=[A-Za-z0-9._:/#@+=,-]{0,256})?\Z"
            ),
        ]
    ] = Field(default_factory=list)


class ReconcileCfg(_Model):
    missing_grace: Duration = "24h"


class BootstrapStep(_Model):
    argv: Annotated[list[StrictStr], Field(min_length=1)]
    sh: StrictBool = False
    timeout: Annotated[StrictInt, Field(ge=1)] = 600

    @model_validator(mode="after")
    def _sh_has_one_argument(self) -> BootstrapStep:
        if self.sh and len(self.argv) != 1:
            raise ValueError("with sh: true, argv must have exactly one element")
        return self


class RepoBootstrap(_Model):
    auto: StrictBool = True
    env: dict[EnvName, StrictStr] = Field(default_factory=dict)
    pass_env: list[EnvName] = Field(default_factory=list)
    steps: list[BootstrapStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _pass_env_unique(self) -> RepoBootstrap:
        if len(set(self.pass_env)) != len(self.pass_env):
            raise ValueError("pass_env entries must be unique")
        return self


class RepoRemove(_Model):
    disposable: list[NonEmpty] | None = None


class RepoCreate(_Model):
    allow_filters: list[FilterName] | None = None


class RepoConfig(_Model):
    clone: Dir | None = None
    clone_url: NonEmpty | None = None
    remote: Annotated[StrictStr, StringConstraints(pattern=r"^[A-Za-z0-9._-]+\Z")] = "origin"
    worktree_root: Dir | None = None
    auto_clone: StrictBool | None = None
    base: NonEmpty | None = None
    branch_template: NonEmpty | None = None
    copy_env: list[NonEmpty] = Field(default_factory=list)
    bootstrap: RepoBootstrap = Field(default_factory=RepoBootstrap)
    remove: RepoRemove = Field(default_factory=RepoRemove)
    create: RepoCreate = Field(default_factory=RepoCreate)


@dataclass(frozen=True)
class RepoSettings:
    """A repo's effective settings: ``repos.<id>`` over the (environment-merged) global config."""

    repo_id: str
    clone: str | None
    clone_url: str | None
    remote: str
    worktree_root: str
    auto_clone: bool
    base: str | None
    branch_template: str
    copy_env: list[str]
    bootstrap: RepoBootstrap
    disposable: list[str]
    allow_filters: list[str]


class Config(_Model):
    me: Me = Field(default_factory=Me)
    root: Dir = "~/wtctl"
    worktree_root: Dir | None = None
    throwaway_root: Dir | None = None
    worktree_path: Annotated[
        StrictStr, StringConstraints(pattern=r"^\{root\}/[^\n]*\{(name|id)\}[^\n]*\Z")
    ] = "{root}/{host}/{path}/{name}"
    branch_template: NonEmpty = "wt/{name}"
    branch_prefix: StrictStr = ""
    search_roots: list[Dir] = Field(default_factory=list)
    search_depth: Annotated[StrictInt, Field(ge=1)] = 3
    auto_clone: StrictBool = False
    clone_root: Dir | None = None
    network_timeout: Duration = "120s"
    clone_timeout: Duration = "30m"
    lease_wait: Duration = "10m"
    hosts: dict[Host, HostConfig] = Field(default_factory=dict)
    create: CreateGlobal = Field(default_factory=CreateGlobal)
    baseline: BaselineCfg = Field(default_factory=BaselineCfg)
    throwaway: ThrowawayCfg = Field(default_factory=ThrowawayCfg)
    remove: RemoveGlobal = Field(default_factory=RemoveGlobal)
    extract: ExtractCfg = Field(default_factory=ExtractCfg)
    prune: PruneCfg = Field(default_factory=PruneCfg)
    reconcile: ReconcileCfg = Field(default_factory=ReconcileCfg)
    tombstone_days: Annotated[StrictInt, Field(ge=0)] = 30
    git_bin: AbsPath | None = None
    mask_env: list[EnvName] = Field(default_factory=list)
    repos: dict[RepoId, RepoConfig] = Field(default_factory=dict)
    hooks: Hooks = Field(default_factory=Hooks)

    @property
    def effective_worktree_root(self) -> str:
        return self.worktree_root or os.path.join(self.root, "worktrees")

    @property
    def effective_throwaway_root(self) -> str:
        return self.throwaway_root or os.path.join(self.root, "throwaway")

    @property
    def effective_clone_root(self) -> str:
        return self.clone_root or os.path.join(self.root, "clones")

    @property
    def network_timeout_s(self) -> int:
        return parse_duration(self.network_timeout)

    @property
    def clone_timeout_s(self) -> int:
        return parse_duration(self.clone_timeout)

    @property
    def lease_wait_s(self) -> int:
        return parse_duration(self.lease_wait)

    def host(self, host: str) -> HostConfig:
        """The host's settings; hosts not listed use the built-in defaults."""
        return self.hosts.get(host) or HostConfig()

    def repo(self, repo_id: str) -> RepoSettings:
        """Per-repo resolution: ``repos.<id>`` > global (which already includes ``WTCTL_*``)."""
        rc = self.repos.get(repo_id) or RepoConfig()
        return RepoSettings(
            repo_id=repo_id,
            clone=rc.clone,
            clone_url=rc.clone_url,
            remote=rc.remote,
            worktree_root=rc.worktree_root or self.effective_worktree_root,
            auto_clone=self.auto_clone if rc.auto_clone is None else rc.auto_clone,
            base=rc.base,
            branch_template=rc.branch_template or self.branch_template,
            copy_env=list(rc.copy_env),
            bootstrap=rc.bootstrap.model_copy(deep=True),
            disposable=list(
                self.remove.disposable if rc.remove.disposable is None else rc.remove.disposable
            ),
            allow_filters=list(
                self.create.allow_filters
                if rc.create.allow_filters is None
                else rc.create.allow_filters
            ),
        )


# ---------------------------------------------------------------- durations


_DURATION_RE = re.compile(r"[1-9][0-9]*[smhdw]")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    """DURATION (a positive integer and one of s, m, h, d, w) in seconds."""
    if not _DURATION_RE.fullmatch(text):
        raise ValueError(f"invalid duration {text!r}: a positive integer and s, m, h, d or w")
    return int(text[:-1]) * _UNITS[text[-1]]


# ---------------------------------------------------------------- environment

_ENV_STR = {
    "ROOT": "root",
    "WORKTREE_ROOT": "worktree_root",
    "THROWAWAY_ROOT": "throwaway_root",
    "CLONE_ROOT": "clone_root",
    "WORKTREE_PATH": "worktree_path",
    "NETWORK_TIMEOUT": "network_timeout",
    "CLONE_TIMEOUT": "clone_timeout",
    "LEASE_WAIT": "lease_wait",
    "GIT_BIN": "git_bin",
    "BRANCH_TEMPLATE": "branch_template",
    "BRANCH_PREFIX": "branch_prefix",
}
_ENV_BOOL = {"AUTO_CLONE": "auto_clone"}
_ENV_INT = {"SEARCH_DEPTH": "search_depth", "TOMBSTONE_DAYS": "tombstone_days"}
_INT_RE = re.compile(r"-?[0-9]{1,18}")


def _env_overrides() -> list[tuple[str, str, Any]]:
    """``(variable, key, value)`` for each set ``WTCTL_<KEY>`` of a top-level scalar key.

    Anything else in the environment (structured keys, reserved hook variables, unknown names)
    is not config and is never read.
    """
    out: list[tuple[str, str, Any]] = []
    for suffix, key in _ENV_STR.items():
        var = f"WTCTL_{suffix}"
        if var in os.environ:
            out.append((var, key, os.environ[var]))
    for suffix, key in _ENV_BOOL.items():
        var = f"WTCTL_{suffix}"
        if var in os.environ:
            raw = os.environ[var]
            if raw not in ("true", "false", "1", "0"):
                raise _invalid(f"{var}: must be true, false, 1 or 0")
            out.append((var, key, raw in ("true", "1")))
    for suffix, key in _ENV_INT.items():
        var = f"WTCTL_{suffix}"
        if var in os.environ:
            raw = os.environ[var]
            if not _INT_RE.fullmatch(raw):
                raise _invalid(f"{var}: must be a decimal integer")
            out.append((var, key, int(raw)))
    return out


# ---------------------------------------------------------------- files


def _printable(text: str) -> str:
    """Escape control characters and undecodable path bytes (lone surrogates) for display."""
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in text)


def _invalid(message: str) -> WtctlError:
    return WtctlError("config_invalid", _printable(message))


def _home() -> str:
    home = os.environ.get("HOME") or os.path.expanduser("~")
    if not home.startswith("/"):
        raise _invalid("HOME is not an absolute path")
    return os.path.normpath(home)


def default_config_path() -> str:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = xdg if xdg.startswith("/") else os.path.join(_home(), ".config")
    return os.path.join(base, "wtctl", "config.yaml")


def _read_file(path: str, *, follow: bool) -> str:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (0 if follow else os.O_NOFOLLOW)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        symlink = exc.errno == errno.ELOOP and not follow
        what = "is a symlink (not followed)" if symlink else exc.strerror
        raise _invalid(f"{path}: cannot read: {what}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _invalid(f"{path}: not a regular file")
        if st.st_size > MAX_FILE_BYTES:
            raise _invalid(f"{path}: file too large (limit {MAX_FILE_BYTES} bytes)")
        data = b""
        while len(data) <= MAX_FILE_BYTES:
            chunk = os.read(fd, MAX_FILE_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise _invalid(f"{path}: file too large (limit {MAX_FILE_BYTES} bytes)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid(f"{path}: not valid UTF-8") from exc


_PLAIN_INT = re.compile(r"^-?(?:0|[1-9][0-9]*)$")


class _KeyProblem(yaml.constructor.ConstructorError):
    """A mapping-key problem whose message is safe to show (it never quotes a value)."""


class _Loader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate and non-string mapping keys and reads only plain
    decimal integers as ints (YAML 1.1's ``1:30``, ``010`` and ``1_0`` stay strings)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            line = key_node.start_mark.line + 1
            if not isinstance(key, str):
                raw = key_node.value if isinstance(key_node, yaml.ScalarNode) else "(not a scalar)"
                raise _KeyProblem(
                    None,
                    None,
                    f"mapping key {_printable(str(raw)[:50])} at line {line} is not a string; "
                    "quote it",
                    key_node.start_mark,
                )
            if key in seen:
                raise _KeyProblem(
                    None, None, f"duplicate key {key[:50]!r} at line {line}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:int"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _ch in "-0123456789":
    _Loader.yaml_implicit_resolvers.setdefault(_ch, []).append(
        ("tag:yaml.org,2002:int", _PLAIN_INT)
    )


def _yaml_message(path: str, exc: yaml.YAMLError) -> str:
    """A YAML error without any of the source text (a value on the line may be a secret)."""
    if isinstance(exc, _KeyProblem):
        return f"{path}: invalid YAML: {exc.problem}"
    if isinstance(exc, yaml.MarkedYAMLError):
        if isinstance(exc, yaml.constructor.ConstructorError):
            what = "an unsupported or invalid YAML construct"
        else:
            what = " ".join(str(exc.problem or "invalid YAML").split())
        mark = exc.problem_mark or exc.context_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        return f"{path}: invalid YAML: {what}{where}"
    if isinstance(exc, yaml.reader.ReaderError):
        return f"{path}: invalid YAML: unacceptable character at position {exc.position}"
    return f"{path}: invalid YAML"


def _parse_yaml(path: str, text: str) -> dict[str, Any]:
    try:
        depth = 0
        documents = 0
        events = 0
        for ev in yaml.parse(text, Loader=yaml.SafeLoader):
            events += 1
            if events > MAX_YAML_EVENTS:
                raise _invalid(f"{path}: YAML too complex (more than {MAX_YAML_EVENTS} events)")
            if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None):
                raise _invalid(f"{path}: YAML anchors and aliases are not allowed")
            if isinstance(ev, yaml.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise _invalid(f"{path}: more than one YAML document")
            elif isinstance(ev, yaml.CollectionStartEvent):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    raise _invalid(f"{path}: YAML nested deeper than {MAX_YAML_DEPTH} levels")
            elif isinstance(ev, yaml.CollectionEndEvent):
                depth -= 1
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 (SafeLoader subclass)
    except yaml.YAMLError as exc:
        raise _invalid(_yaml_message(path, exc)) from exc
    except (ValueError, OverflowError) as exc:  # bad timestamp, `!!int abc`, huge integer
        raise _invalid(f"{path}: invalid YAML: a value could not be constructed") from exc
    except RecursionError as exc:
        raise _invalid(f"{path}: YAML nested too deeply") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _invalid(f"{path}: the top level must be a mapping")
    return data


def _dropin_paths(config_path: str) -> list[str]:
    d = os.path.join(os.path.dirname(config_path), "config.d")
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise _invalid(f"{d}: cannot read: {exc.strerror}") from exc
    if not stat.S_ISDIR(st.st_mode):
        why = "is a symlink (not followed)" if stat.S_ISLNK(st.st_mode) else "is not a directory"
        raise _invalid(f"{d}: {why}")
    try:
        names = sorted(os.listdir(d))
    except OSError as exc:
        raise _invalid(f"{d}: cannot read: {exc.strerror}") from exc
    found = [os.path.join(d, n) for n in names if n.endswith(".yaml") and not n.startswith(".")]
    if len(found) > MAX_DROPINS:
        raise _invalid(f"{d}: too many drop-in files (limit {MAX_DROPINS})")
    return found


# ---------------------------------------------------------------- merge and validate


def _merge(
    base: dict[str, Any], over: dict[str, Any], path: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Maps merge, lists replace, except ``hooks.<name>`` lists, which append."""
    out = dict(base)
    for key, value in over.items():
        cur = out.get(key)
        if path == ("hooks",) and isinstance(value, list) and isinstance(cur, list):
            out[key] = cur + value
        elif isinstance(value, dict) and isinstance(cur, dict):
            out[key] = _merge(cur, value, (*path, key))
        else:
            out[key] = value
    return out


def _describe(exc: ValidationError, source: str) -> WtctlError:
    parts: list[str] = []
    for err in exc.errors()[:MAX_ERRORS_SHOWN]:
        key = ".".join(str(p) for p in err["loc"])[:200]
        if err["type"] == "extra_forbidden":
            parts.append(f"unknown key {key!r}")
        else:
            parts.append(f"{key or '(top level)'}: {err['msg']}")
    more = len(exc.errors()) - MAX_ERRORS_SHOWN
    if more > 0:
        parts.append(f"... and {more} more")
    return _invalid(f"{source}: " + "; ".join(parts))


def _validate(raw: dict[str, Any], source: str) -> Config:
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise _describe(exc, source) from exc


# ---------------------------------------------------------------- root path rules


def _within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip("/") + "/")


def _unsafe_reason(path: str) -> str | None:
    """Why ``path`` may not be a root, or None (the subset of claude-session's rules that
    applies to directories wtctl creates; docs/interface.md, Locations, Path rules)."""
    home = _home()
    homes = {home, os.path.realpath(home)}
    xdg_defaults = {
        "XDG_CONFIG_HOME": ".config",
        "XDG_DATA_HOME": os.path.join(".local", "share"),
        "XDG_STATE_HOME": os.path.join(".local", "state"),
    }
    guarded: list[tuple[str, str]] = []
    for var, default in xdg_defaults.items():
        value = os.environ.get(var, "")
        guarded.append(
            (var, os.path.normpath(value) if value.startswith("/") else os.path.join(home, default))
        )
    for entry in os.environ.get("PATH", "").split(":"):
        if entry.startswith("/") and os.path.normpath(entry) != "/":
            guarded.append(("a PATH directory", os.path.normpath(entry)))

    for candidate in {path, os.path.realpath(path)}:
        for h in homes:
            if _within(h, candidate):
                return "is $HOME or an ancestor of it"
            if _within(candidate, h):
                first = os.path.relpath(candidate, h).split(os.sep)[0]
                if first.startswith("."):
                    return "is inside a hidden directory of $HOME"
        for name, directory in guarded:
            for d in {directory, os.path.realpath(directory)}:
                if _within(candidate, d):
                    return f"is, or is inside, {name} ({d})"
    return None


def _source_of(layers: list[tuple[str, dict[str, Any]]], keys: tuple[str, ...]) -> str:
    for label, data in reversed(layers):
        cur: Any = data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                break
            cur = cur[k]
        else:
            return label
    return "built-in default"


def _check_roots(cfg: Config, layers: list[tuple[str, dict[str, Any]]]) -> None:
    targets: list[tuple[tuple[str, ...], str | None]] = [
        (("root",), cfg.root),
        (("worktree_root",), cfg.worktree_root),
        (("throwaway_root",), cfg.throwaway_root),
    ]
    for repo_id, rc in cfg.repos.items():
        targets.append((("repos", repo_id, "worktree_root"), rc.worktree_root))
    for keys, value in targets:
        if value is None:
            continue
        reason = _unsafe_reason(value)
        if reason:
            name = ".".join(keys)
            raise _invalid(
                f"{_source_of(layers, keys)}: {name}: {value!r} {reason}; "
                "choose a visible directory outside $HOME's hidden directories, the XDG homes "
                "and PATH"
            )


# ---------------------------------------------------------------- public API


def load_config(config_path: str | None) -> Config:
    """Load, merge and validate the configuration (raises ``config_invalid``)."""
    explicit = config_path is not None
    path = config_path if config_path is not None else default_config_path()

    layers: list[tuple[str, dict[str, Any]]] = []
    try:
        os.lstat(path)
        present = True
    except (FileNotFoundError, NotADirectoryError):
        present = explicit  # an explicit path that is missing fails when it is read
    except ValueError as exc:  # embedded NUL
        raise _invalid(f"{path!r}: invalid path") from exc
    except OSError as exc:
        raise _invalid(f"{path}: cannot read: {exc.strerror}") from exc
    if present:
        layers.append((path, _parse_yaml(path, _read_file(path, follow=True))))
    for dropin in _dropin_paths(path):
        layers.append((dropin, _parse_yaml(dropin, _read_file(dropin, follow=False))))

    merged: dict[str, Any] = {}
    for label, data in layers:
        _validate(data, label)  # attribute errors to the file that holds them
        merged = _merge(merged, data)

    for var, key, value in _env_overrides():
        _validate({key: value}, var)
        merged[key] = value
        layers.append((var, {key: value}))

    cfg = _validate(merged, "configuration")
    _check_roots(cfg, layers)
    return cfg


def me_name(config_path: str | None) -> str:
    """``me.name``, else the sanitized OS user name (the default actor is ``human:<this>``)."""
    name = load_config(config_path).me.name
    if name:
        return name
    try:
        return sanitize_user(getpass.getuser())
    except (KeyError, OSError):
        return "unknown"
