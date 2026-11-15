# ruff: noqa: F811
"""The mirror: creation, config, refresh, default branch (docs/interface.md, Mirror)."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from tests.mirrorlib import REPO, World
from tests.vectorlib import plain_git
from wtctl import mirror as mirror_mod
from wtctl.config import Config
from wtctl.errors import WtctlError
from wtctl.gateway import Gateway, GitResult, run_git
from wtctl.mirror import OWNED_KEYS, config_text, mirror_key, mirror_url


@pytest.fixture
def w(tmp_path: Path, private_home: Path) -> World:
    return World(tmp_path, private_home)


def keys_of(path: str) -> dict[str, str]:
    r = plain_git(["config", "--file", f"{path}/config", "--list"], cwd=Path(path), home=Path("/"))
    return dict(line.split("=", 1) for line in r.stdout.decode().splitlines())


def row(w: World, repo: str = REPO) -> dict:  # noqa: ANN401
    r = w.state.get_clone(repo)
    assert r is not None
    return r


def lease(w: World, m):  # noqa: ANN001, ANN201
    return w.state.lease(m.lease_name, wait=1)


# -- location -------------------------------------------------------------------------------------


def test_path_is_a_hash_of_the_realpath_of_the_common_dir(w: World) -> None:
    w.build()
    link = w.tmp / "link"
    link.symlink_to(w.clone / ".git")
    real = os.path.realpath(w.clone / ".git")
    want = hashlib.sha256(real.encode()).hexdigest()[:16]
    assert mirror_key(str(link)) == mirror_key(str(w.clone / ".git")) == want
    m = w.mirror()
    assert m.path.endswith(f"/wtctl/mirrors/{want}.git")


def test_creation_makes_private_directories_and_records_the_path(w: World) -> None:
    w.build()
    m = w.mirror()
    with lease(w, m) as ls:
        assert m.ensure(ls) is True
        assert m.ensure(ls) is False
    for d in (m.path, os.path.dirname(m.path)):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert row(w)["mirror_path"] == m.path


def test_config_holds_only_wtctl_keys(w: World) -> None:
    w.build()
    m = w.mirror()
    with lease(w, m) as ls:
        m.ensure(ls)
    got = keys_of(m.path)
    assert set(got) == OWNED_KEYS
    assert got["remote.origin.url"] == str(w.remote)
    assert got["extensions.partialclone"] == "origin"
    assert got["remote.origin.promisor"] == "true"
    assert got["remote.origin.partialclonefilter"] == "tree:0"
    assert got["gc.auto"] == "0" and got["core.logallrefupdates"] == "false"
    assert got["core.repositoryformatversion"] == "1" and got["core.bare"] == "true"


@pytest.mark.parametrize(
    "url", ['/tmp/a "quoted" \\ dir/x.git', "/tmp/x #y ;z/r.git", "/tmp/üni/r.git", "/tmp/a  b "]
)
def test_config_text_round_trips_awkward_urls(tmp_path: Path, private_home: Path, url: str) -> None:
    d = tmp_path / "c"
    d.mkdir()
    (d / "config").write_bytes(config_text(url))
    assert keys_of(str(d))["remote.origin.url"] == url


def test_config_text_refuses_control_characters() -> None:
    with pytest.raises(ValueError):
        config_text("/tmp/a\nb")


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://h.example/o/r", "https://h.example/o/r.git"),
        ("https://h.example/o/r/", "https://h.example/o/r.git"),
        ("https://h.example/o/r.git", "https://h.example/o/r.git"),
        ("https://h.example/o/r.git/", "https://h.example/o/r.git"),
        ("https://h.example/o/r//", "https://h.example/o/r.git"),
        ("https://h.example/o/r.GIT", "https://h.example/o/r.GIT"),
        ("ssh://git@h.example/o/r", "ssh://git@h.example/o/r"),
        ("git@h.example:o/r", "git@h.example:o/r"),
        ("/srv/r", "/srv/r"),
    ],
)
def test_https_url_is_written_with_dot_git(url: str, want: str) -> None:
    assert mirror_url(url) == want


def test_config_is_rewritten_when_it_differs_and_left_alone_when_it_does_not(w: World) -> None:
    w.build()
    m = w.mirror()
    with lease(w, m) as ls:
        m.ensure(ls)
        cfg = Path(m.path, "config")
        before = cfg.stat().st_ino
        m.ensure(ls)
        assert cfg.stat().st_ino == before
        with open(cfg, "a") as f:
            f.write('[http]\n\tproxy = http://127.0.0.1:1\n[url "x://"]\n\tinsteadOf = https://\n')
        m.ensure(ls)
    assert set(keys_of(m.path)) == OWNED_KEYS


def test_config_is_rewritten_when_the_fetch_url_changes(w: World) -> None:
    w.build()
    m = w.mirror()
    with lease(w, m) as ls:
        m.ensure(ls)
    other = w.mirror(url="https://other.example/o/r")
    with lease(w, other) as ls:
        assert other.ensure(ls) is False
    assert keys_of(m.path)["remote.origin.url"] == "https://other.example/o/r.git"


def test_config_that_is_a_symlink_is_replaced_not_followed(w: World) -> None:
    w.build()
    m = w.mirror()
    victim = w.tmp / "victim"
    victim.write_text("keep")
    with lease(w, m) as ls:
        m.ensure(ls)
        os.unlink(Path(m.path, "config"))
        os.symlink(victim, Path(m.path, "config"))
        m.ensure(ls)
    assert victim.read_text() == "keep"
    assert not os.path.islink(Path(m.path, "config"))
    assert set(keys_of(m.path)) == OWNED_KEYS


def test_a_foreign_directory_is_never_reused(w: World) -> None:
    w.build()
    m = w.mirror()
    os.makedirs(m.path)
    Path(m.path, "precious").write_text("x")
    with pytest.raises(WtctlError) as exc, lease(w, m) as ls:
        m.ensure(ls)
    assert exc.value.code == "fetch_failed" and "not empty" in exc.value.message
    assert m.path in exc.value.message and "remove or move it" in exc.value.message
    assert "not created by wtctl for this clone" in exc.value.message
    assert row(w)["mirror_path"] in (None, "")
    assert Path(m.path, "precious").read_text() == "x"


def test_a_symlinked_ancestor_of_the_state_dir_works(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = w.tmp / "real"
    real.mkdir()
    link = w.tmp / "link"
    link.symlink_to(real)
    monkeypatch.setenv("XDG_STATE_HOME", str(link / "state"))
    w2 = World(w.tmp, w.home)
    w2.build()
    m = w2.mirror()
    assert w2.refresh(m).created is True
    assert w2.refresh(m).created is False
    assert Path(real, "state", "wtctl", "mirrors", os.path.basename(m.path), "config").is_file()


def test_a_symlink_at_the_mirrors_directory_is_refused(w: World) -> None:
    w.build()
    m = w.mirror()
    elsewhere = w.tmp / "elsewhere"
    elsewhere.mkdir()
    os.makedirs(os.path.dirname(os.path.dirname(m.path)), exist_ok=True)
    os.symlink(elsewhere, os.path.dirname(m.path))
    with pytest.raises(WtctlError), lease(w, m) as ls:
        m.ensure(ls)
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize(
    "url",
    ["ext::sh -c id", "--upload-pack=x", "https://h.example", "https://h.example/", "/tmp/a\nb"],
)
def test_a_refused_url_writes_nothing(w: World, url: str) -> None:
    w.build()
    m = w.mirror(url=url)
    with pytest.raises(WtctlError) as exc, lease(w, m) as ls:
        m.ensure(ls)
    assert exc.value.code == "remote_mismatch"
    assert not os.path.lexists(m.path)
    assert row(w)["mirror_path"] in (None, "")


def test_a_refused_url_leaves_an_existing_config_alone(w: World) -> None:
    w.build()
    m = w.mirror()
    w.refresh(m)
    before = Path(m.path, "config").read_bytes()
    bad = w.mirror(url="ext::sh -c id")
    with pytest.raises(WtctlError) as exc:
        w.refresh(bad)
    assert exc.value.code == "remote_mismatch"
    assert Path(m.path, "config").read_bytes() == before


def test_a_half_initialised_recorded_mirror_is_repaired(w: World) -> None:
    import shutil

    w.build()
    m = w.mirror()
    w.refresh(m)
    for sub in ("objects", "refs"):
        shutil.rmtree(Path(m.path, sub))
    res = w.refresh(m)
    assert res.created is False and res.tips.refs
    assert Path(m.path, "objects").is_dir() and Path(m.path, "HEAD").is_file()


def test_a_recorded_mirror_without_head_is_repaired(w: World) -> None:
    w.build()
    m = w.mirror()
    w.refresh(m)
    os.unlink(Path(m.path, "HEAD"))
    assert w.refresh(m).tips.refs
    assert Path(m.path, "HEAD").is_file()


def test_a_head_that_is_not_a_regular_file_is_refused(w: World) -> None:
    w.build()
    m = w.mirror()
    w.refresh(m)
    os.unlink(Path(m.path, "HEAD"))
    os.symlink("/etc/passwd", Path(m.path, "HEAD"))
    with pytest.raises(WtctlError) as exc:
        w.refresh(m)
    assert exc.value.code == "fetch_failed" and "HEAD" in exc.value.message


def test_a_symlinked_core_directory_is_never_followed_by_a_re_init(
    w: World, tmp_path: Path
) -> None:
    import shutil

    w.build()
    m = w.mirror()
    w.refresh(m)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    shutil.rmtree(Path(m.path, "refs"))
    os.unlink(Path(m.path, "HEAD"))
    shutil.rmtree(Path(m.path, "objects"))
    os.symlink(scratch, Path(m.path, "objects"))
    with pytest.raises(WtctlError) as exc:
        w.refresh(m)
    assert exc.value.code == "fetch_failed"
    assert list(scratch.iterdir()) == []


def test_a_half_initialised_mirror_with_a_forbidden_entry_is_not_repaired(w: World) -> None:
    import shutil

    w.build()
    m = w.mirror()
    w.refresh(m)
    shutil.rmtree(Path(m.path, "objects"))
    Path(m.path, "shallow").write_text("x\n")
    with pytest.raises(WtctlError) as exc:
        w.refresh(m)
    assert exc.value.code == "fetch_failed"
    assert not Path(m.path, "objects").exists()  # no git init ran


def test_the_repr_of_a_mirror_does_not_show_the_environment(w: World) -> None:
    w.build()
    m = w.mirror(environ={"PATH": "/usr/bin", "TOKEN_VAR": "s3cr3t-value"})
    assert "s3cr3t-value" not in repr(m)


def test_an_empty_directory_is_used(w: World) -> None:
    w.build()
    m = w.mirror()
    os.makedirs(m.path)
    with lease(w, m) as ls:
        assert m.ensure(ls) is True
    assert Path(m.path, "HEAD").exists()


def test_a_symlink_at_the_mirror_path_is_refused(w: World) -> None:
    w.build()
    m = w.mirror()
    target = w.tmp / "elsewhere"
    target.mkdir()
    os.makedirs(os.path.dirname(m.path), exist_ok=True)
    os.symlink(target, m.path)
    with pytest.raises(WtctlError) as exc, lease(w, m) as ls:
        m.ensure(ls)
    assert exc.value.code == "fetch_failed"
    assert list(target.iterdir()) == []


def test_two_clones_never_store_the_same_mirror_path(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w.build()
    other = w.tmp / "clone2"
    w.g(["clone", "-q", str(w.remote), str(other)], w.tmp)
    w.record("other.example/o/r", other)
    monkeypatch.setattr(mirror_mod, "mirror_key", lambda common: "0" * 16)
    first, second = w.mirror(), w.mirror(repo="other.example/o/r", clone=other)
    assert first.path == second.path
    with lease(w, first) as ls:
        first.ensure(ls)
    with pytest.raises(WtctlError) as exc, lease(w, second) as ls:
        second.ensure(ls)
    assert exc.value.code == "fetch_failed"
    assert row(w, "other.example/o/r")["mirror_path"] in (None, "")


def test_the_lease_must_be_the_clones_own(w: World) -> None:
    w.build()
    m = w.mirror()
    with w.state.lease("clone:/somewhere/else", wait=1) as ls, pytest.raises(ValueError):
        m.ensure(ls)


def test_refresh_locked_holds_and_releases_the_clone_lease(w: World) -> None:
    w.build()
    m = w.mirror()
    seen: list[float] = []

    def runner(argv: list[str], **kw: object) -> GitResult:
        seen.append(w.state.lease_expires(m.lease_name))
        return run_git(argv, **kw)  # type: ignore[arg-type]

    w.gateway = Gateway(git_runner=runner)
    m = w.mirror()
    w.refresh(m)
    assert seen
    with pytest.raises(KeyError):
        w.state.lease_expires(m.lease_name)


# -- refresh --------------------------------------------------------------------------------------


def object_types(m) -> set[str]:  # noqa: ANN001
    r = plain_git(
        ["--git-dir", m.path, "cat-file", "--batch-all-objects", "--batch-check"],
        cwd=Path(m.path), home=Path("/"),
    )  # fmt: skip
    return {line.split()[1] for line in r.stdout.decode().splitlines()}


def test_refresh_is_treeless_and_returns_the_tips(w: World) -> None:
    shas = w.build()
    m = w.mirror()
    res = w.refresh(m)
    assert res.tips.refs == {"refs/heads/main": shas[-1]}
    assert res.created is True
    assert object_types(m) == {"commit"}
    assert w.refresh(m).created is False


def test_default_branch_is_recorded_from_ls_remote(w: World) -> None:
    w.build()
    w.g(["branch", "dev"], w.remote)
    w.g(["symbolic-ref", "HEAD", "refs/heads/dev"], w.remote)
    m = w.mirror()
    res = w.refresh(m)
    assert res.default_branch == "dev"
    assert row(w)["default_branch"] == "dev"
    assert w.g(["symbolic-ref", "HEAD"], Path(m.path)) == "refs/heads/dev"


def test_an_empty_remote_has_no_default_branch(w: World) -> None:
    w.build()
    empty = w.tmp / "empty.git"
    w.g(["init", "-q", "--bare", str(empty)], w.tmp)
    m = w.mirror(url=str(empty))
    res = w.refresh(m)
    assert res.tips.refs == {} and res.default_branch is None


def test_a_detached_remote_head_has_no_default_branch(w: World) -> None:
    shas = w.build()
    w.g(["update-ref", "--no-deref", "HEAD", shas[0]], w.remote)
    res = w.refresh(w.mirror())
    assert res.default_branch is None
    assert row(w)["default_branch"] is None


def test_branches_deleted_and_force_pushed_on_the_remote_follow(w: World) -> None:
    shas = w.build()
    w.g(["branch", "gone", shas[0]], w.remote)
    m = w.mirror()
    assert set(w.refresh(m).tips.refs) == {"refs/heads/main", "refs/heads/gone"}
    w.g(["branch", "-D", "gone"], w.remote)
    w.g(["update-ref", "refs/heads/main", shas[0]], w.remote)
    assert w.refresh(m).tips.refs == {"refs/heads/main": shas[0]}


def test_tags_are_not_fetched(w: World) -> None:
    w.build()
    w.g(["tag", "v1"], w.remote)
    m = w.mirror()
    w.refresh(m)
    assert w.g(["tag", "-l"], Path(m.path)) == ""


def test_branch_names_differing_in_a_non_utf8_byte_stay_distinct(w: World) -> None:
    shas = w.build()
    w.g(["update-ref", "refs/heads/b\udcfe", shas[0]], w.remote)
    w.g(["update-ref", "refs/heads/b\udcff", shas[1]], w.remote)
    res = w.refresh(w.mirror())
    assert len(res.tips.refs) == 3
    assert {shas[0], shas[1]} <= res.tips.shas


def test_a_remote_head_on_a_non_utf8_branch_gives_no_default_branch(w: World) -> None:
    w.build()
    w.g(["update-ref", "refs/heads/b\udcff", w.g(["rev-parse", "main"], w.remote)], w.remote)
    w.g(["symbolic-ref", "HEAD", "refs/heads/b\udcff"], w.remote)
    m = w.mirror()
    with lease(w, m) as ls:
        m.ensure(ls)
    head_before = w.g(["symbolic-ref", "HEAD"], Path(m.path))
    res = w.refresh(m)
    assert res.default_branch is None
    assert w.g(["symbolic-ref", "HEAD"], Path(m.path)) == head_before
    assert row(w)["default_branch"] is None
    assert any("UTF-8" in x for x in w.warnings)


def test_hostile_branch_names_are_data(w: World) -> None:
    shas = w.build()
    for name in ["--upload-pack=touch", "-oProxyCommand=x", "üñí", "a--b", "x;y"]:
        w.g(["update-ref", f"refs/heads/{name}", shas[0]], w.remote)
    m = w.mirror()
    res = w.refresh(m)
    assert len(res.tips.refs) == 6
    assert "refs/heads/--upload-pack=touch" in res.tips.refs


def test_many_branches(w: World) -> None:
    shas = w.build()
    stdin = "".join(f"create refs/heads/b{i:05d} {shas[0]}\n" for i in range(3000))
    r = plain_git(["update-ref", "--stdin"], cwd=w.remote, home=w.home, stdin=stdin.encode(),
                  env={"GIT_DIR": str(w.remote)})  # fmt: skip
    assert r.returncode == 0, r.stderr
    res = w.refresh(w.mirror())
    assert len(res.tips.refs) == 3001


# -- the remote's text is data --------------------------------------------------------------------


def with_ls_remote(w: World, stdout: bytes):  # noqa: ANN201
    def runner(argv: list[str], **kw: object) -> GitResult:
        if "ls-remote" in argv:
            return GitResult(0, stdout, b"")
        return run_git(argv, **kw)  # type: ignore[arg-type]

    w.gateway = Gateway(git_runner=runner)


@pytest.mark.parametrize(
    "out",
    [
        b"ref: refs/heads/../../x\tHEAD\n",
        b"ref: refs/tags/main\tHEAD\n",
        b"ref: refs/heads/nope\tHEAD\n",  # not a branch the mirror holds
        b"ref: refs/heads/ma in\tHEAD\n",
        b"ref: refs/heads/main\tOTHER\n",
        b"ref: refs/heads/\xff\xfe\tHEAD\n",
        b"ref: -x\tHEAD\n",
        b"ref: refs/heads/main\x00\tHEAD\n",
        b"ref: refs/heads/main lock\tHEAD\n",
        b"garbage\n" * 100,
        b"",
    ],
)
def test_hostile_symref_output_gives_no_default_branch(w: World, out: bytes) -> None:
    w.build()
    with_ls_remote(w, out)
    m = w.mirror()
    with lease(w, m) as ls:
        m.ensure(ls)
    head_before = w.g(["symbolic-ref", "HEAD"], Path(m.path))
    res = w.refresh(m)
    assert res.default_branch is None
    assert w.g(["symbolic-ref", "HEAD"], Path(m.path)) == head_before


def test_a_symref_to_a_held_branch_is_believed(w: World) -> None:
    w.build()
    with_ls_remote(w, b"ref: refs/heads/main\tHEAD\n" + b"0" * 40 + b"\tHEAD\n")
    assert w.refresh(w.mirror()).default_branch == "main"


# -- failures -------------------------------------------------------------------------------------


def failing(w: World, results: list[GitResult], needle: str = "fetch") -> list[int]:
    calls = [0]

    def runner(argv: list[str], **kw: object) -> GitResult:
        if needle in argv:
            calls[0] += 1
            if results:
                return results.pop(0)
        return run_git(argv, **kw)  # type: ignore[arg-type]

    w.gateway = Gateway(git_runner=runner)
    return calls


def test_a_missing_remote_is_fetch_failed(w: World) -> None:
    w.build()
    m = w.mirror(url=str(w.tmp / "nowhere.git"))
    with pytest.raises(WtctlError) as exc:
        w.refresh(m)
    assert exc.value.code == "fetch_failed"


def test_lock_contention_is_retried(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    w.build()
    monkeypatch.setattr("time.sleep", lambda s: None)
    lock = GitResult(1, b"", b"error: cannot lock ref 'refs/heads/main': is at x\n")
    calls = failing(w, [lock, lock])
    res = w.refresh(w.mirror())
    assert calls[0] == 3 and res.tips.refs


def test_lock_contention_that_persists_fails(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    w.build()
    monkeypatch.setattr("time.sleep", lambda s: None)
    lock = GitResult(1, b"", b"error: cannot lock ref 'refs/heads/main': is at x\n")
    failing(w, [lock] * 10)
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror())
    assert exc.value.code == "fetch_failed"


def test_prune_incomplete_is_classified(w: World) -> None:
    w.build()
    failing(w, [GitResult(0, b"", b"error: could not delete references: boom\n")])
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror())
    assert exc.value.code == "fetch_failed"
    assert exc.value.fields["details"] == {"cause": "prune_incomplete"}
    assert "could not delete" in exc.value.message


def test_a_timeout_names_its_cause(w: World) -> None:
    w.build()
    failing(w, [GitResult(-9, b"", b"", timed_out=True)])
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror())
    assert exc.value.fields["details"] == {"cause": "timeout"}


def test_a_refused_login_is_auth_required(w: World) -> None:
    w.build()
    failing(w, [GitResult(128, b"", b"fatal: Authentication failed for 'x'\n")])
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror())
    assert exc.value.code == "auth_required"


def test_a_full_disk_fails_the_refresh_and_yields_no_tips(w: World) -> None:
    w.build()
    failing(w, [GitResult(128, b"", b"fatal: write error: No space left on device\n")])
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror())
    assert exc.value.code == "fetch_failed" and "No space left" in exc.value.message


def test_a_failed_default_branch_listing_fails_the_refresh(w: World) -> None:
    w.build()
    failing(w, [GitResult(128, b"", b"fatal: unable to access\n")], needle="ls-remote")
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror())
    assert exc.value.code == "fetch_failed"


@pytest.mark.parametrize("url", ["--upload-pack=x", "ext::sh -c id", "http://h.example/o/r"])
def test_an_unusable_url_is_remote_mismatch_and_runs_nothing(w: World, url: str) -> None:
    w.build()
    ran: list[list[str]] = []

    def runner(argv: list[str], **kw: object) -> GitResult:
        ran.append(argv)
        return run_git(argv, **kw)  # type: ignore[arg-type]

    w.gateway = Gateway(git_runner=runner)
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror(url=url))
    assert exc.value.code == "remote_mismatch"
    assert not any("fetch" in a for a in ran)


def plant_after_ensure(w: World, edit) -> list[list[str]]:  # noqa: ANN001
    """Edit the mirror's config just before the first git command that follows ensure."""
    ran: list[list[str]] = []
    state = {"done": False}

    def runner(argv: list[str], **kw: object) -> GitResult:
        if not state["done"] and any(a == "for-each-ref" for a in argv):
            state["done"] = True
            cfg = Path(mirror_mod.mirror_path(str(w.clone / ".git")), "config")
            cfg.write_bytes(edit(cfg.read_bytes()))
        ran.append(argv)
        return run_git(argv, **kw)  # type: ignore[arg-type]

    w.gateway = Gateway(git_runner=runner)
    return ran


@pytest.mark.parametrize(
    "edit",
    [
        lambda b: b + b'[remote "origin"]\n\turl = /tmp/elsewhere\n',
        lambda b: b.replace(b"promisor = true", b"promisor = false"),
        lambda b: b.replace(b"tree:0", b"blob:none"),
        lambda b: b.replace(b"gc]\n\tauto = 0", b"gc]\n\tauto = 1"),
    ],
    ids=["second-url", "promisor-false", "filter-changed", "gc-auto"],
)
def test_a_config_changed_after_ensure_is_refused_before_any_network_git(w: World, edit) -> None:  # noqa: ANN001
    w.build()
    w.refresh(w.mirror())
    ran = plant_after_ensure(w, edit)
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror())
    assert exc.value.code == "unsafe_repo_config"
    assert not any(a in ("fetch", "ls-remote") for argv in ran for a in argv)


@pytest.mark.parametrize(
    "entry", ["shallow", "commondir", "info/grafts", "objects/info/alternates"]
)
def test_a_mirror_with_a_forbidden_entry_is_not_refreshed(w: World, entry: str) -> None:
    w.build()
    m = w.mirror()
    w.refresh(m)
    p = Path(m.path, entry)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x\n")
    with pytest.raises(WtctlError) as exc:
        w.refresh(m)
    assert exc.value.code == "fetch_failed" and entry in exc.value.message


def test_timeouts_first_refresh_uses_clone_timeout_later_ones_network_timeout(w: World) -> None:
    w.build()
    w.cfg = Config(network_timeout="7s", clone_timeout="90s")
    seen: list[tuple[str, float]] = []

    def runner(argv: list[str], **kw: object) -> GitResult:
        for name in ("fetch", "ls-remote"):
            if name in argv:
                seen.append((name, kw["timeout"]))  # type: ignore[arg-type]
        return run_git(argv, **kw)  # type: ignore[arg-type]

    w.gateway = Gateway(git_runner=runner)
    m = w.mirror()
    w.refresh(m)
    w.refresh(m)
    assert seen == [("fetch", 90), ("ls-remote", 7), ("fetch", 7), ("ls-remote", 7)]


# -- over https (loopback capture server) ---------------------------------------------------------

from tests.test_credentials_git import TOKEN, Capture, server, trusting_gateway  # noqa: E402,F401
from wtctl.config import HostConfig  # noqa: E402


def https_world(w: World, server: Capture, *, token: bool) -> tuple[str, dict[str, str]]:
    w.build()
    url = f"https://127.0.0.1:{server.port}/org/repo"
    w.gateway = trusting_gateway(server.cert)
    hosts = {f"127.0.0.1:{server.port}": HostConfig(token_env="T")} if token else {}
    w.cfg = Config(hosts=hosts)
    return url, {"PATH": "/usr/bin:/bin", "T": TOKEN}


def test_https_refresh_sends_the_scoped_token_to_the_dot_git_url(w: World, server: Capture) -> None:
    url, environ = https_world(w, server, token=True)
    m = w.mirror(url=url, environ=environ)
    with pytest.raises(WtctlError):
        w.refresh(m)  # the capture server answers 404
    assert keys_of(m.path)["remote.origin.url"] == url + ".git"
    assert server.requests and all(r["path"].startswith("/org/repo.git/") for r in server.requests)
    assert all(r.get("authorization", "").startswith("Basic ") for r in server.requests)


def test_a_rejected_token_is_named(w: World, server: Capture) -> None:
    url, environ = https_world(w, server, token=True)
    server.always_401 = True
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror(url=url, environ=environ))
    assert exc.value.code == "auth_required"
    assert f"rejected the configured token for 127.0.0.1:{server.port}" in exc.value.message
    assert TOKEN not in exc.value.message


def test_a_missing_token_is_not_called_rejected(w: World, server: Capture) -> None:
    url, environ = https_world(w, server, token=False)
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror(url=url, environ=environ))
    assert exc.value.code == "auth_required"
    assert "rejected" not in exc.value.message
    assert not any("authorization" in r for r in server.requests)


def test_a_planted_proxy_in_the_mirror_is_removed_before_a_token_is_sent(
    w: World, server: Capture
) -> None:
    url, environ = https_world(w, server, token=True)
    m = w.mirror(url=url, environ=environ)
    with lease(w, m) as ls:
        m.ensure(ls)
    with open(Path(m.path, "config"), "a") as f:
        f.write(
            '[http]\n\tproxy = http://127.0.0.1:9\n[url "http://evil.invalid/"]\n\tinsteadOf = x\n'
        )
    with pytest.raises(WtctlError) as exc:
        w.refresh(m)
    assert exc.value.code != "unsafe_repo_config"  # ensure() rewrote the file first
    assert set(keys_of(m.path)) == OWNED_KEYS
    assert server.requests  # the request went to the real server, not a planted proxy


def test_a_stalled_server_times_out_with_its_cause(w: World, server: Capture) -> None:
    url, environ = https_world(w, server, token=False)
    w.cfg = Config(clone_timeout="2s", network_timeout="2s")
    server.stall = True
    with pytest.raises(WtctlError) as exc:
        w.refresh(w.mirror(url=url, environ=environ))
    assert exc.value.code == "fetch_failed"
    assert exc.value.fields["details"] == {"cause": "timeout"}
