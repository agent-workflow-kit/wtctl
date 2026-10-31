# wtctl: implementation plan

Status: planned, 2026-10-30. Implements [interface.md](interface.md) (normative) using the decisions and internal architecture in [design.md](design.md). Work is tracked as GitHub issues in this repo; each issue names its stage below. The bite-sized, test-first steps for an issue are written when the issue is picked up.

## Conventions for every stage

- Test-first: each behavior starts as a failing test. Unit and integration tests use real git in temporary directories (no network); the process scanner and the clock are faked through `gateway`.
- Every `--json` output gets golden files validated against `schemas/` with a `referencing` registry keyed by `$id`; every hook payload is validated the same way.
- Every relied-on git behavior from the spikes and the safety checks (design.md, Live verification) becomes a regression test in the stage that relies on it. Security vectors (planted config, forged history, symlink swaps) are ordinary tests: the payload only touches a marker file, and the test asserts the marker is absent.
- Tests that need the network or real credentials carry the pytest marker `live`, are deselected by default and never run in CI.
- `make verify` passes before every commit. Runtime dependencies: `pyyaml`, `pydantic`. Test-only: `pytest`, `jsonschema`, `referencing`.
- No awkit tool is imported or called; git is the only external program (plus `lsof` on macOS).

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `cli` | 0 | argparse surface, global flags, actor and human-only rules, exit-code mapping |
| `config` | 0 | YAML + drop-ins + `WTCTL_*` env, Pydantic validation, per-repo resolution, path rules for roots |
| `state` | 0 | `state.db` schema and migrations, leases (PID + start time + expiry, `lease_wait`), compare-and-set state transitions |
| `gateway` | 0 | the only module that runs subprocesses or reads `/proc`: git runner, step runner (process groups, timeouts), process scanner, clock |
| `output` | 0 | human tables (control characters escaped) and `--json` rendering, error objects |
| `audit` | 0 | `log.jsonl`, `audit.jsonl`, `export` |
| `hooks` | 0 | hook execution (process groups, timeouts, veto/escalate, `--override-escalation`), payload building |
| `hardgit` | 1 | hardened invocation (clean environment, overrides, filter blanking, allowed filters, protocols, lazy-fetch block), porcelain parsing, fetch outcome classification |
| `fsafe` | 1 | no-follow per-component opens, fd-based tree deletion, extraction (validation, read, atomic write), atomic pointer rewrites |
| `urls` | 1 | URL normalization, repo-id matching, clone fetch URL |
| `mirror` | 1 | mirror creation and config, refresh, default branch, pushed check with content-verified ancestry, base-commit transfer into a clone |
| `locate` | 1 | config lookup, recorded clones, discovery, `clone`/auto-clone (temp dir + rename) |
| `create` | 2 | worktree creation order, names and branches, holders, `reused` |
| `bootstrap` | 2 | snapshots, env-file copy, steps, bootstrap-created and `bootstrap_nested` sets |
| `baseline` | 2 | item collection (own config parser, includes, classes), digest, compare, accept |
| `checks` | 3 | integrity (incl. `dir_id`), content checks, in-use detection |
| `remove` | 3 | removal orders, fd-based deletion, admin entry, branch CAS delete, partial records |
| `throwaway` | 4 | throwaway creation order, removal, `extract`, `prune` |
| `reconcile` | 5 | `creating` reclaim, pointer rewrite, missing/grace, admin-entry deletion, strays, tombstones |

## Stage 0: Foundations

**Goal**: The skeleton every command uses: CLI, config, state, output, audit and hooks, with no git behavior yet.

**Success Criteria**: `wtctl --version`, `wtctl list`, `wtctl show`, `wtctl label add/remove`, `wtctl export` and `wtctl cron install|uninstall` work against a state database seeded by tests; every error path prints the documented error object; config validation rejects every invalid key with a message naming it.

**Tests**:

- config: built-in defaults equal `config.example.yaml` and `schemas/config.json` defaults; drop-in merge (maps merge, lists replace, `hooks.<name>` append); `WTCTL_<KEY>` for top-level scalars only; per-repo precedence; root path rules ([Locations](interface.md#locations): hidden home directories and XDG homes refused, exit 2); DURATION and decision-id grammars.
- state: migrations from empty; leases (take, wait up to `lease_wait`, dead-holder takeover by PID + start time, `busy`); compare-and-set transitions lose cleanly; ids `wt-` + 8 base32 never reused; prefix resolution (≥ 4, ambiguous 21, tombstones only by full id).
- labels: grammar, 32-pair limit, multiset semantics, sorted `key=value` arrays in `--json`.
- actor and human-only rules: human-only commands exit 13 without a TTY or `--actor human:`; human-only flags exit 2 ([Audit and log](interface.md#audit-and-log)).
- hooks: order, veto (11), escalation (13), `--override-escalation`, timeouts kill the hook's process group, `decision_id` capture, environment without token variables, payloads valid against `schemas/hooks/`.
- output: golden `--json` for `list`, `show`, `label-*`, `export`, `cron-*`, `error`; human output escapes control characters.
- cron: managed block `# wtctl managed`, minutes 17 and 47, `prune --throwaways --if-configured`, `--actor auto:cron`, macOS `PATH` additions.

**Status**: Not Started

## Stage 1: Hardened git, filesystem safety and the mirror (0.1)

**Goal**: Every git call goes through the hardened invocation; clone resolution and the mirror work; `locate` and `clone` are usable.

**Success Criteria**: `wtctl locate` and `wtctl clone` behave as in [Clone resolution](interface.md#clone-resolution); the mirror refreshes, sets its default branch and answers the pushed check ([Mirror](interface.md#mirror), [Reachability](interface.md#reachability)); `fsafe` primitives pass the adversarial suite.

**Tests**:

- hardgit vector suite (spike C, E, G5; safety check SAFE-2..4): planted `core.fsmonitor`, hooks, `remote.<n>.uploadpack`, `insteadOf` to `ext::`, filter drivers, `pager.<cmd>`, `log.showSignature` + `gpg.program`, `core.askPass`, submodule config, caller environment, `http.*extraheader`, promisor remote with `uploadpack` (lazy fetch blocked by protocols `none`), `file://` URL handling — no marker for any command wtctl runs.
- forged history (spike E1a–f): replace refs, grafts, commit-graph, same-sha object substitution — all reported unpushed.
- allowed filters: only drivers with a global `process`; planted repository values do not override; missing global definition stays blanked with a warning.
- fetch outcome classification (spike F4): lock contention retried; 401/auth substrings → `auth_required`; exit 0 with `error:` → `fetch_failed` (`prune_incomplete`); timeouts → `cause: timeout`.
- urls: normalization table (HTTPS, SSH, scp-like, ports, userinfo stripped, GitLab `.git`), repo id grammar rejects `.`/`..` segments, `remote_mismatch`.
- locate: config entry, configured-but-missing (exit 10), recorded clone, discovery (depth, symlinks not followed, cache re-verification, ambiguity 21), auto-clone and `clone` into a temp dir + rename under a per-repo lease, `clone_failed`/`auth_required`, SHA-256 repositories refused.
- credentials: `token_env` then `token_command` (argv, timeout, never logged), URL-scoped header, `http.followRedirects=false`, SSH `BatchMode`.
- mirror: creation and config (only wtctl's keys; rewritten when it differs), path keyed by a hash of the clone's common dir, treeless refresh, default branch from `ls-remote --symref` run in the mirror, pushed check (exists + ancestor + re-hash, protocols `none`, missing → unpushed), immunity to planted clone config.
- pack transfer (spike H): non-thin pack built in the mirror, indexed outside any repository (`--fsck-objects`, `GIT_CEILING_DIRECTORIES`), installed by no-follow file writes, missing-object check before use; a failed `rev-list` never yields a truncated pack; every command against the clone runs with protocols `none`, and a planted promisor remote with emulated unprotected git (`GIT_NO_LAZY_FETCH=0`) leaves no marker (SAFE2-2).
- fsafe: no-follow opens (symlinked components, hard links, FIFOs, directories, `.git` variants incl. case folding and NTFS/HFS names), fd-based deletion with a concurrent symlink swap (SAFE-1), never crossing a mount point (`st_dev` and mount id; a FUSE or bind mount inside the tree is skipped and reported, SAFE2-1), `u+w` on owned read-only directories, atomic pointer rewrite through the no-follow fd chain.
- `live`: anonymous fetch-by-sha and treeless refresh on github.com and gitlab.com; token header against a private repository on each forge; `token_command` with `gh`/`glab`.

**Status**: Not Started

## Stage 2: Create, bootstrap, baseline, adopt (0.2)

**Goal**: Worktrees can be created, bootstrapped, adopted and forgotten, with a recorded baseline.

**Success Criteria**: [Create order](interface.md#create-order), [Bootstrap](interface.md#bootstrap), [Baseline](interface.md#baseline), `adopt`, `forget` and `bootstrap` behave as specified, including holders and `reused`.

**Tests**:

- create: name and branch derivation (template, prefix, suffixes against records and existing paths, `check-ref-format`), existing local branch, remote-only branch via the mirror (explicit upstream config, `--no-track` for new branches), `branch_in_use` with `holder_id`, `branch_exists`, `path_exists`, `--detach`, `--ref`, `--base` resolution and `base_sha`, base-commit transfer into the clone through the mirror (planted clone config inert, per-call symlink), `pre-create` before any branch write, leaked branch deleted on failure, `creating` record and lease in one transaction, `--holder` and idempotent `reused`, `--dry-run`.
- bootstrap: steps (argv/`sh`, timeout kills the process group), environment without token variables plus `pass_env`, ignored/untracked snapshots (`-uall` exact paths with content hash; ignored directories by prefix), `copy_env` (source walk no-follow and link count 1; destination no-follow; mode 0600; never overwrite; tracked conflicts skipped), `bootstrap_nested`, failure keeps the worktree (`bootstrap_failed`), re-run human-only and refusing on either baseline class.
- baseline: parsed config minus the benign allowlist and `baseline.ignore_config_keys`, includes resolved by wtctl's parser, own admin entry, `<admin_dir>/modules/**`, `info/exclude`, `core.excludesFile`, `git` vs `claude` classes, `.claude/.cc-writes/` and empty config files ignored, recorded after bootstrap; routine commands (`push -u`, sibling worktree create/remove, wtctl's own branch writes) leave the digest unchanged (spike F5); `core.hooksPath` changes it.
- adopt/forget: realpath, main worktree, `--holder` reference counting, `not_a_holder`, plain `forget` human-only, throwaways refused, lease and state rules.
- `live`: git-lfs with `create.allow_filters: [lfs]` and the `unsafe_repo_config` refusal for planted LFS transfer keys.

**Status**: Not Started

## Stage 3: Removal (0.3)

**Goal**: Worktrees are removed only when nothing would be lost, by wtctl's own fd-based deletion.

**Success Criteria**: [Remove order](interface.md#remove-order), [Removal checks](interface.md#removal-checks), `status`, `remove --dry-run`, `baseline check` and `baseline accept` behave as specified; every refusal reason and its precedence is reachable in a test; agent-spawn's teardown flow (`remove --yes --holder <h> [--merged-head]`) works end to end against real git.

**Tests**:

- integrity: recorded path and root, `dir_id`, `.git` pointer, `gitdir` back-pointer, `commondir`, `--git-common-dir`; forged pointers → `unlinked`; `locked`.
- content checks: tracked against a private, stat-free index (forged index stat data and untracked cache do not hide changes, SAFE2-3), `hidden_changes` (assume-unchanged, skip-worktree), `mount_point`, untracked (bootstrap-created exempt by exact path and hash), ignored (disposable globs, bootstrap-created prefixes), nested repositories (whole-tree walk, limit, `bootstrap_nested`, `submodule`), stash (entries since create, content-verified ancestry), unpushed head and side branches (reflog anchors, `reflog_unavailable`), `--merged-head`.
- in-use: cwd by directory `(dev, ino)` and path, open files by path, ancestors of wtctl excluded, `(deleted)` suffix, unavailable scanner, sandboxed process with a bind mount at another path (spike E3).
- remove: confirmation (TTY, `--yes`, exit 13), `pre-remove` hooks, `held` and `--holder`, `--force` overrides only `dirty`, `unpushed`, `held` and fetch failures; step-9 re-checks (HEAD moved, new failures refuse even under `--force`); fd-based deletion with a racing symlink swap deletes nothing outside (SAFE-1); admin entry deleted fd-based after `gitdir` verification; partial deletion → `partial` record, human-only finishing; branch CAS delete only for created, pushed or merged branches; tombstones; audit records.
- baseline check/accept: classes, `changed_classes`, `--expect-digest`, re-computation before writing.
- `status`: `would_remove` true/false/null, `human_required`, skipped checks without network, `remove --dry-run` identical output plus `dry_run: true`.

**Status**: Not Started

## Stage 4: Throwaways and extraction (0.4)

**Goal**: Throwaways for review-run and review-respond, safe extraction and pruning.

**Success Criteria**: [Throwaway create order](interface.md#throwaway-create-order), [Throwaway removal](interface.md#throwaway-removal), [Extraction rules](interface.md#extraction-rules) and [Clean checkout for confirm](interface.md#clean-checkout-for-confirm) work; review-respond's round (respond throwaway, clean throwaway with `--source remote`, `extract`, commit and fast-forward push from the shallow clean throwaway, removal of both) runs end to end against a local bare remote.

**Tests**:

- throwaway create: `init --template=`, isolated config, `* -filter`, no reflog, `FETCH_HEAD` removed, `HEAD` verified, sources `auto`/`local`/`remote` with the clone fetch URL, remote depth (default 50, `0` full), local source by pack (full history or depth 1; other depths exit 2 with `--source local`, prefer remote with `auto`), failure removes the directory, location passes claude-session's unsafe-cwd rules (rule list copied into the test).
- extraction: every path rule and error value, all-or-nothing validation, destination rules (no-follow, mode handling, temp + rename), deletions, limits, `--dest` restrictions, `io_error`, concurrent directory swap between validation and write.
- throwaway removal: fd-based, `in_use`, `partial` finishing by automation, read-only directories.
- prune: age, `--if-configured`, `protect_labels`, skipped reasons, retries of partial throwaways.
- push from a depth-50 and a depth-1 throwaway; moved remote and `shallow update not allowed` classification (spike F1).
- `live`: fast-forward push from a shallow throwaway to a real forge branch; fork PR head fetched by sha.

**Status**: Not Started

## Stage 5: Reconcile and recovery (0.5)

**Goal**: State stays honest after crashes, moves and manual changes, without ever deleting user files or branches.

**Success Criteria**: [Reconcile](interface.md#reconcile) and [Commands by record state](interface.md#commands-by-record-state) hold for every state and command; reboot recovery (dead `creating` records, missing directories, moved clones) works.

**Tests**:

- `creating`: skipped while a live process holds the lease; reclaimed (`adopted_after_crash`, `bootstrap.state: interrupted`), `gone` or `abandoned`; in-use check of the planned path; throwaway `creating` deleted fd-based.
- missing: grace period and two observations; admin entry deleted only when owned, unlocked, `gitdir` matches, no `modules/`, no per-worktree refs and every reflog commit pushed (mirror); `kept_unpushed`; directory reappearing; admin entry already gone; adopted records never touch the clone (`adopted_gone`).
- pointer rewrite (never `git worktree repair`): moved worktree and moved clone with `dir_id` verification; a planted admin entry pointing elsewhere is not followed (SAFE-5).
- strays: throwaway directories, clone temp directories, orphaned mirrors reported, never deleted; tombstones expire.
- the commands-by-record-state table as a parametrized test.

**Status**: Not Started

## Stage 6: Platforms and 1.0 readiness

**Goal**: Verified on macOS and WSL, consumers integrated, interface stable.

**Success Criteria**: The macOS checklist (Apple git 2.39, `lsof` in-use detection including sandboxed processes, case-insensitive `.git` handling, `GIT_NO_LAZY_FETCH` availability, cron `PATH`) and the WSL checklist (`/proc` scanning, `/mnt/` warnings, drvfs `.git.`/`GIT~1` names) pass; agent-spawn and review-respond have integrated against the interface without changes to it.

**Tests**:

- manual macOS and WSL checklists recorded in design.md (Live verification).
- `live` suite re-run on the user's machines.
- Large-repository timing: nested scan at the default limit and mirror refresh on a monorepo-sized remote.

**Status**: Not Started

## Version roadmap

- **0.1** (stage 1): hardened git, `locate`, `clone`, the mirror.
- **0.2** (stage 2): `create`, bootstrap, baseline, `adopt`/`forget`, labels, hooks.
- **0.3** (stage 3): `status`, `remove`, `baseline check`/`accept`; enough for agent-spawn's worktree flow.
- **0.4** (stage 4): throwaways, `extract`, `prune`; enough for review-run and review-respond.
- **0.5** (stage 5): `reconcile`, recovery, `cron`.
- **1.0**: stage 6 done; agent-spawn, review-run and review-respond used the interface without changes; declared stable with a deliberate breaking-change commit (architecture §18.3).
- **1.x** (additive): recursive submodule checks (removing worktrees with initialized submodules without `--force`), sparse-checkout support, a `browse` TUI, more fetch-error classifications.
- **2.0**: reserved for a breaking interface change.
