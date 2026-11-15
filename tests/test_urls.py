"""URL normalization, repo id grammar and the clone fetch URL rules (docs/interface.md)."""

from __future__ import annotations

import pytest

from wtctl import urls
from wtctl.errors import WtctlError

REPO = "github.com/org/repo"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # HTTPS
        ("https://github.com/org/repo", "github.com/org/repo"),
        ("https://github.com/org/repo.git", "github.com/org/repo"),
        ("https://github.com/org/repo/", "github.com/org/repo"),
        ("https://github.com/org/repo.git/", "github.com/org/repo"),
        ("https://GitHub.COM/org/Repo.git", "github.com/org/Repo"),
        ("HTTPS://github.com/org/repo", "github.com/org/repo"),
        ("https://user@github.com/org/repo.git", "github.com/org/repo"),
        ("https://user:secret@github.com/org/repo.git", "github.com/org/repo"),
        ("https://x-access-token:abc@github.com:443/org/repo", "github.com/org/repo"),
        ("https://github.com:443/org/repo", "github.com/org/repo"),
        ("https://git.example.test:8443/org/repo", "git.example.test:8443/org/repo"),
        # GitLab: nested groups, .git suffix
        ("https://gitlab.com/grp/sub/proj.git", "gitlab.com/grp/sub/proj"),
        ("git@gitlab.com:grp/sub/proj.git", "gitlab.com/grp/sub/proj"),
        # SSH
        ("ssh://git@github.com/org/repo.git", "github.com/org/repo"),
        ("ssh://github.com/org/repo", "github.com/org/repo"),
        ("ssh://git@github.com:22/org/repo.git", "github.com/org/repo"),
        ("ssh://git@github.com:2222/org/repo.git", "github.com/org/repo"),  # port ignored
        ("ssh://git@GitHub.com/org/repo/", "github.com/org/repo"),
        # scp-like
        ("git@github.com:org/repo.git", "github.com/org/repo"),
        ("git@github.com:org/repo", "github.com/org/repo"),
        ("github.com:org/repo.git", "github.com/org/repo"),
        ("git@GITHUB.com:org/repo.git/", "github.com/org/repo"),
        ("git@github.com:org/repo.name.git", "github.com/org/repo.name"),
    ],
)
def test_normalization_table(url: str, expected: str) -> None:
    assert urls.normalize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        " ",
        "https://github.com/org/repo\n",
        "https://github.com/org/repo\nhttps://evil.test/x",
        "https://github.com/org/repo extra",
        "https://github.com/org/repo\t",
        "https://github.com/org/repo\x00",
        "\x1b[31mhttps://github.com/org/repo",
        # option injection through the URL
        "--upload-pack=touch /tmp/x",
        "-ohttps://github.com/org/repo",
        "-oProxyCommand=x@host:org/repo",
        "git@-oProxyCommand=x:org/repo",
        "ssh://-oProxyCommand=x/org/repo",
        "ssh://git@-oProxyCommand=x/org/repo",
        "https://-bad.test/org/repo",
        # other schemes and transports
        "ext::sh -c 'touch /tmp/x'",
        "ext::https://github.com/org/repo",
        "file:///srv/git/repo.git",
        "file://github.com/org/repo",
        "/srv/git/repo.git",
        "./org/repo",
        "../org/repo",
        "http://github.com/org/repo",
        "git://github.com/org/repo",
        "ftp://github.com/org/repo",
        "fd::17/org/repo",
        "git+ssh://git@github.com/org/repo",
        "https:/github.com/org/repo",
        "https:///org/repo",
        "https://github.com",
        "https://github.com/",
        "https://github.com//org/repo",
        "https://github.com/org//repo",
        "https://github.com/org/repo//",
        # at-sign tricks: the host is what follows the last "@"
        "https://a@b@github.com/org/repo",
        "https://github.com%40evil.test/org/repo",
        "https://evil.test\\@github.com/org/repo",
        "https://github.com\\@evil.test/org/repo",
        "https://github.com#@evil.test/org/repo",
        "https://github.com?@evil.test/org/repo",
        # query, fragment, encoding, dots
        "https://github.com/org/repo?x=1",
        "https://github.com/org/repo#frag",
        "https://github.com/org/%72epo",
        "https://github.com/org/repo%2f..",
        "https://github.com/org/../repo",
        "https://github.com/./org/repo",
        "https://github.com/org/..",
        "https://github.com/org/.",
        "https://github.com./org/repo",
        "https://github.com../org/repo",
        "https://.github.com/org/repo",
        "https://github..com/org/repo",
        # ports
        "https://github.com:/org/repo",
        "https://github.com:0443/org/repo",
        "https://github.com:99999/org/repo",
        "https://github.com:0/org/repo",
        "https://github.com:44a/org/repo",
        "ssh://git@github.com:abc/org/repo",
        "ssh://git@github.com:/org/repo",
        "https://[::1]/org/repo",
        # hosts: unicode, homographs, IDN
        "https://gіthub.com/org/repo",  # cyrillic i
        "https://github.com/org/rеpo",
        "https://ｇithub.com/org/repo",  # fullwidth g
        "https://münchen.test/org/repo",
        "git@gіthub.com:org/repo",
        "https://github。com/org/repo",  # ideographic full stop
        "https://git_hub.com/org/repo",
        # scp-like oddities
        "git@github.com:/org/repo",
        "git@github.com:org/repo?x",
        "git@github.com:",
        "git@github.com:.git",
        "git@github.com:org/../repo",
        "git@github.com:org//repo",
        "@github.com:org/repo",
        "a b@github.com:org/repo",
        "us=er@github.com:org/repo",
        "us@er@github.com:org/repo",
        ":org/repo",
        "github.com",
        "org/repo",
        "https://github.com/org/repo.git.git/..",
        # bare suffix only
        "https://github.com/.git",
        "https://github.com/org/.git",
        "ssh://git@github.com/.git",
    ],
)
def test_hostile_or_unsupported_urls_do_not_normalize(url: str) -> None:
    assert urls.normalize_url(url) is None


def test_the_host_is_what_follows_the_last_at_sign() -> None:
    assert urls.normalize_url("https://github.com@evil.test/org/repo") == "evil.test/org/repo"
    assert not urls.matches_repo("https://github.com@evil.test/org/repo", REPO)
    assert urls.url_host("https://github.com@evil.test/org/repo") == "evil.test"


def test_normalization_is_total_on_garbage() -> None:
    for junk in (
        "\ud800",
        "https://\ud800/a/b",
        "a" * 100_000,
        "https://" + "a" * 100_000 + "/x/y",
    ):
        assert urls.normalize_url(junk) is None


def test_dot_git_dropped_once_and_only_as_a_suffix() -> None:
    assert urls.normalize_url("https://github.com/org/repo.git.git") == "github.com/org/repo.git"
    assert urls.normalize_url("https://github.com/org/.github") == "github.com/org/.github"
    assert urls.normalize_url("https://github.com/org/a.gitx") == "github.com/org/a.gitx"


def test_https_default_port_is_dropped_but_other_ports_distinguish_repos() -> None:
    a = urls.normalize_url("https://h.test:8443/o/r")
    assert a == "h.test:8443/o/r"
    assert a != urls.normalize_url("https://h.test/o/r")


# -- repo id grammar ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo_id",
    [
        "github.com/org/repo",
        "git.example.test:8443/org/repo",
        "gitlab.com/a/b/c/d",
        "h.test/o/re.po",
        "h.test/o/.hidden",
        "h.test/o/-x",
        "h.test/o/a..b",
        "localhost/o/r",
    ],
)
def test_valid_repo_ids(repo_id: str) -> None:
    assert urls.validate_repo_id(repo_id) == repo_id


@pytest.mark.parametrize(
    "repo_id",
    [
        "",
        "github.com",
        "github.com/",
        "github.com//repo",
        "github.com/org/",
        "github.com/org/repo/",
        "github.com/.",
        "github.com/..",
        "github.com/org/..",
        "github.com/../repo",
        "github.com/./repo",
        "github.com/org/./repo",
        "GitHub.com/org/repo",
        "github.com/org/re po",
        "github.com/org/repo\n",
        "github.com/org/re\npo",
        "github.com/org/repo\x00",
        "github.com/org/rep%6f",
        "github.com/org/rеpo",
        "github.com:/org/repo",
        "github.com:abc/org/repo",
        "github.com:123456/org/repo",
        "-github.com/org/repo",
        "https://github.com/org/repo",
        "git@github.com:org/repo",
        "/abs/path",
        "github.com/org/repo?x",
        "github.com/org/repo#x",
        "a@github.com/org/repo",
        "github.com/org/rep\\o",
        # R7: length limits
        "a" * 64 + ".test/o/r",
        ".".join(["a" * 60] * 5) + "/o/r",
        "github.com/org/" + "r" * 201,
        # R8: a ".git" segment, any case
        "github.com/.git/repo",
        "github.com/org/.git",
        "github.com/org/.GIT/repo",
        "github.com/org/.Git",
    ],
)
def test_invalid_repo_ids_exit_2(repo_id: str) -> None:
    with pytest.raises(WtctlError) as exc:
        urls.validate_repo_id(repo_id)
    assert exc.value.exit_code == 2


def test_repo_id_helpers() -> None:
    assert urls.repo_host("git.example.test:8443/org/repo") == "git.example.test:8443"
    assert urls.repo_path("gitlab.com/a/b/c") == "a/b/c"
    assert urls.host_dirname("git.example.test:8443") == "git.example.test_8443"


# -- strip userinfo ---------------------------------------------------------------------------


def test_strip_userinfo() -> None:
    assert (
        urls.strip_userinfo("https://user:secret@github.com/org/repo.git")
        == "https://github.com/org/repo.git"
    )
    assert urls.strip_userinfo("https://github.com/org/repo") == "https://github.com/org/repo"
    assert urls.strip_userinfo("ssh://git@github.com/org/repo") == "ssh://github.com/org/repo"
    assert urls.strip_userinfo("git@github.com:org/repo.git") == "github.com:org/repo.git"
    assert urls.strip_userinfo("/srv/git/repo") == "/srv/git/repo"


def test_strip_userinfo_never_leaves_a_password_behind() -> None:
    for url in (
        "https://u:p@github.com/o/r",
        "https://u:p:q@github.com/o/r",
        "ssh://u:p@github.com/o/r",
        "https://:p@github.com/o/r",
    ):
        stripped = urls.strip_userinfo(url)
        assert "@" not in stripped and "p@" not in stripped
        assert urls.normalize_url(stripped) == "github.com/o/r"


# -- fetch URL and local repositories ------------------------------------------------------------


def test_scheme_of_and_host_of() -> None:
    assert urls.url_scheme("https://github.com/o/r") == "https"
    assert urls.url_scheme("HTTPS://github.com/o/r") == "https"
    assert urls.url_scheme("ssh://git@github.com/o/r") == "ssh"
    assert urls.url_scheme("git@github.com:o/r") == "ssh"
    assert urls.url_scheme("/srv/git/r") == "file"
    assert urls.url_scheme("file:///srv/git/r") == "file"
    assert urls.url_scheme("ext::sh -c x") is None
    assert urls.url_host("https://user:pw@GitHub.com:443/o/r") == "github.com"
    assert urls.url_host("https://h.test:8443/o/r") == "h.test:8443"
    assert urls.url_host("git@github.com:o/r") == "github.com"
    assert urls.url_host("ssh://git@h.test:2222/o/r") == "h.test"
    assert urls.url_host("/srv/git/r") is None
    assert urls.url_host("ext::sh") is None


def test_local_path_of() -> None:
    assert urls.local_path("/srv/git/r.git") == "/srv/git/r.git"
    assert urls.local_path("/srv/git//r.git/") == "/srv/git/r.git"
    assert urls.local_path("file:///srv/git/r.git") == "/srv/git/r.git"
    for bad in (
        "/srv/git/../git/r.git",
        "/srv/git/./r.git",
        "file://localhost/srv/git/r",
        "file://host/srv/git/r",
        "file:///srv/git/%72",
        "relative/path",
        "https://h.test/o/r",
        "/srv/git/r\n",
        "/srv/-x\x00",
        "ext::sh",
        "--upload-pack=x",
    ):
        assert urls.local_path(bad) is None


def test_matches_repo_by_normalization() -> None:
    assert urls.matches_repo("https://u@github.com/org/repo.git", REPO)
    assert urls.matches_repo("git@github.com:org/repo.git", REPO)
    assert not urls.matches_repo("https://github.com/org/other.git", REPO)
    assert not urls.matches_repo("https://evil.test/org/repo.git", REPO)
    assert not urls.matches_repo("https://github.com/Org/repo", REPO)  # path case is significant


def test_local_remote_matches_only_the_configured_clone_url() -> None:
    assert urls.matches_repo("/srv/git/r.git", REPO, clone_url="/srv/git/r.git")
    assert urls.matches_repo("/srv/git/r.git", REPO, clone_url="file:///srv/git/r.git")
    assert urls.matches_repo("file:///srv/git/r.git", REPO, clone_url="/srv/git/r.git")
    assert not urls.matches_repo("/srv/git/r.git", REPO)
    assert not urls.matches_repo("/srv/git/r.git", REPO, clone_url="/srv/git/other.git")
    assert not urls.matches_repo("/srv/git/r.git", REPO, clone_url="https://github.com/org/repo")
    assert not urls.matches_repo("ext::sh -c x", REPO, clone_url="ext::sh -c x")
    assert not urls.matches_repo("/srv/git/r.git/../x", REPO, clone_url="/srv/git/r.git")


def test_a_configured_forge_mirror_url_identifies_the_clone_too() -> None:
    mirror = "https://mirror.example.test/m/repo.git"
    assert urls.matches_repo("https://u@mirror.example.test/m/repo", REPO, clone_url=mirror)
    assert not urls.matches_repo("https://mirror.example.test/m/other", REPO, clone_url=mirror)
    assert not urls.matches_repo("https://evil.test/m/repo", REPO, clone_url=mirror)


def test_clone_url_for_uses_the_repo_override_then_the_host_template() -> None:
    assert urls.render_clone_url(REPO, template="https://{host}/{path}.git") == (
        "https://github.com/org/repo.git"
    )
    assert urls.render_clone_url(
        "git.example.test:8443/org/repo", template="https://{host}/{path}.git"
    ) == ("https://git.example.test:8443/org/repo.git")
    assert urls.render_clone_url(REPO, template="git@{host}:{path}.git") == (
        "git@github.com:org/repo.git"
    )


def test_rendered_clone_url_must_normalize_back_to_the_repo_id() -> None:
    with pytest.raises(WtctlError) as exc:
        urls.render_clone_url(REPO, template="https://evil.test/{path}.git")
    assert exc.value.code == "remote_mismatch"
    with pytest.raises(WtctlError):
        urls.render_clone_url(REPO, template="ext::sh -c {path}")
    with pytest.raises(WtctlError):
        urls.render_clone_url(REPO, template="-oProxyCommand={host}")


# -- review round 1 --------------------------------------------------------------------------


def test_length_limits_are_exact() -> None:
    assert urls.validate_repo_id("a" * 63 + ".test/o/r")
    assert urls.validate_repo_id("github.com/org/" + "r" * 200)
    host = ".".join(["a" * 61] * 4)  # 247 characters
    assert urls.validate_repo_id(f"{host}/o/r")
    assert urls.normalize_url("https://" + "a" * 64 + ".test/o/r") is None
    assert urls.normalize_url("https://github.com/o/" + "r" * 201) is None
    assert urls.normalize_url("https://github.com/o/" + "r" * 200) is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/.git/repo",
        "https://github.com/org/.GIT/repo",
        "https://github.com/org/.git/",
        "git@github.com:org/.git/x",
        "git@github.com:.git",
    ],
)
def test_a_dot_git_segment_does_not_normalize(url: str) -> None:
    assert urls.normalize_url(url) is None


def test_strip_userinfo_emits_a_lowercase_scheme() -> None:
    assert urls.strip_userinfo("HTTPS://u:p@github.com/o/r") == "https://github.com/o/r"
    assert urls.strip_userinfo("HTTPS://github.com/o/r") == "https://github.com/o/r"
    assert urls.strip_userinfo("SSH://git@github.com/o/r") == "ssh://github.com/o/r"


def test_local_path_needs_printable_ascii_and_accepts_an_uppercase_file_scheme() -> None:
    assert urls.local_path("FILE:///srv/git/r") == "/srv/git/r"
    assert urls.local_path("/srv/git/r\u00e9") is None
    assert urls.local_path("/srv/git/r\x80") is None
    assert urls.local_path("/srv/git/r\u202e") is None


def test_the_template_error_never_echoes_the_template() -> None:
    with pytest.raises(WtctlError) as exc:
        urls.render_clone_url(REPO, template="https://user:hunter2@evil.test/{path}.git")
    assert "hunter2" not in exc.value.message and "evil.test" not in exc.value.message
