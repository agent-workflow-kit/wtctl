# wtctl

Git worktrees, throwaway checkouts, and the mapping from repo ids to local clones.

## Status

Designed: interface 0.1.0-design ([docs/interface.md](docs/interface.md)); not yet implemented. Implementation plan: [docs/plan.md](docs/plan.md).

## Layer

L1 (primitive). Works fully standalone; shareable (Linux, WSL, macOS).

## What it does

- Creates linked worktrees in a central, configurable location, with a branch named by the caller or by a template, and bootstraps them with the project's own commands (env-file copying is opt-in per call and per repo).
- Creates **throwaway checkouts**: separate disposable clones detached at one commit, copied by sha from the local clone (full history, or `--depth 1`) or fetched from the remote (`--depth`, default `throwaway.depth`), never trusted. `extract` copies listed files out of one without ever following a link.
- Resolves repo ids (`host/<full project path>`) to local clones: config, optional discovery under search roots, optional auto-clone.
- Removes worktrees only when nothing would be lost (v1 limitation: a worktree with initialized submodules always needs a human `--force`): refuses dirty, unpushed, in-use, held or changed-config worktrees (exit 20 with a `reason`); a human may override `held`, `dirty` and `unpushed` with `--force`. `remove --dry-run` (the same as `status`) reports what it would do. "Pushed" is checked against a freshly refreshed, wtctl-owned treeless [mirror](docs/interface.md#mirror) of the remote (no fetch ever runs against your clone: missing base commits are copied in as packs wtctl builds itself) and re-hashes the commits it relies on. Tracked files are compared by content, not by the agent-writable index's stat data. wtctl deletes worktrees itself, without following links or crossing mount points, and never runs `git worktree remove` or `git worktree repair`. `remove` is confirm-by-default.
- Records a **repo-config baseline** for every worktree and refuses removal when repository-controlled git config changed (`baseline check`, human `baseline accept`).
- Runs every git command with a hardened invocation that ignores repository-controlled executable config.
- Supports labels, hooks, an audit log (`export`) and metadata-only `reconcile`.

## Quick start

```sh
# Tell wtctl where a clone lives (or set search_roots, or auto_clone: true).
mkdir -p ~/.config/wtctl
printf 'repos:\n  github.com/example/app:\n    clone: ~/dev/app\n' >> ~/.config/wtctl/config.yaml

# A worktree on a new branch wt/fix-login, under ~/wtctl/worktrees/.
cd "$(wtctl create --repo github.com/example/app --name fix-login)"

# What would removal do? Then remove it (asks for confirmation on a TTY).
wtctl status "$PWD"
wtctl remove "$PWD"

# A throwaway checkout of one commit, and copying listed files out of it
# into a scratch directory (never into your real clone).
wtctl create --throwaway --repo github.com/example/app --ref <40-hex sha> --json
mkdir -p /tmp/review-out
wtctl extract <id> --files-from changed.txt --dest /tmp/review-out
```

## Commands

```text
wtctl create --repo <id> [--branch B | --detach] [--base BRANCH] [--ref SHA] [--name N] [--holder H]
             [--label k=v]... [--no-bootstrap] [--copy-env] [--hook NAME=CMD]... [--override-escalation]
wtctl create --throwaway --repo <id> --ref SHA [--source auto|local|remote] [--depth N] [--label k=v]...
             [--hook NAME=CMD]... [--override-escalation]
wtctl list [--repo <id>] [--label k=v]... [--throwaways | --worktrees] [--older-than D] [--state S]... [--all]
wtctl show <id|path>
wtctl status <id|path> [--merged-head SHA] [--holder H]
wtctl remove <id|path> [--merged-head SHA] [--keep-branch] [--holder H] [--force] [--yes]
             [--hook NAME=CMD]... [--override-escalation]
wtctl bootstrap <id|path> [--copy-env]
wtctl extract <id|path> [--files-from FILE|-] [--delete-from FILE|-] --dest DIR [--max-size BYTES]
wtctl baseline check <id|path>
wtctl baseline accept <id|path> [--expect-digest D]
wtctl adopt <path> [--repo <id>] [--name N] [--holder H] [--label k=v]...
wtctl forget <id|path> [--holder H]
wtctl locate <repo-id>
wtctl clone <repo-id>
wtctl label add|remove <id|path> k=v...
wtctl reconcile [--dry-run]
wtctl prune --throwaways [--older-than D] [--if-configured] [--label k=v]... [--dry-run]
wtctl export [--since TIME] [--audit | --log]
wtctl cron install [--env-file FILE]
wtctl cron uninstall
```

Global flags: `--json`, `--config`, `--verbose`, `--quiet`, `--dry-run`, `--yes`, `--actor`, `--decision-id`, `--version`. The full grammar of every command is in [Commands](docs/interface.md#commands).

## Exit codes

The reserved awkit codes only (0, 1, 2, 10, 11, 12, 13, 20, 21; no tool-specific codes); refusal reasons `dirty`, `unpushed`, `repo_config_changed`, `branch_in_use`, `branch_exists`, `path_exists`, `in_use`, `held`, `unlinked`, `not_owned`, `main_worktree`, `locked`. See [Exit codes](docs/interface.md#exit-codes) and [Errors](docs/interface.md#errors).

## Config, hooks and environment

- Config: `~/.config/wtctl/config.yaml` plus `config.d/*.yaml`; every key with its default in [config.example.yaml](config.example.yaml); per-repo overrides under `repos.<repo-id>`.
- Hooks: `pre-create`, `post-create`, `pre-remove`, `post-remove`, `post-reconcile`, `post-baseline-accept` ([Hooks](docs/interface.md#hooks)).
- Locations: everything wtctl creates for you to work in is under `root` (default `~/wtctl`): `worktrees/`, `throwaway/`, `clones/`, each overridable ([Locations](docs/interface.md#locations)). They stay outside hidden home directories so isolated Claude sessions can run there.
- Environment: `WTCTL_<KEY>` for top-level scalar keys (e.g. `WTCTL_ROOT`, `WTCTL_AUTO_CLONE`); hook variables `WTCTL_HOOK`, `WTCTL_ID`, `WTCTL_OBJECT_TYPE` ([Environment](docs/interface.md#environment)).
- Credentials: wtctl never uses git credential helpers. **Private HTTPS repos need a token**: the variable named by `hosts.<host>.token_env` (e.g. `GH_TOKEN`), or `hosts.<host>.token_command` (an argv such as `[gh, auth, token]`), or use an SSH URL, which authenticates through your ssh-agent (`SSH_AUTH_SOCK`; ssh runs in batch mode, so keys need the agent and hosts need a `known_hosts` entry). See [Credentials](docs/interface.md#credentials).
- State: `~/.local/state/wtctl/` (`state.db`, `log.jsonl`, `audit.jsonl`, and `mirrors/`, one treeless bare mirror per clone).

## Dependencies

- `git` 2.34 or newer (binding A, called directly).
- Python 3.12+, `pyyaml`, `pydantic`.
- macOS: `lsof` for in-use detection (optional).
- No awkit tool dependencies.

## Platforms

Linux, WSL (Ubuntu 22.04 or newer) and macOS. SHA-1 repositories only in v1 (SHA-256 repositories are refused). See [Platform notes](docs/interface.md#platform-notes).

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/wtctl
```

## Documentation

- [Interface](docs/interface.md) (normative), [design](docs/design.md), [plan](docs/plan.md)
- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
