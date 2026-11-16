# Live testing

The `live` suite (`tests/live/`) checks wtctl against real forges: behavior that local bare
repositories and loopback servers cannot show. It is run by the maintainer, on their own machine, with
their own credentials. It is **never run in CI, never by `make verify`, and never by a plain
`pytest`** (the default options deselect the `live` marker). Nothing in the repository names a
real project, organization, host or credential: every target comes from the environment variables
below.

## Running

```bash
# Anonymous reads only: any public repository on each forge (see "Choosing targets").
export WTCTL_LIVE_GITHUB_PUBLIC_REPO=<owner>/<project>
export WTCTL_LIVE_GITLAB_PUBLIC_REPO=<group>/<project>
make live
```

`make live` runs `uv run pytest -m live tests/live -rs`. A test whose prerequisites are missing is
**skipped** with a message naming the variable (never a value), so a partly configured run is safe.
Run a subset with a keyword or a file, for example `uv run pytest -m live tests/live -k github -rs`
or `uv run pytest -m live tests/live/test_live_push.py -rs`. The suite never prints. Outcomes that are observed rather than
asserted (redirects, ssh, fork-only commits, git version, default branch) are attached to the report
as pytest properties: add `--junitxml=live.xml -o junit_family=xunit2` and read the `<property>`
elements. Do not run with `-l` or `--showlocals`: a failing test would print its local variables,
and the child environment (which holds the tokens) can be among them.

Every command runs as a real child process (`python -m wtctl --json ...`) in a private HOME under
pytest's temporary directory, with an environment built from scratch (no inherited `GIT_*`, ssh
agent or token variables) and the git on your `PATH`. State, mirrors, clones and throwaways all live
in that temporary directory and are removed with it.

## Cost and side effects

- Network: reads of the public and private repositories, a full clone of each public repository (the
  mirror and local-transfer tests) and two full-history fetches (`--depth 0`: the strict-fsck test
  and the local-transfer throwaway of the mirror test): pick repositories within the size bound
  under "Choosing targets".
- Push: only `test_live_push.py` writes, and only to the repository you name in
  `WTCTL_LIVE_<FORGE>_PUSH_REPO`. It creates one branch named `WTCTL_LIVE_PUSH_BRANCH_PREFIX`
  (default `wtctl-live-`; a plain, valid ref-name prefix with at most one `/`, never a name such as
  `main` or `release`, else the default is used) plus 12 random hex characters, and first checks that
  no such branch exists (if one does, the test fails and deletes nothing). It then pushes two commits
  to it (a create, then a fast-forward; never a force push) and deletes the branch it created
  afterwards, also when a step failed. A failed deletion fails the test with the branch name in the
  message (and as a note on any earlier failure); the branch name is recorded as the `push_branch`
  report property. It never touches the default branch. Use a disposable repository; a crash
  between create and delete can leave the one branch behind, which you delete by hand.
- Credentials: tokens are read from the variables you name, handed to the child processes through
  their environment only, never put in argv, a config file or a log, and scrubbed from every string
  a test can print. Use a read-only token for the private-repository tests and a separate
  write-scoped token for the push repository.
- Billing: forge API rate limits and, for private repositories, your account's usage. The suite makes
  a few dozen requests per forge.

## Choosing targets

- **Public repository** (`..._PUBLIC_REPO`): small and long-lived: under 200 MB, under about 5000
  commits, at least 8 commits and years of history (the strict-fsck test wants real old history,
  and the unadvertised-sha test needs a commit six back from the tip that no branch or request
  points at). The mirror test (a full clone) and the strict-fsck test (a full-history fetch)
  both fetch the whole history, so the bound is what a run costs. Choose a project you do not
  control, so nothing depends on it staying yours.
- **Fork request head** (`..._FORK_PR_HEAD`): the head commit of an open or closed pull request
  (GitHub) or merge request (GitLab) from a fork of the public repository, a commit on no branch of
  it. Copy the full sha from the request.
- **Private repository** (`..._PRIVATE_REPO`): any private repository your read token can see.
- **Push repository** (`..._PUSH_REPO`): disposable, with one commit on its default branch.
- **git-lfs repository**: a PUBLIC repository on github.com or gitlab.com with a file tracked by
  git-lfs; give a commit and the file's path. There is no token for it: the LFS download during
  `git worktree add` is anonymous, so a private LFS repository cannot work, and a host other than
  the two forges is refused.

## Environment variables

Paths are project paths without a host or `.git` (`owner/name`, `group/subgroup/name`). A `*_TOKEN_ENV`
variable holds the **name** of the variable that holds the token, so the token never appears in the
variable you export for the suite itself. A token variable may not be named like a variable the
suite sets itself (`HOME`, `PATH`, `XDG_*`, `GIT_*`, `SSH_*`, `WTCTL_*`, `LC_*`, `LD_*`, `PYTHON*`).

| Variable | Needed by | Meaning |
|---|---|---|
| `WTCTL_LIVE_GITHUB_PUBLIC_REPO` | anonymous, mirror | public repository on github.com |
| `WTCTL_LIVE_GITLAB_PUBLIC_REPO` | anonymous, mirror | public repository on gitlab.com |
| `WTCTL_LIVE_GITHUB_FORK_PR_HEAD` | fork head | sha of a fork's pull request head (needs the public repository) |
| `WTCTL_LIVE_GITHUB_FORK_ONLY_SHA` | fork-only (optional) | sha of a commit only in a fork; outcome recorded |
| `WTCTL_LIVE_GITLAB_FORK_ONLY_SHA` | fork-only (optional) | the same on gitlab.com |
| `WTCTL_LIVE_GITHUB_PRIVATE_REPO` | private | private repository on github.com |
| `WTCTL_LIVE_GITLAB_PRIVATE_REPO` | private | private repository on gitlab.com |
| `WTCTL_LIVE_GITHUB_TOKEN_ENV` | private | name of the variable holding a github.com read token |
| `WTCTL_LIVE_GITLAB_TOKEN_ENV` | private | name of the variable holding a gitlab.com read token |
| `WTCTL_LIVE_GITHUB_TOKEN_COMMAND` | token command (optional) | JSON argv, default `["gh","auth","token","--hostname","{host}"]`; `gh` and `glab` may only run `auth token` |
| `WTCTL_LIVE_GITLAB_TOKEN_COMMAND` | token command | JSON argv printing a gitlab.com token (no default: depends on your `glab`) |
| `WTCTL_LIVE_GITHUB_CONFIG_DIR` | token command (optional) | `gh` config directory handed to the command as `GH_CONFIG_DIR` (default: `$XDG_CONFIG_HOME/gh`, when it exists) |
| `WTCTL_LIVE_GITLAB_CONFIG_DIR` | token command (optional) | the same for `glab` (`GLAB_CONFIG_DIR`; default `$XDG_CONFIG_HOME/glab-cli`) |
| `WTCTL_LIVE_GITHUB_PUSH_REPO` | push | disposable repository on github.com |
| `WTCTL_LIVE_GITLAB_PUSH_REPO` | push | disposable repository on gitlab.com |
| `WTCTL_LIVE_GITHUB_PUSH_TOKEN_ENV` | push | name of the variable holding a github.com write token |
| `WTCTL_LIVE_GITLAB_PUSH_TOKEN_ENV` | push | name of the variable holding a gitlab.com write token |
| `WTCTL_LIVE_PUSH_BRANCH_PREFIX` | push (optional) | prefix of the disposable branch (default `wtctl-live-`) |
| `WTCTL_LIVE_SSH_EXPECT` | ssh (optional) | `ok` or `auth_required`: pin the expected outcome |
| `WTCTL_LIVE_LFS_REPO` | lfs | repository id `github.com/<path>` or `gitlab.com/<path>` of a public repository with git-lfs files |
| `WTCTL_LIVE_LFS_REF` | lfs | full sha in it whose tree holds the LFS file |
| `WTCTL_LIVE_LFS_PATH` | lfs | path of the LFS file at that sha |
| `WTCTL_LIVE_GITLAB_FORK_PR_HEAD` | fork head | sha of a fork's merge request head (needs the public repository) |

Binaries: `git` (record its version; the claims for git 2.45 and newer need it), `ssh` (for the ssh
test), `gh` and `glab` (token-command tests), `git-lfs` (lfs tests). A missing binary skips the tests
that need it. The ssh test uses your real ssh identity and `known_hosts` (a fetch only; nothing is
written), because ssh reads them from the passwd home, not from the suite's private one.

The token command runs once in the suite's scratch environment (only `HOME`, the isolation
variables and the config directory, no keyring session, `GH_TOKEN` or session bus), and again by
wtctl itself. It must print the token under those conditions; if it prints nothing the test is
skipped with a message saying so. Its output is scrubbed from everything a test prints and scanned
for in every file the run wrote.

## What each test verifies

Items refer to the live-verification notes on issues #25, #26 and #27 and to
[design.md](design.md#live-verification) (finding ids A6, A7, G1 to G3, F1 and decisions D11, D27,
D33, D46).

| Test (file) | Needs | Verifies |
|---|---|---|
| `test_fetch_the_tip_by_sha` (anonymous) | public | A6: anonymous fetch of a full sha at depth 1 and 50 on each forge |
| `test_fetch_an_unadvertised_sha` | public | A6: a commit no advertised ref points at, fetched by sha (`allowAnySHA1InWant` per forge), depth 1 and 50; skipped (reason recorded) when every candidate is advertised |
| `test_strict_fsck_accepts_legitimate_old_history` | public | `fetch.fsckObjects=true` accepts real full history (`--strict` refusals of legitimate old objects) |
| `test_port_443_in_the_url_matches_the_bare_host` | public | a `:443` URL is fetched anonymously by real git (the token half is in the private tests) |
| `test_a_redirecting_url_is_not_followed_blindly` | public | a URL the forge redirects is fetched or refused with exit 12, never hung on (outcome recorded) |
| `test_ssh_never_prompts` | public, `ssh` | real ssh `BatchMode`: no hang, `auth_required` for no key or a locked key |
| `test_a_fork_request_head_is_fetched_by_sha` | fork head | fork PR or MR head fetched by sha, depth 1 |
| `test_a_fork_only_commit_is_observed` | fork-only sha | a commit in a fork and no PR or MR head (outcome recorded) |
| `test_mirror_refresh_default_branch_and_local_strict_transfer` (mirror) | public | G1 to G3: treeless first refresh and a second one with `GIT_NO_LAZY_FETCH=1` (upstream is unchanged between them); the mirror is a partial clone with no tree or blob object; default branch from `ls-remote --symref` equals the forge's; pushed check at `remove`; `create --throwaway --source local --depth 0` through `index-pack --strict`; git version recorded |
| `test_token_env_header_reaches_a_private_repository` (private) | private | A7/D11: URL-scoped header with the default `token_user`; the token is written nowhere |
| `test_a_private_repository_without_a_token_is_auth_required` | private | no token: `auth_required`, no prompt |
| `test_a_rejected_token_says_so_and_does_not_fall_back` | private | a made-up token is refused with `auth_required` and the message says the token was rejected |
| `test_port_443_still_gets_the_token` | private | the private repository is read through a `:443` URL, so real git matched the host-scoped header |
| `test_token_command_with_the_forge_cli` | private, `gh` or `glab` | `token_command` (`auth token` only) yields a token the forge accepts; skipped when the command prints nothing in the scratch environment |
| `test_fast_forward_push_from_a_shallow_throwaway` (push) | push repo and write token | F1: create and fast-forward push from depth 50 and depth 1 to a real branch, branch deleted afterwards (an existing branch of that name fails the test untouched); no `shallow update not allowed` |
| `test_lfs_is_smudged_when_allowed_and_a_pointer_otherwise` (lfs) | lfs, `git-lfs` | `create.allow_filters: [lfs]` smudges (anonymously, public repository), no honored driver leaves the pointer; the `create` warnings go in the failure message; the worktree's `status` is a manual note for the false-`dirty` question (design.md, Still to verify) |
| `test_planted_lfs_transfer_keys_are_refused` | lfs, `git-lfs` | `lfs.url` and `lfs.customtransfer.*` in the clone give `unsafe_repo_config` |

Not covered here (manual checklists, design.md): macOS and WSL behavior, FUSE and drvfs mounts,
bubblewrap, large real monorepos and the `bench` suite, a force-moved remote, `receive.shallowUpdate`
refusals by a forge with shallow pushes disabled, and Apple git. Re-run `tests/test_hardgit_vectors.py`
(ordinary tests, no network) on the newest git you have.

## Results checklist

Paste into #25 (and #26/#27 for the platform and git version) after a run:

```text
git --version:
OS / filesystem:
make live (date, wtctl version):
anonymous    github: pass/skip/fail   gitlab: pass/skip/fail
unadvertised sha:       github: fetched / refused (message)   gitlab: ...
strict fsck full history: github ...  gitlab ...
:443:        github ...  gitlab ...
redirect outcome:       github ...  gitlab ...
ssh outcome (no key / locked key): ...
fork head:   github ...  gitlab ...   fork-only outcome: ...
mirror:      github ...  gitlab ...   default branch: ...
private token_env:      github ...  gitlab ...
no token / bad token message:  ...
token_command (gh / glab): ...
push depth 50 / depth 1:  github ...  gitlab ...
lfs smudge / pointer / planted keys: ...
```
