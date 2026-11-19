"""Pack transfer into the clone and local throwaways (D54, spike H), against real git."""

from __future__ import annotations

import json
import os
import resource
import shutil
import stat
import threading
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

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
from tests.schemacheck import SCHEMAS, _registry
from tests.test_mirror import lease
from tests.vectorlib import GIT, plain_git
from wtctl import fsafe, packs
from wtctl.errors import WtctlError
from wtctl.gateway import Gateway, GitResult
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


def test_the_pack_is_indexed_in_a_throwaway_bare_repository(w: World) -> None:
    """D65: never outside a repository (git 2.34 segfaults on some packs), never in the clone."""
    rec = Recorder()
    w = World(w.tmp, w.home, gateway=rec.gateway())
    _, new = lagging(w)
    m = refreshed(w)
    rec.calls.clear()
    run_transfer(w, m, new[-1])
    ((argv, env, cwd),) = [c for c in rec.calls if "index-pack" in c[0]]
    gds = [a for a in argv if a.startswith("--git-dir=")]
    assert len(gds) == 1
    gd = gds[0].split("=", 1)[1]
    assert os.path.dirname(gd) == cwd  # a repository inside wtctl's private directory
    assert gd != str(w.clone / ".git") and gd != m.path
    assert "--fsck-objects" in argv and "--strict" not in argv and "--fix-thin" not in argv
    assert env["GIT_ALLOW_PROTOCOL"] == "none"
    assert any(
        "init" in c[0] and "--bare" in c[0] and "--template=" in c[0] and gd in c[0]
        for c in rec.calls
    )
    assert not os.path.exists(gd)  # gone with the private directory


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
    got = run_transfer(w, m, new[-1])  # the planted state does not stop the transfer
    assert not got.already_present
    # the clone, seen without the plants, holds everything the start commit reaches
    (gd / "shallow").unlink()
    (gd / "objects/info/alternates").unlink()
    (gd / f"refs/replace/{new[0]}").unlink()
    r = plain_git(
        ["--git-dir", str(gd), "rev-list", "--objects", "--missing=print", new[-1]],
        cwd=w.tmp, home=w.home, env={"GIT_NO_REPLACE_OBJECTS": "1"},
    )  # fmt: skip
    assert r.returncode == 0 and b"?" not in r.stdout


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
    """A valid pack of a small repository, indexed in a throwaway bare repository (D65)."""
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


def test_a_symlink_at_a_final_name_is_never_trusted_or_written_through(
    w: World,
    one_pack,  # noqa: ANN001
) -> None:
    pack, idx, name = one_pack
    gd = empty_clone(w)
    victim = w.tmp / "victim"
    victim.write_text("keep")
    planted = gd / f"objects/pack/pack-{name}.idx"
    planted.symlink_to(victim)
    with pytest.raises(WtctlError) as e:
        packs.install_pack(
            common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
        )
    assert e.value.code == "create_failed" and planted.name in str(e.value)
    assert victim.read_text() == "keep" and planted.is_symlink()
    assert sorted(os.listdir(gd / "objects/pack")) == [planted.name]  # pack taken back, no tmp


def test_a_garbage_index_at_the_final_name_is_a_failure(w: World, one_pack) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    gd = empty_clone(w)
    planted = gd / f"objects/pack/pack-{name}.idx"
    planted.write_bytes(b"garbage")
    with pytest.raises(WtctlError) as e:
        packs.install_pack(
            common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
        )
    assert e.value.code == "create_failed" and planted.name in str(e.value)
    assert planted.read_bytes() == b"garbage"
    assert os.listdir(gd / "objects/pack") == [planted.name]


def test_a_same_sized_index_with_another_checksum_is_a_failure(w: World, one_pack) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    gd = empty_clone(w)
    data = bytearray(idx.read_bytes())
    data[-1] ^= 1  # same size, other trailer
    (gd / f"objects/pack/pack-{name}.idx").write_bytes(bytes(data))
    with pytest.raises(WtctlError):
        packs.install_pack(
            common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
        )


def test_a_garbage_pack_at_the_final_name_is_a_failure_and_no_index_is_linked(
    w: World,
    one_pack,  # noqa: ANN001
) -> None:
    pack, idx, name = one_pack
    gd = empty_clone(w)
    planted = gd / f"objects/pack/pack-{name}.pack"
    planted.write_bytes(b"garbage")
    with pytest.raises(WtctlError) as e:
        packs.install_pack(
            common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
        )
    assert e.value.code == "create_failed" and planted.name in str(e.value)
    assert os.listdir(gd / "objects/pack") == [planted.name]


def test_an_identical_pre_existing_pair_is_accepted(w: World, one_pack) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    gd = empty_clone(w)
    kw = dict(common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name)
    packs.install_pack(**kw)  # type: ignore[arg-type]
    again = packs.install_pack(**kw)  # type: ignore[arg-type]
    assert not again.pack_new and not again.idx_new
    assert sorted(os.listdir(gd / "objects/pack")) == [f"pack-{name}.idx", f"pack-{name}.pack"]


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


def index_with_stdout(w: World, pack: Path, stdout: str) -> str:
    """Run ``index_pack`` on ``pack`` with the index-pack call's stdout replaced by ``stdout``."""

    def after(argv: list[str], kw: dict[str, object], result: GitResult) -> GitResult:
        if "index-pack" in argv:
            return GitResult(result.returncode, stdout.encode(), result.stderr)
        return result

    git = Git(Recorder(after=after).gateway(), GIT, str(w.home))
    p = w.tmp / "P3"
    p.mkdir()
    return packs.index_pack(git, str(pack), str(p / "z.idx"), p=str(p), timeout=60)


def test_index_pack_takes_only_the_first_output_line_as_the_checksum(w: World, one_pack) -> None:  # noqa: ANN001
    """Version-independent: git prints unchecked ``.gitmodules`` blob ids after the checksum."""
    pack, _, name = one_pack
    assert index_with_stdout(w, pack, f"{name}\n{'ab' * 20}\n") == name


def test_index_pack_refuses_when_only_a_later_line_is_the_pack_checksum(w: World, one_pack) -> None:  # noqa: ANN001
    pack, _, name = one_pack
    with pytest.raises(WtctlError) as e:
        index_with_stdout(w, pack, f"{'ab' * 20}\n{name}\n")
    assert e.value.code == "create_failed"


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


def gitmodules_tree_pack(w: World, dest: Path) -> None:
    """A pack of one tree naming ``.gitmodules`` whose blob is NOT in the pack (a range that does
    not touch ``.gitmodules`` has exactly this shape)."""
    repo = w.tmp / "gm"
    repo.mkdir()
    plain_git(["init", "-q", "-b", "main"], cwd=repo, home=w.home)
    absent = "d0dba77f6852e55da49a014badab3970a79d66d3"
    body = b"100644 .gitmodules\0" + bytes.fromhex(absent)
    r = plain_git(["hash-object", "-w", "--literally", "-t", "tree", "--stdin"],
                  cwd=repo, home=w.home, stdin=body)  # fmt: skip
    assert r.returncode == 0, r.stderr
    pack_ids(w, repo, [r.stdout.decode().strip()], dest)


def test_a_tree_naming_a_gitmodules_blob_that_is_not_in_the_pack_is_indexed(w: World) -> None:
    """Regression (#90): git 2.34.1 segfaulted (exit 139) in ``index-pack --fsck-objects`` outside
    a repository on any pack whose tree names ``.gitmodules`` without carrying the blob."""
    # The crash coverage applies only on git versions that crash outside a repository (2.34.x);
    # the version-independent guards are the argv/env test above and the first-line tests below.
    pack = w.tmp / "gm.pack"
    gitmodules_tree_pack(w, pack)
    name = idx_of(w, pack)
    assert len(name) == 40


def test_a_bad_gitmodules_url_in_a_pack_with_its_tree_is_still_refused(w: World) -> None:
    """The invariant behind D65: moving into a repository must not skip the fsck of the blob."""
    mod = b'[submodule "x"]\n\tpath = x\n\turl = -u./payload\n'
    repo, _, ids = evil_repo(w, [(b"100644", b".gitmodules", mod)])
    pack = w.tmp / "evil.pack"
    pack_ids(w, repo, ids, pack)
    with pytest.raises(WtctlError) as e:
        idx_of(w, pack)
    assert e.value.code == "create_failed"


def test_a_range_whose_trees_name_an_unchanged_gitmodules_transfers(w: World) -> None:
    """Regression (#90) through the whole transfer: the clone already has ``.gitmodules``; the
    range's new commits leave it alone, so their trees name a blob the pack does not carry."""
    # The crash coverage applies only on git versions that crash outside a repository (2.34.x);
    # the version-independent guards are the argv/env test and the first-line tests.
    w.build(1)
    (w.work / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n\turl = ../x.git\n')
    w.commit("gm")
    w.g(["push", "-q", "origin", "HEAD:main"])
    w.g(["pull", "-q", "origin", "main"], w.clone)
    new = [w.commit(f"n{i}") for i in range(3)]
    w.g(["push", "-q", "origin", "HEAD:main"])
    m = refreshed(w)
    got = run_transfer(w, m, new[-1])
    assert got.commits == 3
    assert cg(w, ["cat-file", "-t", new[-1]]) == "commit"
    cg(w, ["fsck", "--strict", "--no-dangling"])


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


def noop() -> None:
    return None


def prepare(w: World) -> Path:
    """What the throwaway create order does before the fill: a private directory and
    ``init --template=`` (the fill itself no longer initializes anything)."""
    t = thrown(w)
    if not t.exists():
        t.mkdir(mode=0o700)
        r = plain_git(["init", "-q", "--template=", "--", str(t)], cwd=w.tmp, home=w.home)
        assert r.returncode == 0, r.stderr
    return t


def unfilled(w: World) -> bool:
    """Nothing was checked out: the prepared directory still holds only its repository."""
    t = thrown(w)
    return os.listdir(t) == [".git"] and not (t / ".git" / "index").exists()


def fill(w: World, repo: Path, sha: str, **kw) -> Path:  # noqa: ANN003
    git = Git(w.gateway, GIT, str(w.home))
    t = prepare(w)
    packs.fill_local_throwaway(
        git,
        w.cfg,
        clone_dir=str(repo / ".git"),
        throwaway=str(t),
        sha=sha,
        verify=kw.pop("verify", noop),
        **kw,
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
    assert unfilled(w)


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
    assert unfilled(w) and tmp_entries() == []


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


def test_local_verify_runs_before_every_write_and_stops_the_fill(w: World) -> None:
    repo, shas = local_repo(w)
    for stop_at in (1, 2, 3, 4, 5):
        calls: list[int] = []

        def verify(stop_at: int = stop_at, calls: list[int] = calls) -> None:
            calls.append(1)
            if len(calls) == stop_at:
                raise WtctlError("create_failed", "the directory was replaced")

        t = thrown(w)
        if t.exists():
            shutil.rmtree(t)
        with pytest.raises(WtctlError, match="replaced"):
            fill(w, repo, shas[2], depth=1, verify=verify)
        assert len(calls) == stop_at and (t / "f2.txt").exists() is (stop_at == 5)
    ok: list[int] = []
    shutil.rmtree(thrown(w))
    fill(w, repo, shas[2], depth=1, verify=lambda: ok.append(1))
    # before the shallow file, the index, the head ref, the work tree, and at the end
    assert len(ok) == 5
    shutil.rmtree(thrown(w))
    fill(w, repo, shas[2], verify=lambda: ok.append(1))
    assert len(ok) == 10


@pytest.mark.parametrize("sha", ["", "abc", "--x", "1" * 40])
def test_local_bad_or_unknown_commit(w: World, sha: str) -> None:
    repo, _ = local_repo(w)
    with pytest.raises((ValueError, WtctlError)):
        fill(w, repo, sha)
    assert unfilled(w)


def test_local_blob_is_not_a_commit(w: World) -> None:
    repo, shas = local_repo(w)
    blob = (
        plain_git(["rev-parse", f"{shas[0]}:f0.txt"], cwd=repo, home=w.home).stdout.decode().strip()
    )
    with pytest.raises(WtctlError):
        fill(w, repo, blob)


# -- round 1 review: transfer ----------------------------------


def delete_loose(w: World, rev: str) -> str:
    blob = cg(w, ["rev-parse", rev])
    obj = w.clone / ".git/objects" / blob[:2] / blob[2:]
    assert obj.exists(), "the test needs a loose object"
    os.chmod(obj, 0o644)
    obj.unlink()
    return blob


def test_junk_refs_do_not_push_the_real_tip_out_of_the_cap(w: World) -> None:
    _, new = lagging(w)
    m = refreshed(w)
    gd = w.clone / ".git"
    blob = cg(w, ["hash-object", "-w", "--stdin"], stdin=b"junk")
    packed = gd / "packed-refs"
    old = (
        [ln for ln in packed.read_text().splitlines() if ln and ln[0] != "#"]
        if packed.exists()
        else []
    )
    junk = [f"{blob} refs/heads/aaa{i:05d}" for i in range(2100)]
    packed.write_text("# pack-refs with: peeled fully-peeled \n" + "\n".join([*old, *junk]) + "\n")
    got = run_transfer(w, m, new[-1])
    assert got.present_tips >= 1
    assert got.commits == 2  # the range excludes the clone's own tip


def test_a_clone_ref_the_mirror_does_not_have_is_not_a_bound(w: World) -> None:
    _, new = lagging(w)
    m = refreshed(w)
    tree = cg(w, ["rev-parse", "HEAD^{tree}"])
    only_here = cg(w, ["commit-tree", tree, "-m", "clone only"])
    cg(w, ["update-ref", "refs/heads/ahead", only_here])
    got = run_transfer(w, m, new[-1])
    assert got.present_tips >= 1 and got.commits == 2


def test_the_tree_fetch_uses_noop_negotiation(w: World) -> None:
    rec = Recorder()
    w = World(w.tmp, w.home, gateway=rec.gateway())
    _, new = lagging(w)
    m = refreshed(w)
    rec.calls.clear()
    run_transfer(w, m, new[-1])
    tree_fetch = [c[0] for c in rec.calls if "fetch" in c[0] and "--stdin" in c[0]]
    assert len(tree_fetch) == 1 and "fetch.negotiationAlgorithm=noop" in tree_fetch[0]


def test_a_clone_missing_a_blob_below_its_tip_is_repaired_when_no_ref_bounds_it(w: World) -> None:
    old, _ = lagging(w)
    m = refreshed(w)
    blob = delete_loose(w, f"{old[-1]}:c3.txt")
    for ref in cg(w, ["for-each-ref", "--format=%(refname)"]).split():
        cg(w, ["update-ref", "--no-deref", "-d", ref])
    got = run_transfer(w, m, old[-1])
    assert not got.already_present and got.pack
    assert cg(w, ["cat-file", "-t", blob]) == "blob"
    assert cg(w, ["rev-list", "--objects", "--missing=print", old[-1]]).count("?") == 0


def test_a_clone_missing_a_blob_below_a_bounding_tip_fails_the_final_check(w: World) -> None:
    old, _ = lagging(w)
    m = refreshed(w)
    delete_loose(w, f"{old[-1]}:c3.txt")  # refs still name the tip, so the range is empty
    before = pack_dir_entries(w)
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, old[-1])
    assert e.value.code == "create_failed"
    assert len(pack_dir_entries(w)) >= len(before)


def test_a_tampered_mirror_config_stops_the_tree_fetch(w: World, monkeypatch) -> None:  # noqa: ANN001
    rec = Recorder()
    w = World(w.tmp, w.home, gateway=rec.gateway())
    _, new = lagging(w)
    m = refreshed(w)
    monkeypatch.setattr(type(m), "ensure", lambda self, lease: None)  # would rewrite the config
    with open(Path(m.path) / "config", "a") as f:
        f.write("[core]\n\tfsmonitor = /bin/true\n")
    rec.calls.clear()
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, new[-1])
    assert e.value.code == "unsafe_repo_config"
    assert not any("fetch" in c[0] for c in rec.calls)


def test_a_fetch_that_succeeds_without_the_commit_is_fetch_failed(w: World) -> None:
    late: list[str] = []

    def fake_fetch(argv: list[str], kw: dict[str, object]) -> GitResult | None:
        if late and "fetch" in argv and late[0] in argv:
            return GitResult(0, b"", b"")
        return None

    rec = Recorder(replace=fake_fetch)
    w = World(w.tmp, w.home, gateway=rec.gateway())
    w.build(2)
    m = refreshed(w)
    late.append(w.commit("late"))
    w.g(["push", "-q", "origin", "HEAD:main"])
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, late[0])
    assert e.value.code == "fetch_failed" and late[0] in str(e.value)


def test_an_empty_root_tree_list_is_refused(w: World) -> None:
    def empty_trees(argv: list[str], kw: dict[str, object]) -> GitResult | None:
        if "--no-commit-header" in argv:
            open(str(kw["stdout_path"]), "xb").close()
            return GitResult(0, b"", b"")
        return None

    rec = Recorder(replace=empty_trees)
    w = World(w.tmp, w.home, gateway=rec.gateway())
    _, new = lagging(w)
    m = refreshed(w)
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, new[-1])
    assert e.value.code == "create_failed"
    assert not any("pack-objects" in c[0] for c in rec.calls)


def test_a_failed_tree_fetch_that_slips_past_classification_is_still_a_failure(
    w: World,
    monkeypatch,  # noqa: ANN001
) -> None:
    def failing_tree_fetch(argv: list[str], kw: dict[str, object]) -> GitResult | None:
        if "fetch" in argv and "--stdin" in argv:
            return GitResult(128, b"", b"fatal: injected failure\n")
        return None

    rec = Recorder(replace=failing_tree_fetch)
    w = World(w.tmp, w.home, gateway=rec.gateway())
    _, new = lagging(w)
    m = refreshed(w)
    monkeypatch.setattr(packs, "rejected_token_error", lambda auth, err: None)
    before = pack_dir_entries(w)
    with pytest.raises(WtctlError) as e:
        run_transfer(w, m, new[-1])
    assert e.value.code == "create_failed" and "did not succeed" in str(e.value)
    assert pack_dir_entries(w) == before


def test_missing_objects_lists_what_the_repository_lacks(w: World) -> None:
    repo, shas = local_repo(w)
    blob = (
        plain_git(["rev-parse", f"{shas[2]}:f2.txt"], cwd=repo, home=w.home).stdout.decode().strip()
    )
    obj = repo / ".git/objects" / blob[:2] / blob[2:]
    os.chmod(obj, 0o644)
    obj.unlink()
    git = Git(w.gateway, GIT, str(w.home))
    p = w.tmp / "P3"
    p.mkdir()
    assert packs.missing_objects(git, str(repo / ".git"), shas[2], p=str(p), timeout=60) == [blob]
    assert packs.missing_objects(git, str(repo / ".git"), shas[0], p=str(p), timeout=60) == []


# -- round 1 review: install ----------------------------------


def test_the_pack_is_linked_before_the_index(w: World, one_pack, monkeypatch) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    gd = empty_clone(w)
    order: list[str] = []
    real = packs._link_new

    def spy(dir_fd: int, tmp: str, final: str) -> bool:
        order.append(final)
        return real(dir_fd, tmp, final)

    monkeypatch.setattr(packs, "_link_new", spy)
    packs.install_pack(
        common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
    )
    assert order == [f"pack-{name}.pack", f"pack-{name}.idx"]


def test_a_failed_index_link_takes_back_the_pack(w: World, one_pack, monkeypatch) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    gd = empty_clone(w)
    real = packs._link_new

    def flaky(dir_fd: int, tmp: str, final: str) -> bool:
        if final.endswith(".idx"):
            raise OSError("injected")
        return real(dir_fd, tmp, final)

    monkeypatch.setattr(packs, "_link_new", flaky)
    with pytest.raises(WtctlError) as e:
        packs.install_pack(
            common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
        )
    assert e.value.code == "create_failed"
    assert os.listdir(gd / "objects/pack") == []


def test_a_file_on_another_mount_is_refused_before_it_is_written(w: World, one_pack) -> None:  # noqa: ANN001
    class RegularFilesElsewhere(fsafe.Fs):
        def adjust(self, st: fsafe.FdStat) -> fsafe.FdStat:
            if stat.S_ISREG(st.mode):
                return fsafe.FdStat(st.dev + 1, st.ino, st.mode, st.uid, st.nlink)
            return st

    pack, idx, name = one_pack
    gd = empty_clone(w)
    with pytest.raises(WtctlError) as e:
        packs.install_pack(
            common_dir=str(gd), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name,
            fs=RegularFilesElsewhere(),
        )  # fmt: skip
    assert e.value.code == "create_failed"
    assert os.listdir(gd / "objects/pack") == []


def test_a_source_that_changes_size_while_copied_is_refused(w: World, one_pack) -> None:  # noqa: ANN001
    pack, _, _ = one_pack
    gd = empty_clone(w)
    with packs.open_common_dir(str(gd), cid_of(gd)) as anchor:
        dir_fd = fsafe.open_chain(anchor, anchor.fd, ["objects", "pack"])
        try:
            size = pack.stat().st_size
            got = packs._copy_to_new_file(anchor, dir_fd, "ok", str(pack), expected_size=size)
            assert got.st_size == size
            with pytest.raises(OSError):
                packs._copy_to_new_file(anchor, dir_fd, "bad", str(pack), expected_size=size + 1)
        finally:
            os.close(dir_fd)


def test_the_common_dir_is_reached_through_its_realpath(w: World, one_pack) -> None:  # noqa: ANN001
    pack, idx, name = one_pack
    gd = empty_clone(w)
    link = w.tmp / "link.git"
    link.symlink_to(gd)
    packs.open_common_dir(str(link), cid_of(gd)).close()
    got = packs.install_pack(
        common_dir=str(link), common_id=cid_of(gd), pack=str(pack), idx=str(idx), name=name
    )
    assert got.pack_new


def test_anchor_changed_details_escape_a_newline_and_validate_against_the_schema(
    w: World,
    one_pack,  # noqa: ANN001
) -> None:
    pack, idx, name = one_pack
    gd = w.tmp / "bare\nrepo.git"
    plain_git(["init", "-q", "--bare", str(gd)], cwd=w.tmp, home=w.home)
    wrong = fsafe.Identity(1, 2)
    with pytest.raises(WtctlError) as e:
        packs.install_pack(
            common_dir=str(gd), common_id=wrong, pack=str(pack), idx=str(idx), name=name
        )
    (entry,) = e.value.fields["details"]["anchors"]
    assert "\n" not in entry["path"] and "\\x0a" in entry["path"]
    check_anchor_change(entry)


def check_anchor_change(entry: dict[str, object]) -> None:
    defs_id = json.loads((SCHEMAS / "defs.json").read_text())["$id"]
    schema = {"$ref": f"{defs_id}#/$defs/anchor_change"}
    Draft202012Validator(schema, registry=_registry()).validate(entry)


def test_anchor_changed_when_the_anchor_cannot_be_reopened_names_another_current(
    w: World,
    one_pack,  # noqa: ANN001
) -> None:
    pack, idx, name = one_pack
    gd = empty_clone(w)
    cid = cid_of(gd)
    shutil.rmtree(gd)
    with pytest.raises(WtctlError) as e:
        packs.install_pack(
            common_dir=str(gd), common_id=cid, pack=str(pack), idx=str(idx), name=name
        )
    (entry,) = e.value.fields["details"]["anchors"]
    assert entry["changed"] is True
    assert entry["current"] != entry["recorded"]
    check_anchor_change(entry)


# -- round 1 review: helpers and index checks ----------------------------------


def test_checked_refuses_a_timeout_and_an_output_limit() -> None:
    with pytest.raises(WtctlError, match="timed out"):
        packs._checked(GitResult(-9, b"", b"", timed_out=True), "git x")
    with pytest.raises(WtctlError, match="more output"):
        packs._checked(GitResult(0, b"", b"", output_limit=True), "git x")
    with pytest.raises(WtctlError, match="exit 3"):
        packs._checked(GitResult(3, b"", b"boom\n"), "git x")


def test_an_object_list_with_a_line_that_is_not_an_id_is_refused(tmp_path: Path) -> None:
    good, bad = tmp_path / "good", tmp_path / "bad"
    good.write_text(("a" * 40 + "\n") * 3)
    assert packs._sha_lines(str(good), what="git x") == 3
    for line in ("A" * 40, "a" * 39, "a" * 40 + " name", "", "zz"):
        bad.write_text(("a" * 40 + "\n") + line + "\n")
        with pytest.raises(WtctlError):
            packs._sha_lines(str(bad), what="git x")
    with pytest.raises(WtctlError):
        packs._sha_lines(str(good), what="git x", limit=2)


def test_a_pack_without_a_version_2_header_is_refused(w: World, one_pack, monkeypatch) -> None:  # noqa: ANN001
    pack, _, _ = one_pack
    monkeypatch.setattr(packs, "PACK_HEADER", b"PACK\x00\x00\x00\x03")
    with pytest.raises(WtctlError):
        idx_of(w, pack)


def test_an_index_without_a_version_2_header_is_refused(w: World, one_pack, monkeypatch) -> None:  # noqa: ANN001
    pack, _, _ = one_pack
    monkeypatch.setattr(packs, "IDX_HEADER", b"\xfftOc\x00\x00\x00\x03")
    with pytest.raises(WtctlError):
        idx_of(w, pack)


def indexing_with(w: World, rec: Recorder, pack: Path) -> str:
    git = Git(Gateway(git_runner=rec), GIT, str(w.home))
    p = w.tmp / "P4"
    p.mkdir(exist_ok=True)
    return packs.index_pack(git, str(pack), str(p / "z.idx"), p=str(p), timeout=60)


def test_an_index_pack_name_that_is_not_the_trailer_is_refused(w: World, one_pack) -> None:  # noqa: ANN001
    pack, _, name = one_pack

    def other_name(argv: list[str], kw: dict[str, object], r: GitResult) -> GitResult:
        return GitResult(r.returncode, b"a" * 40 + b"\n", r.stderr)

    assert indexing_with(w, Recorder(), pack) == name
    with pytest.raises(WtctlError):
        indexing_with(w, Recorder(after=other_name), pack)


def test_an_index_that_names_another_pack_is_refused(w: World, one_pack) -> None:  # noqa: ANN001
    pack, _, _ = one_pack

    def corrupt_idx(argv: list[str], kw: dict[str, object], r: GitResult) -> GitResult:
        if "index-pack" not in argv:
            return r
        idx = argv[argv.index("-o") + 1]
        os.chmod(idx, 0o644)
        with open(idx, "r+b") as f:
            f.seek(-40, os.SEEK_END)
            first = f.read(1)
            f.seek(-40, os.SEEK_END)
            f.write(bytes([first[0] ^ 1]))
        return r

    with pytest.raises(WtctlError):
        indexing_with(w, Recorder(after=corrupt_idx), pack)


# -- round 1 review: local throwaway ----------------------------------


def test_local_depth_one_has_no_older_objects(w: World) -> None:
    repo, shas = local_repo(w)
    t = fill(w, repo, shas[2], depth=1)
    for older in (shas[0], shas[1]):
        r = plain_git(["--git-dir", str(t / ".git"), "cat-file", "-e", older], cwd=t, home=w.home)
        assert r.returncode != 0


def test_local_full_history_of_a_shallow_source_is_refused_early(w: World) -> None:
    repo, shas = local_repo(w)
    shallow = w.tmp / "shallow"
    r = plain_git(
        ["clone", "-q", "--depth=2", f"file://{repo}", str(shallow)], cwd=w.tmp, home=w.home
    )
    assert r.returncode == 0, r.stderr
    tip = plain_git(["rev-parse", "HEAD"], cwd=shallow, home=w.home).stdout.decode().strip()
    rec = Recorder()
    w2 = World(w.tmp, w.home, gateway=rec.gateway())
    git = Git(w2.gateway, GIT, str(w.home))
    prepare(w)
    with pytest.raises(WtctlError, match="shallow") as e:
        packs.fill_local_throwaway(
            git,
            w.cfg,
            clone_dir=str(shallow / ".git"),
            throwaway=str(thrown(w)),
            sha=tip,
            verify=noop,
        )
    assert e.value.code == "create_failed"
    assert not any("pack-objects" in c[0] for c in rec.calls) and unfilled(w)
    packs.fill_local_throwaway(
        git,
        w.cfg,
        clone_dir=str(shallow / ".git"),
        throwaway=str(thrown(w)),
        sha=tip,
        depth=1,
        verify=noop,
    )
    assert (thrown(w) / ".git/shallow").read_text() == tip + "\n"


def test_the_local_fill_needs_a_prepared_real_directory(w: World) -> None:
    repo, shas = local_repo(w)
    git = Git(w.gateway, GIT, str(w.home))
    t = prepare(w)
    calls: list[str] = []

    def attempt(target: Path) -> None:
        packs.fill_local_throwaway(
            git, w.cfg, clone_dir=str(repo / ".git"), throwaway=str(target), sha=shas[2],
            verify=lambda: calls.append("verify"),
        )  # fmt: skip

    link = w.tmp / "link"
    link.symlink_to(t)
    for bad in (link, w.tmp / "missing"):
        with pytest.raises(WtctlError) as e:
            attempt(bad)
        assert e.value.code == "create_failed"
    (t / "stray").write_text("x")  # not empty apart from the repository
    with pytest.raises(WtctlError) as e:
        attempt(t)
    assert e.value.code == "create_failed" and calls == []
    assert not (t / ".git" / "index").exists()


def test_local_pack_over_the_limit_is_refused(w: World) -> None:
    repo, shas = local_repo(w)
    with pytest.raises(WtctlError) as e:
        fill(w, repo, shas[2], max_pack_bytes=100)
    assert e.value.code == "create_failed"
    assert unfilled(w) and tmp_entries() == []


def test_local_unknown_commit_runs_nothing_against_the_clone_but_the_probe(w: World) -> None:
    repo, _ = local_repo(w)
    rec = Recorder()
    w2 = World(w.tmp, w.home, gateway=rec.gateway())
    with pytest.raises(WtctlError) as e:
        fill(w2, repo, "1" * 40)
    assert e.value.code == "create_failed" and "1" * 40 in str(e.value)
    assert not any("rev-list" in c[0] or "pack-objects" in c[0] for c in rec.calls)


def test_local_head_that_is_not_the_requested_commit_is_refused(w: World) -> None:
    def wrong_head(argv: list[str], kw: dict[str, object]) -> GitResult | None:
        if "rev-parse" in argv:
            return GitResult(0, b"1" * 40 + b"\n", b"")
        return None

    repo, shas = local_repo(w)
    w2 = World(w.tmp, w.home, gateway=Recorder(replace=wrong_head).gateway())
    with pytest.raises(WtctlError) as e:
        fill(w2, repo, shas[2])
    assert e.value.code == "create_failed"


@pytest.mark.parametrize("honor", [False, True])
def test_local_read_tree_honors_filters_only_when_asked(w: World, honor: bool) -> None:
    repo, shas = local_repo(w)
    rec = Recorder()
    gw = Gateway(git_runner=rec)
    git = Git(gw, GIT, str(w.home), packs_filters())
    prepare(w)
    packs.fill_local_throwaway(
        git, w.cfg, clone_dir=str(repo / ".git"), throwaway=str(thrown(w)), sha=shas[2],
        honor_filters=honor, verify=noop,
    )  # fmt: skip
    ((_, env, _),) = [c for c in rec.calls if "read-tree" in c[0]]
    assert (FILTER_DIR in env["PATH"].split(os.pathsep)) is honor


FILTER_DIR = "/opt/wtctl-test-filter-bin"


def packs_filters():  # noqa: ANN201
    from wtctl.hardgit import FilterPolicy

    return FilterPolicy(path_dirs=(FILTER_DIR,))


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
