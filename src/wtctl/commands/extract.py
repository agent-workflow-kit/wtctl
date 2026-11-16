"""``wtctl extract <id|path> [--files-from F|-] [--delete-from F|-] --dest DIR [--max-size N]``
(docs/interface.md, extract and Extraction rules).

Copies listed regular files out of an agent-written throwaway into ``--dest`` and deletes listed
files there. Nothing is trusted: the lists, the source tree and its ``.git`` are data; wtctl never
runs git on the throwaway nor anything from it. Order: usage, record, per-record lease(s), state,
in-use check, path validation, source read (child process, deadline), destination probe; only
then the first write. Any problem before that is ``extract_invalid`` with nothing changed; an I/O
error after is ``io_error`` with exact progress. Extraction is logged, not audited.
"""

from __future__ import annotations

import base64
import contextlib
import os
import sys
from typing import Any

from .. import audit, extract_dest, extraction, fsafe, fsafe_child, removalfs
from ..config import default_config_path
from ..context import Context, Result
from ..errors import UsageError, WtctlError
from ..removal_status import Status
from ..state import TOMBSTONE_STATES, Record, State, record_lease, state_path

LIST_LIMIT = 64 * 1024 * 1024


def _read_list(spec: str | None) -> list[bytes] | None:
    if spec is None:
        return None
    try:
        if spec == "-":
            data = sys.stdin.buffer.read(LIST_LIMIT + 1)
        else:
            with open(spec, "rb") as f:
                data = f.read(LIST_LIMIT + 1)
    except (OSError, AttributeError, ValueError) as exc:
        raise UsageError(f"cannot read the list {spec!r}: {exc}") from None
    if len(data) > LIST_LIMIT:
        raise UsageError(f"the list {spec!r} is larger than {LIST_LIMIT} bytes")
    return extraction.parse_list(data)


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _real_dest(raw: str) -> str:
    """The destination as an absolute path whose parents are resolved (a symlinked parent is
    followed, a symlinked final component is refused)."""
    p = os.path.abspath(raw)
    parent, name = os.path.split(p)
    real = os.path.join(os.path.realpath(parent), name) if name else p
    try:
        st = os.lstat(real)
    except OSError:
        raise UsageError(f"--dest {raw!r} is not an existing directory") from None
    if not os.path.isdir(real) or os.path.islink(real) or st.st_mode == 0:
        raise UsageError(f"--dest {raw!r} must be an existing directory, not a link")
    for shape in (p, real):
        if any(fsafe.is_dotgit(c) for c in shape.split("/") if c):
            raise UsageError("--dest must not be inside a .git directory")
    return real


class Extractor:
    def __init__(self, ctx: Context, source: Record) -> None:
        self.ctx = ctx
        self.state: State = ctx.state
        self.config = ctx.config
        self.rid: str = source["id"]
        self.journal = audit.journal_for(ctx)
        self.dest_rec: Record | None = None
        self.dest_real = ""
        self.warnings: list[str] = []
        self._deleted: list[dict[str, str]] = []
        a = ctx.args
        cfg = self.config.extract
        self.max_file: int = cfg.max_file_bytes
        if a.max_size is not None:
            if a.max_size < 1:
                raise UsageError("--max-size must be a positive number of bytes")
            self.max_file = min(self.max_file, a.max_size)
        self.max_total: int = cfg.max_total_bytes
        self.max_files: int = cfg.max_files
        self.files_raw = _read_list(a.files_from)
        self.deletes_raw = _read_list(a.delete_from)

    # -- usage checks -------------------------------------------------------------------------

    def check_dest(self, source: Record) -> None:
        real = _real_dest(self.ctx.args.dest)
        protected = {os.path.realpath(str(state_path().parent))}
        for cfg_path in (self.ctx.config_path, default_config_path()):
            if cfg_path:
                protected.add(os.path.realpath(os.path.dirname(os.path.abspath(cfg_path))))
        if any(_inside(real, p) for p in protected):
            raise UsageError("--dest must not be inside wtctl's state or configuration directory")
        if _inside(real, source["path"]):
            raise UsageError("--dest must not be inside the source throwaway")
        for rec in self.state.records(object_type="throwaway"):
            if rec["id"] != source["id"] and _inside(real, rec["path"]):
                if rec["state"] != "active":
                    raise UsageError(f"--dest is inside {rec['id']}, which is {rec['state']}")
                self.dest_rec = rec
        if self.dest_rec is None and _inside(
            real, os.path.realpath(self.config.effective_throwaway_root)
        ):
            raise UsageError("--dest is inside the throwaway root but in no active throwaway")
        self.dest_real = real

    # -- entry --------------------------------------------------------------------------------

    def run(self) -> Result:
        ids: list[str] = sorted({self.rid, *([str(self.dest_rec["id"])] if self.dest_rec else [])})
        try:
            with contextlib.ExitStack() as stack:
                for i in ids:
                    stack.enter_context(
                        self.state.lease(record_lease(i), wait=self.config.lease_wait_s)
                    )
                return self.extract()
        except WtctlError as exc:
            self._log_failure(exc)
            raise

    def _fresh(self, rid: str) -> Record:
        rec = self.state.get_record(rid)
        if rec is None or rec["state"] in TOMBSTONE_STATES:
            self.state.resolve(rid)  # raises not_found (with the tombstone's details)
            raise WtctlError("not_found", f"no record {rid}")
        if rec["object_type"] != "throwaway":
            raise WtctlError("busy", f"record {rid} changed while it was being extracted")
        return rec

    def extract(self) -> Result:
        src = self._fresh(self.rid)
        if src["state"] != "active":
            raise self._unlinked(f"{self.rid} is {src['state']}, not active")
        if self.dest_rec is not None:
            self.dest_rec = self._fresh(self.dest_rec["id"])
            if self.dest_rec["state"] != "active":
                raise WtctlError("busy", f"record {self.dest_rec['id']} changed")
        self._in_use(src)
        plan = extraction.plan_lists(
            self.files_raw or [], self.deletes_raw or [], max_files=self.max_files
        )
        self._read_source(src, plan)
        dest = self._probe_dest(plan)
        try:
            return self._finish(plan, dest)
        finally:
            if dest is not None:
                dest.close()

    def _finish(self, plan: extraction.Plan, dest: extract_dest.Dest | None) -> Result:
        bad = plan.errors()
        if bad:
            capped = len(bad) > extraction.MAX_ERRORS_SHOWN
            raise WtctlError(
                "extract_invalid",
                f"{len(bad)} of {len(plan.entries)} entries cannot be extracted; nothing changed"
                + (f" (the first {extraction.MAX_ERRORS_SHOWN} are listed)" if capped else ""),
                details={
                    "paths": [
                        {"path": e.shown, "error": e.error}
                        for e in bad[: extraction.MAX_ERRORS_SHOWN]
                    ]
                },
            )
        if self.ctx.dry_run:
            return self._result(plan, dry=True)
        if extraction.AFTER_VALIDATE is not None:
            extraction.AFTER_VALIDATE()
        assert dest is not None or not plan.entries
        return self._apply(plan, dest)

    # -- checks -------------------------------------------------------------------------------

    def _unlinked(self, message: str) -> WtctlError:
        return WtctlError("refused", message, reason="unlinked", checks={})

    def _in_use(self, rec: Record) -> None:
        st = Status(self.ctx, rec)
        st.budget = fsafe_child.Budget()
        st.in_use()
        self.warnings += st.warnings
        check = st.checks["in_use"]
        if check["status"] == "fail":
            raise WtctlError(
                "refused",
                f"{rec['id']} is in use by {len(check['details'])} process(es)",
                reason="in_use",
                checks={"in_use": check},
                processes=check["details"],
            )
        if check["status"] != "pass":
            raise WtctlError(
                "refused",
                f"cannot tell whether {rec['id']} is in use: no process scanner works",
                reason="in_use",
                checks={"in_use": check},
                processes=[],
            )

    def _read_source(self, rec: Record, plan: extraction.Plan) -> None:
        todo = [e for e in plan.files if e.error is None]
        # The root is always opened and checked, also when nothing is left to read: a source that
        # vanished or was replaced must not let a delete-only call through.
        if not (rec["base_dir"] and rec["root_id"] and rec["dir_id"]):
            raise self._unlinked(f"{rec['id']} has no recorded root or directory identity")
        try:
            args = removalfs.walk_args(
                self.state, rec,
                paths=[list(e.parts or ()) for e in todo],
                max_file=self.max_file, max_total=self.max_total,
            )  # fmt: skip
        except fsafe.FsafeError as exc:
            raise self._unlinked(
                f"{fsafe.escape_for_details(rec['path'])}: {exc.message}"
            ) from None
        res = removalfs.run("extract_read", args, fsafe_child.Budget(fsafe_child.WALK_DEADLINE))
        if not res.ok or res.value is None:
            kind = res.error.kind if res.error else "deadline"
            if kind == "missing":
                raise self._unlinked(f"{rec['id']}: its directory no longer exists")
            # an expired deadline or failed read leaves every path of the call over the limit; a
            # root that is not the recorded one is not found
            fail = "limit_exceeded" if res.timed_out or kind == "deadline" else "not_found"
            for e in plan.entries:
                e.error = e.error or fail
            return
        results = res.value["results"]
        for e, r in zip(todo, results, strict=True):
            if "error" in r:
                e.error = r["error"]
            else:
                e.data = base64.b64decode(r["data"])

    def _probe_dest(self, plan: extraction.Plan) -> extract_dest.Dest | None:
        """Open the destination once (the same descriptor serves the write, so a swap of the
        directory after this point redirects nothing) and probe every target."""
        if not plan.entries:
            return None
        try:
            dest = extract_dest.Dest.open(self.dest_real, self.dest_rec)
        except fsafe.FsafeError:
            for e in plan.entries:
                e.error = e.error or "dest_conflict"
            return None
        for e in plan.entries:
            if e.error is not None or e.parts is None:
                continue
            try:
                p = dest.probe(e.parts)
            except (extract_dest.DestConflict, fsafe.FsafeError):
                e.error = "dest_conflict"
                continue
            e.exists, e.mode = p.exists, p.mode
        return dest

    # -- changes ------------------------------------------------------------------------------

    def _apply(self, plan: extraction.Plan, dest: extract_dest.Dest | None) -> Result:
        written: list[extraction.Item] = []
        deleted: list[tuple[extraction.Item, str]] = []
        results: dict[int, Any] = {}
        current = ""
        try:
            if dest is not None:
                for e in plan.files:
                    current = e.shown
                    assert e.parts is not None and e.data is not None
                    mode, created = dest.write(e.parts, e.data)
                    results[id(e)] = (mode, created)
                    written.append(e)
                for e in plan.deletes:
                    current = e.shown
                    assert e.parts is not None
                    outcome = dest.delete(e.parts)
                    deleted.append((e, outcome))
        except (OSError, fsafe.FsafeError, extract_dest.DestConflict) as exc:
            why = exc.strerror if isinstance(exc, OSError) else getattr(exc, "message", str(exc))
            raise WtctlError(
                "io_error",
                f"cannot extract {current or '<destination>'}: "
                + fsafe.escape_for_details(str(why)),
                details={
                    "path": current or ".",
                    "written": [e.shown for e in written],
                    "deleted": [e.shown for e, o in deleted if o == "deleted"],
                },
            ) from None
        self._deleted = [{"path": e.shown, "result": o} for e, o in deleted]
        return self._result(plan, dry=False, results=results)

    def _result(
        self, plan: extraction.Plan, *, dry: bool, results: dict[int, Any] | None = None
    ) -> Result:
        out_written, out_deleted = [], []
        for e in plan.files:
            assert e.data is not None
            if dry:
                mode, created = (
                    (e.mode if e.mode is not None else extract_dest.NEW_FILE_MODE),
                    not e.exists,
                )
            else:
                mode, created = (results or {})[id(e)]
            out_written.append(
                {"path": e.shown, "bytes": len(e.data), "mode": f"{mode:04o}", "created": created}
            )
        for e in plan.deletes:
            if dry:
                out_deleted.append({"path": e.shown, "result": "deleted" if e.exists else "absent"})
        if not dry:
            out_deleted = list(self._deleted)
        data = {
            "record_id": self.rid,
            "dest": self.dest_real,
            "written": out_written,
            "deleted": out_deleted,
            "dry_run": dry,
        }
        if not dry:
            self._log_success(out_written, out_deleted)
        verb = "would " if dry else ""
        lines = [f"{verb}wrote {w['path']} ({w['bytes']} bytes)" for w in out_written]
        lines += [f"{verb}deleted {d['path']}" if d["result"] == "deleted"
                  else f"{d['path']} already absent" for d in out_deleted]  # fmt: skip
        gone = sum(d["result"] == "deleted" for d in out_deleted)
        summary = f"{verb}wrote {len(out_written)} file(s), deleted {gone}"
        return Result(data, human="\n".join(lines), stderr="\n".join([summary, *self.warnings]))

    # -- log ----------------------------------------------------------------------------------

    def _log(self, level: str, details: dict[str, Any], event: str = "extract") -> None:
        rec = self.state.get_record(self.rid)
        warn = self.journal.log_best_effort(
            event,
            actor=self.ctx.actor.value,
            level=level,
            id=self.rid,
            repo=rec["repo"] if rec else None,
            decision_ids=list(self.ctx.decision_ids),
            details=details,
        )
        if warn:
            self.warnings.append(warn)

    def _log_success(self, written: list[dict[str, Any]], deleted: list[dict[str, Any]]) -> None:
        self._log(
            "info",
            {
                "written": len(written),
                "deleted": sum(d["result"] == "deleted" for d in deleted),
                "absent": sum(d["result"] == "absent" for d in deleted),
                "bytes": sum(w["bytes"] for w in written),
            },
        )

    def _log_failure(self, exc: WtctlError) -> None:
        d = exc.fields.get("details") or {}
        details: dict[str, Any] = {"code": exc.code, "reason": exc.fields.get("reason")}
        if exc.code == "extract_invalid":
            details["invalid"] = len(d.get("paths", []))
        if exc.code == "io_error":
            details["written"] = len(d.get("written", []))
            details["deleted"] = len(d.get("deleted", []))
        self._log(
            audit.level_for(exc.exit_code, refused=False),
            details,
            "refused" if exc.code == "refused" else "extract",
        )


def run(ctx: Context) -> Result:
    a = ctx.args
    if a.files_from is None and a.delete_from is None:
        raise UsageError("give --files-from or --delete-from (or both)")
    if a.files_from == "-" and a.delete_from == "-":
        raise UsageError("only one of --files-from and --delete-from may read standard input")
    record = ctx.state.resolve(a.target)
    if record["object_type"] != "throwaway":
        raise UsageError(f"{record['id']} is a worktree; extract works on throwaways")
    ex = Extractor(ctx, record)
    ex.check_dest(record)
    return ex.run()
