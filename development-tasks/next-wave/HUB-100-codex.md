# HUB-100: SQLite, CAS, Idempotency And Leases

Task ID: `HUB-100`

Owner: Codex + GPT-5.6 xhigh

Reviewer: Claude Code

Branch: `agent/codex-runtime-security`

Worktree: `E:\agent_hub_worktrees\codex-runtime-security`

## Goal

Implement the durable storage/concurrency foundation consumed by every later workflow task, using short SQLite transactions and explicit compare-and-swap semantics.

## Owned Paths

- `migrations/**`
- `storage/db.py`
- `storage/repositories.py`
- `storage/idempotency_repository.py`
- new storage lease/migration modules
- `app/cli.py` only for DB initialization and single-Master startup checks
- focused storage/migration/lease tests

## Forbidden Paths

- `protocol/**` and frozen TypeScript contracts
- `master/planner.py`, `master/router.py`
- workflow compiler/executor implementation
- frontend files

## Required Behavior

1. Implement the v1 schema from chapter 19 with schema versioning, FK/CHECK constraints and required partial/unique indexes.
2. Configure each connection consistently: foreign keys on, WAL, bounded busy timeout and Repository-owned short transactions.
3. Repository status changes use expected-state/version CAS; stale writers fail without partial updates.
4. Idempotency is atomic with the business mutation: same key/hash replays the stored response, same key/different hash conflicts.
5. Master/workspace leases support acquire, heartbeat, release, expiry takeover and monotonically increasing fencing tokens.
6. A second live Master fails fast; stale owners cannot commit after lease loss.
7. Initialization is idempotent and never places runtime DB/data under the source tree by default.

## Required Tests

- migration from an empty DB and repeated initialization
- FK/CHECK/index enforcement
- CAS success and stale-version conflict
- idempotency replay/hash conflict/transaction rollback
- two-connection lease contention, heartbeat, expiry takeover and stale fencing rejection
- CLI `init-db` smoke using a temporary `AGENT_HUB_DATA_DIR`
- full pytest and Ruff checks

## Acceptance

The database initializes deterministically; concurrency tests prove only one valid owner and no split-brain write; repositories expose typed methods rather than raw rows/dicts. Commit once as `feat(HUB-100): ...` and request Claude Code review before merge.
