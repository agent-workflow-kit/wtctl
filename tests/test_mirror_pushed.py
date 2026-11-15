"""The pushed check and the forged-history vectors (spike E1a-f), against real git."""

from __future__ import annotations

import hashlib
import socket
import struct
import threading
from pathlib import Path

import pytest

from tests.mirrorlib import World, forge_loose_object
from tests.test_mirror import lease
from tests.vectorlib import build_hostile, plain_git
from wtctl.mirror import (
    Tips,
    commit_parents,
    find_path,
    parse_cat_file_batch,
    verify_path,
)


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> World:
    return World(tmp_path, private_home)


def mg(w: World, m, args: list[str], **kw):  # noqa: ANN001, ANN201
    """Unhardened git on the mirror (the attacker's view, and the controls)."""
    return plain_git(["--git-dir", m.path, *args], cwd=Path(m.path), home=w.home, **kw)


def unpushed_commit(w: World) -> str:
    """A root commit that exists only in the clone."""
    empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    sha = w.g(["commit-tree", empty_tree, "-m", "local-only"], w.clone)
    w.g(["update-ref", "refs/heads/local-only", sha], w.clone)
    return sha


def inject(w: World, m, sha: str, source: Path) -> None:  # noqa: ANN001
    """Put the commit object ``sha`` of ``source`` into the mirror (an agent writing its store)."""
    body = plain_git(["cat-file", "commit", sha], cwd=source, home=w.home).stdout
    r = mg(w, m, ["hash-object", "-w", "-t", "commit", "--stdin"], stdin=body)
    assert r.stdout.decode().strip() == sha, r.stderr


# -- the check ------------------------------------------------------------------------------------


def test_commits_on_the_remote_are_pushed_and_local_ones_are_not(w: World) -> None:
    shas = w.build()
    local = unpushed_commit(w)
    m = w.mirror()
    tips = w.refresh(m).tips
    for sha in shas:  # interior commits and the tip itself
        assert m.is_pushed(sha, tips).pushed, sha
    res = m.is_pushed(local, tips)
    assert (res.pushed, res.detail) == (False, "missing")


def test_a_merged_history_is_pushed_through_either_parent(w: World) -> None:
    w.build(2)
    w.g(["checkout", "-q", "-b", "side", "HEAD~1"])
    side = w.commit("side")
    w.g(["checkout", "-q", "main"])
    w.g(["merge", "-q", "--no-ff", "-m", "merge", "side"])
    w.g(["push", "-q", "origin", "main"])
    m = w.mirror()
    tips = w.refresh(m).tips
    assert m.is_pushed(side, tips).pushed


def test_a_commit_on_any_branch_tip_counts(w: World) -> None:
    shas = w.build()
    w.g(["checkout", "-q", "-b", "feat", shas[0]])
    f = w.commit("feat")
    w.g(["push", "-q", "origin", "feat"])
    m = w.mirror()
    tips = w.refresh(m).tips
    assert m.is_pushed(f, tips).pushed and m.is_pushed(shas[1], tips).pushed


def test_a_force_pushed_away_commit_is_unpushed_though_the_object_stays(w: World) -> None:
    shas = w.build()
    m = w.mirror()
    tips = w.refresh(m).tips
    assert m.is_pushed(shas[2], tips).pushed
    w.g(["update-ref", "refs/heads/main", shas[0]], w.remote)
    tips = w.refresh(m).tips
    assert mg(w, m, ["cat-file", "-t", shas[2]]).stdout.strip() == b"commit"  # still an object
    res = m.is_pushed(shas[2], tips)
    assert (res.pushed, res.detail) == (False, "not_ancestor")
    assert m.is_pushed(shas[0], tips).pushed


def test_no_tips_means_nothing_is_pushed(w: World) -> None:
    shas = w.build()
    m = w.mirror()
    w.refresh(m)
    for tips in (None, Tips({})):
        res = m.is_pushed(shas[0], tips)
        assert (res.pushed, res.detail) == (False, "no_tips")


def test_a_failed_refresh_leaves_no_tips_to_ask_with(w: World) -> None:
    w.build()
    m = w.mirror(url=str(w.tmp / "nowhere.git"))
    with pytest.raises(Exception):  # noqa: B017, PT011
        w.refresh(m)


@pytest.mark.parametrize(
    "bad", ["", "HEAD", "main", "abc", "A" * 40, "0" * 39, "0" * 41, "--all", "HEAD~1", "x" * 40]
)
def test_only_full_lowercase_shas_are_accepted(w: World, bad: str) -> None:
    w.build()
    m = w.mirror()
    tips = w.refresh(m).tips
    assert m.is_pushed(bad, tips) == m.is_pushed(bad, tips)
    assert m.is_pushed(bad, tips).pushed is False


def test_a_sha_that_is_not_a_commit_is_unpushed(w: World) -> None:
    w.build()
    m = w.mirror()
    tips = w.refresh(m).tips
    blob = mg(w, m, ["hash-object", "-w", "--stdin"], stdin=b"x").stdout.decode().strip()
    res = m.is_pushed(blob, tips)
    assert (res.pushed, res.detail) == (False, "not_a_commit")


def test_a_missing_commit_in_the_treeless_mirror_makes_no_network_call(
    w: World, private_home: Path
) -> None:
    w.build()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.5)
    port = listener.getsockname()[1]
    hits: list[int] = []

    def accept() -> None:
        for _ in range(20):
            try:
                c, _ = listener.accept()
            except OSError:
                continue
            hits.append(1)
            c.close()

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    m = w.mirror()
    tips = w.refresh(m).tips
    # Point the promisor remote at the listener the way a fetch URL would be (https, so the
    # transport would try to connect if it were allowed).
    mg(w, m, ["config", "remote.origin.url", f"https://127.0.0.1:{port}/o/r.git"])
    absent = "1" * 40
    # Control: unhardened git asks the promisor remote for a missing object.
    mg(w, m, ["cat-file", "-p", absent], env={"GIT_TERMINAL_PROMPT": "0"})
    assert hits, "the vector is live: unhardened git contacted the promisor remote"
    hits.clear()
    res = m.is_pushed(absent, tips)
    assert (res.pushed, res.detail) == (False, "missing")
    assert m.is_pushed(w.g(["rev-parse", "HEAD"], w.clone), tips).pushed
    assert hits == []
    listener.close()


# -- spike E1: forged history ----------------------------------------------------------------------


def forged_world(w: World):  # noqa: ANN202
    """A refreshed mirror whose object store also holds an unpushed commit ``u``."""
    shas = w.build()
    u = unpushed_commit(w)
    m = w.mirror()
    tips = w.refresh(m).tips
    inject(w, m, u, w.clone)
    return shas, u, m, tips


def test_e1a_replace_refs_do_not_forge_ancestry(w: World) -> None:
    shas, u, m, tips = forged_world(w)
    assert mg(w, m, ["replace", "--graft", shas[2], u]).returncode == 0
    control = mg(w, m, ["merge-base", "--is-ancestor", u, shas[2]])
    assert control.returncode == 0, "the vector is live for unhardened git"
    res = m.is_pushed(u, tips)
    assert res.pushed is False


def test_e1b_grafts_do_not_forge_ancestry(w: World) -> None:
    shas, u, m, tips = forged_world(w)
    Path(m.path, "info").mkdir(exist_ok=True)
    Path(m.path, "info", "grafts").write_text(f"{shas[2]} {u}\n")
    control = mg(w, m, ["merge-base", "--is-ancestor", u, shas[2]])
    assert control.returncode == 0, "the vector is live for unhardened git"
    assert m.is_pushed(u, tips).pushed is False


def test_e1d_a_planted_shallow_file_only_cuts_history(w: World) -> None:
    shas = w.build()
    m = w.mirror()
    tips = w.refresh(m).tips
    Path(m.path, "shallow").write_text(shas[2] + "\n")
    assert m.is_pushed(shas[0], tips).pushed is False  # fails safe


def test_e1e_alternates_give_nothing_beyond_writing_the_store(w: World) -> None:
    shas = w.build()
    u = unpushed_commit(w)
    m = w.mirror()
    tips = w.refresh(m).tips
    Path(m.path, "objects", "info", "alternates").write_text(
        str(w.clone / ".git" / "objects") + "\n"
    )
    assert mg(w, m, ["cat-file", "-t", u]).stdout.strip() == b"commit"
    assert m.is_pushed(u, tips).pushed is False
    assert m.is_pushed(shas[0], tips).pushed


def loose_mirror(w: World, shas: list[str]):  # noqa: ANN202
    """A mirror whose commits are loose objects written by hand, with ``main`` at the last."""
    m = w.mirror()
    with lease(w, m) as ls:
        m.ensure(ls)
    for sha in shas:
        inject(w, m, sha, w.work)
    assert mg(w, m, ["update-ref", "refs/heads/main", shas[-1]]).returncode == 0
    return m, Tips({"refs/heads/main": shas[-1]})


def test_e1f_same_sha_substitution_is_detected_by_the_rehash(w: World) -> None:
    shas = w.build(3)
    u = unpushed_commit(w)
    m, tips = loose_mirror(w, shas)
    inject(w, m, u, w.clone)
    assert m.is_pushed(shas[0], tips).pushed
    # An interior commit is replaced under its own sha by one whose parents are u and c1.
    old = plain_git(["cat-file", "commit", shas[1]], cwd=w.work, home=w.home).stdout
    forged = old.replace(f"parent {shas[0]}\n".encode(), f"parent {u}\nparent {shas[0]}\n".encode())
    assert forged != old
    forge_loose_object(Path(m.path), shas[1], b"commit", forged)
    control = mg(w, m, ["merge-base", "--is-ancestor", u, shas[2]])
    assert control.returncode == 0, "the vector is live: git believes the substituted content"
    res = m.is_pushed(u, tips)
    assert (res.pushed, res.detail) == (False, "ancestry_unverified")
    # A genuine commit behind the forged one is also refused: the path runs through it.
    res = m.is_pushed(shas[0], tips)
    assert (res.pushed, res.detail) == (False, "ancestry_unverified")
    # The tip's other side is untouched.
    assert m.is_pushed(shas[2], tips).pushed


def test_e1f_a_substituted_tip_is_detected(w: World) -> None:
    shas = w.build(2)
    m, tips = loose_mirror(w, shas)
    old = plain_git(["cat-file", "commit", shas[1]], cwd=w.work, home=w.home).stdout
    forge_loose_object(Path(m.path), shas[1], b"commit", old + b"extra\n")
    res = m.is_pushed(shas[1], tips)
    assert res.pushed is False and res.detail in ("ancestry_unverified", "check_failed")


def test_a_commit_missing_from_the_middle_of_the_path_is_unpushed(w: World) -> None:
    shas = w.build(3)
    m, tips = loose_mirror(w, shas)
    assert m.is_pushed(shas[0], tips).pushed
    obj = Path(m.path, "objects", shas[1][:2], shas[1][2:])
    obj.chmod(0o644)
    obj.unlink()
    res = m.is_pushed(shas[0], tips)
    assert res.pushed is False


def graft_commit_graph(w: World, m, child: str, parent: str) -> None:  # noqa: ANN001
    """Hand-forge objects/info/commit-graph so that ``child`` has ``parent`` as first parent."""
    assert mg(w, m, ["update-ref", "refs/heads/tmp-forge", parent]).returncode == 0
    assert mg(w, m, ["commit-graph", "write", "--reachable"]).returncode == 0
    path = Path(m.path, "objects", "info", "commit-graph")
    data = bytearray(path.read_bytes())
    nchunks = data[6]
    chunks = {}
    for i in range(nchunks + 1):
        cid, off = struct.unpack(">4sQ", data[8 + 12 * i : 8 + 12 * i + 12])
        chunks[cid] = off
    oidl, cdat = chunks[b"OIDL"], chunks[b"CDAT"]
    n = (cdat - oidl) // 20
    oids = [data[oidl + 20 * i : oidl + 20 * i + 20].hex() for i in range(n)]
    ci, pi = oids.index(child), oids.index(parent)
    struct.pack_into(">I", data, cdat + 36 * ci + 20, pi)
    data[-20:] = hashlib.sha1(bytes(data[:-20])).digest()  # noqa: S324
    path.chmod(0o644)
    path.write_bytes(bytes(data))
    assert mg(w, m, ["update-ref", "-d", "refs/heads/tmp-forge"]).returncode == 0


def test_e1c_a_forged_commit_graph_does_not_forge_ancestry(w: World) -> None:
    shas = w.build(3)
    u = unpushed_commit(w)
    m, tips = loose_mirror(w, shas)
    inject(w, m, u, w.clone)
    graft_commit_graph(w, m, shas[1], u)
    control = mg(w, m, ["merge-base", "--is-ancestor", u, shas[2]])
    assert control.returncode == 0, "the vector is live: git reads the forged graph"
    assert m.is_pushed(u, tips).pushed is False
    assert m.is_pushed(shas[0], tips).pushed  # the graph hid c1 from git; ours ignores it


# -- planted clone config --------------------------------------------------------------------------


def test_a_hostile_clone_has_no_effect_on_the_mirror_or_the_check(
    tmp_path: Path, private_home: Path
) -> None:
    h = build_hostile(tmp_path / "h", private_home)
    w = World(tmp_path, private_home)
    w.remote = h.src / ".git"
    w.clone = h.clone
    w.work = h.src
    plain_git(["config", "uploadpack.allowFilter", "true"], cwd=h.src, home=private_home)
    w.record(url=str(h.src))
    m = w.mirror(url=str(h.src))
    tips = w.refresh(m).tips
    tip = tips.refs["refs/heads/main"] if "refs/heads/main" in tips.refs else next(iter(tips.shas))
    assert m.is_pushed(tip, tips).pushed
    local = plain_git(["rev-parse", "HEAD"], cwd=h.clone, home=private_home).stdout.decode().strip()
    m.is_pushed(local, tips)
    assert h.hits() == [] and h.payload_hits() == []
    from tests.test_mirror import keys_of
    from wtctl.mirror import OWNED_KEYS

    assert set(keys_of(m.path)) == OWNED_KEYS


# -- the pure parts --------------------------------------------------------------------------------


def test_commit_parents_reads_only_the_header() -> None:
    p1, p2 = "a" * 40, "b" * 40
    content = f"tree {'c' * 40}\nparent {p1}\nparent {p2}\nauthor x\n\nparent {'d' * 40}\n".encode()
    assert commit_parents(content) == [p1, p2]
    gpg = f"tree {'c' * 40}\nparent {p1}\ngpgsig -----BEGIN\n parent {'e' * 40}\n -----END\n\nm\n"
    assert commit_parents(gpg.encode()) == [p1]
    assert commit_parents(b"tree x\nparent nothex\n\nm") is None


def test_find_path_prefers_short_paths_and_ignores_unlisted_parents() -> None:
    edges = {"t": ["m", "x"], "m": ["c"], "x": ["y"], "y": ["c"]}
    assert find_path(edges, "t", "c") == ["t", "m", "c"]
    assert find_path(edges, "t", "t") == ["t"]
    assert find_path({"t": ["z"]}, "t", "c") is None


def test_parse_cat_file_batch_rejects_truncation() -> None:
    assert parse_cat_file_batch(b"%s missing\n" % (b"a" * 40)) == {"a" * 40: None}
    with pytest.raises(ValueError):
        parse_cat_file_batch(b"%s commit 10\nshort\n" % (b"a" * 40))
    with pytest.raises(ValueError):
        parse_cat_file_batch(b"garbage\n")


def test_verify_path_needs_every_hop() -> None:
    from wtctl.mirror import object_sha1

    c1 = b"tree " + b"0" * 40 + b"\n\nroot\n"
    s1 = object_sha1(b"commit", c1)
    c2 = f"tree {'0' * 40}\nparent {s1}\n\nnext\n".encode()
    s2 = object_sha1(b"commit", c2)
    objs = {s1: ("commit", c1), s2: ("commit", c2)}
    assert verify_path([s2, s1], objs)
    assert not verify_path([s2, s1], {s1: ("commit", c1), s2: ("commit", c2 + b"x")})
    assert not verify_path([s2, s1], {s2: ("commit", c2)})
    assert not verify_path([s2, s1], {s1: ("tree", c1), s2: ("commit", c2)})
    assert not verify_path([s2, "f" * 40], {**objs, "f" * 40: ("commit", c1)})
