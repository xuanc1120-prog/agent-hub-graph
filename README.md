# Agent Hub

Agent Hub is a local visual orchestration demo for coding agents. The repository currently contains the `HUB-000` project baseline; core protocol schemas are intentionally reserved for `HUB-010`.

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

```powershell
python -m pytest
ruff check .
ruff format --check .
cd web\frontend
npm run lint
npm run test
npm run build
```

See `agent-hub-development-plan.md` for the architecture and `agent-hub-task-allocation.md` for task ownership.
