"""Config loading: defaults, drop-ins, environment, per-repo resolution, root rules, validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator

from tests.schemacheck import SCHEMAS, _registry
from wtctl import config as cfgmod
from wtctl.config import Config, load_config, me_name, parse_duration
from wtctl.context import Runtime
from wtctl.errors import WtctlError

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config.example.yaml"


@pytest.fixture
def home(private_home: Path) -> Path:
    return private_home


@pytest.fixture(autouse=True)
def clean_wtctl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("WTCTL_"):
            monkeypatch.delenv(name)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def cfg_file(home: Path, text: str) -> str:
    return str(write(home / ".config" / "wtctl" / "config.yaml", text))


def invalid(config_path: str | None) -> WtctlError:
    with pytest.raises(WtctlError) as exc:
        load_config(config_path)
    assert exc.value.code == "config_invalid"
    assert exc.value.exit_code == 2
    return exc.value


# ---------------------------------------------------------------- defaults


def schema_defaults() -> dict[str, Any]:
    schema = json.loads((SCHEMAS / "config.json").read_text())
    return {k: v["default"] for k, v in schema["properties"].items() if "default" in v}


def test_no_config_file_gives_defaults(home: Path) -> None:
    c = load_config(None)
    assert c.root == str(home / "wtctl")
    assert c.worktree_root is None
    assert c.effective_worktree_root == str(home / "wtctl" / "worktrees")
    assert c.effective_throwaway_root == str(home / "wtctl" / "throwaway")
    assert c.effective_clone_root == str(home / "wtctl" / "clones")


def test_defaults_equal_example_yaml(home: Path) -> None:
    example = Config.model_validate(yaml.safe_load(EXAMPLE.read_text()))
    assert load_config(None) == example


def test_defaults_equal_schema_defaults(home: Path) -> None:
    assert load_config(None) == Config.model_validate(schema_defaults())


def test_example_yaml_loads_as_a_config_file(home: Path) -> None:
    assert load_config(cfg_file(home, EXAMPLE.read_text())) == load_config(None)


def test_example_yaml_validates_against_schema() -> None:
    validator = Draft202012Validator(
        json.loads((SCHEMAS / "config.json").read_text()), registry=_registry()
    )
    validator.validate(yaml.safe_load(EXAMPLE.read_text()))


def test_default_path_follows_xdg_config_home(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    write(xdg / "wtctl" / "config.yaml", "branch_prefix: x/\n")
    assert load_config(None).branch_prefix == "x/"


def test_relative_xdg_config_home_is_ignored(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    write(home / ".config" / "wtctl" / "config.yaml", "branch_prefix: y/\n")
    assert load_config(None).branch_prefix == "y/"


def test_explicit_missing_config_is_an_error(home: Path) -> None:
    err = invalid(str(home / "nope.yaml"))
    assert "nope.yaml" in err.message


def test_explicit_config_reads_dropins_from_its_directory(home: Path, tmp_path: Path) -> None:
    main = write(tmp_path / "elsewhere" / "c.yaml", "branch_prefix: a/\n")
    write(tmp_path / "elsewhere" / "config.d" / "10.yaml", "search_depth: 7\n")
    write(home / ".config" / "wtctl" / "config.d" / "10.yaml", "search_depth: 9\n")
    c = load_config(str(main))
    assert (c.branch_prefix, c.search_depth) == ("a/", 7)


# ---------------------------------------------- schema and model agree on values

CASES: list[tuple[str, Any, bool]] = [
    ("search_depth", 0, False),
    ("search_depth", 1, True),
    ("search_depth", True, False),
    ("search_depth", "3", False),
    ("auto_clone", "yes-string", False),
    ("auto_clone", True, True),
    ("network_timeout", "120s", True),
    ("network_timeout", "0s", False),
    ("network_timeout", "10", False),
    ("network_timeout", "10x", False),
    ("lease_wait", "1w", True),
    ("branch_template", "", False),
    ("branch_prefix", "", True),
    ("worktree_path", "{root}/{name}", True),
    ("worktree_path", "{root}/{id}", True),
    ("worktree_path", "{root}/{host}/{path}", False),
    ("worktree_path", "/abs/{name}", False),
    ("worktree_path", "{root}{name}", False),
    ("tombstone_days", 0, True),
    ("tombstone_days", -1, False),
    ("git_bin", "/usr/bin/git", True),
    ("git_bin", "git", False),
    ("git_bin", None, True),
    ("mask_env", ["GOOD_1"], True),
    ("mask_env", ["1bad"], False),
    ("mask_env", "GOOD", False),
    ("search_roots", ["/a", "~/b"], True),
    ("search_roots", ["rel"], False),
    ("search_roots", "/a", False),
    ("me", {"name": "al.ice@x"}, True),
    ("me", {"name": "a b"}, False),
    ("me", {"name": "a" * 65}, False),
    ("me", {"name": None}, True),
    ("me", {"nom": "x"}, False),
    ("throwaway", {"depth": 0, "max_age": "30d"}, True),
    ("throwaway", {"depth": -1}, False),
    ("throwaway", {"max_age": "30"}, False),
    ("remove", {"disposable": ["a/", "b"]}, True),
    ("remove", {"disposable": [""]}, False),
    ("remove", {"fetch_retries": 0}, True),
    ("remove", {"nested_scan_limit": 0}, False),
    ("extract", {"max_files": 0}, False),
    ("prune", {"protect_labels": ["k", "k=v"]}, True),
    ("prune", {"protect_labels": ["K"]}, False),
    ("create", {"allow_filters": ["lfs"]}, True),
    ("create", {"allow_filters": ["l/fs"]}, False),
    ("baseline", {"ignore_config_keys": ["branch.*.remote"]}, True),
    ("baseline", {"ignore_config_keys": ["nokey"]}, False),
    ("reconcile", {"missing_grace": "24h"}, True),
    ("hosts", {"github.com": {"token_env": "GH_TOKEN"}}, True),
    ("hosts", {"GitHub.com": {}}, False),
    ("hosts", {"github.com": {"token_env": "1X"}}, False),
    ("hosts", {"github.com": {"token_command": []}}, False),
    ("hosts", {"github.com": {"token_command": ["gh", "auth"]}}, True),
    ("hosts", {"github.com": {"token_user": ""}}, False),
    ("hosts", {"github.com": {"token_user": "oauth2"}}, True),
    ("hosts", {"github.com": {"token_user": "a:b"}}, False),
    ("hosts", {"github.com": {"token_user": "a b"}}, False),
    ("hosts", {"github.com": {"token_user": "é"}}, False),
    ("hosts", {"github.com": {"token_user": "a" * 256}}, True),
    ("hosts", {"github.com": {"token_user": "a" * 257}}, False),
    ("hosts", {"github.com:443": {}}, False),
    ("hosts", {"github.com:8443": {}}, True),
    ("hosts", {"h:8443": {}}, True),
    ("hosts", {"h:0": {}}, False),
    ("hosts", {"h:0443": {}}, False),
    ("hosts", {"h:65536": {}}, False),
    ("hosts", {"h:65535": {}}, True),
    ("hosts", {"github.com": {"other": 1}}, False),
    ("repos", {"github.com/o/r": {"remote": "up.stream"}}, True),
    ("repos", {"github.com/o/r": {"remote": "bad remote"}}, False),
    ("repos", {"github.com/o/../r": {}}, False),
    ("repos", {"github.com": {}}, False),
    ("repos", {"github.com/o/r": {"unknown": 1}}, False),
    ("repos", {"github.com/o/r": {"auto_clone": None}}, True),
    ("repos", {"github.com/o/r": {"clone": "rel"}}, False),
    ("repos", {"github.com/o/r": {"bootstrap": {"steps": [{"argv": ["a"], "sh": True}]}}}, True),
    (
        "repos",
        {"github.com/o/r": {"bootstrap": {"steps": [{"argv": ["a", "b"], "sh": True}]}}},
        False,
    ),
    ("repos", {"github.com/o/r": {"bootstrap": {"steps": [{"sh": True}]}}}, False),
    ("repos", {"github.com/o/r": {"bootstrap": {"steps": [{"argv": []}]}}}, False),
    ("repos", {"github.com/o/r": {"bootstrap": {"pass_env": ["A", "A"]}}}, False),
    ("repos", {"github.com/o/r": {"bootstrap": {"env": {"1": "x"}}}}, False),
    ("repos", {"github.com/o/r": {"bootstrap": {"env": {"A": 1}}}}, False),
    ("repos", {"github.com/o/r": {"remove": {"disposable": None}}}, True),
    ("repos", {"github.com/o/r": {"create": {"allow_filters": None}}}, True),
    ("hooks", {"pre-create": [{"cmd": "x", "timeout": 5}]}, True),
    ("hooks", {"pre-create": [{"cmd": "x", "async": True}]}, False),
    ("hooks", {"post-create": [{"cmd": "x", "async": True}]}, True),
    ("hooks", {"post-create": [{"cmd": ""}]}, False),
    ("hooks", {"post-create": [{"timeout": 3}]}, False),
    ("hooks", {"on-create": []}, False),
]


@pytest.mark.parametrize(("key", "value", "ok"), CASES)
def test_model_and_schema_agree(home: Path, key: str, value: Any, ok: bool) -> None:
    validator = Draft202012Validator(
        json.loads((SCHEMAS / "config.json").read_text()), registry=_registry()
    )
    assert validator.is_valid({key: value}) is ok, "schema disagrees with the case table"
    path = cfg_file(home, yaml.safe_dump({key: value}))
    if ok:
        load_config(path)
    else:
        err = invalid(path)
        assert key.split(".")[0] in err.message or key in err.message


@pytest.mark.parametrize(
    ("key", "value"), [("search_depth", 3.0), ("network_timeout", "10s\n"), ("lease_wait", "5m\n")]
)
def test_model_stricter_than_json_schema_validators(home: Path, key: str, value: Any) -> None:
    # A float integer and a trailing newline pass a Python jsonschema run; the model refuses.
    assert key in invalid(cfg_file(home, yaml.safe_dump({key: value}))).message


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("bogus: 1\n", "bogus"),
        ("remove:\n  nope: 1\n", "remove.nope"),
        ("repos:\n  github.com/o/r:\n    wat: 1\n", "wat"),
        ("hosts:\n  github.com:\n    token_usr: x\n", "token_usr"),
        ("hosts:\n  github.com:\n    token_user: 'a:b'\n", "token_user"),
        ("hosts:\n  'github.com:443': {}\n", "use the bare host"),
        ("search_depth: 0\n", "search_depth"),
        ("throwaway:\n  depth: -2\n", "throwaway.depth"),
        ("repos:\n  github.com/o/r:\n    remote: 'a b'\n", "repos.github.com/o/r.remote"),
    ],
)
def test_error_names_the_key(home: Path, text: str, needle: str) -> None:
    assert needle in invalid(cfg_file(home, text)).message


def test_error_names_the_file(home: Path) -> None:
    path = cfg_file(home, "bogus: 1\n")
    assert path in invalid(path).message


# ---------------------------------------------------------------- paths


def test_tilde_expanded_once(home: Path) -> None:
    c = load_config(cfg_file(home, "root: ~/work/wt\nsearch_roots: [~/dev, /srv/x]\n"))
    assert c.root == str(home / "work" / "wt")
    assert c.search_roots == [str(home / "dev"), "/srv/x"]


@pytest.mark.parametrize("value", ["wtctl", "~user/x", "~", "./x", "", "/a\nb", "~/a\nb"])
def test_bad_path_forms_refused(home: Path, value: str) -> None:
    assert "root" in invalid(cfg_file(home, yaml.safe_dump({"root": value}))).message


def test_path_is_normalized(home: Path) -> None:
    c = load_config(cfg_file(home, "root: ~/a//b/./c/\n"))
    assert c.root == str(home / "a" / "b" / "c")


def test_per_repo_path_keys_expand(home: Path) -> None:
    c = load_config(
        cfg_file(home, "repos:\n  github.com/o/r:\n    clone: ~/dev/r\n    worktree_root: ~/wts\n")
    )
    r = c.repo("github.com/o/r")
    assert (r.clone, r.worktree_root) == (str(home / "dev" / "r"), str(home / "wts"))


# ---------------------------------------------------------------- root rules


@pytest.mark.parametrize("key", ["root", "worktree_root", "throwaway_root"])
@pytest.mark.parametrize(
    "value",
    [
        "~/",
        "/",
        "{parent}",
        "~/.ssh",
        "~/.ssh/wt",
        "~/.local",
        "~/.local/share",
        "~/.local/share/wt",
        "~/.local/state/claude-session",
        "~/.config",
        "~/.config/wtctl",
        "~/.hidden/a/b/c",
    ],
)
def test_unsafe_roots_refused(home: Path, key: str, value: str) -> None:
    value = value.replace("{parent}", str(home.parent))
    err = invalid(cfg_file(home, yaml.safe_dump({key: value})))
    assert key in err.message


@pytest.mark.parametrize("key", ["root", "worktree_root", "throwaway_root"])
@pytest.mark.parametrize("value", ["~/wtctl", "~/work/wt", "~/visible", "/srv/wt"])
def test_safe_roots_accepted(home: Path, key: str, value: str) -> None:
    load_config(cfg_file(home, yaml.safe_dump({key: value})))


def test_clone_root_and_search_roots_are_not_root_rule_targets(home: Path) -> None:
    load_config(cfg_file(home, "clone_root: ~/.cache/clones\nsearch_roots: [~/.local/src]\n"))


def test_per_repo_worktree_root_checked(home: Path) -> None:
    err = invalid(cfg_file(home, "repos:\n  github.com/o/r:\n    worktree_root: ~/.ssh/x\n"))
    assert "repos.github.com/o/r.worktree_root" in err.message


def test_custom_xdg_homes_refused(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        d = home / "visible" / var.lower()
        monkeypatch.setenv(var, str(d))
        invalid(cfg_file(home, f"root: {d}\n"))
        invalid(cfg_file(home, f"root: {d}/inner\n"))


def test_config_home_inside_visible_dir_still_loads(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = home / "visible" / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    write(d / "wtctl" / "config.yaml", "root: ~/wtctl\n")
    load_config(None)


def test_path_dir_refused(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bindir = home / "tools" / "bin"
    bindir.mkdir(parents=True)
    monkeypatch.setenv("PATH", f"/usr/bin:{bindir}:relative:")
    invalid(cfg_file(home, f"root: {bindir}\n"))
    invalid(cfg_file(home, f"root: {bindir}/inner\n"))
    load_config(cfg_file(home, f"root: {home}/tools\n"))  # parent of a PATH dir is fine


def test_symlink_into_forbidden_dir_refused(home: Path) -> None:
    (home / ".ssh").mkdir()
    (home / "innocent").symlink_to(home / ".ssh")
    invalid(cfg_file(home, "root: ~/innocent\n"))
    invalid(cfg_file(home, "root: ~/innocent/sub\n"))


def test_dotdot_cannot_escape_the_rule(home: Path) -> None:
    invalid(cfg_file(home, "root: ~/wtctl/../.ssh\n"))
    invalid(cfg_file(home, "root: ~/wtctl/../../\n"))


def test_default_root_passes_rules_even_with_hidden_home_layout(home: Path) -> None:
    assert load_config(None).root == str(home / "wtctl")


def test_env_root_checked_and_named(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WTCTL_ROOT", "~/.ssh")
    assert "WTCTL_ROOT" in invalid(None).message


# ---------------------------------------------------------------- drop-ins


def test_dropins_merge_in_lexical_order(home: Path) -> None:
    base = home / ".config" / "wtctl"
    write(
        base / "config.yaml", "me: {name: base}\nsearch_depth: 2\nhosts:\n  a.com: {token_env: A}\n"
    )
    write(base / "config.d" / "20-b.yaml", "search_depth: 20\nhosts:\n  b.com: {token_env: B}\n")
    write(base / "config.d" / "10-a.yaml", "search_depth: 10\nme: {name: dropin}\n")
    c = load_config(None)
    assert c.search_depth == 20
    assert c.me.name == "dropin"
    assert set(c.hosts) == {"a.com", "b.com"}


def test_maps_merge_deeply(home: Path) -> None:
    base = home / ".config" / "wtctl"
    write(base / "config.yaml", "repos:\n  h.com/o/r: {remote: up, base: main}\n")
    write(base / "config.d" / "a.yaml", "repos:\n  h.com/o/r: {base: dev}\n  h.com/o/s: {}\n")
    c = load_config(None)
    assert (c.repo("h.com/o/r").remote, c.repo("h.com/o/r").base) == ("up", "dev")
    assert "h.com/o/s" in c.repos


def test_lists_replace(home: Path) -> None:
    base = home / ".config" / "wtctl"
    write(base / "config.yaml", "search_roots: [/a, /b]\nremove: {disposable: [x, y]}\n")
    write(base / "config.d" / "a.yaml", "search_roots: [/c]\nremove: {disposable: [z]}\n")
    c = load_config(None)
    assert c.search_roots == ["/c"]
    assert c.remove.disposable == ["z"]


def test_hooks_append_across_dropins(home: Path) -> None:
    base = home / ".config" / "wtctl"
    write(base / "config.yaml", "hooks:\n  post-create: [{cmd: one}]\n  pre-remove: [{cmd: p}]\n")
    write(base / "config.d" / "a.yaml", "hooks:\n  post-create: [{cmd: two}, {cmd: three}]\n")
    write(base / "config.d" / "b.yaml", "hooks:\n  post-create: [{cmd: four, async: true}]\n")
    c = load_config(None)
    assert [h.cmd for h in c.hooks.post_create] == ["one", "two", "three", "four"]
    assert c.hooks.post_create[3].async_ is True
    assert [h.cmd for h in c.hooks.pre_remove] == ["p"]
    assert c.hooks.pre_create == []


def test_null_in_dropin_resets_scalar(home: Path) -> None:
    base = home / ".config" / "wtctl"
    write(base / "config.yaml", "git_bin: /usr/bin/git\n")
    write(base / "config.d" / "a.yaml", "git_bin: null\n")
    assert load_config(None).git_bin is None


def test_only_yaml_visible_files_are_dropins(home: Path) -> None:
    d = home / ".config" / "wtctl" / "config.d"
    write(d / "a.yaml", "search_depth: 4\n")
    write(d / "b.yml", "search_depth: 5\n")
    write(d / "c.yaml.bak", "search_depth: 6\n")
    write(d / ".d.yaml", "search_depth: 7\n")
    write(d / "e.txt", "not: [yaml")
    assert load_config(None).search_depth == 4


def test_empty_dropin_and_empty_main_are_fine(home: Path) -> None:
    base = home / ".config" / "wtctl"
    write(base / "config.yaml", "")
    write(base / "config.d" / "a.yaml", "# only a comment\n")
    assert load_config(None).search_depth == 3


def test_dropin_error_names_the_dropin_file(home: Path) -> None:
    d = home / ".config" / "wtctl" / "config.d"
    p = write(d / "50-bad.yaml", "search_depth: 0\n")
    err = invalid(None)
    assert str(p) in err.message
    assert "search_depth" in err.message


def test_symlinked_dropin_dir_refused(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "real-dropins"
    write(real / "a.yaml", "search_depth: 9\n")
    base = home / ".config" / "wtctl"
    base.mkdir(parents=True)
    (base / "config.d").symlink_to(real)
    assert "config.d" in invalid(None).message


def test_symlinked_dropin_file_refused(home: Path, tmp_path: Path) -> None:
    target = write(tmp_path / "t.yaml", "search_depth: 9\n")
    d = home / ".config" / "wtctl" / "config.d"
    d.mkdir(parents=True)
    (d / "a.yaml").symlink_to(target)
    assert "a.yaml" in invalid(None).message


def test_dangling_symlinked_dropin_refused(home: Path) -> None:
    d = home / ".config" / "wtctl" / "config.d"
    d.mkdir(parents=True)
    (d / "a.yaml").symlink_to(home / "nowhere")
    invalid(None)


def test_directory_named_like_a_dropin_refused(home: Path) -> None:
    (home / ".config" / "wtctl" / "config.d" / "a.yaml").mkdir(parents=True)
    invalid(None)


def test_fifo_dropin_refused_without_blocking(home: Path) -> None:
    d = home / ".config" / "wtctl" / "config.d"
    d.mkdir(parents=True)
    os.mkfifo(d / "a.yaml")
    invalid(None)


def test_symlinked_main_config_is_followed(home: Path, tmp_path: Path) -> None:
    target = write(tmp_path / "dotfiles" / "c.yaml", "search_depth: 8\n")
    base = home / ".config" / "wtctl"
    base.mkdir(parents=True)
    (base / "config.yaml").symlink_to(target)
    assert load_config(None).search_depth == 8


def test_main_config_must_be_a_file(home: Path) -> None:
    (home / ".config" / "wtctl" / "config.yaml").mkdir(parents=True)
    invalid(None)


# ---------------------------------------------------------------- hostile yaml


@pytest.mark.parametrize(
    "text",
    [
        "a: [unclosed\n",
        "\t- tab\n",
        "key: value: other\n",
        "- list\n- at top\n",
        "just a string\n",
        "42\n",
        "a: 1\n---\nb: 2\n",
        "!!python/object/apply:os.system ['echo hi']\n",
        "search_depth: !!python/object/apply:os.getcwd []\n",
        "a: 1\na: 2\n",
        "1: x\n",
        "? [a, b]\n: c\n",
        "search_depth: 3\x00\n",
    ],
)
def test_malformed_or_unsafe_yaml_refused(home: Path, text: str) -> None:
    invalid(cfg_file(home, text))


def test_invalid_utf8_refused(home: Path) -> None:
    p = home / ".config" / "wtctl" / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"branch_prefix: \xff\xfe\n")
    invalid(None)


def test_duplicate_key_named(home: Path) -> None:
    assert "search_depth" in invalid(cfg_file(home, "search_depth: 1\nsearch_depth: 2\n")).message


def test_anchors_and_aliases_refused_billion_laughs(home: Path) -> None:
    lines = ['a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]']
    prev = "a"
    for i in range(1, 9):
        name = chr(ord("a") + i)
        lines.append(f"{name}: &{name} [{', '.join(['*' + prev] * 9)}]")
        prev = name
    err = invalid(cfg_file(home, "\n".join(lines) + "\n"))
    assert "alias" in err.message or "anchor" in err.message


def test_simple_alias_refused(home: Path) -> None:
    invalid(cfg_file(home, "mask_env: &x [A]\nrepos: {}\nx: *x\n"))


def test_merge_key_refused(home: Path) -> None:
    invalid(cfg_file(home, "base: &b {a: 1}\nme:\n  <<: *b\n"))


def test_huge_file_refused(home: Path) -> None:
    text = "mask_env:\n" + "  - AAAA\n" * 400_000
    err = invalid(cfg_file(home, text))
    assert "large" in err.message or "size" in err.message


def test_huge_dropin_refused(home: Path) -> None:
    d = home / ".config" / "wtctl" / "config.d"
    write(d / "a.yaml", "# " + "x" * 2_000_000 + "\n")
    invalid(None)


def test_deeply_nested_yaml_refused(home: Path) -> None:
    invalid(cfg_file(home, "a: " + "[" * 5000 + "]" * 5000 + "\n"))
    invalid(cfg_file(home, "a: " + "{b: " * 3000 + "1" + "}" * 3000 + "\n"))


@pytest.mark.parametrize(
    "text",
    [
        "when: 2024-13-45\n",
        "me: {name: 2024-02-31}\n",
        "search_depth: !!int abc\n",
        "search_depth: " + "9" * 5000 + "\n",
        "? 2024-13-45\n: x\n",
    ],
)
def test_constructor_value_errors_are_config_invalid(home: Path, text: str) -> None:
    path = cfg_file(home, text)
    assert path in invalid(path).message


@pytest.mark.parametrize(
    "text",
    [
        "token_env: [SECRETVALUE123\n",
        "token_env: SECRETVALUE123: x: y\n",
        "a: 1\nb: !!int SECRETVALUE123\n",
        "a: !!SECRETVALUE123 x\n",
        "a: !!python/object:SECRETVALUE123 x\n",
        "a: {b: 2024-13-45 SECRETVALUE123}\nc: 'SECRETVALUE123\n",
    ],
)
def test_yaml_errors_never_echo_source_text(home: Path, text: str) -> None:
    assert "SECRETVALUE123" not in invalid(cfg_file(home, text)).message


def test_yaml_syntax_error_reports_line_and_column(home: Path) -> None:
    message = invalid(cfg_file(home, "a: 1\nb: [SECRETVALUE123\n")).message
    assert "line " in message
    assert "column " in message


def test_too_many_yaml_events_refused(home: Path) -> None:
    err = invalid(cfg_file(home, "mask_env:\n" + "  - A\n" * 20_000))
    assert "too complex" in err.message


def test_hostile_megabyte_file_fails_fast(home: Path) -> None:
    import time

    start = time.monotonic()
    invalid(cfg_file(home, "mask_env:\n" + "- a\n" * 250_000))
    assert time.monotonic() - start < 5


def test_many_dropins_refused(home: Path) -> None:
    d = home / ".config" / "wtctl" / "config.d"
    for i in range(101):
        write(d / f"{i:03}.yaml", "")
    err = invalid(None)
    assert "config.d" in err.message
    (d / "100.yaml").unlink()
    load_config(None)  # exactly 100 is fine


@pytest.mark.parametrize("value", ["1:30", "010", "1_0", "0x10", "0o10", "+3"])
def test_yaml11_integer_forms_are_not_integers(home: Path, value: str) -> None:
    assert "search_depth" in invalid(cfg_file(home, f"search_depth: {value}\n")).message


def test_plain_and_negative_integers_still_load(home: Path) -> None:
    assert load_config(cfg_file(home, "search_depth: 7\ntombstone_days: 0\n")).search_depth == 7


@pytest.mark.parametrize("word", ["no", "on", "yes", "off", "true", "null"])
def test_non_string_key_message_says_to_quote(home: Path, word: str) -> None:
    message = invalid(cfg_file(home, f"a: 1\n{word}: 1\n")).message
    assert f"mapping key {word} at line 2 is not a string; quote it" in message


def test_unescaped_control_bytes_in_errors(home: Path) -> None:
    err = invalid(cfg_file(home, 'hosts:\n  "a\\e[31mb": {}\n'))
    assert "\x1b" not in err.message
    assert "\\x1b" in err.message


def test_env_name_control_bytes_in_errors(home: Path) -> None:
    err = invalid(cfg_file(home, 'mask_env: ["A\\eB"]\n'))
    assert "\x1b" not in err.message


def test_undecodable_path_bytes_are_safe_in_errors(home: Path) -> None:
    d = home / ".config" / "wtctl" / "config.d"
    d.mkdir(parents=True)
    with open(os.path.join(os.fsencode(d), b"\xff.yaml"), "wb") as f:
        f.write(b"bogus: 1\n")
    err = invalid(None)
    err.message.encode("utf-8")  # must not raise
    assert "\\udcff" in err.message


@pytest.mark.skipif(os.geteuid() == 0, reason="permissions are not enforced for root")
def test_unreadable_default_config_is_an_error_naming_the_file(home: Path) -> None:
    cfg_file(home, "search_depth: 2\n")
    d = home / ".config" / "wtctl"
    d.chmod(0o000)
    try:
        err = invalid(None)
    finally:
        d.chmod(0o700)
    assert str(d / "config.yaml") in err.message


def test_unicode_values(home: Path) -> None:
    c = load_config(
        cfg_file(home, "branch_prefix: 'prefix-é/'\nbranch_template: 'wt/{name}-日本'\n")
    )
    assert c.branch_prefix == "prefix-é/"


def test_unicode_digits_in_duration_refused(home: Path) -> None:
    invalid(cfg_file(home, "lease_wait: '١٠s'\n"))


def test_unknown_key_with_hostile_name(home: Path) -> None:
    err = invalid(cfg_file(home, 'ev"il\\x1b[31m: 1\n'))
    assert "ev" in err.message


def test_repeated_calls_independent(home: Path) -> None:
    a = load_config(None)
    b = load_config(None)
    assert a == b and a is not b
    a.search_roots.append("/mutated")
    assert load_config(None).search_roots == []


# ---------------------------------------------------------------- environment


def test_env_overrides_file(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WTCTL_BRANCH_PREFIX", "env/")
    monkeypatch.setenv("WTCTL_SEARCH_DEPTH", "5")
    monkeypatch.setenv("WTCTL_AUTO_CLONE", "1")
    monkeypatch.setenv("WTCTL_ROOT", "~/fromenv")
    c = load_config(cfg_file(home, "branch_prefix: file/\nsearch_depth: 2\nroot: ~/fromfile\n"))
    assert (c.branch_prefix, c.search_depth, c.auto_clone) == ("env/", 5, True)
    assert c.root == str(home / "fromenv")


def test_env_beats_dropin(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(home / ".config" / "wtctl" / "config.d" / "a.yaml", "lease_wait: 5m\n")
    monkeypatch.setenv("WTCTL_LEASE_WAIT", "7m")
    assert load_config(None).lease_wait == "7m"


@pytest.mark.parametrize(
    ("var", "value", "attr", "expected"),
    [
        ("WTCTL_ROOT", "/srv/r", "root", "/srv/r"),
        ("WTCTL_WORKTREE_ROOT", "/srv/w", "worktree_root", "/srv/w"),
        ("WTCTL_THROWAWAY_ROOT", "/srv/t", "throwaway_root", "/srv/t"),
        ("WTCTL_CLONE_ROOT", "/srv/c", "clone_root", "/srv/c"),
        ("WTCTL_WORKTREE_PATH", "{root}/{name}", "worktree_path", "{root}/{name}"),
        ("WTCTL_AUTO_CLONE", "true", "auto_clone", True),
        ("WTCTL_AUTO_CLONE", "false", "auto_clone", False),
        ("WTCTL_AUTO_CLONE", "0", "auto_clone", False),
        ("WTCTL_SEARCH_DEPTH", "9", "search_depth", 9),
        ("WTCTL_NETWORK_TIMEOUT", "5s", "network_timeout", "5s"),
        ("WTCTL_CLONE_TIMEOUT", "2h", "clone_timeout", "2h"),
        ("WTCTL_LEASE_WAIT", "1m", "lease_wait", "1m"),
        ("WTCTL_TOMBSTONE_DAYS", "0", "tombstone_days", 0),
        ("WTCTL_GIT_BIN", "/opt/git", "git_bin", "/opt/git"),
        ("WTCTL_BRANCH_TEMPLATE", "x/{name}", "branch_template", "x/{name}"),
        ("WTCTL_BRANCH_PREFIX", "p/", "branch_prefix", "p/"),
        ("WTCTL_BRANCH_PREFIX", "", "branch_prefix", ""),
    ],
)
def test_env_scalars(
    home: Path, monkeypatch: pytest.MonkeyPatch, var: str, value: str, attr: str, expected: Any
) -> None:
    monkeypatch.setenv(var, value)
    assert getattr(load_config(None), attr) == expected


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("WTCTL_AUTO_CLONE", "yes"),
        ("WTCTL_AUTO_CLONE", "True"),
        ("WTCTL_AUTO_CLONE", ""),
        ("WTCTL_SEARCH_DEPTH", "three"),
        ("WTCTL_SEARCH_DEPTH", "3.0"),
        ("WTCTL_SEARCH_DEPTH", "0"),
        ("WTCTL_SEARCH_DEPTH", " 3"),
        ("WTCTL_SEARCH_DEPTH", "٣"),
        ("WTCTL_SEARCH_DEPTH", "9" * 5000),
        ("WTCTL_TOMBSTONE_DAYS", "-1"),
        ("WTCTL_LEASE_WAIT", "soon"),
        ("WTCTL_ROOT", ""),
        ("WTCTL_ROOT", "relative"),
        ("WTCTL_WORKTREE_PATH", "/no/root/{name}"),
        ("WTCTL_GIT_BIN", "git"),
    ],
)
def test_bad_env_value_exit_2_names_variable(
    home: Path, monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    monkeypatch.setenv(var, value)
    err = invalid(None)
    assert var in err.message


def test_env_for_structured_keys_is_ignored(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var, value in {
        "WTCTL_REPOS": '{"github.com/o/r": {"clone": "/x"}}',
        "WTCTL_HOSTS": "{x: 1}",
        "WTCTL_HOOKS": "garbage",
        "WTCTL_SEARCH_ROOTS": "/a:/b",
        "WTCTL_MASK_ENV": "A,B",
        "WTCTL_ME": "x",
        "WTCTL_ME_NAME": "envname",
        "WTCTL_REMOVE": "x",
        "WTCTL_REMOVE_DELETE_BRANCH": "false",
        "WTCTL_THROWAWAY_DEPTH": "1",
        "WTCTL_CREATE_ALLOW_FILTERS": "lfs",
        "WTCTL_UNKNOWN": "x",
        "WTCTL_": "x",
        "wtctl_root": "/lower",
    }.items():
        monkeypatch.setenv(var, value)
    c = load_config(None)
    assert c.repos == {} and c.hosts == {} and c.search_roots == [] and c.mask_env == []
    assert c.me.name is None
    assert c.remove.delete_branch is True
    assert c.throwaway.depth == 50
    assert c.root == str(home / "wtctl")


def test_reserved_hook_variables_never_read_as_config(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("WTCTL_HOOK", "WTCTL_ID", "WTCTL_OBJECT_TYPE", "WTCTL_PATH", "WTCTL_REPO"):
        monkeypatch.setenv(var, "../../etc")
    assert load_config(None) == Config.model_validate({})


def test_per_repo_beats_env_beats_global(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WTCTL_WORKTREE_ROOT", "/srv/env-wt")
    monkeypatch.setenv("WTCTL_BRANCH_TEMPLATE", "env/{name}")
    monkeypatch.setenv("WTCTL_AUTO_CLONE", "true")
    c = load_config(
        cfg_file(
            home,
            "worktree_root: /srv/file-wt\nbranch_template: file/{name}\nrepos:\n"
            "  h.com/o/r:\n    worktree_root: /srv/repo-wt\n    branch_template: repo/{name}\n"
            "    auto_clone: false\n",
        )
    )
    r = c.repo("h.com/o/r")
    assert r.worktree_root == "/srv/repo-wt"
    assert r.branch_template == "repo/{name}"
    assert r.auto_clone is False
    other = c.repo("h.com/o/other")
    assert other.worktree_root == "/srv/env-wt"
    assert other.branch_template == "env/{name}"
    assert other.auto_clone is True


# ---------------------------------------------------------------- per-repo resolution


def test_repo_defaults_inherit_global(home: Path) -> None:
    c = load_config(
        cfg_file(
            home,
            "branch_template: g/{name}\nauto_clone: true\ncreate: {allow_filters: [lfs]}\n"
            "remove: {disposable: [a/]}\n",
        )
    )
    r = c.repo("h.com/o/unknown")
    assert r.repo_id == "h.com/o/unknown"
    assert r.clone is None and r.clone_url is None and r.base is None
    assert r.remote == "origin"
    assert r.worktree_root == str(home / "wtctl" / "worktrees")
    assert r.branch_template == "g/{name}"
    assert r.auto_clone is True
    assert r.copy_env == []
    assert r.allow_filters == ["lfs"]
    assert r.disposable == ["a/"]
    assert r.bootstrap.auto is True
    assert r.bootstrap.steps == [] and r.bootstrap.env == {} and r.bootstrap.pass_env == []


def test_repo_overrides_replace_lists(home: Path) -> None:
    c = load_config(
        cfg_file(
            home,
            "create: {allow_filters: [lfs]}\nremove: {disposable: [a/]}\nrepos:\n  h.com/o/r:\n"
            "    create: {allow_filters: []}\n    remove: {disposable: [b/]}\n"
            "    copy_env: ['.env*']\n    clone_url: file:///x\n    base: dev\n    remote: up\n"
            "    bootstrap:\n      auto: false\n      env: {A: b}\n      pass_env: [TOK]\n"
            "      steps:\n        - {argv: [make], timeout: 5}\n"
            "        - {argv: ['x y'], sh: true}\n",
        )
    )
    r = c.repo("h.com/o/r")
    assert r.allow_filters == []
    assert r.disposable == ["b/"]
    assert r.copy_env == [".env*"]
    assert (r.clone_url, r.base, r.remote) == ("file:///x", "dev", "up")
    assert r.bootstrap.auto is False
    assert r.bootstrap.env == {"A": "b"}
    assert r.bootstrap.pass_env == ["TOK"]
    assert [(s.argv, s.sh, s.timeout) for s in r.bootstrap.steps] == [
        (["make"], False, 5),
        (["x y"], True, 600),
    ]


def test_repo_worktree_root_precedence_chain(home: Path) -> None:
    c = load_config(cfg_file(home, "root: ~/r\n"))
    assert c.repo("h.com/o/r").worktree_root == str(home / "r" / "worktrees")
    c = load_config(cfg_file(home, "root: ~/r\nworktree_root: ~/gw\n"))
    assert c.repo("h.com/o/r").worktree_root == str(home / "gw")


def test_host_lookup_defaults(home: Path) -> None:
    c = load_config(cfg_file(home, "hosts:\n  git.example.com:\n    token_env: TOK\n"))
    assert c.host("git.example.com").token_env == "TOK"
    assert c.host("git.example.com").clone_url == "https://{host}/{path}.git"
    h = c.host("other.example.com")
    assert (h.clone_url, h.token_env, h.token_command, h.token_user) == (
        "https://{host}/{path}.git",
        None,
        None,
        "x-access-token",
    )


# ---------------------------------------------------------------- durations, me_name


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("1s", 1), ("90s", 90), ("2m", 120), ("1h", 3600), ("1d", 86400), ("2w", 1209600)],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize(
    "text", ["", "0s", "s", "1", "1y", "-1s", "1.5h", "1s ", " 1s", "1S", "١s"]
)
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(text)


def test_effective_durations(home: Path) -> None:
    c = load_config(None)
    assert (c.network_timeout_s, c.clone_timeout_s, c.lease_wait_s) == (120, 1800, 600)


def test_me_name_from_config(home: Path) -> None:
    assert me_name(cfg_file(home, "me: {name: carol}\n")) == "carol"


def test_me_name_defaults_to_sanitized_os_user(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfgmod.getpass, "getuser", lambda: "Jo Doe/x")
    assert me_name(None) == "Jo_Doe_x"


def test_me_name_invalid_config_is_config_invalid(home: Path) -> None:
    with pytest.raises(WtctlError) as exc:
        me_name(cfg_file(home, "me: {name: 'bad name'}\n"))
    assert exc.value.code == "config_invalid"


# ---------------------------------------------------------------- seams


def test_runtime_defaults_find_the_config_seams(home: Path) -> None:
    rt = Runtime()
    path = cfg_file(home, "me: {name: dave}\nsearch_depth: 6\n")
    assert rt.config_loader(path).search_depth == 6
    assert rt.me_name_source(path) == "dave"
    assert rt.resolve_me_name(path) == "dave"


def test_invalid_config_through_the_cli_exits_2(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.helpers import install_handler, run_cli
    from wtctl.context import Result

    install_handler(monkeypatch, "list", lambda ctx: Result({"d": ctx.config.search_depth}))
    r = run_cli(capsys, "--json", "--config", cfg_file(home, "bogus: 1\n"), "list")
    assert r.code == 2
    err = r.json()["error"]
    assert err["code"] == "config_invalid" and "bogus" in err["message"]
