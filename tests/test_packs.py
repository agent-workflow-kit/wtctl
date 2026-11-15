"""Pack transfer into the clone and local throwaways (D54, spike H), against real git."""

from __future__ import annotations

import os
import resource
import stat
import threading
from pathlib import Path

import pytest

from tests.mirrorlib import World
from tests.packlib import (
    Recorder,
    cg,
    common_id,
    git_tree_snapshot,
    lagging,
    pack_dir_entries,
    refreshed,
    tmp_entries,
)
from tests.test_mirror import lease
from tests.vectorlib import GIT, plain_git
from wtctl import fsafe, packs
from wtctl.errors import WtctlError
from wtctl.hardgit import Git


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> World:
    return World(tmp_path, private_home)


def run_transfer(w: World, m, start: str, **kw):  # noqa: ANN001, ANN201
    with lease(w, m) as ls:
        return packs.transfer_base(m, ls, common_id=kw.pop("cid", common_id(w)), start=start, **kw)


def build_pack(w: World, repo: Path, tip: str, dest: Path, *extra: str, stdin: str | None = None):  # noqa: ANN201
    body = stdin if stdin is not None else f"{tip}\n"
    r = plain_git(["pack-objects", "--revs", "--stdout", "-q", *extra], cwd=repo, home=w.home,
                  stdin=body.encode())  # fmt: skip
    assert r.returncode == 0, r.stderr
    dest.write_bytes(r.stdout)


# -- base commit into the clone ----------------------------------


def test_the_base_commit_arrives_as_a_pack_and_nothing_else_changes(w: World) -> None:
    old, new = lagging(w)
    m = refreshed(w)
    before = git_tree_snapshot(w.clone / ".git")
    got = run_transfer(w, m, new[-1])
    assert not got.already_present and got.pack and got.pack_new
    assert got.commits == 2 and got.present_tips >= 1
    assert cg(w, ["rev-list", "--objects", "--missing=print", new[-1]]).count("?") == 0
    cg(w, ["fsck", "--strict", "--no-dangling"])
    names = pack_dir_entries(w)
    assert f"pack-{got.pack}.pack" in names and f"pack-{got.pack}.idx" in names
    assert not [n for n in names if n.startswith("tmp_")]
    for n in (f"pack-{got.pack}.pack", f"pack-{got.pack}.idx"):
        assert stat.S_IMODE(os.stat(w.clone / ".git/objects/pack" / n).st_mode) == 0o444
    assert git_tree_snapshot(w.clone / ".git") == before  # no ref, config or other file written
    assert tmp_entries() == []


def test_the_pack_holds_only_the_range_not_what_the_clone_has(w: World) -> None:
    old, new = lagging(w, 2)
    m = refreshed(w)
    got = run_transfer(w, m, new[-1])
    idx = w.clone / f".git/objects/pack/pack-{got.pack}.idx"
    out = plain_git(["show-index"], cwd=w.tmp, home=w.home, stdin=idx.read_bytes())
    # 2 commits + 2 trees + the 2 new blobs; the three old blobs and commits are excluded
    assert len(out.stdout.decode().splitlines()) <= 8


def test_a_clone_that_already_has_the_commit_is_left_alone(w: World) -> None:
    old, _ = lagging(w)
    m = refreshed(w)
    before = pack_dir_entries(w)
    got = run_transfer(w, m, old[-1])
    assert got.already_present and got.pack is None
    assert pack_dir_entries(w) == before


def test_a_second_transfer_is_a_no_op(w: World) -> None:
    _, new = lagging(w)
    m = refreshed(w)
    run_transfer(w, m, new[-1])
    again = run_transfer(w, m, new[-1])
    assert again.already_present


def test_a_clone_with_no_shared_tips_gets_the_whole_history(w: World) -> None:
    _, new = lagging(w)
    for ref in cg(w, ["for-each-ref", "--format=%(refname)"]).split():
        cg(w, ["update-ref", "--no-deref", "-d", ref])
    m = refreshed(w)
    got = run_transfer(w, m, new[-1])
    assert got.present_tips == 0 and got.commits == 5
    assert cg(w, ["rev-list", "--objects", "--missing=print", new[-1]]).count("?") == 0


def test_a_planted_ref_for_a_commit_the_clone_lacks_fails_cleanly(w: World) -> None:
    old, new = lagging(w)
    m = refreshed(w)
    (w.clone / ".git/refs/heads/liar").write_text(new[0] + "\n")  # claims a commit it lacks
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, new[-1])
    assert e.value.code == "create_failed"
    assert tmp_entries() == []


def test_a_commit_known_to_the_remote_but_not_the_mirror_is_fetched_by_id(w: World) -> None:
    w.build(2)
    m = refreshed(w)
    sha = w.commit("late")
    w.g(["push", "-q", "origin", "HEAD:main"])  # after the mirror's refresh
    assert not packs.clone_has_commit(m.git, m.path, sha)
    got = run_transfer(w, m, sha)
    assert got.pack
    assert packs.clone_has_commit(m.git, str(w.clone / ".git"), sha)


def test_a_commit_the_remote_does_not_have_is_fetch_failed(w: World) -> None:
    w.build(2)
    m = refreshed(w)
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, "1" * 40)
    assert e.value.code == "fetch_failed"


@pytest.mark.parametrize("bad", ["", "abc", "A" * 40, "--upload-pack=x", "1" * 41])
def test_a_start_that_is_not_a_full_id_is_refused(w: World, bad: str) -> None:
    w.build(1)
    m = refreshed(w)
    with pytest.raises(ValueError):
        run_transfer(w, m, bad)


def test_a_pack_over_the_size_limit_installs_nothing(w: World) -> None:
    _, new = lagging(w)
    m = refreshed(w)
    before = pack_dir_entries(w)
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, new[-1], max_pack_bytes=100)
    assert e.value.code == "create_failed"
    assert pack_dir_entries(w) == before and tmp_entries() == []


def test_a_failed_rev_list_never_becomes_a_pack(w: World) -> None:
    rec = Recorder(fail="--no-walk=unsorted")
    w = World(w.tmp, w.home, gateway=rec.gateway())
    _, new = lagging(w)
    m = refreshed(w)
    before = pack_dir_entries(w)
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, new[-1])
    assert e.value.code == "create_failed"
    assert not any("pack-objects" in c[0] for c in rec.calls)
    assert pack_dir_entries(w) == before and tmp_entries() == []


def test_a_failed_pack_objects_installs_nothing(w: World) -> None:
    rec = Recorder(fail="pack-objects")
    w = World(w.tmp, w.home, gateway=rec.gateway())
    _, new = lagging(w)
    m = refreshed(w)
    before = pack_dir_entries(w)
    with pytest.raises(WtctlError):
        run_transfer(w, m, new[-1])
    assert pack_dir_entries(w) == before


def test_every_command_against_the_clone_runs_with_protocols_none(w: World) -> None:
    rec = Recorder()
    w = World(w.tmp, w.home, gateway=rec.gateway())
    _, new = lagging(w)
    m = refreshed(w)
    rec.calls.clear()
    run_transfer(w, m, new[-1])
    clone = str(w.clone / ".git")
    on_clone = [c for c in rec.calls if f"--git-dir={clone}" in c[0]]
    assert on_clone
    for argv, env, _cwd in on_clone:
        assert env["GIT_ALLOW_PROTOCOL"] == "none", argv
        assert not {"fetch", "clone", "push"} & set(argv)
    network = [c for c in rec.calls if c[1]["GIT_ALLOW_PROTOCOL"] != "none"]
    assert network and all(f"--git-dir={m.path}" in c[0] for c in network)
    assert all("fetch" in c[0] for c in network)
    pack_objs = [c for c in rec.calls if "pack-objects" in c[0]]
    assert pack_objs and all(f"--git-dir={m.path}" in c[0] for c in pack_objs)
    assert not any("--thin" in c[0] or "--fix-thin" in c[0] for c in rec.calls)


def test_the_pack_is_indexed_outside_any_repository(w: World) -> None:
    rec = Recorder()
    w = World(w.tmp, w.home, gateway=rec.gateway())
    _, new = lagging(w)
    m = refreshed(w)
    rec.calls.clear()
    run_transfer(w, m, new[-1])
    ((argv, env, cwd),) = [c for c in rec.calls if "index-pack" in c[0]]
    assert "--fsck-objects" in argv and not any(a.startswith("--git-dir") for a in argv)
    assert env["GIT_CEILING_DIRECTORIES"] == os.path.dirname(cwd)
    assert "--strict" not in argv and "--fix-thin" not in argv


# -- planted promisor remote (SAFE2-2) -----------------------------------------------------------


def plant_promisor(w: World, repo: Path, marker_dir: Path) -> Path:
    marker_dir.mkdir()
    payload = w.tmp / "payload.sh"
    payload.write_text(f'#!/bin/sh\necho "$0 $*" >> {marker_dir}/hit\nexit 1\n')
    payload.chmod(0o755)
    gd = str(repo)
    for k, v in (
        ("core.repositoryformatversion", "1"),
        ("extensions.partialClone", "promo"),
        ("remote.promo.url", str(w.remote)),
        ("remote.promo.promisor", "true"),
        ("remote.promo.uploadpack", str(payload)),
    ):
        r = plain_git(["--git-dir", gd, "config", k, v], cwd=w.tmp, home=w.home)
        assert r.returncode == 0, r.stderr
    return marker_dir


def unprotected(w: World, repo: Path, args: list[str]) -> None:
    plain_git(["--git-dir", str(repo), *args], cwd=w.tmp, home=w.home,
              env={"GIT_ALLOW_PROTOCOL": "file", "GIT_NO_LAZY_FETCH": "0"})  # fmt: skip


def test_a_planted_promisor_leaves_no_marker_with_emulated_unprotected_git(w: World) -> None:
    rec = Recorder(env_override={"GIT_NO_LAZY_FETCH": "0"})  # a git build that ignores the switch
    w = World(w.tmp, w.home, gateway=rec.gateway())
    _, new = lagging(w)
    m = refreshed(w)
    markers = plant_promisor(w, w.clone / ".git", w.tmp / "markers")
    # control: unprotected git does fire the payload when an object is missing
    unprotected(w, w.clone / ".git", ["cat-file", "-p", new[-1]])
    assert (markers / "hit").exists(), "the vector is dead: the test proves nothing"
    (markers / "hit").unlink()
    try:
        run_transfer(w, m, new[-1])
    except WtctlError:
        pass  # a failure is acceptable; a marker is not
    assert not (markers / "hit").exists()
    assert all(
        c[1]["GIT_ALLOW_PROTOCOL"] == "none"
        for c in rec.calls
        if "--git-dir=" + str(w.clone / ".git") in c[0]
    )


def test_planted_shallow_alternates_and_replace_cannot_make_a_wrong_checkout(w: World) -> None:
    _, new = lagging(w)
    m = refreshed(w)
    gd = w.clone / ".git"
    (gd / "shallow").write_text(w.g(["rev-parse", "HEAD~1"]) + "\n")
    (gd / "objects/info").mkdir(exist_ok=True)
    (gd / "objects/info/alternates").write_text(str(w.tmp / "nowhere") + "\n")
    (gd / "refs/replace").mkdir(exist_ok=True)
    (gd / f"refs/replace/{new[0]}").write_text(new[-1] + "\n")
    try:
        run_transfer(w, m, new[-1])
    except WtctlError as e:
        assert e.code == "create_failed"
        return
    assert cg(w, ["rev-list", "--objects", "--missing=print", new[-1]]).count("?") == 0


# -- the clone's directories ----------------------------------


@pytest.mark.parametrize("which", ["objects", "pack"])
def test_a_symlinked_objects_or_pack_directory_is_never_written_through(
    w: World, which: str
) -> None:
    _, new = lagging(w)
    m = refreshed(w)
    decoy = w.tmp / "decoy"
    decoy.mkdir()
    target = w.clone / ".git" / ("objects" if which == "objects" else "objects/pack")
    real = w.tmp / "moved"
    target.rename(real)
    target.symlink_to(decoy)
    if which == "pack":
        pass
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, new[-1])
    assert e.value.code == "create_failed"
    assert os.listdir(decoy) == []
    assert tmp_entries() == []


def test_a_swapped_common_dir_is_an_anchor_change(w: World) -> None:
    _, new = lagging(w)
    m = refreshed(w)
    cid = common_id(w)
    (w.clone / ".git").rename(w.tmp / "old.git")
    (w.clone / ".git").mkdir()
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, new[-1], cid=cid)
    assert e.value.code == "create_failed"
    assert e.value.fields["details"]["cause"] == "anchor_changed"
    (anchor,) = e.value.fields["details"]["anchors"]
    assert anchor["anchor"] == "common_dir" and anchor["changed"] is True
    assert anchor["recorded"] == f"{cid.dev}:{cid.ino}"


def test_a_symlinked_common_dir_is_an_anchor_change(w: World) -> None:
    _, new = lagging(w)
    m = refreshed(w)
    cid = common_id(w)
    decoy = w.tmp / "decoy.git"
    (decoy / "objects/pack").mkdir(parents=True)
    (w.clone / ".git").rename(w.tmp / "old.git")
    (w.clone / ".git").symlink_to(decoy)
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, new[-1], cid=cid)
    assert e.value.code == "create_failed"
    assert os.listdir(decoy / "objects/pack") == []


def test_a_mount_boundary_on_the_pack_directory_is_refused(w: World) -> None:
    class OtherDevice(fsafe.Fs):
        pack_ino = 0

        def adjust(self, st: fsafe.FdStat) -> fsafe.FdStat:
            if st.ino == self.pack_ino:
                return fsafe.FdStat(st.dev + 1, st.ino, st.mode, st.uid, st.nlink)
            return st

    _, new = lagging(w)
    m = refreshed(w)
    fs = OtherDevice()
    fs.pack_ino = os.stat(w.clone / ".git/objects/pack").st_ino
    before = pack_dir_entries(w)
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, new[-1], fs=fs)
    assert e.value.code == "create_failed"
    assert pack_dir_entries(w) == before


# -- install_pack directly ----------------------------------


@pytest.fixture
def one_pack(w: World, tmp_path: Path) -> tuple[Path, Path, str]:
    """A valid pack of a small repository, indexed outside any repository."""
    repo = w.tmp / "src"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"],):
        assert plain_git(args, cwd=repo, home=w.home).returncode == 0
    (repo / "f").write_text("hello\n")
    plain_git(["add", "."], cwd=repo, home=w.home)
    plain_git(["commit", "-q", "-m", "one"], cwd=repo, home=w.home)
    tip = plain_git(["rev-parse", "HEAD"], cwd=repo, home=w.home).stdout.decode().strip()
    pack = w.tmp / "x.pack"
    build_pack(w, repo, tip, pack)
    git = Git(w.gateway, GIT, str(w.home))
    p = w.tmp / "P"
    p.mkdir()
    idx = p / "x.idx"
    name = packs.index_pack(git, str(pack), str(idx), p=str(p), timeout=60)
    return pack, idx, name


def empty_clone(w: World) -> Path:
    plain_git(["init", "-q", "--bare", str(w.clone)], cwd=w.tmp, home=w.home)
    return w.clone


def cid_of(path: Path) -> fsafe.Identity:
    st = os.stat(path)
    return fsafe.Identity(st.st_dev, st.st_ino)


def test_install_writes_pack_then_idx_read_only(w: World, one_pack) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    gd = empty_clone(w)
    got = packs.install_pack(
        common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
    )
    assert got.pack_new and got.idx_new
    assert sorted(os.listdir(gd / "objects/pack")) == [f"pack-{name}.idx", f"pack-{name}.pack"]
    assert (gd / f"objects/pack/pack-{name}.pack").read_bytes() == pack.read_bytes()


@pytest.mark.parametrize("bad", ["../x", "g" * 40, "A" * 40, "", "a" * 39, "a" * 40 + "/.."])
def test_a_forged_pack_name_is_refused(w: World, one_pack, bad: str) -> None:  # noqa: ANN001
    pack, idx, _ = one_pack
    gd = empty_clone(w)
    with pytest.raises(WtctlError):
        packs.install_pack(
            common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=bad
        )
    assert os.listdir(gd / "objects/pack") == []


def test_an_existing_file_of_the_same_name_is_left_as_it_is(w: World, one_pack) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    gd = empty_clone(w)
    victim = w.tmp / "victim"
    victim.write_text("keep")
    planted = gd / f"objects/pack/pack-{name}.idx"
    planted.symlink_to(victim)  # a dangling-or-not link at the final name
    got = packs.install_pack(
        common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
    )
    assert got.pack_new and not got.idx_new
    assert victim.read_text() == "keep" and planted.is_symlink()
    assert not [n for n in os.listdir(gd / "objects/pack") if n.startswith("tmp_")]


def test_a_symlink_planted_at_a_temp_name_is_not_followed(w: World, one_pack, monkeypatch) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    gd = empty_clone(w)
    victim = w.tmp / "victim"
    victim.write_text("keep")
    monkeypatch.setattr(packs.secrets, "token_hex", lambda n: "fixed")
    (gd / "objects/pack/tmp_pack_wtctl_fixed").symlink_to(victim)
    with pytest.raises(WtctlError):
        packs.install_pack(
            common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
        )
    assert victim.read_text() == "keep"


@pytest.mark.parametrize("limit", [100, 400])
def test_disk_full_mid_install_leaves_nothing_visible(w: World, one_pack, limit: int) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    assert (
        pack.stat().st_size > 100
        and idx.stat().st_size > 400 >= pack.stat().st_size
        or limit == 100
    )
    gd = empty_clone(w)
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, hard))
    try:
        with pytest.raises(WtctlError) as e:
            packs.install_pack(
                common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
            )
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
    assert e.value.code == "create_failed"
    assert os.listdir(gd / "objects/pack") == []


def test_concurrent_installs_of_the_same_pack_end_with_one_pack(w: World, one_pack) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    gd = empty_clone(w)
    errors: list[BaseException] = []
    news: list[bool] = []

    def go() -> None:
        try:
            r = packs.install_pack(
                common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
            )
            news.append(r.pack_new)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=go) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors and news.count(True) == 1
    assert sorted(os.listdir(gd / "objects/pack")) == [f"pack-{name}.idx", f"pack-{name}.pack"]
    assert plain_git(["--git-dir", str(gd), "fsck"], cwd=w.tmp, home=w.home).returncode == 0


# -- hostile packs ----------------------------------


def idx_of(w: World, pack: Path):  # noqa: ANN201
    git = Git(w.gateway, GIT, str(w.home))
    p = w.tmp / "P2"
    p.mkdir(exist_ok=True)
    return packs.index_pack(git, str(pack), str(p / "y.idx"), p=str(p), timeout=60)


def test_a_corrupt_pack_is_refused(w: World, one_pack) -> None:  # noqa: ANN001
    pack, _, _ = one_pack
    data = bytearray(pack.read_bytes())
    data[20] ^= 0xFF
    bad = w.tmp / "bad.pack"
    bad.write_bytes(bytes(data))
    with pytest.raises(WtctlError):
        idx_of(w, bad)


@pytest.mark.parametrize("cut", [0, 5, 12, -1, -20])
def test_a_truncated_pack_is_refused(w: World, one_pack, cut: int) -> None:  # noqa: ANN001
    pack, _, _ = one_pack
    data = pack.read_bytes()
    bad = w.tmp / "bad.pack"
    bad.write_bytes(data[:cut] if cut > 0 else data[:cut] if cut else b"")
    with pytest.raises(WtctlError):
        idx_of(w, bad)


def test_trailing_garbage_is_refused(w: World, one_pack) -> None:  # noqa: ANN001
    pack, _, _ = one_pack
    bad = w.tmp / "bad.pack"
    bad.write_bytes(pack.read_bytes() + b"\0" * 50)
    with pytest.raises(WtctlError):
        idx_of(w, bad)


def raw_commit(
    w: World, repo: Path, tree_entries: list[tuple[bytes, bytes, bytes]]
) -> tuple[str, list[str]]:
    """Write a tree from raw ``(mode, name, content)`` entries (no validation), a commit on it."""
    ids: list[str] = []
    body = b""
    for mode, name, content in tree_entries:
        b = plain_git(["hash-object", "-w", "--stdin"], cwd=repo, home=w.home, stdin=content)
        sha = b.stdout.decode().strip()
        ids.append(sha)
        body += mode + b" " + name + b"\0" + bytes.fromhex(sha)
    t = plain_git(
        ["hash-object", "-w", "--literally", "-t", "tree", "--stdin"],
        cwd=repo,
        home=w.home,
        stdin=body,
    )
    tree = t.stdout.decode().strip()
    c = plain_git(["commit-tree", tree, "-m", "evil"], cwd=repo, home=w.home)
    commit = c.stdout.decode().strip()
    assert c.returncode == 0, c.stderr
    return commit, [commit, tree, *ids]


def evil_repo(w: World, entries: list[tuple[bytes, bytes, bytes]]) -> tuple[Path, str, list[str]]:
    repo = w.tmp / "evil"
    repo.mkdir()
    plain_git(["init", "-q", "-b", "main"], cwd=repo, home=w.home)
    commit, ids = raw_commit(w, repo, entries)
    plain_git(["update-ref", "refs/heads/main", commit], cwd=repo, home=w.home)
    return repo, commit, ids


def pack_ids(w: World, repo: Path, ids: list[str], dest: Path) -> None:
    """A pack of exactly ``ids`` (no traversal, so git does not look inside the bad objects)."""
    r = plain_git(
        ["pack-objects", "--stdout", "-q"],
        cwd=repo,
        home=w.home,
        stdin=("\n".join(ids) + "\n").encode(),
    )
    assert r.returncode == 0, r.stderr
    dest.write_bytes(r.stdout)


BAD_TREES = {
    "gitmodules-symlink": [(b"120000", b".gitmodules", b"elsewhere")],
    "dotgit-entry": [(b"100644", b".git", b"x")],
    "dotdot-entry": [(b"100644", b"..", b"x")],
    "slash-in-name": [(b"100644", b"a/b", b"x")],
    "bad-mode": [(b"0100644", b"f", b"x")],
    "empty-name": [(b"100644", b"", b"x")],
    "unsorted-duplicates": [
        (b"100644", b"b", b"1"),
        (b"100644", b"a", b"2"),
        (b"100644", b"a", b"3"),
    ],
}


@pytest.mark.parametrize("name", sorted(BAD_TREES))
def test_objects_that_fail_fsck_are_refused_before_the_clone(w: World, name: str) -> None:
    repo, _, ids = evil_repo(w, BAD_TREES[name])
    pack = w.tmp / "evil.pack"
    pack_ids(w, repo, ids, pack)
    with pytest.raises(WtctlError) as e:
        idx_of(w, pack)
    assert e.value.code == "create_failed"


@pytest.mark.parametrize("name", ["gitmodules-symlink", "dotgit-entry", "bad-mode"])
def test_local_objects_that_fail_strict_are_refused(w: World, name: str) -> None:
    repo, tip, _ = evil_repo(w, BAD_TREES[name])
    with pytest.raises(WtctlError) as e:
        fill(w, repo, tip)
    assert e.value.code == "create_failed"


def test_a_pack_with_a_bad_gitmodules_url_is_refused_when_git_flags_it(w: World) -> None:
    mod = b'[submodule "x"]\n\tpath = x\n\turl = -u./payload\n'
    repo, _, ids = evil_repo(w, [(b"100644", b".gitmodules", mod)])
    pack = w.tmp / "evil.pack"
    pack_ids(w, repo, ids, pack)
    try:
        idx_of(w, pack)
    except WtctlError:
        return
    pytest.skip("this git's --fsck-objects accepts it")


def test_a_thin_pack_is_refused(w: World) -> None:
    repo = w.tmp / "thin"
    repo.mkdir()
    plain_git(["init", "-q", "-b", "main"], cwd=repo, home=w.home)
    (repo / "f").write_text("line\n" * 400)
    plain_git(["add", "."], cwd=repo, home=w.home)
    plain_git(["commit", "-q", "-m", "a"], cwd=repo, home=w.home)
    a = plain_git(["rev-parse", "HEAD"], cwd=repo, home=w.home).stdout.decode().strip()
    (repo / "f").write_text("line\n" * 400 + "more\n")
    plain_git(["commit", "-q", "-am", "b"], cwd=repo, home=w.home)
    b = plain_git(["rev-parse", "HEAD"], cwd=repo, home=w.home).stdout.decode().strip()
    pack = w.tmp / "thin.pack"
    build_pack(w, repo, b, pack, "--thin", stdin=f"{b}\n--not\n{a}\n")
    with pytest.raises(WtctlError):
        idx_of(w, pack)


def test_a_forged_object_in_the_mirror_never_reaches_the_clone(w: World) -> None:
    """A tree the mirror holds that fails fsck: the pack is built, the index refuses it."""
    _, new = lagging(w)
    m = refreshed(w)
    # rewrite the newest commit's tree in the mirror so it carries a .gitmodules symlink
    blob = plain_git(
        ["--git-dir", m.path, "hash-object", "-w", "--stdin"], cwd=w.tmp, home=w.home, stdin=b"x"
    )
    sha = blob.stdout.decode().strip()
    entry = b"120000 .gitmodules\0" + bytes.fromhex(sha)
    mk = ["--git-dir", m.path, "hash-object", "-w", "--literally", "-t", "tree", "--stdin"]
    r = plain_git(mk, cwd=w.tmp, home=w.home, stdin=entry)
    tree = r.stdout.decode().strip()
    commit = plain_git(["--git-dir", m.path, "commit-tree", tree, "-p", new[-1], "-m", "x"],
                       cwd=w.tmp, home=w.home).stdout.decode().strip()  # fmt: skip
    before = pack_dir_entries(w)
    try:
        run_transfer(w, m, commit)
    except WtctlError as e:
        assert e.code in ("create_failed", "fetch_failed")
    else:
        pytest.skip("this git accepts the object in --fsck-objects")
    assert pack_dir_entries(w) == before and tmp_entries() == []


# -- hardened git additions ----------------------------------


def test_only_the_negotiation_override_may_be_added(w: World) -> None:
    git = Git(w.gateway, GIT, str(w.home))
    with pytest.raises(ValueError):
        git.build(["version"], private_dir=str(w.tmp), overrides=[("core.hooksPath", "/x")])
    with pytest.raises(ValueError):
        git.build(
            ["version"], private_dir=str(w.tmp), overrides=[("fetch.negotiationAlgorithm", "a\nb")]
        )
    cmd = git.build(
        ["version"], private_dir=str(w.tmp), overrides=[("fetch.negotiationAlgorithm", "noop")]
    )
    assert "fetch.negotiationAlgorithm=noop" in cmd.argv


# -- local-source throwaway -----------------------------------------


def local_repo(w: World, n: int = 3) -> tuple[Path, list[str]]:
    """A repository with loose objects (so a test can delete one)."""
    repo = w.tmp / "local"
    repo.mkdir()
    plain_git(["init", "-q", "-b", "main"], cwd=repo, home=w.home)
    shas = []
    for i in range(n):
        (repo / f"f{i}.txt").write_text(f"v{i}\n")
        (repo / "sub").mkdir(exist_ok=True)
        (repo / "sub" / f"g{i}.txt").write_text(f"w{i}\n")
        plain_git(["add", "."], cwd=repo, home=w.home)
        plain_git(["commit", "-q", "-m", f"c{i}"], cwd=repo, home=w.home)
        shas.append(plain_git(["rev-parse", "HEAD"], cwd=repo, home=w.home).stdout.decode().strip())
    return repo, shas


def thrown(w: World) -> Path:
    return w.tmp / "throw"


def fill(w: World, repo: Path, sha: str, **kw) -> Path:  # noqa: ANN003
    git = Git(w.gateway, GIT, str(w.home))
    t = thrown(w)
    packs.fill_local_throwaway(
        git, w.cfg, clone_dir=str(repo / ".git"), throwaway=str(t), sha=sha, **kw
    )
    return t


def tg(w: World, t: Path, args: list[str]) -> str:
    r = plain_git(["--git-dir", str(t / ".git"), "--work-tree", str(t), *args], cwd=t, home=w.home)
    assert r.returncode == 0, r.stderr
    return r.stdout.decode().strip()


def test_local_full_history(w: World) -> None:
    repo, shas = local_repo(w)
    t = fill(w, repo, shas[1])
    assert tg(w, t, ["rev-parse", "HEAD"]) == shas[1]
    assert tg(w, t, ["rev-list", "--count", "HEAD"]) == "2"
    assert (t / "f1.txt").read_text() == "v1\n" and not (t / "f2.txt").exists()
    assert not (t / ".git" / "shallow").exists()
    tg(w, t, ["fsck", "--strict", "--no-dangling"])
    assert tg(w, t, ["status", "--porcelain"]) == ""
    assert tmp_entries() == []


def test_local_depth_one_is_shallow(w: World) -> None:
    repo, shas = local_repo(w)
    t = fill(w, repo, shas[2], depth=1)
    assert (t / ".git/shallow").read_text() == shas[2] + "\n"
    assert tg(w, t, ["rev-list", "--count", "HEAD"]) == "1"
    assert (t / "sub/g2.txt").read_text() == "w2\n"
    tg(w, t, ["fsck", "--no-dangling"])


@pytest.mark.parametrize("depth", [2, 50, -1])
def test_local_other_depths_are_a_usage_error(w: World, depth: int) -> None:
    repo, shas = local_repo(w)
    with pytest.raises(ValueError):
        fill(w, repo, shas[0], depth=depth)
    assert not thrown(w).exists()


def test_local_truncated_list_is_never_packed(w: World) -> None:
    repo, shas = local_repo(w)
    blob = (
        plain_git(["rev-parse", f"{shas[0]}:f0.txt"], cwd=repo, home=w.home).stdout.decode().strip()
    )
    obj = repo / ".git/objects" / blob[:2] / blob[2:]
    os.chmod(obj, 0o644)
    obj.unlink()
    rec = Recorder()
    w2 = World(w.tmp, w.home, gateway=rec.gateway())
    with pytest.raises(WtctlError) as e:
        fill(w2, repo, shas[2])
    assert e.value.code == "create_failed"
    assert not any("pack-objects" in c[0] for c in rec.calls)
    assert not thrown(w).exists() and tmp_entries() == []


def test_local_runs_every_clone_command_with_protocols_none(w: World) -> None:
    repo, shas = local_repo(w)
    rec = Recorder()
    w2 = World(w.tmp, w.home, gateway=rec.gateway())
    fill(w2, repo, shas[2])
    on_clone = [c for c in rec.calls if f"--git-dir={repo / '.git'}" in c[0]]
    assert on_clone and all(c[1]["GIT_ALLOW_PROTOCOL"] == "none" for c in on_clone)
    assert all(c[1]["GIT_ALLOW_PROTOCOL"] == "none" for c in rec.calls)
    assert not any({"fetch", "clone", "push"} & set(c[0]) for c in rec.calls)


def test_local_planted_promisor_leaves_no_marker(w: World) -> None:
    repo, shas = local_repo(w)
    blob = (
        plain_git(["rev-parse", f"{shas[0]}:f0.txt"], cwd=repo, home=w.home).stdout.decode().strip()
    )
    obj = repo / ".git/objects" / blob[:2] / blob[2:]
    os.chmod(obj, 0o644)
    obj.unlink()
    markers = plant_promisor(w, repo / ".git", w.tmp / "markers")
    unprotected(w, repo / ".git", ["cat-file", "-p", blob])
    assert (markers / "hit").exists(), "the vector is dead: the test proves nothing"
    (markers / "hit").unlink()
    rec = Recorder(env_override={"GIT_NO_LAZY_FETCH": "0"})
    w2 = World(w.tmp, w.home, gateway=rec.gateway())
    with pytest.raises(WtctlError):
        fill(w2, repo, shas[2])
    assert not (markers / "hit").exists()


def test_local_refuses_a_non_empty_throwaway_directory(w: World) -> None:
    repo, shas = local_repo(w)
    t = thrown(w)
    t.mkdir()
    (t / "keep").write_text("x")
    with pytest.raises(WtctlError):
        fill(w, repo, shas[0])
    assert (t / "keep").read_text() == "x"


def test_local_refuses_a_symlink_as_the_throwaway(w: World) -> None:
    repo, shas = local_repo(w)
    real = w.tmp / "real"
    real.mkdir()
    thrown(w).symlink_to(real)
    with pytest.raises(WtctlError):
        fill(w, repo, shas[0])
    assert os.listdir(real) == []


@pytest.mark.parametrize("sha", ["", "abc", "--x", "1" * 40])
def test_local_bad_or_unknown_commit(w: World, sha: str) -> None:
    repo, _ = local_repo(w)
    with pytest.raises((ValueError, WtctlError)):
        fill(w, repo, sha)
    assert not thrown(w).exists()


def test_local_blob_is_not_a_commit(w: World) -> None:
    repo, shas = local_repo(w)
    blob = (
        plain_git(["rev-parse", f"{shas[0]}:f0.txt"], cwd=repo, home=w.home).stdout.decode().strip()
    )
    with pytest.raises(WtctlError):
        fill(w, repo, blob)


# -- misc ----------------------------------


def test_parse_identity() -> None:
    assert packs.parse_identity("12:34") == fsafe.Identity(12, 34)
    for bad in ("", "1", "a:b", "1:2:3", "-1:2", "1: 2"):
        with pytest.raises(ValueError):
            packs.parse_identity(bad)


def test_escape_of_newline_names_for_details() -> None:
    assert fsafe.escape_for_details("/a/b\nc\x00d\x7f") == "/a/b\\x0ac\\x00d\\x7f"
    res = fsafe.DeleteResult()
    res.add(fsafe.Remaining(("d", "x\ny"), "error"))
    assert res.remaining_paths("/root") == ["/root/d/x\\x0ay"]
