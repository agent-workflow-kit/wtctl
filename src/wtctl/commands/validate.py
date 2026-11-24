"""``wtctl validate``: an offline, read-only check of the configuration and the environment.

It loads the configuration (exit 2 ``config_invalid`` on a problem), checks that ``root`` is a
usable directory (or can be created), looks at every ``search_roots`` entry, and runs
``git --version`` once. It opens no state database, takes no lease, writes nothing and never
touches the network; the only process it starts is ``git --version``.
"""

from __future__ import annotations

import errno
import os
import stat
from typing import Any

from ..context import Context, Result
from ..errors import WtctlError
from ..hardgit import MIN_GIT_VERSION, build_env, parse_git_version, resolve_binary
from ..output import escape_control

# Bounds on what an agent- or user-written config can make the command do or print.
MAX_SEARCH_ROOTS = 50  # entries examined; more is a warning
MAX_SHOWN = 200  # characters of a config value shown in a message
GIT_TIMEOUT_S = 15

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_NOT_DIRECTORY = "not_directory"
STATUS_UNREADABLE = "unreadable"


def shown(text: str) -> str:
    """A config value for a message: control characters escaped, cut to ``MAX_SHOWN``."""
    text = escape_control(text)
    return text if len(text) <= MAX_SHOWN else text[:MAX_SHOWN] + "..."


def _invalid(key: str, detail: str) -> WtctlError:
    return WtctlError("config_invalid", f"{key}: {detail}")


def _check_root(root: str) -> dict[str, Any]:
    """``root`` must be a directory wtctl can write in, or be creatable below one.

    Looks only (``stat``, ``access``): the directory is never created. A symlink is followed
    (``create`` resolves the root with ``realpath`` first), also a dangling one: the walk and the
    writability check run on the resolved path, while ``path`` stays the configured root.
    """
    try:
        resolved = os.path.realpath(root)
    except (OSError, ValueError):
        resolved = root
    path = resolved
    while True:
        try:
            st = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            parent = os.path.dirname(path)
            if parent == path:
                raise _invalid("root", f"{shown(root)} has no existing parent directory") from None
            path = parent
            continue
        except (OSError, ValueError) as exc:
            why = exc.strerror if isinstance(exc, OSError) and exc.strerror else "invalid path"
            raise _invalid("root", f"cannot examine {shown(path)}: {why}") from None
        break
    if not stat.S_ISDIR(st.st_mode):
        raise _invalid("root", f"{shown(path)} is not a directory")
    if not os.access(path, os.W_OK | os.X_OK) or _read_only_mount(path):
        if path == resolved:
            raise _invalid("root", f"{shown(root)} is not writable")
        raise _invalid("root", f"{shown(root)} does not exist and {shown(path)} is not writable")
    return {"path": root, "exists": path == resolved}


def _read_only_mount(path: str) -> bool:
    """True when ``path`` is on a read-only mount (``os.access`` can miss that); errors: no."""
    try:
        return bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except (OSError, ValueError):
        return False


def _check_search_root(path: str) -> str:
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return STATUS_MISSING
    except OSError as exc:
        # A path too long for the file system cannot exist.
        return STATUS_MISSING if exc.errno == errno.ENAMETOOLONG else STATUS_UNREADABLE
    except ValueError:
        return STATUS_UNREADABLE
    if not stat.S_ISDIR(st.st_mode):
        return STATUS_NOT_DIRECTORY
    return STATUS_OK if os.access(path, os.R_OK | os.X_OK) else STATUS_UNREADABLE


_SEARCH_ROOT_WARNING = {
    STATUS_MISSING: ("search_root_missing", "does not exist"),
    STATUS_NOT_DIRECTORY: ("search_root_unusable", "is not a directory"),
    STATUS_UNREADABLE: ("search_root_unusable", "cannot be read"),
}


def _check_git(ctx: Context, git_bin_config: str | None) -> dict[str, Any]:
    git_bin = resolve_binary("git", os.environ.get("PATH", ""), git_bin_config)
    if git_bin is None:
        if git_bin_config is not None:
            raise _invalid("git_bin", f"{shown(git_bin_config)} is not an executable file")
        # As every git-using command: not a config problem, so exit 1.
        raise WtctlError("internal", "git not found: set git_bin or put git on an absolute PATH")
    # cwd and HOME are "/": nothing is created (open_git's private directory would be).
    result = ctx.gateway.git(
        [git_bin, "--version"],
        env=build_env(git_bin=git_bin, home="/"),
        cwd="/",
        timeout=GIT_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise WtctlError("internal", f"{shown(git_bin)} --version failed")
    try:
        version = parse_git_version(result.out)
    except Exception as exc:  # noqa: BLE001 - PorcelainError: unrecognized output
        raise WtctlError("internal", f"{shown(git_bin)} --version: unrecognized output") from exc
    text = ".".join(map(str, version))
    minimum = ".".join(map(str, MIN_GIT_VERSION))
    if version[:2] < MIN_GIT_VERSION:
        raise WtctlError("git_too_old", f"git {text} is too old; need {minimum} or newer")
    return {"path": git_bin, "version": text, "minimum": minimum}


def run(ctx: Context) -> Result:
    cfg = ctx.config  # config_invalid names the key and the file
    warnings: list[dict[str, str]] = []

    root = _check_root(cfg.root)

    roots = list(cfg.search_roots)
    search: list[dict[str, str]] = []
    for index, path in enumerate(roots[:MAX_SEARCH_ROOTS]):
        status = _check_search_root(path)
        search.append({"path": path, "status": status})
        if status != STATUS_OK:
            code, what = _SEARCH_ROOT_WARNING[status]
            warnings.append(
                {"code": code, "key": f"search_roots[{index}]", "message": f"{shown(path)} {what}"}
            )
    if len(roots) > MAX_SEARCH_ROOTS:
        warnings.append(
            {
                "code": "search_roots_truncated",
                "key": "search_roots",
                "message": f"{len(roots)} entries; only the first {MAX_SEARCH_ROOTS} were checked",
            }
        )

    git = _check_git(ctx, cfg.git_bin)

    data: dict[str, Any] = {
        "checks": {"root": root, "search_roots": search, "git": git},
        "warnings": warnings,
    }
    probe_line = None
    if getattr(ctx.args, "probe", False):
        from ..validate_probe import run_probe  # live checks: only with --probe

        data["probe"], probe_warnings = run_probe(ctx, git["path"])
        warnings.extend(probe_warnings)
        items = [i for fam in data["probe"].values() for i in fam]
        counts = {s: sum(1 for i in items if i["status"] == s) for s in ("ok", "failed", "skipped")}
        probe_line = (
            f"probe: {counts['ok']} ok, {counts['failed']} failed, {counts['skipped']} skipped"
        )
    human = ["wtctl: configuration and environment are usable"]
    human.append(f"root: {shown(root['path'])}")
    human.append(f"git: {git['version']} ({shown(git['path'])})")
    if probe_line:
        human.append(probe_line)
    stderr = "\n".join(f"warning: {w['key']}: {w['message']}" for w in warnings)
    return Result(data, human="\n".join(human), stderr=stderr)
