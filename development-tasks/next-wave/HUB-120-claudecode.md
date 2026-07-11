# HUB-120: Planner, Router And Workflow Lineage

Task ID: `HUB-120`

Owner: Claude Code + Claude Opus 4.8

Reviewer: Codex

Branch: `agent/claude-architecture-ui`

Worktree: `E:\agent_hub_worktrees\claude-architecture-ui`

## Goal

Implement deterministic planning and assignment around the frozen v1 protocol. LLM/OpenCode planning may propose a draft, but RuleBasedPlanner remains the reliable fallback and no planner output executes directly.

## Owned Paths

- `master/planner.py`
- `master/router.py`
- planner-specific modules under `master/**`
- `context/planner_bundle.py` only
- planner/router tests
- frontend fixture data/type smoke under `web/frontend/src/fixtures/**`
- planner ADR only when a real architecture decision is needed

## Forbidden Paths

- `protocol/**` without a new accepted ADR
- migrations, SQLite repositories and lease implementation
- GraphExecutor, subprocess, Git/workspace, Guard or merge code
- production React Flow editor/components

## Required Behavior

1. RuleBasedPlanner deterministically generates valid bugfix, feature, refactor and docs WorkflowDraft templates with stable ordering/IDs.
2. Planner input is a bounded PlannerContextBundle without secrets or unrestricted repo content.
3. AgentRouter scores only catalog capabilities/availability and task needs; manual/locked assignments are preserved and unavailable agents produce explicit validation outcomes.
4. OpenCode planner failure, timeout or invalid structured output creates separate run evidence and invokes RuleBased fallback; it never overwrites the failed run.
5. Replan creates lineage metadata referencing a same-session parent workflow and never mutates the parent.
6. Planner returns only WorkflowDraft/decision objects and cannot execute commands, edit files, approve or merge.
7. Add AuthorGraph/CompiledGraph fixture data that type-checks against the frozen frontend contract; full React Flow behavior remains HUB-410.

## Required Tests

- deterministic template snapshots for all four task families
- invalid/oversized input and unknown task family behavior
- auto/manual/locked routing and unavailable-agent cases
- fallback creates distinct run/result metadata
- cross-session parent rejection and parent immutability
- Python pytest/Ruff and frontend type-check for fixtures

## Acceptance

The same normalized input/catalog produces the same WorkflowDraft and routing decision; failures retain evidence and fallback is explicit. Commit once as `feat(HUB-120): ...` and return contract assumptions for Codex review.
