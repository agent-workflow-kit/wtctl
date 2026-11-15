"""The repo-config baseline: collection, digest and comparison (docs/interface.md, Baseline).

Two collectors, ``collect_git`` and ``collect_claude``, each run in a child process under the walk
deadline (``fsafe_child`` operations ``baseline_git`` and ``baseline_claude``). Both read only
through no-follow file descriptors verified against an anchor's device and mount, never run git,
and turn every bound, deadline, mount point and unreadable entry into a *changed* item
(``detail`` ``incomplete`` or ``mount_point``) so a computation that could not finish never
compares equal to a recorded baseline.

An ``Item`` is one thing that is compared: a config value (``type: config``), a file, a symlink
(by target string) or a directory listing (names, types, content hashes and the executable bit).
Absent things are not items; "absent" appears only in a comparison.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import os
import pwd
import stat
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from . import fsafe, gitconfig

EMPTY = hashlib.sha256(b"").hexdigest()
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_INCLUDE_DEPTH = 10
_CHUNK = 1 << 16
_DIGEST_VERSION = b"wtctl-baseline-v1\n"
_SKIP_MODULE_DIRS = frozenset({"objects", "refs", "logs"})


def _printable(text: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "\u2400hex:" + os.fsencode(text).hex()
    return text


@dataclass(frozen=True)
class Item:
    cls: str  # "git" or "claude"
    path: str
    key: str | None
    type: str  # file, directory, symlink, config
    sha256: str
    detail: str | None = None  # incomplete or mount_point
    anchor_id: str | None = None  # "<dev>:<ino>" of an external core.hooksPath directory

    def __post_init__(self) -> None:
        # a name that is not valid UTF-8 (undecodable bytes arrive as lone surrogates) could not be
        # stored or printed: it becomes its bytes in hex behind a marker no real item starts with
        object.__setattr__(self, "path", _printable(self.path))
        if self.key is not None:
            object.__setattr__(self, "key", _printable(self.key))

    def ident(self) -> tuple[str, str, str | None]:
        return (self.cls, self.path, self.key)

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Item:
        return cls(**d)


@dataclass
class Spec:
    common_dir: str
    common_id: str
    worktree: str
    dir_id: str
    admin_dir: str | None = None
    home: str = "/"
    ignore: list[str] = field(default_factory=list)
    limit: int = 2_000_000
    max_file: int = MAX_FILE
    max_total: int = MAX_TOTAL
    hooks_identity: str | None = None  # the recorded identity of an external hooksPath directory
    rebind_hooks: bool = False  # trust the current identity (baseline accept after inspection)


@dataclass
class Baseline:
    items: list[Item]
    digest: str
    entries: int = 0
    bytes: int = 0

    def has_detail(self) -> bool:
        return any(i.detail for i in self.items)


# --- digest and comparison -----------------------------------------------------------------


def _line(i: Item) -> bytes:
    parts = [i.cls, i.path, i.key if i.key is not None else "\x00none", i.type, i.sha256]
    parts.append(i.detail or "")
    out = bytearray()
    for p in parts:
        b = p.encode("utf-8", "surrogateescape")
        out += b"%d:" % len(b) + b + b"|"
    return bytes(out) + b"\n"


def digest_of(items: Iterable[Item]) -> str:
    h = hashlib.sha256(_DIGEST_VERSION)
    for line in sorted({_line(i) for i in items}):
        h.update(line)
    return h.hexdigest()


def _entry(i: Item | None) -> Any:
    return "absent" if i is None else {"type": i.type, "sha256": i.sha256}


def compare(recorded: Iterable[Item], current: Iterable[Item]) -> list[dict[str, Any]]:
    """The changed items, as ``baseline_item`` objects (contents are never reported)."""
    rmap = {i.ident(): i for i in recorded}
    cmap = {i.ident(): i for i in current}
    rows: list[dict[str, Any]] = []
    for ident in sorted(set(rmap) | set(cmap), key=lambda t: (t[0], t[1], t[2] or "")):
        r, c = rmap.get(ident), cmap.get(ident)
        if r is not None and c is not None and (r.type, r.sha256) == (c.type, c.sha256):
            if not c.detail:
                continue
        row: dict[str, Any] = {
            "class": ident[0],
            "path": ident[1],
            "key": ident[2],
            "before": _entry(r),
            "after": _entry(c),
        }
        if c is not None and c.detail:
            row["detail"] = c.detail
        rows.append(row)
    return rows


def changed_classes(rows: Iterable[dict[str, Any]]) -> list[str]:
    return sorted({r["class"] for r in rows})


def _dedupe(items: list[Item]) -> list[Item]:
    seen: dict[tuple[str, str, str | None], Item] = {}
    for i in items:
        old = seen.get(i.ident())
        if old is None or (i.detail and not old.detail):
            seen[i.ident()] = i
    return sorted(seen.values(), key=lambda i: (i.cls, i.path, i.key or ""))


# --- budgets -------------------------------------------------------------------------------


class Stop(Exception):
    """A bound was reached: the item being read and the rest are ``incomplete``."""


class Budget:
    def __init__(self, entries: int, total: int, max_file: int) -> None:
        self.entries = entries
        self.total = total
        self.max_file = max_file
        self.used_entries = 0
        self.used_bytes = 0

    def entry(self, n: int = 1) -> None:
        self.entries -= n
        self.used_entries += n
        if self.entries < 0:
            raise Stop("entries")

    def take(self, n: int) -> None:
        self.total -= n
        self.used_bytes += n
        if self.total < 0:
            raise Stop("bytes")


def _read_fd(fd: int, budget: Budget, keep: bool) -> tuple[str, bytes]:
    """SHA-256 (and the bytes when ``keep``) of the opened regular file, bounded."""
    h = hashlib.sha256()
    buf = bytearray()
    size = 0
    while True:
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > budget.max_file:
            raise Stop("file")
        budget.take(len(chunk))
        h.update(chunk)
        if keep:
            buf += chunk
    return h.hexdigest(), bytes(buf)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _detail_for(exc: fsafe.FsafeError | Stop) -> str:
    if isinstance(exc, fsafe.FsafeError) and exc.kind in ("mount_point", "identity"):
        return "mount_point"
    return "incomplete"


def _enc(s: str) -> bytes:
    return s.encode("utf-8", "surrogateescape")


# --- listing a directory tree --------------------------------------------------------------


class Tree:
    """An open directory below an anchor: hashing files and listing subtrees through no-follow
    descriptors, with a one-directory cache of the last parent opened."""

    def __init__(self, anchor: fsafe.Anchor, root_fd: int, root_path: str, budget: Budget) -> None:
        self.anchor = anchor
        self.root_fd = root_fd
        self.root_path = root_path
        self.budget = budget
        self._parts: tuple[str, ...] | None = None
        self._fd = -1

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
            self._parts = None

    def parent(self, parts: tuple[str, ...]) -> int:
        """The fd of the directory holding ``parts[-1]``; valid until the next call."""
        want = parts[:-1]
        if not want:
            return self.root_fd
        if self._parts == want:
            return self._fd
        self.close()
        self._fd = fsafe.open_chain(
            self.anchor, self.root_fd, list(want), start_path=self.root_path
        )
        self._parts = want
        return self._fd

    def hash_file(self, parts: tuple[str, ...]) -> tuple[str, bool, bytes]:
        pfd = self.parent(parts)
        fd = fsafe.open_file(self.anchor, pfd, parts[-1])
        try:
            st = self.anchor.fs.fstat(fd)
            sha, data = _read_fd(fd, self.budget, keep=False)
        finally:
            os.close(fd)
        return sha, bool(st.mode & 0o111), data

    def read_file(self, parts: tuple[str, ...]) -> bytes:
        pfd = self.parent(parts)
        fd = fsafe.open_file(self.anchor, pfd, parts[-1])
        try:
            return _read_fd(fd, self.budget, keep=True)[1]
        finally:
            os.close(fd)

    def readlink(self, parts: tuple[str, ...]) -> str:
        return os.readlink(parts[-1], dir_fd=self.parent(parts))

    def listing(self, skip: Callable[[fsafe.Entry], bool] | None = None) -> tuple[str, str | None]:
        """Hash of the recursive listing of the whole tree below the root, and a status
        (``None``, ``incomplete`` or ``mount_point``)."""
        h = hashlib.sha256()
        status: str | None = None
        progress = [0]
        sub = Tree(self.anchor, self.root_fd, self.root_path, self.budget)

        def descend(e: fsafe.Entry) -> bool:
            return not (skip is not None and skip(e))

        try:
            walker = fsafe.walk_tree(
                self.anchor,
                self.root_fd,
                limit=max(self.budget.entries, 0),
                descend=descend,
                root_path=self.root_path,
                progress=progress,
            )
            for e in walker:
                if skip is not None and skip(e):
                    continue
                rel = _enc("/".join(e.path))
                typ, sha, x = e.kind, "", ""
                if e.kind == "mount":
                    status = "mount_point"
                    break
                if e.kind in ("unreadable", "changed"):
                    status = "incomplete"
                    break
                if e.kind == "file":
                    sha, exe, _ = sub.hash_file(e.path)
                    x = "x" if exe else ""
                elif e.kind == "symlink":
                    sha = _sha(_enc(sub.readlink(e.path)))
                h.update(b"%d:" % len(rel) + rel + f"|{typ}|{sha}|{x}\n".encode())
        except (fsafe.FsafeError, Stop, OSError) as exc:
            status = (
                "mount_point"
                if isinstance(exc, fsafe.FsafeError) and exc.kind in ("mount_point", "identity")
                else "incomplete"
            )
        finally:
            sub.close()
            self.budget.entries -= progress[0]
            self.budget.used_entries += progress[0]
        return h.hexdigest(), status


# --- the git class -------------------------------------------------------------------------


def _expand(raw: str, home: str, base: str) -> str | None:
    """A config path value as git expands it; None when it cannot be resolved."""
    if raw.startswith("~/"):
        return os.path.normpath(os.path.join(home, raw[2:]))
    if raw == "~":
        return os.path.normpath(home)
    if raw.startswith("~"):
        user, _, rest = raw[1:].partition("/")
        try:
            return os.path.normpath(os.path.join(pwd.getpwnam(user).pw_dir, rest))
        except KeyError:
            return None
    if raw.startswith("%(prefix)/") or not raw:
        return None
    return os.path.normpath(os.path.join(base, raw))


class GitCollector:
    def __init__(self, spec: Spec) -> None:
        self.spec = spec
        self.budget = Budget(spec.limit, spec.max_total, spec.max_file)
        self.items: list[Item] = []
        self.ignores = gitconfig.compile_ignores(spec.ignore)
        self.effective: dict[str, bytes | None] = {}
        self.common: fsafe.Anchor | None = None
        self.common_path = ""
        self._admin_fd = -1

    # -- item helpers ---------------------------------------------------------------------

    def _problem(self, path: str, detail: str, typ: str = "file", key: str | None = None) -> None:
        self.items.append(Item("git", path, key, typ, EMPTY, detail))

    def _inside_common(self, path: str) -> bool:
        return path.startswith(self.common_path + "/")

    @contextlib.contextmanager
    def _parent_of(self, path: str) -> Iterator[tuple[fsafe.Anchor, int, str]]:
        """``(anchor, fd of the parent directory, name)`` for an absolute path: through the
        common anchor below the common dir, else through a no-follow chain from ``/`` to the
        parent's realpath (an include or excludes file outside the common dir is not anchored)."""
        assert self.common is not None
        name = os.path.basename(path)
        if self._inside_common(path):
            rel = path[len(self.common_path) + 1 :].split("/")
            fd = fsafe.open_chain(
                self.common, self.common.fd, rel[:-1], start_path=self.common_path
            )
            try:
                yield self.common, fd, name
            finally:
                os.close(fd)
            return
        anchor = fsafe.open_anchor(os.path.realpath(os.path.dirname(path)), precheck=False)
        try:
            yield anchor, anchor.fd, name
        finally:
            anchor.close()

    def _leaf(self, path: str, *, keep: bool = False) -> tuple[Item | None, bytes | None]:
        """The item for a file-ish path (``None``: absent) and, with ``keep``, the bytes of a
        regular file (``None`` for anything else)."""
        try:
            with self._parent_of(path) as (anchor, pfd, name):
                try:
                    st = anchor.fs.lstat_at(pfd, name)
                except FileNotFoundError:
                    return None, None
                kind = fsafe.kind_of_mode(st.mode)
                self.budget.entry()
                if kind == "symlink":
                    target = os.readlink(name, dir_fd=pfd)
                    return Item("git", path, None, "symlink", _sha(_enc(target))), None
                if kind == "dir":
                    return self._dir_item(path, anchor, pfd, name), None
                if kind != "file":
                    return Item("git", path, None, "file", _sha(f"special:{kind}".encode())), None
                fd = fsafe.open_file(anchor, pfd, name)
                try:
                    sha, data = _read_fd(fd, self.budget, keep)
                finally:
                    os.close(fd)
                return Item("git", path, None, "file", sha), data
        except fsafe.FsafeError as exc:
            if exc.kind in ("missing", "not_dir"):
                return None, None
            return Item("git", path, None, "file", EMPTY, _detail_for(exc)), None
        except (Stop, OSError):
            return Item("git", path, None, "file", EMPTY, "incomplete"), None

    def _dir_item(self, path: str, anchor: fsafe.Anchor, pfd: int, name: str) -> Item:
        """A directory listing item for ``name`` inside ``pfd`` (anchored by ``anchor``)."""
        try:
            fd = fsafe.open_dir(anchor, pfd, name)
        except fsafe.FsafeError as exc:
            return Item("git", path, None, "directory", EMPTY, _detail_for(exc))
        try:
            tree = Tree(anchor, fd, path, self.budget)
            sha, status = tree.listing()
            tree.close()
        finally:
            os.close(fd)
        return Item("git", path, None, "directory", sha, status)

    def _add_leaf(self, path: str) -> None:
        item, _ = self._leaf(path)
        if item is not None:
            self.items.append(item)

    # -- config ---------------------------------------------------------------------------

    def _flatten(self, path: str, depth: int, out: list[gitconfig.Entry]) -> None:
        if depth > MAX_INCLUDE_DEPTH:
            self._problem(path, "incomplete")
            return
        item, data = self._leaf(path, keep=True)
        if item is None:
            return
        if item.type != "file" or item.detail or data is None:
            self.items.append(item)
            return
        try:
            entries = gitconfig.parse(data, max_entries=max(self.budget.entries, 0))
        except gitconfig.ConfigLimitError:
            self._problem(path, "incomplete")
            return
        except gitconfig.ConfigSyntaxError:
            self.items.append(item)  # unparsable: recorded by its content hash
            return
        try:
            for e in entries:
                self.budget.entry()
                out.append(e)
                if e.value is None or not e.value or gitconfig.include_kind(e.key) is None:
                    continue
                raw = e.value.decode("utf-8", "surrogateescape")
                target = _expand(raw, self.spec.home, os.path.dirname(path))
                if target is None:
                    self._problem(path, "incomplete")
                    continue
                self._flatten(target, depth + 1, out)
        except Stop:
            self._problem(path, "incomplete")

    def _config_root(self, path: str) -> None:
        entries: list[gitconfig.Entry] = []
        self._flatten(path, 0, entries)
        grouped: dict[str, list[bytes | None]] = {}
        for e in entries:
            self.effective[e.key] = e.value
            if gitconfig.is_benign(e.key) or gitconfig.is_ignored(e.key, self.ignores):
                continue
            grouped.setdefault(e.key, []).append(e.value)
        for key, values in grouped.items():
            h = hashlib.sha256()
            for v in values:
                h.update(b"\x00" if v is None else b"\x01%d:" % len(v) + v)
            self.items.append(Item("git", path, key, "config", h.hexdigest()))

    # -- the pieces -----------------------------------------------------------------------

    def _modules(self, base_path: str, base_fd: int) -> None:
        assert self.common is not None
        try:
            mfd = fsafe.open_dir(self.common, base_fd, "modules")
        except fsafe.FsafeError as exc:
            if exc.kind not in ("missing", "not_dir"):
                self._problem(base_path + "/modules", _detail_for(exc), "directory")
            return
        root = base_path + "/modules"
        tree = Tree(self.common, mfd, root, self.budget)
        progress = [0]
        try:
            walker = fsafe.walk_tree(
                self.common,
                mfd,
                limit=max(self.budget.entries, 0),
                descend=lambda e: e.path[-1] not in _SKIP_MODULE_DIRS and e.path[-1] != "hooks",
                root_path=root,
                progress=progress,
            )
            for e in walker:
                name = e.path[-1]
                full = root + "/" + "/".join(e.path)
                if e.kind == "mount":
                    self._problem(full, "mount_point", "directory")
                elif e.kind in ("unreadable", "changed"):
                    self._problem(full, "incomplete", "directory")
                elif name == "config" and e.kind in ("file", "symlink"):
                    self._add_leaf(full)
                elif name == "hooks" and e.kind == "dir":
                    self._add_leaf(full)
        except (fsafe.FsafeError, Stop) as exc:
            self._problem(root, _detail_for(exc), "directory")
        finally:
            tree.close()
            os.close(mfd)
            self.budget.entries -= progress[0]
            self.budget.used_entries += progress[0]

    def _external_hooks(self, path: str) -> None:
        """A ``core.hooksPath`` directory outside the common dir: its own anchor."""
        try:
            anchor = fsafe.open_anchor(path, expected=None)
        except fsafe.FsafeError as exc:
            if exc.kind in ("missing",):
                return
            if exc.kind == "not_dir":
                self._add_leaf(path)  # a file or a symlink there: recorded as such
                return
            if exc.kind == "symlink":
                self._add_leaf(path)
                return
            self._problem(path, _detail_for(exc), "directory")
            return
        try:
            cur = f"{anchor.identity.dev}:{anchor.identity.ino}"
            rec = self.spec.hooks_identity
            if rec is not None and rec != cur and not self.spec.rebind_hooks:
                self.items.append(Item("git", path, None, "directory", EMPTY, "mount_point", cur))
                return
            tree = Tree(anchor, anchor.fd, anchor.path, self.budget)
            sha, status = tree.listing()
            tree.close()
            self.items.append(Item("git", path, None, "directory", sha, status, cur))
        finally:
            anchor.close()

    def _hooks_path(self) -> None:
        val = self.effective.get("core.hookspath")
        if not val:
            return
        raw = val.decode("utf-8", "surrogateescape")
        path = _expand(raw, self.spec.home, self.spec.worktree)
        if path is None:
            self._problem(raw or "core.hooksPath", "incomplete", "directory")
            return
        if path == self.common_path + "/hooks":
            return
        if self._inside_common(path):
            self._add_leaf(path)
        else:
            self._external_hooks(path)

    def _excludes_file(self) -> None:
        val = self.effective.get("core.excludesfile")
        if not val:
            return
        raw = val.decode("utf-8", "surrogateescape")
        path = _expand(raw, self.spec.home, self.spec.worktree)
        if path is None:
            self._problem(raw, "incomplete")
            return
        self._add_leaf(path)

    def run(self) -> list[Item]:
        spec = self.spec
        try:
            self.common = fsafe.open_anchor(
                spec.common_dir, expected=_identity(spec.common_id), precheck=True
            )
        except fsafe.FsafeError as exc:
            self._problem(spec.common_dir, _detail_for(exc), "directory")
            return self.items
        self.common_path = self.common.path
        try:
            self._collect()
        except (Stop, fsafe.FsafeError) as exc:
            self._problem(self.common_path, _detail_for(exc), "directory")
        finally:
            if self._admin_fd >= 0:
                os.close(self._admin_fd)
            self.common.close()
        return self.items

    def _collect(self) -> None:
        assert self.common is not None
        c = self.common_path
        admin = self.spec.admin_dir
        roots = [f"{c}/config", f"{c}/config.worktree"]
        admin_ok = False
        if admin:
            roots.append(f"{admin}/config.worktree")
            if admin.startswith(c + "/worktrees/"):
                try:
                    self._admin_fd = fsafe.open_chain(
                        self.common,
                        self.common.fd,
                        admin[len(c) + 1 :].split("/"),
                        start_path=c,
                    )
                    admin_ok = True
                except fsafe.FsafeError as exc:
                    self._problem(admin, _detail_for(exc), "directory")
            else:
                self._problem(admin, "incomplete", "directory")
        for root in roots:
            if admin and root.startswith(admin + "/") and not admin_ok:
                continue
            self._config_root(root)
        self._add_leaf(f"{c}/hooks")
        self._hooks_path()
        self._add_leaf(f"{c}/objects/info/alternates")
        self._add_leaf(f"{c}/info/exclude")
        self._excludes_file()
        self._modules(c, self.common.fd)
        if admin and admin_ok:
            self._add_leaf(f"{admin}/commondir")
            self._add_leaf(f"{admin}/gitdir")
            self._modules(admin, self._admin_fd)


def _identity(text: str) -> fsafe.Identity:
    dev, _, ino = text.partition(":")
    return fsafe.Identity(int(dev), int(ino))


# --- the claude class ----------------------------------------------------------------------


def _is_claude_md(name: str) -> bool:
    return name.startswith("CLAUDE") and name.endswith(".md") and len(name) >= len("CLAUDE.md")


def _cc_writes(e: fsafe.Entry) -> bool:
    return (
        e.kind == "dir"
        and e.path[-1] == ".cc-writes"
        and (len(e.path) == 1 or e.path[-2] == ".claude")
    )


class ClaudeCollector:
    def __init__(self, spec: Spec) -> None:
        self.spec = spec
        self.budget = Budget(spec.limit, spec.max_total, spec.max_file)
        self.items: list[Item] = []

    def _problem(self, rel: str, detail: str, typ: str = "directory") -> None:
        self.items.append(Item("claude", rel, None, typ, EMPTY, detail))

    def run(self) -> list[Item]:
        spec = self.spec
        try:
            anchor = fsafe.open_anchor(spec.worktree, expected=_identity(spec.dir_id))
        except fsafe.FsafeError as exc:
            self._problem(".", _detail_for(exc))
            return self.items
        tree = Tree(anchor, anchor.fd, anchor.path, self.budget)
        progress = [0]
        try:
            self._walk(anchor, tree, progress)
        except (fsafe.FsafeError, Stop, OSError) as exc:
            self._problem(".", _detail_for(exc) if not isinstance(exc, OSError) else "incomplete")
        finally:
            tree.close()
            self.budget.entries -= progress[0]
            self.budget.used_entries += progress[0]
            anchor.close()
        return self.items

    def _walk(self, anchor: fsafe.Anchor, tree: Tree, progress: list[int]) -> None:
        def descend(e: fsafe.Entry) -> bool:
            name = e.path[-1]
            return name not in (".git", ".claude", ".mcp.json")

        for e in fsafe.walk_tree(
            anchor,
            anchor.fd,
            limit=max(self.budget.entries, 0),
            descend=descend,
            root_path=anchor.path,
            progress=progress,
        ):
            name = e.path[-1]
            rel = "/".join(e.path)
            if e.kind == "mount":
                self._problem(rel, "mount_point")
                continue
            if e.kind in ("unreadable", "changed"):
                if name in (".claude", ".mcp.json") or _is_claude_md(name):
                    self._problem(rel, "incomplete")
                continue
            if name in (".claude", ".mcp.json"):
                self._item(anchor, tree, e, rel)
            elif _is_claude_md(name) and e.kind in ("file", "symlink"):
                self._item(anchor, tree, e, rel)

    def _item(self, anchor: fsafe.Anchor, tree: Tree, e: fsafe.Entry, rel: str) -> None:
        try:
            if e.kind == "symlink":
                self.items.append(
                    Item("claude", rel, None, "symlink", _sha(_enc(tree.readlink(e.path))))
                )
            elif e.kind == "file":
                sha, _, _ = tree.hash_file(e.path)
                self.items.append(Item("claude", rel, None, "file", sha))
            elif e.kind == "dir":
                pfd = tree.parent(e.path)
                fd = fsafe.open_dir(anchor, pfd, e.path[-1])
                try:
                    sub = Tree(anchor, fd, anchor.path + "/" + rel, self.budget)
                    sha, status = sub.listing(skip=_cc_writes)
                    sub.close()
                finally:
                    os.close(fd)
                self.items.append(Item("claude", rel, None, "directory", sha, status))
            else:
                self.items.append(
                    Item("claude", rel, None, "file", _sha(f"special:{e.kind}".encode()))
                )
        except (fsafe.FsafeError, Stop, OSError) as exc:
            if isinstance(exc, fsafe.FsafeError) and exc.kind == "missing":
                return
            self._problem(
                rel, _detail_for(exc) if not isinstance(exc, OSError) else "incomplete", "file"
            )


# --- running the collectors ----------------------------------------------------------------


def op_baseline_git(args: dict[str, Any]) -> dict[str, Any]:
    c = GitCollector(Spec(**args))
    items = c.run()
    return {
        "items": [i.to_json() for i in items],
        "entries": c.budget.used_entries,
        "bytes": c.budget.used_bytes,
    }


def op_baseline_claude(args: dict[str, Any]) -> dict[str, Any]:
    c = ClaudeCollector(Spec(**args))
    items = c.run()
    return {
        "items": [i.to_json() for i in items],
        "entries": c.budget.used_entries,
        "bytes": c.budget.used_bytes,
    }


Runner = Callable[..., Any]


def inproc(op: str, args: dict[str, Any], **_: Any) -> Any:
    """A runner that executes the operation in this process (tests; no deadline)."""
    from . import fsafe_child

    try:
        return fsafe_child.OpResult(True, value=fsafe_child.OPS[op](args))
    except fsafe.FsafeError as exc:
        return fsafe_child.OpResult(False, error=exc)
    except OSError as exc:
        return fsafe_child.OpResult(False, error=fsafe.FsafeError("io", str(exc)))


def _failed_item(cls: str, spec: Spec, res: Any) -> Item:
    detail = "incomplete"
    if res.error is not None and res.error.kind in ("mount_point", "identity"):
        detail = "mount_point"
    path = spec.common_dir if cls == "git" else "."
    return Item(cls, path, None, "directory", EMPTY, detail)


def compute(spec: Spec, *, runner: Runner | None = None) -> Baseline:
    """Both classes, each collector in a child under the walk deadline (``runner`` replaces the
    child, for tests). A failed or timed-out collector is one changed item of its class."""
    from . import fsafe_child

    run = runner or fsafe_child.run_op
    base = dataclasses.asdict(spec)
    items: list[Item] = []
    entries = bytes_ = 0
    for cls, op in (("git", "baseline_git"), ("claude", "baseline_claude")):
        args = {**base, "limit": spec.limit - entries, "max_total": spec.max_total - bytes_}
        if args["limit"] < 0 or args["max_total"] < 0:
            items.append(Item(cls, ".", None, "directory", EMPTY, "incomplete"))
            continue
        res = run(op, args)
        if not res.ok or res.value is None:
            items.append(_failed_item(cls, spec, res))
            continue
        items += [Item.from_json(d) for d in res.value["items"]]
        entries += int(res.value["entries"])
        bytes_ += int(res.value["bytes"])
    final = _dedupe(items)
    return Baseline(final, digest_of(final), entries, bytes_)


def stat_identity(path: str) -> str | None:
    """``<dev>:<ino>`` of a directory, no-follow on the last component (None if absent)."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    return f"{st.st_dev}:{st.st_ino}"
