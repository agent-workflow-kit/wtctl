"""The pure rules of ``extract`` (docs/interface.md, Extraction rules): list parsing, path
validation, collision detection and the source read.

Nothing here opens a destination. ``read_op`` is the operation the child process runs
(``fsafe_child`` registers it as ``extract_read``): it opens the source throwaway from its recorded
identities, one component at a time without following a link, and returns the bytes of each
listed regular file. Every path in the lists is untrusted (agent-chosen).
"""

from __future__ import annotations

import base64
import os
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from . import fsafe

MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255

# Test seam: called once all validation is done, right before the first write.
AFTER_VALIDATE: Callable[[], None] | None = None

# FsafeError kinds of a source read, mapped to the per-path error values.
_SOURCE_ERRORS = {
    "symlink": "symlink",
    "not_regular": "not_regular",
    "hardlink": "hard_link",
    "name": "invalid_path",
}


def parse_list(data: bytes) -> list[bytes]:
    """Entries separated by NUL, or by newlines when there is no NUL; empty entries dropped."""
    sep = b"\0" if b"\0" in data else b"\n"
    return [e for e in data.split(sep) if e]


# Characters a terminal or a reader may act on: C1 controls, bidirectional controls and the line
# and paragraph separators. They stay valid in a path; only their display is escaped.
_DISPLAY_ESCAPE = re.compile(
    "[\u0080-\u009f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)

# At most this many per-path errors are listed in the error output.
MAX_ERRORS_SHOWN = 100


def shown(raw: bytes) -> str:
    """The entry as text for output: invalid bytes and control characters escaped."""
    text = fsafe.escape_for_details(raw.decode("utf-8", "backslashreplace"))
    return _DISPLAY_ESCAPE.sub(_escape_display, text)


def _escape_display(m: re.Match[str]) -> str:
    code = ord(m.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def _has_control(text: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def check_path(raw: bytes) -> tuple[tuple[str, ...] | None, str | None]:
    """``(components, None)`` for a valid path, else ``(None, error)`` with the error value."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "invalid_path"
    if len(raw) > MAX_PATH_BYTES or _has_control(text):
        return None, "invalid_path"
    if text.startswith("/") or "\\" in text:
        return None, "invalid_path"
    parts = text.split("/")
    for c in parts:
        if c in ("", ".", "..") or len(c.encode("utf-8")) > MAX_COMPONENT_BYTES:
            return None, "invalid_path"
    if any(fsafe.is_dotgit(c) for c in parts):
        return None, "git_component"
    if ":" in text:
        return None, "invalid_path"
    return tuple(parts), None


def _key_component(name: str) -> str:
    s = "".join(
        c for c in name if ord(c) not in fsafe._HFS_IGNORABLE and unicodedata.category(c) != "Cf"
    )
    s = unicodedata.normalize("NFD", unicodedata.normalize("NFD", s).casefold())
    return s.rstrip(" .")


@dataclass
class Item:
    """One list entry. ``error`` is the first problem found (None: valid so far)."""

    role: str  # "file" or "delete"
    shown: str
    parts: tuple[str, ...] | None
    error: str | None = None
    # filled by the source read (files only)
    data: bytes | None = None
    # filled by the destination probe
    exists: bool = False
    mode: int | None = None

    @property
    def key(self) -> tuple[str, ...]:
        assert self.parts is not None
        return tuple(_key_component(c) for c in self.parts)


@dataclass
class Plan:
    files: list[Item] = field(default_factory=list)
    deletes: list[Item] = field(default_factory=list)

    @property
    def entries(self) -> list[Item]:
        return [*self.files, *self.deletes]

    def errors(self) -> list[Item]:
        return [e for e in self.entries if e.error]


def plan_lists(files: list[bytes], deletes: list[bytes], *, max_files: int) -> Plan:
    """Validate every entry of both lists (nothing is opened).

    Memory and output stay bounded by ``max_files``: no item is built beyond the first entry past
    the limit, which is reported ``limit_exceeded`` (whatever else is wrong with it).
    """
    plan = Plan()
    for role, raws, out in (("file", files, plan.files), ("delete", deletes, plan.deletes)):
        for i, raw in enumerate(raws):
            if i >= max_files:
                out.append(Item(role, shown(raw), None, "limit_exceeded"))
                break
            parts, error = check_path(raw)
            out.append(Item(role, shown(raw), parts, error))
    _mark_collisions(plan.entries)
    return plan


def _mark_collisions(entries: list[Item]) -> None:
    """Paths equal after folding, or one a prefix directory of another, are ``duplicate``."""
    live = [e for e in entries if e.error is None]
    by_key: dict[tuple[str, ...], list[Item]] = {}
    for e in live:
        by_key.setdefault(e.key, []).append(e)
    for group in by_key.values():
        for e in group[1:]:
            e.error = "duplicate"
    for e in live:
        k = e.key
        for n in range(1, len(k)):
            for other in by_key.get(k[:n], ()):
                other.error = other.error or "duplicate"
                e.error = e.error or "duplicate"


# -- the source read (runs in the child) --------------------------------------------------------


def _read_limited(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    got = 0
    while got <= limit:
        chunk = os.read(fd, min(1 << 20, limit + 1 - got))
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _read_one(
    anchor: fsafe.Anchor, root_fd: int, root_path: str, parts: list[str], limit: int
) -> tuple[str | None, bytes]:
    """``(error, data)`` for one source path."""
    try:
        if "/".join([root_path.rstrip("/"), *parts]) in anchor.mounts_below:
            return "not_found", b""
        dir_fd = fsafe.open_chain(anchor, root_fd, parts[:-1], start_path=root_path)
        try:
            fd = fsafe.open_file(anchor, dir_fd, parts[-1], single_link=True)
        finally:
            os.close(dir_fd)
        try:
            size = os.fstat(fd).st_size
            if size > limit:
                return "too_large", b""
            data = _read_limited(fd, limit)
        finally:
            os.close(fd)
    except fsafe.FsafeError as exc:
        return _SOURCE_ERRORS.get(exc.kind, "not_found"), b""
    except OSError:
        return "not_found", b""
    if len(data) > limit:
        return "too_large", b""
    return None, data


def read_op(args: dict[str, object]) -> dict[str, object]:
    """Child operation ``extract_read``: read the listed files of the source throwaway.

    ``args``: ``anchor``/``chain``/``root_identity`` as for the walks (the throwaway directory is
    the walk root), ``paths`` (lists of components), ``max_file`` and ``max_total``. Returns one
    result per path: ``{"error": value}`` or ``{"data": base64}``. A root that cannot be opened is
    an ``FsafeError`` for the caller to classify.
    """
    from . import fsafe_child

    raw = args["paths"]
    max_file, max_total = args["max_file"], args["max_total"]
    if not (isinstance(raw, list) and isinstance(max_file, int) and isinstance(max_total, int)):
        raise fsafe.FsafeError("bad_request", "paths, max_file and max_total are required")
    anchor, root_fd, root_path = fsafe_child._open_root(args, parent=False)
    results: list[dict[str, str]] = []
    used = 0
    overflow = False
    try:
        for parts in raw:
            if overflow:  # nothing after the first overflow is read
                results.append({"error": "limit_exceeded"})
                continue
            error, data = _read_one(anchor, root_fd, root_path, [str(p) for p in parts], max_file)
            if error is None and used + len(data) > max_total:
                error, data = "limit_exceeded", b""
                overflow = True
            if error is not None:
                results.append({"error": error})
                continue
            used += len(data)
            results.append({"data": base64.b64encode(data).decode("ascii")})
    finally:
        os.close(root_fd)
        anchor.close()
    return {"results": results}  # type: ignore[dict-item]
