# wtctl

Git worktrees, throwaway checkouts, and the mapping from repo ids to local clones.

## Status

Pre-design: interface not yet specified; see [docs/design.md](docs/design.md).

## Layer

L1 (primitive).

## What it does

- Creates, lists and removes git worktrees, and bootstraps their dependencies (configurable; env-file copying is opt-in per call with `bootstrap --copy-env`).
- Owns the repo id to local clone mapping (`repos.<id>.clone: <dir>`, optional auto-clone); `locate <repo-id>` resolves a local clone.
- `remove` is confirm-by-default and refuses dirty or unpushed worktrees (exit 20) unless a human passes `--force`; `--yes` never bypasses that refusal. `--merged-head <sha>` lets squash-merged branches be removed.
- Throwaway checkouts (`create --throwaway --ref <sha>`) are separate disposable clones detached at a sha; removing them skips the confirmation and the dirty/unpushed checks.
- Supports labels.

## Indicative commands

Indicative, finalized in [docs/interface.md](docs/interface.md).

```text
  wtctl create [--repo <id>] [--branch <name>] [--throwaway --ref <sha>]
  wtctl list
  wtctl remove [--merged-head <sha>] [--force]
  wtctl bootstrap [--copy-env]
  wtctl locate <repo-id>
```

## Dependencies

- `git`: binding: TBD (A/B/C), decided in this tool's design session

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/wtctl
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit tool design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
