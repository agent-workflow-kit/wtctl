"""The whole bootstrap run on a real worktree: snapshots, copy, steps, sets (no CLI)."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from tests.bootlib import Repo
from wtctl import bootstrapper as b


@pytest.fixture
def r(tmp_path: Path, private_home: Path) -> Repo:
    return Repo(tmp_path, private_home)


def go(
    r: Repo,
    script: str = "",
    *,
    copy_env=False,
    rerun=False,
    patterns=(),
    limit=2_000_000,  # noqa: ANN001
    steps=None,
):
    cfg = NS(hosts={}, mask_env=[], remove=NS(nested_scan_limit=limit))
    chosen = (
        steps
        if steps is not None
        else ([NS(argv=[sys.executable, "-c", script], sh=False, timeout=60)] if script else [])
    )
    st = NS(bootstrap=NS(steps=chosen, pass_env=[], env={}), copy_env=list(patterns))
    rec = {
        "id": "wt-aaaaaaaa",
        "path": str(r.wt),
        "admin_dir": r.admin_dir,
        "dir_id": r.dir_id,
        "clone_path": str(r.main),
        "repo": "o/r",
        "common_dir": None,
        "common_id": None,
    }
    warns: list[str] = []
    out = b.run_bootstrap(
        git=r.git,
        config=cfg,
        settings=st,
        record=rec,
        copy_env=copy_env,
        run_the_steps=True,
        rerun=rerun,
        warn=warns.append,
        environ={"PATH": os.environ["PATH"]},
    )
    return out, warns


def paths(out: b.Outcome) -> dict[str, tuple[str, str | None]]:
    return {p: (k, d) for p, k, d in out.paths}


def test_new_untracked_files_and_ignored_entries_become_the_created_set(r: Repo) -> None:
    code = (
        "import os\n"
        "os.makedirs('node_modules/pkg'); open('node_modules/pkg/i.js','w').write('1')\n"
        "open('gen.txt','w').write('g'); open('out.log','w').write('l')\n"
    )
    out, _ = go(r, code)
    assert out.state == "ok" and out.failed_step is None
    got = paths(out)
    assert got["gen.txt"] == ("untracked", hashlib.sha256(b"g").hexdigest())
    assert got["node_modules"] == ("ignored_dir", None)
    assert got["out.log"] == ("ignored_file", None)
    assert len(got) == 3


def test_entries_that_existed_before_are_not_created_by_bootstrap(r: Repo) -> None:
    r.write("agent.txt", "mine")
    r.write("keep.log", "k")
    out, _ = go(r, "open('new.txt','w').write('n')")
    assert set(paths(out)) == {"new.txt"}


def test_tracked_changes_are_reported_not_recorded(r: Repo) -> None:
    out, _ = go(r, "open('README.md','a').write('more')")
    assert out.tracked_changes == ["README.md"] and paths(out) == {}


def test_nested_git_entries_inside_created_directories_are_recorded(r: Repo) -> None:
    code = (
        "import os,subprocess\n"
        "os.makedirs('node_modules/dep'); open('node_modules/dep/.git','w').write('gitdir: x')\n"
        "os.makedirs('node_modules/a/b/c.git/.git'); open('node_modules/a/f','w')\n"
        "open('node_modules/dep/f','w')\n"
        "os.makedirs('vendor/lib'); subprocess.run(['git','init','-q','vendor/lib'],check=True)\n"
        "open('vendor/lib/f','w').write('1')\n"
    )
    out, _ = go(r, code)
    assert out.nested == ["node_modules/a/b/c.git/.git", "node_modules/dep/.git", "vendor/lib/.git"]


def test_a_dotgit_directory_is_not_descended_into(r: Repo) -> None:
    code = "import os; os.makedirs('build/x/.git/modules/y/.git'); open('build/x/f','w')"
    out, _ = go(r, code)
    assert out.nested == ["build/x/.git"]


def test_a_nested_repo_outside_created_directories_is_not_exempted(r: Repo) -> None:
    os.makedirs(r.wt / "src" / "old" / ".git")  # the agent's, before bootstrap
    out, _ = go(r, "open('x.txt','w').write('1')")
    assert out.nested == []


def test_symlinks_in_created_directories_are_not_followed_by_the_scan(
    r: Repo, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    (outside / "d" / ".git").mkdir(parents=True)
    code = f"import os; os.makedirs('build'); os.symlink({str(outside)!r}, 'build/link')"
    out, _ = go(r, code)
    assert out.nested == []


def test_the_nested_scan_limit_fails_safe_with_a_warning(r: Repo) -> None:
    code = "import os\nos.makedirs('build/d/.git')\nfor i in range(50): open(f'build/f{i}','w')"
    out, warns = go(r, code, limit=10)
    assert any("scan" in w for w in warns)
    assert out.nested == []  # nothing is exempt that was not listed


def test_a_failed_step_still_records_what_it_created(r: Repo) -> None:
    out, _ = go(r, "open('partial.txt','w').write('p'); raise SystemExit(4)")
    assert out.state == "failed" and out.failed_step is not None and out.failed_step["exit"] == 4
    assert "partial.txt" in paths(out)


def test_copied_env_files_are_created_even_when_ignored_and_even_on_a_rerun(r: Repo) -> None:
    r.write(".env", "S=1", root=r.main)  # ignored by .gitignore
    r.write("agent2.txt", "agent")
    out, _ = go(r, copy_env=True, patterns=[".env"], rerun=True)
    assert paths(out) == {".env": ("untracked", hashlib.sha256(b"S=1").hexdigest())}
    assert [c.path for c in out.copied] == [".env"]


def test_a_rerun_records_new_ignored_entries_but_not_new_untracked_files(r: Repo) -> None:
    out, _ = go(r, "open('u.txt','w'); open('x.log','w')", rerun=True)
    assert set(paths(out)) == {"x.log"}


def test_copy_env_without_patterns_warns(r: Repo) -> None:
    out, warns = go(r, copy_env=True, patterns=[])
    assert any("copy_env" in w for w in warns) and out.copied == []


def test_no_steps_still_runs_the_snapshots(r: Repo) -> None:
    out, _ = go(r, steps=[])
    assert out.state == "ok" and out.steps == []


def test_a_worktree_with_another_identity_is_refused(r: Repo) -> None:
    from wtctl.fsafe import FsafeError

    r.dir_id = "1:2"
    with pytest.raises(FsafeError):
        go(r, "print(1)")


def test_a_planted_hook_and_config_in_the_worktree_stay_inert(r: Repo, tmp_path: Path) -> None:
    marker = tmp_path / "hit"
    hooks = Path(r.admin_dir) / "hooks"
    hooks.mkdir(exist_ok=True)
    for h in ("post-checkout", "pre-commit", "post-index-change", "reference-transaction"):
        (hooks / h).write_text(f"#!/bin/sh\necho {h} >> {marker}\n")
        (hooks / h).chmod(0o755)
    r.g(["config", "core.hooksPath", str(hooks)], r.main)
    r.g(["config", "core.fsmonitor", f"touch {marker};true"], r.main)
    r.g(["config", "core.pager", f"touch {marker}"], r.main)
    r.g(["config", "diff.external", f"touch {marker}"], r.main)
    r.g(["config", "filter.x.clean", f"touch {marker}"], r.main)
    r.write(".gitattributes", "* filter=x\n")
    out, _ = go(r, "open('a.txt','w').write('a')")
    assert out.state == "ok" and not marker.exists()
