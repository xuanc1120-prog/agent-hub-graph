# Agent Hub

Agent Hub is a local visual orchestration demo for coding agents. It combines deterministic workflow execution with a visual AuthorGraph/CompiledGraph model, bounded planner input, auditable agent routing, and local-first storage.

## Project Status

Development is in stage 1. The repository currently includes:

- frozen v1 protocol contracts for workflows, tasks, events, approvals, and artifacts;
- SQLite migrations, compare-and-swap repositories, idempotency, and fenced leases;
- deterministic bugfix, feature, refactor, and docs planners with explicit fallback evidence;
- capability-based agent routing and workflow lineage metadata;
- CI, dependency audits, frontend contract fixtures, and Playwright smoke coverage.

The Compiler/Executor/MockAgent vertical slice and the full React Flow experience are not implemented yet. See `agent-hub-task-allocation.md` for the live task board.

## Prerequisites

- Python 3.11+
- Node.js 20+
- Git
- `uv` is recommended for deterministic Python setup

## Backend

```powershell
uv venv .venv
uv pip sync requirements.lock
uv pip install -e . --no-deps
.venv\Scripts\agent-hub.exe init-data
.venv\Scripts\agent-hub.exe serve
```

The API listens on `http://127.0.0.1:8765` by default. Runtime data is stored outside the source tree under `%LOCALAPPDATA%\AgentHub` on Windows or `~/.local/share/agent-hub` on POSIX systems. Override it with `AGENT_HUB_DATA_DIR`.

## Frontend

```powershell
cd web\frontend
npm ci
npm run dev
```

## Verification

Run the full CI suite locally (creates `.venv`, does not modify your environment):

```powershell
# Windows (PowerShell) — default
scripts\ci.ps1

# WSL / macOS / Linux
bash scripts/ci.sh
```

Or run individual checks manually:

```powershell
# Python (inside activated .venv)
uv pip sync requirements.lock
uv pip install -e . --no-deps
python -m ruff check .
python -m ruff format --check .
python -m pytest
python -m pip_audit -r requirements.lock --strict --desc

# Frontend
cd web\frontend
npm ci
npm run lint
npm run test
npm run build
npm audit --audit-level=high
npx playwright install chromium
npx playwright test --project=smoke
```

The CI script and `.github/workflows/ci.yml` run the same checks. Both create
an isolated venv with `uv venv`, sync dependencies from `requirements.lock`,
and install the project with `--no-deps` to avoid re-resolving.

See `agent-hub-development-plan.md` for the architecture and `agent-hub-task-allocation.md` for task ownership.
