"""The hardened git invocation (architecture section 13.2 rule 10; docs/interface.md, Hardened git).

Every git command wtctl runs goes through :meth:`Git.run` (and so through ``gateway``). The
environment is built from scratch, never inherited; the override set rides on every command line;
filter drivers and repository extra headers are neutralized by name; the protocol allowlist is
``none`` unless the command is meant to contact the network, and lazy fetching is switched off.

This module also holds the pure helpers around those calls: porcelain parsing and the
classification of a fetch outcome.
"""

from __future__ import annotations

import enum
import os
import re
import shlex
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .errors import WtctlError
from .gateway import Gateway, GitResult

MIN_GIT_VERSION = (2, 34)

# The only schemes a command meant to contact a remote may allow (local paths are addressed by
# plain path and reach git as "file"; see interface.md, Hardened git).
NETWORK_SCHEMES = frozenset({"https", "ssh", "file"})

# The subcommands wtctl runs. A name outside this set is a programming error, not a request:
# adding a command here is a conscious, reviewed step.
SUBCOMMANDS = frozenset(
    {
        "add",
        "branch",
        "cat-file",
        "check-ref-format",
        "clone",
        "commit",
        "commit-tree",
        "config",
        "count-objects",
        "diff",
        "diff-files",
        "diff-index",
        "diff-tree",
        "fetch",
        "for-each-ref",
        "hash-object",
        "index-pack",
        "init",
        "log",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "merge-base",
        "pack-objects",
        "push",
        "read-tree",
        "remote",
        "rev-list",
        "rev-parse",
        "show",
        "show-ref",
        "status",
        "symbolic-ref",
        "update-index",
        "update-ref",
        "version",
        "worktree",
        "write-tree",
    }
)
DIFF_FAMILY = frozenset({"diff", "diff-files", "diff-index", "diff-tree", "log", "show"})
# Subcommands that print patches and so can run a textconv driver.
TEXTCONV_FAMILY = frozenset({"diff", "log", "show"})

# The forged-history vectors (replace refs, grafts, a forged commit-graph; spike E1a-f) are tested
# against real git in tests/test_mirror_pushed.py; the pushed check lives in mirror.py. Here the
# overrides below (core.useReplaceRefs, core.commitGraph, GIT_NO_REPLACE_OBJECTS, GIT_GRAFT_FILE)
# are set on every command.
#
# The override set of the recipe, on every call. ``core.alternateRefsCommand=true`` is the safety
# check's addition (SAFE-3): an alternate's ref listing runs it through a shell during fetch
# negotiation, and an empty value prints an error instead of disabling it.
OVERRIDES: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", ""),
    ("core.hooksPath", "/dev/null"),
    ("core.sshCommand", "ssh -o BatchMode=yes"),
    ("credential.helper", ""),
    ("core.askPass", ""),
    ("core.pager", "cat"),
    ("diff.external", ""),
    ("core.attributesFile", "/dev/null"),
    ("log.showSignature", "false"),
    ("fetch.recurseSubmodules", "false"),
    ("submodule.recurse", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("core.useReplaceRefs", "false"),
    ("core.commitGraph", "false"),
    ("http.followRedirects", "false"),
    ("core.alternateRefsCommand", "true"),
    # An explicit --show-signature (or a caller's verify) would run the repository's gpg program;
    # wtctl never verifies signatures, so every program setting points at a command that fails.
    ("gpg.program", "/bin/false"),
    ("gpg.openpgp.program", "/bin/false"),
    ("gpg.x509.program", "/bin/false"),
    ("gpg.ssh.program", "/bin/false"),
    # `commit` is the one subcommand that can start an editor or sign: neutralize every program a
    # repository could name for either (the editor, ssh's default-key command, signing itself).
    ("core.editor", "true"),
    ("sequence.editor", "true"),
    ("gpg.ssh.defaultKeyCommand", "true"),
    ("commit.gpgsign", "false"),
    ("tag.gpgsign", "false"),
    ("push.gpgsign", "false"),
    ("interactive.diffFilter", ""),
    # A nested repository (submodule) is diffed by a child git that reads its own, unblanked
    # config: only the short summary is safe (`diff` and `log` formats run that child).
    ("diff.submodule", "short"),  # belt and braces: nothing wtctl runs today diffs a submodule
)
# The only keys a caller may add to the override set (`Git.build(overrides=...)`), lowercase. The
# tree fetch into the mirror needs `fetch.negotiationAlgorithm=noop` (interface.md, Mirror).
CALLER_OVERRIDE_KEYS = frozenset(
    {
        "fetch.negotiationalgorithm",
        # the removal content checks (interface.md, Content checks): a private index compared by
        # content, never by the stat data or untracked cache an agent could forge
        "core.untrackedcache",
        "core.checkstat",
        "core.trustctime",
        "core.ignorestat",
        "core.splitindex",
    }
)
NETWORK_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("http.lowSpeedLimit", "1000"),
    ("http.lowSpeedTime", "60"),
)

# Wall-clock limit of a git command that passes no timeout of its own (seconds).
DEFAULT_TIMEOUT = 300.0

_FILTER_KEYS = ("clean", "smudge", "process")


class UnsafePathError(ValueError):
    """A path wtctl would hand to git contains a newline (or is not absolute)."""


class PorcelainError(ValueError):
    """Git output did not have the documented shape; callers treat the check as failed."""


# --------------------------------------------------------------------------------------------
# Binaries and version
# --------------------------------------------------------------------------------------------


def resolve_binary(name: str, path_env: str, explicit: str | None = None) -> str | None:
    """``explicit`` (must be absolute) else the first absolute ``PATH`` entry with an executable.

    Relative and empty ``PATH`` entries are ignored (they would depend on the cwd; D49).
    """
    if explicit is not None:
        if not os.path.isabs(explicit) or "\n" in explicit:
            return None
        return explicit if _is_executable(explicit) else None
    for entry in path_env.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        candidate = os.path.join(entry, name)
        if _is_executable(candidate):
            return candidate
    return None


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def parse_git_version(text: str) -> tuple[int, int, int]:
    m = re.match(r"git version (\d+)\.(\d+)(?:\.(\d+))?", text.strip())
    if not m:
        raise PorcelainError(f"unrecognized git version output: {text.strip()[:80]!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


# --------------------------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------------------------


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


def _check_git_bin(git_bin: str) -> None:
    """An empty or relative entry in the child's ``PATH`` would depend on the cwd (D49)."""
    if not os.path.isabs(git_bin) or "\n" in git_bin or "\0" in git_bin:
        raise ValueError(f"git_bin must be an absolute path: {git_bin!r}")


def build_env(
    *,
    git_bin: str,
    home: str,
    scheme: str | None = None,
    extra_path: Sequence[str] = (),
    config: Sequence[tuple[str, str]] = (),
    ssh_auth_sock: str | None = None,
    ceiling: str | None = None,
    index_file: str | None = None,
    allow_shallow: bool = False,
) -> dict[str, str]:
    """The complete environment of one git process. Nothing of the caller's reaches git."""
    _check_git_bin(git_bin)
    if scheme is not None and scheme not in NETWORK_SCHEMES:
        raise ValueError(f"scheme {scheme!r} is not one of {sorted(NETWORK_SCHEMES)}")
    path = _dedupe([os.path.dirname(git_bin), "/usr/bin", "/bin", *extra_path])
    env = {
        "PATH": os.pathsep.join(path),
        "HOME": home,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_EDITOR": ":",
        "GIT_ALLOW_PROTOCOL": scheme if scheme is not None else "none",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": "/dev/null/none",
        "GIT_SSH_VARIANT": "ssh",
    }
    if not allow_shallow:
        # a `shallow` file in the (agent-writable) clone cuts history short and so changes every
        # `merge-base --is-ancestor`; only the local-throwaway fill, which copies from a possibly
        # shallow source into a shallow throwaway on purpose, reads one
        env["GIT_SHALLOW_FILE"] = "/dev/null"
    if ssh_auth_sock is not None and scheme == "ssh":
        env["SSH_AUTH_SOCK"] = ssh_auth_sock
    if ceiling is not None:
        env["GIT_CEILING_DIRECTORIES"] = ceiling
    if index_file is not None:
        env["GIT_INDEX_FILE"] = index_file
    if config:
        env["GIT_CONFIG_COUNT"] = str(len(config))
        for i, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{i}"] = key
            env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


# --------------------------------------------------------------------------------------------
# Config listing, filters and extra headers
# --------------------------------------------------------------------------------------------


def parse_config_list(data: bytes) -> list[tuple[str, str | None]]:
    """``git config --list -z`` output: ``key\\nvalue\\0`` (a key without ``\\n`` has no value)."""
    entries: list[tuple[str, str | None]] = []
    for record in data.split(b"\0"):
        if not record:
            continue
        # fsdecode keeps every byte (surrogateescape): a name that is not UTF-8 must come back
        # byte for byte in the blanking entries, or the blank would name a different key.
        text = os.fsdecode(record)
        key, sep, value = text.partition("\n")
        entries.append((key, value if sep else None))
    return entries


def filter_names(entries: Iterable[tuple[str, str | None]]) -> list[str]:
    """Every driver name ``filter.<name>.<var>`` defined (name case-preserved, may hold dots)."""
    names: dict[str, None] = {}
    for key, _ in entries:
        if key.startswith("filter.") and key.count(".") >= 2:
            names.setdefault(key[len("filter.") : key.rindex(".")], None)
    return list(names)


def extra_header_keys(entries: Iterable[tuple[str, str | None]]) -> list[str]:
    """Exact names of every ``http.*extraheader`` key (URL-scoped and wildcard ones included)."""
    keys: dict[str, None] = {}
    for key, _ in entries:
        if key.lower().startswith("http.") and key.rsplit(".", 1)[-1].lower() == "extraheader":
            keys.setdefault(key, None)
    return list(keys)


@dataclass(frozen=True)
class FilterPolicy:
    """Honored filter drivers (``create.allow_filters``) with their trusted definitions."""

    drivers: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    path_dirs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def filter_config(
    names: Iterable[str], honored: Mapping[str, Mapping[str, str]]
) -> list[tuple[str, str]]:
    """``GIT_CONFIG_COUNT`` entries blanking every driver, then restoring the honored ones.

    A later entry for the same key wins, so a honored driver is blanked first (a repository
    ``smudge`` must not survive next to the trusted ``process``) and then given its definition.
    """
    entries: list[tuple[str, str]] = []
    for name in _dedupe([*names, *honored]):
        for var in _FILTER_KEYS:
            entries.append((f"filter.{name}.{var}", ""))
        entries.append((f"filter.{name}.required", "false"))
    for name, definition in honored.items():
        for var, value in definition.items():
            entries.append((f"filter.{name}.{var}", value))
    return entries


# --------------------------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------------------------


def _option_name(arg: str) -> str | None:
    """``name`` of a long option ``--name[=value]``; ``None`` for anything else."""
    if arg == "--" or not arg.startswith("--"):
        return None
    return arg[2:].split("=", 1)[0]


def _abbreviates(name: str, full: str) -> bool:
    """Whether ``--name`` could select ``--full`` (git accepts unambiguous abbreviations)."""
    return len(name) >= 2 and full.startswith(name)


def _guard_options(sub: str, args: Sequence[str]) -> None:
    """Refuse caller options that undo the injected diff and textconv guards.

    Only arguments before a ``--`` separator are options; everything after it is a path and is
    not inspected. A later option beats an earlier one, so the guards cannot just be skipped when
    the caller repeats them: the options that turn them off are rejected instead. Abbreviations
    are rejected too (newer git accepts them).
    """
    for arg in args:
        if arg == "--":
            return
        name = _option_name(arg)
        if name is None:
            continue
        if _abbreviates(name, "textconv") and name != "text":
            raise ValueError(f"{arg!r} would run a textconv driver; wtctl never allows it")
        if _abbreviates(name, "recurse-submodules"):
            raise ValueError(f"{arg!r} would recurse into nested repositories")
        if _abbreviates(name, "ignore-submodules") and arg != "--ignore-submodules=all":
            if sub in DIFF_FAMILY or sub == "status":
                raise ValueError(f"{arg!r}: only --ignore-submodules=all is allowed")
        if sub not in DIFF_FAMILY:
            continue
        if name.startswith("no-no-") or _abbreviates(name, "ext-diff"):
            raise ValueError(f"{arg!r} would undo --no-ext-diff or --no-textconv")
        if _abbreviates(name, "submodule") and arg != "--submodule=short":
            raise ValueError(f"{arg!r}: only --submodule=short is allowed (nested repository)")


def _passes_untracked_files(args: Sequence[str]) -> bool:
    for arg in args:
        if arg == "--":
            return False
        name = _option_name(arg)
        if name is not None:
            if _abbreviates(name, "untracked-files"):
                return True
        elif arg.startswith("-") and "u" in arg[1:]:
            return True
    return False


def _is_extraheader_key(key: str) -> bool:
    return key.lower().startswith("http.") and key.rsplit(".", 1)[-1].lower() == "extraheader"


def _check_path(path: str | os.PathLike[str], what: str) -> str:
    text = os.fspath(path)
    if "\n" in text or "\0" in text:
        raise UnsafePathError(f"{what} contains a newline")
    if not os.path.isabs(text):
        raise UnsafePathError(f"{what} must be absolute")
    return text


@dataclass(frozen=True)
class GitCommand:
    argv: list[str]
    # The environment can hold a token header: keep it out of repr and of assertion diffs.
    env: dict[str, str] = field(repr=False, compare=False)
    cwd: str


@dataclass(frozen=True)
class Git:
    """A hardened git: the binary, the home directory and the honored filter drivers."""

    gateway: Gateway
    git_bin: str
    home: str
    filters: FilterPolicy = field(default_factory=FilterPolicy)

    def __post_init__(self) -> None:
        _check_git_bin(self.git_bin)

    # -- building ------------------------------------------------------------------------------

    def _list_config(self, git_dir: str, work_tree: str | None, cwd: str) -> list:
        cmd = self._assemble(
            ["config", "--list", "-z"],
            git_dir=git_dir,
            work_tree=work_tree,
            cwd=cwd,
            scheme=None,
            config=(),
            ssh_auth_sock=None,
            ceiling=None,
            extra_path=(),
        )
        r = self.gateway.git(cmd.argv, env=cmd.env, cwd=cmd.cwd, timeout=30)
        if r.returncode != 0 or r.timed_out:
            # Failing closed: without the listing the drivers and headers cannot be blanked.
            raise WtctlError(
                "internal", f"could not read repository config of {git_dir}: {r.err.strip()[:200]}"
            )
        return parse_config_list(r.stdout)

    def _assemble(
        self,
        args: Sequence[str],
        *,
        git_dir: str | None,
        work_tree: str | None,
        cwd: str,
        scheme: str | None,
        config: Sequence[tuple[str, str]],
        ssh_auth_sock: str | None,
        ceiling: str | None,
        extra_path: Sequence[str],
        overrides: Sequence[tuple[str, str]] = (),
        index_file: str | None = None,
        allow_shallow: bool = False,
    ) -> GitCommand:
        if not args or not re.fullmatch(r"[a-z][a-z-]*", args[0]) or args[0] not in SUBCOMMANDS:
            raise ValueError(f"not a git subcommand wtctl runs: {args[:1]!r}")
        sub = args[0]
        rest = list(args[1:])
        _guard_options(sub, rest)
        # The guards are inserted unconditionally right after the subcommand (a pathspec that
        # happens to equal a guard must not suppress it); _guard_options rejected every caller
        # option that could turn them off again.
        guards: list[str] = []
        if sub in DIFF_FAMILY:
            guards += ["--no-ext-diff", "--ignore-submodules=all"]
        if sub in TEXTCONV_FAMILY:
            guards.append("--no-textconv")
        if sub == "status":
            guards.append("--ignore-submodules=all")
            # status.showUntrackedFiles=no in the repository would hide untracked files.
            if not _passes_untracked_files(rest):
                guards.append("--untracked-files=all")
        rest = guards + rest
        argv = [self.git_bin, "--no-pager"]
        for key, value in overrides:
            if key.lower() not in CALLER_OVERRIDE_KEYS or "\0" in value or "\n" in value:
                raise ValueError(f"not an override a caller may add: {key!r}")
        all_overrides = (
            OVERRIDES + (NETWORK_OVERRIDES if scheme is not None else ()) + tuple(overrides)
        )
        for key, value in all_overrides:
            argv += ["-c", f"{key}={value}"]
        if git_dir is not None:
            argv.append(f"--git-dir={git_dir}")
        if work_tree is not None:
            argv.append(f"--work-tree={work_tree}")
        argv += [sub, *rest]
        env = build_env(
            git_bin=self.git_bin,
            home=self.home,
            scheme=scheme,
            extra_path=extra_path,
            config=config,
            ssh_auth_sock=ssh_auth_sock,
            ceiling=ceiling,
            index_file=index_file,
            allow_shallow=allow_shallow,
        )
        return GitCommand(argv, env, cwd)

    def build(
        self,
        args: Sequence[str],
        *,
        git_dir: str | os.PathLike[str] | None = None,
        work_tree: str | os.PathLike[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        private_dir: str | os.PathLike[str] | None = None,
        scheme: str | None = None,
        extra_config: Sequence[tuple[str, str]] = (),
        ssh_auth_sock: str | None = None,
        honor_filters: bool = False,
        tool_owned: bool = False,
        overrides: Sequence[tuple[str, str]] = (),
        index_file: str | None = None,
        allow_shallow: bool = False,
    ) -> GitCommand:
        """Build the argv, environment and cwd of one command (``run`` runs the result).

        ``git_dir`` (and ``work_tree`` for commands in a worktree) are always explicit when the
        command runs in a repository. A command outside any repository passes ``private_dir``, a
        fresh wtctl-private directory; ``GIT_CEILING_DIRECTORIES`` is its parent so git never
        discovers an ancestor repository. ``extra_config`` (token headers, owned by credentials)
        comes after the blanking, so it wins and may only be an ``http.*extraheader`` key.

        ``scheme`` (a command that contacts a remote) needs ``tool_owned=True``: the repository is
        one wtctl itself owns (the mirror, a fresh throwaway, or a new clone directory), never a
        repository an agent may have written. Its config could name a proxy or other settings
        that would receive the token.
        """
        if scheme is not None and not tool_owned:
            raise ValueError("a network git command needs tool_owned=True (mirror or fresh dir)")
        gd = _check_path(git_dir, "git dir") if git_dir is not None else None
        wt = _check_path(work_tree, "work tree") if work_tree is not None else None
        pd = _check_path(private_dir, "private dir") if private_dir is not None else None
        if wt is not None and gd is None:
            raise ValueError("work_tree needs git_dir")
        ceiling = None
        if gd is not None:
            run_cwd = _check_path(cwd, "cwd") if cwd is not None else (wt or gd)
        else:
            if pd is None:
                raise ValueError("a command outside a repository needs a private_dir")
            run_cwd = pd
            ceiling = os.path.dirname(pd.rstrip("/"))
        config: list[tuple[str, str]] = []
        extra_path: tuple[str, ...] = ()
        if gd is not None:
            entries = self._list_config(gd, wt, run_cwd)
            honored = self.filters.drivers if honor_filters else {}
            config += filter_config(filter_names(entries), honored)
            config += [(k, "") for k in extra_header_keys(entries)]
            if honor_filters:
                extra_path = self.filters.path_dirs
        for key, value in extra_config:
            if "\0" in key or "\0" in value or not _is_extraheader_key(key):
                raise ValueError(f"extra_config takes only http.*extraheader keys, got {key!r}")
            config.append((key, value))
        return self._assemble(
            args,
            git_dir=gd,
            work_tree=wt,
            cwd=run_cwd,
            scheme=scheme,
            config=config,
            ssh_auth_sock=ssh_auth_sock,
            ceiling=ceiling,
            extra_path=extra_path,
            overrides=overrides,
            index_file=None if index_file is None else _check_path(index_file, "index file"),
            allow_shallow=allow_shallow,
        )

    def run(
        self,
        args: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout: float | None = None,
        scheme: str | None = None,
        stdin_path: str | None = None,
        stdout_path: str | None = None,
        max_stdout_bytes: int | None = None,
        **kwargs: object,
    ) -> GitResult:
        """Run one hardened git command through the gateway.

        A command meant to contact a remote passes ``scheme`` (with ``tool_owned=True``, see
        :meth:`build`) and must pass a ``timeout`` (network calls always run under a wall-clock
        limit that kills their process group). Any other command runs under
        ``DEFAULT_TIMEOUT`` (300 seconds) unless the caller passes its own, so a stuck git can
        never hang wtctl forever.

        ``stdin_path`` feeds a file to git's stdin and ``stdout_path`` sends its stdout to a new
        file (created exclusively, no-follow, mode 0600; the result's ``stdout`` is empty), so a
        pack or a long object list never sits in memory. ``max_stdout_bytes`` kills the command
        once the file is larger (``GitResult.output_limit``).
        """
        if scheme is not None and timeout is None:
            raise ValueError("a network git command needs a timeout")
        cmd = self.build(args, scheme=scheme, **kwargs)  # type: ignore[arg-type]
        limit = DEFAULT_TIMEOUT if timeout is None else timeout
        files: dict[str, object] = {}
        if stdin_path is not None:
            files["stdin_path"] = stdin_path
        if stdout_path is not None:
            files["stdout_path"] = stdout_path
        if max_stdout_bytes is not None:
            files["max_stdout_bytes"] = max_stdout_bytes
        return self.gateway.git(
            cmd.argv, env=cmd.env, cwd=cmd.cwd, stdin=stdin, timeout=limit, **files
        )


# --------------------------------------------------------------------------------------------
# Setup helpers: version check and allowed filters
# --------------------------------------------------------------------------------------------


def check_version(gateway: Gateway, git_bin: str, home: str, private_dir: str) -> tuple[int, ...]:
    """The binary's version; ``git_too_old`` (exit 12) below 2.34."""
    env = build_env(git_bin=git_bin, home=home)
    r = gateway.git([git_bin, "--version"], env=env, cwd=private_dir, timeout=15)
    if r.returncode != 0:
        raise WtctlError("internal", f"{git_bin} --version failed")
    version = parse_git_version(r.out)
    if version[:2] < MIN_GIT_VERSION:
        raise WtctlError(
            "git_too_old", f"git {'.'.join(map(str, version))} is too old; need 2.34 or newer"
        )
    return version


def load_filter_policy(
    gateway: Gateway,
    git_bin: str,
    *,
    home: str,
    xdg_config_home: str | None,
    allow: Sequence[str],
    path_env: str,
    private_dir: str,
) -> FilterPolicy:
    """Honored drivers: listed in ``allow``, and defined with a ``process`` in the global config.

    Definitions come from ``$XDG_CONFIG_HOME/git/config`` then ``~/.gitconfig`` (the latter
    wins), read with ``--file`` and without includes. A listed driver that the global config
    does not define with a ``process`` (a repository-planted ``process`` would beat a trusted
    ``smudge``), or whose command cannot be resolved on ``path_env``, stays blanked with a
    warning.
    """
    if not allow:
        return FilterPolicy()
    xdg = xdg_config_home or os.path.join(home, ".config")
    values: dict[str, str] = {}
    # `--file` does no repository discovery; the ceiling makes that explicit and keeps it so.
    ceiling = os.path.dirname(private_dir.rstrip("/"))
    for cfg in (os.path.join(xdg, "git", "config"), os.path.join(home, ".gitconfig")):
        if not os.path.isfile(cfg):
            continue
        env = build_env(git_bin=git_bin, home=home, ceiling=ceiling)
        r = gateway.git(
            [git_bin, "config", "--file", cfg, "--no-includes", "--list", "-z"],
            env=env,
            cwd=private_dir,
            timeout=15,
        )
        if r.returncode != 0:
            continue
        for key, value in parse_config_list(r.stdout):
            if not (key.startswith("filter.") and key.count(".") >= 2):
                continue
            if value is None:
                # A key without "=" is boolean true; only `required` is a boolean here.
                if key.rsplit(".", 1)[-1].lower() == "required":
                    values[key] = "true"
                continue
            values[key] = value
    drivers: dict[str, dict[str, str]] = {}
    dirs: list[str] = []
    warnings: list[str] = []
    for name in allow:
        process = values.get(f"filter.{name}.process", "")
        if not process.strip():
            warnings.append(f"filter driver {name!r} has no global 'process'; it stays blanked")
            continue
        try:
            first = shlex.split(process)[0]
        except (ValueError, IndexError):
            warnings.append(f"filter driver {name!r}: unparsable process command; stays blanked")
            continue
        if "/" in first:
            resolved = first if os.path.isabs(first) and _is_executable(first) else None
        else:
            resolved = resolve_binary(first, path_env)
        if resolved is None:
            warnings.append(f"filter driver {name!r}: command {first!r} not found; stays blanked")
            continue
        drivers[name] = {
            var: values[f"filter.{name}.{var}"]
            for var in (*_FILTER_KEYS, "required")
            if f"filter.{name}.{var}" in values
        }
        dirs.append(os.path.dirname(resolved))
    return FilterPolicy(drivers, tuple(_dedupe(dirs)), tuple(warnings))


# --------------------------------------------------------------------------------------------
# Fetch outcome classification (D15, interface.md Remove order step 5)
# --------------------------------------------------------------------------------------------

AUTH_MARKERS = (
    "could not read Username",
    "terminal prompts disabled",
    "Authentication failed",
    "Permission denied (publickey",
    "Host key verification failed",
    "The requested URL returned error: 401",
)
LOCK_MARKERS = ("cannot lock ref", "unable to update local ref")

# Transport causes of an exit 128 that was not an authentication failure (`details.cause`).
# Each pattern is anchored to the start of a line git or ssh printed (text captured from git
# 2.34.1 with curl and OpenSSH, LC_ALL=C); `remote:` and `fatal: remote error:` lines never count.
_CURL_LINE = r"fatal: unable to access '[^\n]*': "
_NETWORK_UNREACHABLE = re.compile(
    rf"(?:{_CURL_LINE}(?:Could not resolve host: |Failed to connect to )"
    r"|ssh: Could not resolve hostname |ssh: connect to host )"
)
_REPOSITORY_NOT_FOUND = re.compile(
    r"(?:fatal: repository '[^\n]*' not found$"
    r"|fatal: '[^\n]*' does not appear to be a git repository$)"
)
_RATE_LIMITED = re.compile(rf"{_CURL_LINE}The requested URL returned error: 429$")
TRANSPORT_CAUSES = ("rate_limited", "repository_not_found", "network_unreachable")
FETCH_CAUSES = ("timeout", "prune_incomplete", *TRANSPORT_CAUSES)  # `details.cause` of fetch_failed
_TRANSPORT_PATTERNS = (
    ("rate_limited", _RATE_LIMITED),
    ("repository_not_found", _REPOSITORY_NOT_FOUND),
    ("network_unreachable", _NETWORK_UNREACHABLE),
)


class FetchKind(enum.Enum):
    OK = "ok"
    LOCK_CONTENTION = "lock_contention"
    AUTH_REQUIRED = "auth_required"
    FAILED = "failed"


@dataclass(frozen=True)
class FetchOutcome:
    kind: FetchKind
    # with FAILED: "timeout", "prune_incomplete" or (exit 128) a transport cause:
    # "network_unreachable", "repository_not_found" or "rate_limited"
    cause: str | None = None

    @property
    def ok(self) -> bool:
        return self.kind is FetchKind.OK


_SERVER_TEXT_PREFIXES = ("remote:", "fatal: remote error:", "fatal: protocol error:")


def _stderr_lines(err: str) -> list[str]:
    """git's stderr lines: split on ``\\n`` only (one trailing ``\\r`` stripped per line).

    ``str.splitlines`` also breaks on \\x0b, \\x0c, \\x1c-\\x1e, \\x85, U+2028/2029 and a lone
    ``\\r``, which would let text inside a ``remote:`` line start a fake unprefixed line.
    """
    return [ln[:-1] if ln.endswith("\r") else ln for ln in err.split("\n")]


def _auth_lines(err: str) -> list[str]:
    """Lines that may carry an authentication marker: never a ``remote:`` line, which is text
    the server chose (a malicious remote could print any marker)."""
    return [ln for ln in _stderr_lines(err) if not ln.startswith(_SERVER_TEXT_PREFIXES)]


def classify_fetch(result: GitResult) -> FetchOutcome:
    """Classify one network git call by exit status and stderr (``LC_ALL=C`` is set)."""
    if result.timed_out:
        return FetchOutcome(FetchKind.FAILED, "timeout")
    err = result.err
    if result.returncode == 0:
        if any(line.startswith("error:") for line in _stderr_lines(err)):
            return FetchOutcome(FetchKind.FAILED, "prune_incomplete")
        return FetchOutcome(FetchKind.OK)
    if result.returncode == 1:
        # Newer git (2.56) exits 1 where older git exits 0 for a prune blocked by a ref lock.
        if "error: could not delete reference" in err and any(
            line.lstrip().startswith("- [deleted]") for line in _stderr_lines(err)
        ):
            return FetchOutcome(FetchKind.FAILED, "prune_incomplete")
        if any(marker in err for marker in LOCK_MARKERS):
            return FetchOutcome(FetchKind.LOCK_CONTENTION)
        return FetchOutcome(FetchKind.FAILED)
    if result.returncode == 128:
        lines = _auth_lines(err)
        if any(marker in ln for ln in lines for marker in AUTH_MARKERS):
            return FetchOutcome(FetchKind.AUTH_REQUIRED)
        for cause, pattern in _TRANSPORT_PATTERNS:
            if any(pattern.match(ln) for ln in lines):
                return FetchOutcome(FetchKind.FAILED, cause)
        return FetchOutcome(FetchKind.FAILED)
    return FetchOutcome(FetchKind.FAILED)


def fetch_with_retries(
    attempt: Callable[[], GitResult],
    *,
    retries: int,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[GitResult, FetchOutcome]:
    """Run ``attempt``; ref-lock contention is retried ``retries`` times, 1 second apart.

    Contention that outlasts the retries is a plain failure.
    """
    result = attempt()
    outcome = classify_fetch(result)
    for _ in range(max(retries, 0)):
        if outcome.kind is not FetchKind.LOCK_CONTENTION:
            return result, outcome
        sleep(1.0)
        result = attempt()
        outcome = classify_fetch(result)
    if outcome.kind is FetchKind.LOCK_CONTENTION:
        outcome = FetchOutcome(FetchKind.FAILED)
    return result, outcome


def outcome_error(
    outcome: FetchOutcome, result: GitResult, *, failure_code: str = "fetch_failed"
) -> WtctlError | None:
    """The error for a failed outcome (``None`` when it succeeded).

    ``failure_code`` is ``fetch_failed`` or ``clone_failed`` (a clone's timeout has the cause
    ``timeout`` too, and so are the transport causes; ``prune_incomplete`` only exists for
    fetches).
    """
    if outcome.ok:
        return None
    if outcome.kind is FetchKind.AUTH_REQUIRED:
        return WtctlError("auth_required", "the remote asked for credentials wtctl does not have")
    if failure_code not in ("fetch_failed", "clone_failed"):
        raise ValueError(failure_code)
    cause = outcome.cause
    if failure_code == "clone_failed" and cause == "prune_incomplete":
        cause = None
    lines = _stderr_lines(result.err.strip()) if result.err.strip() else []
    if cause == "prune_incomplete":
        # exit 0 with an `error:` line: that line is the reason, not whatever git printed last
        lines = [ln for ln in lines if ln.startswith("error:")] or lines
        tail = lines[0][:200]
    elif cause in TRANSPORT_CAUSES:
        # the line that classified it, not the "Could not read from remote repository" trailer
        pattern = dict(_TRANSPORT_PATTERNS)[cause]
        lines = [
            ln for ln in lines if not ln.startswith(_SERVER_TEXT_PREFIXES) and pattern.match(ln)
        ] or lines
        tail = lines[0][:200]
    else:
        tail = lines[-1][:200] if lines else "no output"
    details = {"cause": cause} if cause else None
    message = f"git failed ({'timed out' if cause == 'timeout' else tail})"
    if details:
        return WtctlError(failure_code, message, details=details)
    return WtctlError(failure_code, message)


# --------------------------------------------------------------------------------------------
# Porcelain parsing (git 2.34: newline-separated; wtctl's own paths with newlines are refused)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusEntry:
    kind: str  # "1" ordinary, "2" renamed or copied, "u" unmerged, "?" untracked, "!" ignored
    path: str
    xy: str = ""
    submodule: str = ""
    orig_path: str | None = None


def parse_status_v2(data: bytes) -> list[StatusEntry]:
    """``git status --porcelain=v2 -z``. Branch headers (``# ...``) are skipped."""
    fields = data.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    out: list[StatusEntry] = []
    i = 0
    while i < len(fields):
        rec = fields[i].decode("utf-8", "surrogateescape")
        i += 1
        if rec.startswith("# "):
            continue
        kind = rec[:1]
        if kind in ("?", "!") and rec[1:2] == " ":
            out.append(StatusEntry(kind, rec[2:]))
        elif kind == "1":
            parts = rec.split(" ", 8)
            if len(parts) != 9:
                raise PorcelainError(f"malformed status record: {rec[:60]!r}")
            out.append(StatusEntry("1", parts[8], parts[1], parts[2]))
        elif kind == "2":
            parts = rec.split(" ", 9)
            if len(parts) != 10 or i >= len(fields):
                raise PorcelainError(f"malformed status record: {rec[:60]!r}")
            orig = fields[i].decode("utf-8", "surrogateescape")
            i += 1
            out.append(StatusEntry("2", parts[9], parts[1], parts[2], orig))
        elif kind == "u":
            parts = rec.split(" ", 10)
            if len(parts) != 11:
                raise PorcelainError(f"malformed status record: {rec[:60]!r}")
            out.append(StatusEntry("u", parts[10], parts[1], parts[2]))
        else:
            raise PorcelainError(f"unknown status record: {rec[:60]!r}")
    return out


def parse_ls_files_tagged(data: bytes) -> list[tuple[str, str]]:
    """``git ls-files -v -z``: ``(tag, path)`` pairs."""
    out: list[tuple[str, str]] = []
    for rec in data.split(b"\0"):
        if not rec:
            continue
        text = rec.decode("utf-8", "surrogateescape")
        if len(text) < 3 or text[1] != " ":
            raise PorcelainError(f"malformed ls-files record: {text[:60]!r}")
        out.append((text[0], text[2:]))
    return out


def hidden_index_flag(tag: str) -> bool:
    """An assume-unchanged (lowercase tag) or skip-worktree (``S``) entry hides edits (D53)."""
    return tag.islower() or tag == "S"


@dataclass(frozen=True)
class WorktreeEntry:
    path: str
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    bare: bool = False
    locked: str | None = None  # None: not locked; "" locked without a reason
    prunable: str | None = None


def parse_worktree_list(data: bytes, *, expected: int | None) -> list[WorktreeEntry]:
    """``git worktree list --porcelain`` (no ``-z`` before 2.36): blocks split by blank lines.

    A path containing a newline can forge a blank line and whole extra entries: the output is
    ambiguous, and no local rule on the text can tell. So the caller supplies ``expected``, the
    number of entries there must be (1 plus the number of directories in
    ``<common git dir>/worktrees``, or just that number for a bare main repository), and a
    different count raises ``internal``. ``None`` skips the count (only for output that is not
    used for a decision). Each block must also be well formed: ``worktree`` first, no attribute
    twice, a ``HEAD`` unless bare, and a ``branch`` that starts with ``refs/``.
    """
    # TODO(#10): read <common>/worktrees with the fsafe no-follow primitives to get `expected`.
    entries: list[WorktreeEntry] = []
    for block in data.decode("utf-8", "surrogateescape").split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln != ""]
        if not lines:
            continue
        if not lines[0].startswith("worktree "):
            raise PorcelainError(f"malformed worktree record: {lines[0][:60]!r}")
        attrs: dict[str, object] = {"path": lines[0][len("worktree ") :]}
        seen: set[str] = set()
        for ln in lines[1:]:
            word, _, value = ln.partition(" ")
            if word in seen:
                raise PorcelainError(f"duplicate worktree attribute: {ln[:60]!r}")
            seen.add(word)
            if word == "HEAD":
                attrs["head"] = value
            elif word == "branch":
                if not value.startswith("refs/"):
                    raise PorcelainError(f"malformed worktree branch: {ln[:60]!r}")
                attrs["branch"] = value
            elif word == "detached":
                attrs["detached"] = True
            elif word == "bare":
                attrs["bare"] = True
            elif word == "locked":
                attrs["locked"] = value
            elif word == "prunable":
                attrs["prunable"] = value
            else:
                raise PorcelainError(f"unknown worktree attribute: {ln[:60]!r}")
        if "bare" not in seen and "HEAD" not in seen:
            raise PorcelainError(f"worktree record without HEAD: {lines[0][:60]!r}")
        entries.append(WorktreeEntry(**attrs))  # type: ignore[arg-type]
    if expected is not None and len(entries) != expected:
        raise WtctlError(
            "internal",
            f"git worktree list returned {len(entries)} entries, expected {expected}"
            " (a path with a newline can forge entries)",
        )
    return entries


def refuse_newline_path(path: str | Path) -> str:
    """Paths with a newline are refused (git 2.34's porcelain is newline-separated)."""
    text = os.fspath(path)
    if "\n" in text or "\r" in text:
        raise UnsafePathError("path contains a newline")
    return text
