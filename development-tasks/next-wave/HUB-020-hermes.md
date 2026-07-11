# HUB-020: CI And Test Baseline

Task ID: `HUB-020`

Owner: Hermes + MiMo-V2.5-Pro

Reviewer: Codex

Branch: `agent/hermes-qa-support`

Worktree: `E:\agent_hub_worktrees\hermes-qa-support`

## Goal

Finish the stage 0 quality baseline without changing runtime behavior: reproducible CI commands, dependency audits, Vitest/Playwright setup and deterministic fixture repositories.

## Owned Paths

- `.github/workflows/**`
- `scripts/ci*`
- `tests/fixtures/source_repo/**`
- `tests/ci/**`
- `web/frontend/e2e/**`
- `web/frontend/playwright.config.ts`
- `web/frontend/package.json` and `package-lock.json` only for test scripts/dependencies
- CI/testing documentation

## Forbidden Paths

- `protocol/**`
- `migrations/**`, `storage/**`, `workflow/**`, `master/**`
- application behavior under `app/**`
- product UI components

## Required Behavior

1. Preserve existing hash-locked Python and npm installs; regenerate locks only when a required test dependency changes.
2. Provide one local CI entry point and one CI workflow that run Python tests/Ruff, frontend lint/test/build and dependency audits.
3. Add a minimal Playwright smoke test against the frontend baseline; do not start implementing HUB-410 UI behavior.
4. Keep fixture repos tiny, deterministic and free of credentials, absolute local paths and nested real `.git` directories.
5. High/critical audit failures fail CI unless a dated, documented exception exists.

## Required Tests

- clean `uv pip sync requirements.lock`
- `python -m pytest`
- `ruff check .` and `ruff format --check .`
- `npm ci`, `npm run lint`, `npm run test`, `npm run build`
- Playwright smoke
- `pip-audit` and `npm audit --audit-level=high`

## Acceptance

Fresh setup commands are documented and reproducible; CI and local entry points run the same checks; no frozen protocol or runtime file changes. Commit once as `ci(HUB-020): ...` and report changed files, test output, risks and base commit.
