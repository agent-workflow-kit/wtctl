# wtctl: design

Status: Seeded from awkit architecture v1.4; not yet designed.

## Role and layer

Git worktrees, throwaway checkouts, and the mapping from repo ids to local clones. Layer 1 (primitive). Must work fully standalone (architecture §2, C2).

## Responsibilities

From architecture §5.1:

Git worktrees, throwaway checkouts, and the mapping from repo ids to local clones.

- Commands: `create [--repo <id>] [--branch <name>] [--throwaway --ref <sha>]`, `list`, `remove [--merged-head <sha>] [--force]`, `bootstrap [--copy-env]`, `locate <repo-id>`.
- Uses: `git`.
- Notes:
  - Owns the repo id → local clone mapping (`repos.<id>.clone: <dir>`, with optional auto-clone). agent-spawn, review-run and ticket-work resolve local clones through `wtctl locate`.
  - Naming conventions are configurable. Dependency bootstrapping (shared package-store settings, per-worktree port/container prefixes) is configurable; env-file copying is opt-in per call (`bootstrap --copy-env`). Supports labels.
  - `remove` is confirm-by-default (section 12.4) and refuses dirty or unpushed worktrees (exit 20, `reason` `dirty` or `unpushed`) unless a human passes `--force`; `--yes` never bypasses that refusal. "Unpushed" means HEAD is not reachable from any remote ref **and** is not equal to a supplied `--merged-head <sha>`; callers pass the merged PR head so squash merges do not block removal.
  - **Throwaway checkouts.** `create --throwaway --ref <sha>` creates a separate disposable clone (not a linked worktree, so nothing run in it can touch the user clone's `.git` hooks or config), detached at the sha. `remove` of a throwaway checkout skips the confirmation and the dirty/unpushed checks.

## Uses

- `git`: binding: TBD (A/B/C), decided in this tool's design session

## Architecture sections to read

Section numbers refer to `../awkit-docs/architecture.md` (v1.4).

- §3.4: never destroy work; canonical repo ids.
- §6: conventions summary.
- §9.2: repo-config baseline recorded at launch by the launcher (hashes of `.git/config`, `config.worktree`, hooks dir), relevant to what this tool's git operations must not disturb.
- §9.5: teardown semantics this tool's `remove` must support: `--yes` for local removal, refusals for dirty/unpushed, human-only `--force`, `--merged-head` for squash merges.
- §12.1: public default for tearing down a worktree.
- §12.4: confirmations, holds and `--yes` (`remove` is a confirm-by-default primitive without a pending store).
- §13.2: rule 9 (throwaway checkouts for third-party code) and rule 10 (repository-controlled config and hooks).
- §14: audit.
- §20: this tool's constraints and open topics.

## Constraints (decided)

From architecture §20: "dirty" means any tracked change or any untracked, non-ignored file; ignored files are listed in the refusal unless they were created by `bootstrap`. `remove` fetches first. The stash check covers only stash entries based on the worktree's branch. The pushed check exempts the worktree's own branch when HEAD equals `--merged-head` or is reachable from a remote ref. Refusals exit 20 with a `reason`.

## Open topics

From architecture §20:

- worktree location
- branch naming
- dependency bootstrap
- port/container isolation
- env-file copying
- dangling worktree recovery
- cleanup policy
- auto-clone details
- throwaway clone source

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. Where are worktrees created (relative to the clone, a central dir, configurable)?
2. What is the default branch naming scheme, and how is it configured?
3. How does dependency bootstrap work (shared package-store settings, what runs, what is recorded)?
4. How are per-worktree ports/containers isolated (prefixes, allocation)?
5. How does env-file copying work (`bootstrap --copy-env`: which files, from where)?
6. How are dangling worktrees (missing dirs, stale git metadata) detected and recovered?
7. What is the cleanup policy for worktrees and throwaway checkouts?
8. What are the auto-clone details (when, where, with which credentials)?
9. What is the clone source for throwaway checkouts (local clone vs remote URL)?
