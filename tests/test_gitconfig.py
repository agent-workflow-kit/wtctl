"""wtctl's own git-config parser: differential against real git, and strict on hostile input."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from tests.vectorlib import plain_git
from wtctl import gitconfig
from wtctl.gitconfig import ConfigSyntaxError, parse


def git_list(tmp: Path, home: Path, data: bytes) -> list[tuple[str, str | None]] | None:
    """``git config -f FILE --list --null`` (no includes): None when git rejects the file."""
    f = tmp / "probe.cfg"
    f.write_bytes(data)
    r = plain_git(["config", "-f", str(f), "--list", "--null"], cwd=tmp, home=home)
    if r.returncode != 0:
        return None
    out: list[tuple[str, str | None]] = []
    for rec in r.stdout.split(b"\0")[:-1]:
        key, sep, value = rec.partition(b"\n")
        out.append((key.decode(), value.decode("utf-8", "surrogateescape") if sep else None))
    return out


def _norm(value: str | None) -> str | None:
    # git changed between versions (2.34 -> 2.56) from turning an unquoted internal tab run into
    # spaces to keeping the bytes. wtctl keeps the bytes (finer-grained than old git, equal to
    # new git), so differential comparisons normalise tabs on both sides.
    return None if value is None else value.replace("\t", " ")


def _normed(rows: list[tuple[str, str | None]] | None) -> list[tuple[str, str | None]] | None:
    return None if rows is None else [(k, _norm(v)) for k, v in rows]


def ours(data: bytes) -> list[tuple[str, str | None]]:
    return [
        (e.key, None if e.value is None else e.value.decode("utf-8", "surrogateescape"))
        for e in parse(data)
    ]


BENIGN = [
    b"",
    b"# only a comment\n; another\n",
    b"[core]\n\tbare = false\n\tfilemode = true\n",
    b"[Core]\n\tHooksPath = /x/y\n",
    b'[remote "Origin"]\n\turl = https://example.test/a.git\n\tfetch = +refs/heads/*:refs/o/*\n',
    b'[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n',
    b"[core]\n\tbare\n",
    b"[core]\n\tbare =\n",
    b"[core]\nbare = true",
    b"[core]hooksPath = /tmp/h\n",
    b"[core] ; comment\n\tbare = true\n",
    b'[a "b\\"c"]\n\tk = v\n',
    b'[a "b\\\\c"]\n\tk = v\n',
    b'[a "b\\tc"]\n\tk = v\n',
    b'[a ""]\n\tk = v\n',
    b"[a.Sub]\n\tk = v\n",
    b"[a]\n\tk = one \\\n  two\n",
    b'[a]\n\tk = "quoted # not comment"\n',
    b'[a]\n\tk = "x" y "z"  # c\n',
    b"[a]\n\tk = x    y\n",
    b"[a]\n\tk = x\t\ty\n",
    b'[a]\n\tk = \\n\\t\\b\\\\\\"\n',
    b"[a]\n\tk = v\r\n\tj = w\r\n",
    b"\xef\xbb\xbf[a]\n\tk = v\n",
    b"[a]\n\tk = 1\n\tk = 2\n[A]\n\tK = 3\n",
    b"[a]\n  k = trailing   \n",
    b'[a]\n\tk = "trailing in quotes   "\n',
    b"[a]\n\tk = ;comment only\n",
    b"[include]\n\tpath = ../x\n",
    b'[includeIf "gitdir:/x/"]\n\tpath = ~/y\n',
    b"[a]\n\tk = caf\xc3\xa9\n",
    b"[a]\n\tk = \xff\xfe\n",
    b"[a-b]\n\tk-1 = v\n",
    b"[1x]\n\tk = v\n",
    b"[a]\n\tk = \\\n\n",
]


@pytest.mark.parametrize("data", BENIGN, ids=lambda b: repr(b)[:40])
def test_benign_inputs_match_git(tmp_path: Path, private_home: Path, data: bytes) -> None:
    want = git_list(tmp_path, private_home, data)
    assert want is not None, "corpus entry must be accepted by git"
    assert _normed(ours(data)) == _normed(want)


HOSTILE = [
    b"[core\n\tk = v\n",
    b'[core]\n\tk = "unterminated\n',
    b"[core]\n\t= v\n",
    b"[core]\n\t1k = v\n",
    b"k = v\n",
    b"[]\n\tk = v\n",
    b'[a "x"y]\n\tk = v\n',
    b'[a "x\n"]\n\tk = v\n',
    b"[a]\n\tk = \\q\n",
    b"[a]\n\tk v\n",
    b"[a]\n\tk # c\n",
    b"[a]\n\tk = v\0w\n",
    b"[a b]\n\tk = v\n",
    b"[a]\n\tk\0 = v\n",
    b"[a]\n\tk = a\rb\n",
    b"[a]\n\\k = v\n",
    b"[ ]\n",
    b"[a ",
    b'[a]\n\tk = "\\',
]


@pytest.mark.parametrize("data", HOSTILE, ids=lambda b: repr(b)[:40])
def test_hostile_inputs_are_rejected_when_git_rejects_them(
    tmp_path: Path, private_home: Path, data: bytes
) -> None:
    git = git_list(tmp_path, private_home, data)
    try:
        got = ours(data)
    except ConfigSyntaxError:
        return  # being stricter than git is always fine
    assert git is not None, f"git rejects {data!r} but wtctl accepted {got!r}"
    assert _normed(got) == _normed(git)


def test_unquoted_internal_whitespace_bytes_are_preserved() -> None:
    tab = ours(b"[a]\n\tk = x\t\ty\n")
    assert tab == [("a.k", "x\t\ty")]
    assert ours(b"[a]\n\tk = x\ty\n") != ours(b"[a]\n\tk = x y\n")


def test_nul_is_always_refused() -> None:
    with pytest.raises(ConfigSyntaxError):
        parse(b"[a]\n\tk = v\0\n")


def test_fuzz_never_accepts_what_git_rejects_and_never_disagrees(
    tmp_path: Path, private_home: Path
) -> None:
    rng = random.Random(1234)
    atoms = [
        b"[", b"]", b"core", b"a", b"B", b'"', b"\\", b"\n", b"\n", b"\n", b" ", b"\t", b"=",
        b"#", b";", b"k", b"K-1", b"v", b"sub", b".", b"\r\n", b"\\\n", b"\xc3\xa9", b"1",
    ]  # fmt: skip
    stricter = 0
    for _ in range(400):
        data = b"".join(rng.choice(atoms) for _ in range(rng.randint(1, 18)))
        git = git_list(tmp_path, private_home, data)
        try:
            got = ours(data)
        except ConfigSyntaxError:
            stricter += git is not None
            continue
        assert git is not None, f"git rejects {data!r}; wtctl returned {got!r}"
        assert _normed(got) == _normed(git), data
    assert stricter < 200  # sanity: the parser is not rejecting nearly everything


def test_huge_line_and_many_entries_parse_fast() -> None:
    big = b"[a]\n\tk = " + b"x" * 5_000_000 + b"\n"
    assert len(parse(big)[0].value or b"") == 5_000_000
    many = b"[a]\n" + b"".join(b"\tk%d = v\n" % i for i in range(50_000))
    assert len(parse(many)) == 50_000


def test_entry_limit_is_enforced() -> None:
    many = b"[a]\n" + b"".join(b"\tk%d = v\n" % i for i in range(100))
    with pytest.raises(ConfigSyntaxError):
        parse(many, max_entries=50)


def test_key_helpers() -> None:
    assert gitconfig.split_key("core.hookspath") == ("core", None, "hookspath")
    assert gitconfig.split_key("remote.o.rigin.url") == ("remote", "o.rigin", "url")
    assert gitconfig.include_kind("include.path") == "include"
    assert gitconfig.include_kind("includeif.gitdir:/a.path") == "includeif"
    assert gitconfig.include_kind("includeif.gitdir:/a.pathx") is None
    assert gitconfig.include_kind("core.path") is None


@pytest.mark.parametrize(
    ("key", "ignored"),
    [
        ("branch.main.remote", True),
        ("branch.feature/x.merge", True),
        ("branch.main.rebase", True),
        ("branch.main.pushremote", True),
        ("branch.main.description", True),
        ("branch.main.vscode-merge-base", True),
        ("branch.main.autosetuprebase", False),
        ("branch.main.remote.extra", False),
        ("remote.origin.gh-resolved", True),
        ("remote.origin.fetch", True),
        ("remote.origin.prune", True),
        ("remote.origin.tagopt", True),
        ("remote.origin.url", False),
        ("remote.origin.pushurl", False),
        ("remote.origin.uploadpack", False),
        ("core.hookspath", False),
        ("core.fsmonitor", False),
        ("url.x.insteadof", False),
    ],
)
def test_benign_allowlist(key: str, ignored: bool) -> None:
    assert gitconfig.is_benign(key) is ignored


def test_ignore_patterns() -> None:
    pats = gitconfig.compile_ignores(["foo.bar", "baz.*.qux", "sec.sub.name"])
    assert gitconfig.is_ignored("foo.bar", pats)
    assert not gitconfig.is_ignored("foo.baz", pats)
    assert gitconfig.is_ignored("baz.anything.qux", pats)
    assert gitconfig.is_ignored("baz.a.b.qux", pats)
    assert not gitconfig.is_ignored("baz.qux", pats)
    assert gitconfig.is_ignored("sec.sub.name", pats)
    assert not gitconfig.is_ignored("sec.Sub.name", pats)  # subsections are case-sensitive
    assert gitconfig.is_ignored("FOO.BAR".lower(), pats)
