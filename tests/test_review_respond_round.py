"""The consumer's round end to end (docs/interface.md, Clean checkout for confirm): a respond
throwaway an agent wrote into, a clean throwaway with ``--source remote``, ``extract``, a commit
and a fast-forward push from the shallow clean throwaway to a local bare remote, then the removal
of both. Only wtctl's CLI, its hardened git invocation and plain git are used."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.createlib import REPO
from tests.extractlib import XW
from tests.vectorlib import GIT
from wtctl.gateway import Gateway
from wtctl.hardgit import Git

pytestmark = pytest.mark.usefixtures("inproc")


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> XW:
    return XW(tmp_path, private_home).build()


class Consumer:
    """The hardened git a consumer runs in the clean throwaway (explicit git dir and work tree)."""

    def __init__(self, w: XW, rec: dict[str, Any]) -> None:
        self.git = Git(Gateway(), GIT, str(w.home))
        self.path = rec["path"]

    def run(self, args: list[str], **kw: Any) -> Any:
        return self.git.run(
            args,
            git_dir=f"{self.path}/.git",
            work_tree=self.path,
            cwd=self.path,
            allow_shallow=True,
            timeout=60,
            **kw,
        )

    def ok(self, args: list[str]) -> str:
        r = self.run(args)
        assert r.returncode == 0, (args, r.err)
        return str(r.stdout.decode().strip())

    def commit(self, msg: str) -> str:
        self.ok(["config", "--local", "user.name", "Round Trip"])
        self.ok(["config", "--local", "user.email", "round@example.invalid"])
        self.ok(["add", "-A", "--", "."])
        self.ok(["commit", "-q", "-m", msg])
        return self.ok(["rev-parse", "HEAD"])

    def push(self, url: str) -> Any:
        return self.git.run(
            ["push", "--porcelain", url, "HEAD:refs/heads/main"],
            git_dir=f"{self.path}/.git",
            work_tree=self.path,
            cwd=self.path,
            allow_shallow=True,
            scheme="file",
            tool_owned=True,
            timeout=60,
        )


def locate_url(w: XW) -> str:
    r = w.run("locate", REPO)
    assert r.code == 0, (r.out, r.err)
    return str(r.json()["fetch_url"])


def deepen(w: XW, commits: int = 55) -> str:
    """More history on the remote's main than the default clone depth (50), so a default-depth
    clean checkout is really shallow. Returns the new head."""
    for i in range(commits):
        w.commit(f"h{i}", w.work)
    w.g(["push", "-q", str(w.remote), "main"], w.work)
    return w.g(["rev-parse", "HEAD"], w.work)


def agent_work(w: XW, agent: dict[str, Any]) -> None:
    """What an agent leaves behind, including a hostile ``.git`` it planted."""
    w.put(agent, "c1.txt", b"c1 reworked\n")
    w.put(agent, "docs/new.md", b"# new\n")
    w.put(agent, "tools/run.sh", b"#!/bin/sh\necho hi\n", mode=0o755)
    marker = w.tmp / "agent-hook-ran"
    hook = Path(agent["path"]) / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    with (Path(agent["path"]) / ".git" / "config").open("a") as f:
        f.write(f"[core]\n\tfsmonitor = touch {marker}\n\thooksPath = {hook.parent}\n")


@pytest.mark.parametrize("depth", [[], ["--depth", "1"]], ids=["default-depth", "depth-1"])
def test_the_review_respond_round(w: XW, depth: list[str]) -> None:
    agent = w.made("--source", "local")  # the respond throwaway
    agent_work(w, agent)
    head = deepen(w)
    clean = w.made("--source", "remote", *depth, ref=head)  # never handed to anything untrusted
    assert clean["source"] == "remote" and clean["id"] != agent["id"]
    shallow = Path(clean["path"]) / ".git" / "shallow"
    assert shallow.exists(), "the clean checkout must be shallow for this test to mean anything"
    remote_before = w.g(["rev-parse", "refs/heads/main"], w.remote)

    data = w.extracted(agent, clean["path"], ["c1.txt", "docs/new.md", "tools/run.sh"], ["c2.txt"])
    assert [x["path"] for x in data["written"]] == ["c1.txt", "docs/new.md", "tools/run.sh"]
    assert data["deleted"] == [{"path": "c2.txt", "result": "deleted"}]
    assert not (w.tmp / "agent-hook-ran").exists()

    consumer = Consumer(w, clean)
    sha = consumer.commit("address review")
    # the hostile configuration and hook of the agent's throwaway never reached the clean one
    assert not (Path(clean["path"]) / ".git" / "hooks" / "pre-commit").exists()
    assert "fsmonitor" not in (Path(clean["path"]) / ".git" / "config").read_text()

    out = consumer.push(locate_url(w))
    assert out.returncode == 0, out.err
    assert w.g(["rev-parse", "refs/heads/main"], w.remote) == sha != remote_before
    assert w.g(["rev-parse", f"{sha}^"], w.remote) == remote_before
    tree = w.g(["ls-tree", "-r", "--name-only", sha], w.remote).splitlines()
    assert "docs/new.md" in tree and "tools/run.sh" in tree and "c2.txt" not in tree
    assert w.g(["show", f"{sha}:c1.txt"], w.remote) == "c1 reworked"
    mode = w.g(["ls-tree", sha, "tools/run.sh"], w.remote).split()[0]
    assert mode == "100644", "source modes are never copied"
    assert not (w.tmp / "agent-hook-ran").exists()

    for rec in (agent, clean):
        w.removed(rec["id"])
        assert not Path(rec["path"]).exists()
    assert w.leases() == []
    assert [r["state"] for r in w.records()] == ["removed", "removed"]


def test_a_push_after_the_remote_moved_is_rejected_and_both_are_still_removable(w: XW) -> None:
    agent = w.made("--source", "local")
    w.put(agent, "mine.txt", b"mine\n")
    clean = w.made("--source", "remote")
    w.advance_remote("n1")
    w.extracted(agent, clean["path"], ["mine.txt"])
    consumer = Consumer(w, clean)
    consumer.commit("mine")
    out = consumer.push(locate_url(w))
    assert out.returncode != 0
    rejected = [ln for ln in out.stdout.decode().splitlines() if ln.startswith("!")]
    assert rejected, out.stdout
    assert "mine" not in w.g(["log", "--format=%s", "refs/heads/main"], w.remote)
    w.removed(agent["id"])
    w.removed(clean["id"])


def test_extract_wrote_only_listed_regular_files_into_the_clean_checkout(w: XW) -> None:
    agent = w.made("--source", "local")
    agent_work(w, agent)
    w.put(agent, "unlisted.txt", b"never copied\n")
    clean = w.made("--source", "remote")
    consumer = Consumer(w, clean)
    before = consumer.ok(["status", "--porcelain=v2"])
    w.extracted(agent, clean["path"], ["c1.txt"])
    status = consumer.ok(["status", "--porcelain=v2"])
    assert before == "" and "c1.txt" in status and "unlisted.txt" not in status
    assert not (Path(clean["path"]) / "unlisted.txt").exists()
