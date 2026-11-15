"""Run the real list/show/label handlers against a seeded state.db."""

from __future__ import annotations

import pytest

from tests.helpers import Run
from tests.statehelpers import FakeClock
from wtctl.cli import main
from wtctl.context import Runtime
from wtctl.state import State

SHA = "a" * 40
DIGEST = "b" * 64


def seed(st: State, id_: str, **kw: object) -> dict:
    """A schema-valid record (worktree by default); ``kw`` overrides columns."""
    fields: dict[str, object] = {
        "id": id_,
        "object_type": "worktree",
        "state": "active",
        "owned": True,
        "repo": "example.com/o/r",
        "clone_path": "/c/r",
        "common_dir": "/c/r/.git",
        "common_id": "1:2",
        "admin_dir": f"/c/r/.git/worktrees/{id_[3:]}",
        "dir_id": "1:3",
        "base_dir": "/w",
        "path": f"/w/{id_}",
        "name": id_[3:],
        "branch": f"b-{id_[3:]}",
        "created_branch": True,
        "base": "refs/heads/main",
        "base_sha": SHA,
        "head_at_create": SHA,
        "fetch_url": "https://example.com/o/r.git",
        "remote": "origin",
        "baseline_digest": DIGEST,
    }
    if kw.get("object_type") == "throwaway":
        for k in (
            "owned clone_path common_dir common_id admin_dir name branch created_branch base "
            "base_sha head_at_create remote baseline_digest"
        ).split():
            fields[k] = None
        fields.update(
            base_dir="/t",
            root_id="1:9",
            ref=SHA,
            source="local",
            depth=0,
            name=None,
            clone_path="/c/r",
        )
    fields.update(kw)
    labels = fields.pop("labels", ())
    holders = fields.pop("holders", ())
    assert isinstance(labels, (list, tuple)) and isinstance(holders, (list, tuple))
    st.reserve_id(str(id_))
    return st.insert_record(fields, labels=labels, holders=holders)


def cli(
    capsys: pytest.CaptureFixture[str],
    st: State,
    *argv: str,
    clock: FakeClock | None = None,
) -> Run:
    runtime = Runtime(
        stdin_isatty=False, me_name="alice", gateway=st.gateway, state_opener=lambda ctx: st
    )
    code = main(list(argv), runtime=runtime)
    captured = capsys.readouterr()
    return Run(code, captured.out, captured.err)
