"""The managed crontab block behind ``cron install`` / ``cron uninstall``.

wtctl owns one block of the user's crontab, delimited by two exact marker lines::

    # wtctl managed
    17 * * * * /bin/sh -c '... reconcile --actor auto:cron'
    47 * * * * /bin/sh -c '... prune --throwaways --if-configured --actor auto:cron'
    # wtctl managed end

Rules (docs/interface.md, ``cron install`` / ``cron uninstall``):

- Everything outside the block is kept byte for byte: the crontab is edited as text by offset,
  never re-joined from lines, and read and written as bytes (a crontab that is not valid UTF-8
  is refused rather than re-encoded). Installing appends the block after one separating newline
  when the last foreign line has none; nothing else changes.
- ``crontab -l`` failing is "no crontab" only when its error says so; any other failure ends
  the command rather than treating the crontab as empty and overwriting it.
- A malformed marker structure (unterminated, repeated, reversed, stray end marker) is refused;
  the user fixes it by hand.
- ``crontab`` has no lock: the crontab is read again right before the write (the edit is
  recomputed when it changed) and read back afterwards. An edit made between that second read
  and the write is still lost.
- Every value reaching a crontab line is shell-quoted and ``%`` is escaped for cron. Values
  holding a newline, carriage return, NUL or backslash are refused (cron cannot represent a
  backslash before ``%``). The command runs under an explicit ``/bin/sh -c`` so a ``SHELL=``
  line elsewhere cannot change its meaning, and ``PATH`` is set inside the command, never as a
  crontab assignment (which would change the environment of the user's own entries).
- ``--env-file`` is sourced by cron as the user, so it and every directory above it must be
  safe from other users (``check_env_file``).

All ``crontab`` runs go through the gateway, so tests use a fake.
"""

from __future__ import annotations

import os
import pwd
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .context import Context, Result
from .errors import WtctlError
from .gateway import Gateway

HEADER = "# wtctl managed"
FOOTER = "# wtctl managed end"
SCHEDULE = (("17", "reconcile"), ("47", "prune --throwaways --if-configured"))
ACTOR = "auto:cron"
XDG_VARS = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")
ATTEMPTS = 3
_BAD_CHARS = ("\n", "\r", "\x00", "\\")


class CronError(WtctlError):
    """``usage`` (exit 2) for a bad argument; ``internal`` (exit 1) for the crontab or host."""

    def __init__(self, message: str, code: str = "internal") -> None:
        super().__init__(code, message)


def _clean(text: str, limit: int = 200) -> str:
    shown = "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in text.strip())
    return shown[:limit]


def _bad(text: str) -> bool:
    return any(c in text for c in _BAD_CHARS)


# ---------------------------------------------------------------- the block


def _is_macos() -> bool:
    return sys.platform == "darwin"


def cron_path(home: str, *, macos: bool, isdir: Callable[[str], bool]) -> str:
    """The ``PATH`` of the cron commands: ``~/.local/bin`` first; on macOS also Homebrew's and
    ``/usr/local/bin``, each only if it exists; then the system directories."""
    parts = [f"{home.rstrip('/')}/.local/bin"] if home.rstrip("/") else []
    if macos:
        parts += [d for d in ("/opt/homebrew/bin", "/usr/local/bin") if isdir(d)]
    parts += ["/usr/bin", "/bin"]
    return ":".join(parts)


def _quote(value: str, what: str, code: str = "internal") -> str:
    if not value or _bad(value):
        raise CronError(
            f"{what}: not usable in a crontab line (empty, or holds a newline, carriage return, "
            "NUL or backslash)",
            code,
        )
    return shlex.quote(value)


def build_entries(
    *,
    wtctl: str,
    config: str | None,
    env_file: str | None,
    path: str,
    xdg: Mapping[str, str],
) -> list[str]:
    """The crontab job lines. ``env_file`` is an already checked real path, ``config`` absolute."""
    prefix = f"PATH={_quote(path, 'PATH')}; export PATH; "
    for var, value in xdg.items():
        prefix += f"{var}={_quote(value, var)}; export {var}; "
    if env_file is not None:
        # `.` is a special builtin: a missing file aborts the non-interactive shell.
        prefix += f". {_quote(env_file, '--env-file', 'usage')}; "
    tail = f" --actor {ACTOR}"
    if config is not None:
        tail += f" --config {_quote(config, '--config', 'usage')}"
    exe = _quote(wtctl, "the wtctl path")
    lines = []
    for minute, args in SCHEDULE:
        script = f"{prefix}exec {exe} {args}{tail}"
        command = f"/bin/sh -c {shlex.quote(script)}".replace("%", "\\%")
        lines.append(f"{minute} * * * * {command}")
    return lines


def block_text(entries: list[str]) -> str:
    return "\n".join([HEADER, *entries, FOOTER])


def find_wtctl(environ: Mapping[str, str]) -> str:
    """The absolute path of the ``wtctl`` executable on ``PATH`` (absolute entries only), else
    the bare name, which the cron ``PATH`` resolves."""
    for entry in environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(entry):
            continue
        candidate = os.path.join(entry, "wtctl")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "wtctl"


def _home(environ: Mapping[str, str]) -> str:
    home = environ.get("HOME", "")
    if not os.path.isabs(home):
        try:
            home = pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            home = ""
    if not os.path.isabs(home) or _bad(home) or ":" in home:
        raise CronError("cannot determine a usable home directory for the cron PATH")
    return home.rstrip("/") or "/"


# ---------------------------------------------------------------- --env-file


def check_env_file(path: str) -> str:
    """The real path of ``--env-file`` once it passed the checks, else a usage error: a regular
    file owned by the user, readable, not group- or world-writable, in directories (all the way
    up) owned by the user or root that others cannot write to (a sticky directory is accepted
    above the file's own, never as its own parent). Anyone who can replace the file runs code
    as the user."""

    def refuse(why: str) -> CronError:
        return CronError(f"--env-file {_clean(path)!r}: {why}", "usage")

    if not path or _bad(path):
        raise refuse("not a usable path")
    real = os.path.realpath(os.path.abspath(path))
    if _bad(real):
        raise refuse("not a usable path")
    try:
        info = os.stat(real)
    except OSError as exc:
        raise refuse(f"cannot read: {exc.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise refuse("not a regular file")
    if info.st_uid != os.geteuid():
        raise refuse("not owned by you")
    if info.st_mode & 0o022:
        raise refuse("group- or world-writable")
    if not os.access(real, os.R_OK):
        raise refuse("not readable by you")
    directory, immediate = os.path.dirname(real), True
    while True:
        try:
            dinfo = os.stat(directory)
        except OSError as exc:
            raise refuse(f"cannot check {_clean(directory)!r}: {exc.strerror}") from None
        if dinfo.st_uid not in (os.geteuid(), 0):
            raise refuse(f"directory {_clean(directory)!r} is owned by neither you nor root")
        if dinfo.st_mode & 0o022 and (immediate or not dinfo.st_mode & stat.S_ISVTX):
            raise refuse(f"directory {_clean(directory)!r} is writable by others")
        parent = os.path.dirname(directory)
        if parent == directory:
            return real
        directory, immediate = parent, False


# ---------------------------------------------------------------- finding the block


@dataclass(frozen=True)
class Found:
    """The managed block inside a crontab text: ``start`` is the header line's offset, ``end``
    the offset just after the footer line's text (before its newline); ``body`` the lines
    between the markers."""

    start: int
    end: int
    body: list[str]


def find_block(text: str) -> Found | None:
    heads: list[int] = []
    feet: list[tuple[int, int]] = []  # (line start, offset after the line's text)
    pos = 0
    while pos <= len(text):
        nl = text.find("\n", pos)
        stop = len(text) if nl < 0 else nl
        stripped = text[pos:stop].strip(" \t\r")
        if stripped == HEADER:
            heads.append(pos)
        elif stripped == FOOTER:
            feet.append((pos, stop))
        if nl < 0:
            break
        pos = nl + 1
    if not heads and not feet:
        return None
    if len(heads) != 1 or len(feet) != 1 or feet[0][0] < heads[0]:
        raise CronError(
            f"the crontab has a malformed wtctl block (expected one {HEADER!r} line followed by "
            f"one {FOOTER!r} line); fix or remove the marker lines by hand, then retry"
        )
    start, (foot_start, end) = heads[0], feet[0]
    inner = text[text.index("\n", start) + 1 : foot_start] if foot_start > start else ""
    body = inner.split("\n")
    if body and body[-1] == "":
        body.pop()
    return Found(start, end, body)


# ---------------------------------------------------------------- the crontab program


class Crontab:
    """The user's crontab, through the gateway."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def _run(
        self, args: list[str], stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return self.gateway.crontab(args, stdin)
        except FileNotFoundError:
            raise CronError("the crontab command was not found on PATH") from None
        except subprocess.TimeoutExpired:
            raise CronError(f"crontab {' '.join(args)} timed out") from None
        except OSError as exc:
            raise CronError(f"cannot run crontab: {exc.strerror or type(exc).__name__}") from None

    def read(self) -> str:
        """The crontab text ('' when the user has none)."""
        result = self._run(["-l"])
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", "replace")
            if "no crontab" in err.lower():
                return ""
            raise CronError(f"crontab -l failed (exit {result.returncode}): {_clean(err)}")
        try:
            return result.stdout.decode("utf-8")  # strict, no newline translation
        except UnicodeDecodeError:
            raise CronError(
                "the crontab is not valid UTF-8 and cannot be rewritten safely; nothing changed"
            ) from None

    def write(self, text: str) -> None:
        """Replace the crontab by ``text`` (``crontab -r`` for the empty text)."""
        op = ["-r"] if text == "" else ["-"]
        result = self._run(op, None if text == "" else text.encode("utf-8"))
        if result.returncode != 0:
            err = _clean(result.stderr.decode("utf-8", "replace"))
            raise CronError(f"crontab {op[0]} failed (exit {result.returncode}): {err}")


Plan = tuple[str | None, list[str]]  # (new text or None for no change, entries to report)


def _edit(
    crontab: Crontab, compute: Callable[[str], Plan], expect: list[str] | None, dry_run: bool
) -> list[str]:
    """Read, compute and write the crontab; ``expect`` is the body the block must hold
    afterwards (None: no block)."""
    for _ in range(ATTEMPTS):
        current = crontab.read()
        new, entries = compute(current)
        if new is None or dry_run:
            return entries
        if crontab.read() != current:
            continue  # edited since we read it: start over on the new text
        crontab.write(new)
        after = find_block(crontab.read())
        if (None if after is None else after.body) != expect:
            raise CronError("the crontab does not hold the expected block after the write")
        return entries
    raise CronError("the crontab keeps changing; gave up, retry")


# ---------------------------------------------------------------- the commands


def _result(installed: bool, entries: list[str], dry_run: bool, summary: str) -> Result:
    return Result(
        data={"installed": installed, "entries": entries, "dry_run": dry_run},
        human="\n".join(entries),
        stderr=summary,
    )


def install(ctx: Context) -> Result:
    environ = os.environ
    env_arg = ctx.args.env_file
    env_file = None if env_arg is None else check_env_file(env_arg)
    config = None
    if ctx.config_path is not None:
        config = os.path.abspath(ctx.config_path)
    xdg = {v: environ[v] for v in XDG_VARS if os.path.isabs(environ.get(v, ""))}
    entries = build_entries(
        wtctl=find_wtctl(environ),
        config=config,
        env_file=env_file,
        path=cron_path(_home(environ), macos=_is_macos(), isdir=os.path.isdir),
        xdg=xdg,
    )
    wanted = block_text(entries)

    def compute(text: str) -> Plan:
        found = find_block(text)
        if found is None:
            sep = "" if text == "" or text.endswith("\n") else "\n"
            return text + sep + wanted + "\n", entries
        if found.body == entries:
            return None, entries
        return text[: found.start] + wanted + text[found.end :], entries

    # TODO(#5): write the `cron` log.jsonl record once the log writer exists.
    changed = _edit(Crontab(ctx.gateway), compute, entries, ctx.dry_run)
    verb = "would install" if ctx.dry_run else "installed"
    return _result(True, changed, ctx.dry_run, f"cron block {verb} ({len(changed)} entries)")


def uninstall(ctx: Context) -> Result:
    def compute(text: str) -> Plan:
        found = find_block(text)
        if found is None:
            return None, []
        end = found.end + 1 if text[found.end : found.end + 1] == "\n" else found.end
        return text[: found.start] + text[end:], found.body

    removed = _edit(Crontab(ctx.gateway), compute, None, ctx.dry_run)
    if not removed:
        summary = "no cron block present"
    else:
        summary = f"cron block {'would be removed' if ctx.dry_run else 'removed'}"
    return _result(False, removed, ctx.dry_run, summary)
