#!/usr/bin/env bash
# Local CI entry point for Agent Hub.
# Runs the same checks as the GitHub Actions workflow (.github/workflows/ci.yml).
# Creates an isolated .venv to avoid modifying the caller's Python environment.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-python3.11}"
VENV_DIR="$REPO_ROOT/.venv"

# Platform-aware venv python path
if [[ "${OSTYPE:-}" == msys* ]] || [[ "${OSTYPE:-}" == cygwin* ]]; then
    VENV_PYTHON="$VENV_DIR/Scripts/python"
else
    VENV_PYTHON="$VENV_DIR/bin/python"
fi

# ── Python ──────────────────────────────────────────────────────────────────
echo "=== Create project venv (.venv) ==="
uv venv "$VENV_DIR" --python "$PYTHON" --clear

echo "=== Sync locked dependencies ==="
uv pip sync requirements.lock --python "$VENV_PYTHON"

echo "=== Install project (editable, no-deps) ==="
uv pip install -e . --no-deps --python "$VENV_PYTHON"

echo "=== Ruff check ==="
"$VENV_PYTHON" -m ruff check .

echo "=== Ruff format ==="
"$VENV_PYTHON" -m ruff format --check .

echo "=== Pytest ==="
"$VENV_PYTHON" -m pytest

echo "=== pip-audit ==="
"$VENV_PYTHON" -m pip_audit -r requirements.lock --strict --desc

# ── Frontend ────────────────────────────────────────────────────────────────
echo "=== Frontend: npm ci ==="
cd web/frontend
npm ci

echo "=== Frontend: lint ==="
npm run lint

echo "=== Frontend: test ==="
npm run test

echo "=== Frontend: build ==="
npm run build

echo "=== Frontend: npm audit ==="
npm audit --audit-level=high

echo "=== Frontend: Playwright smoke ==="
npx playwright install chromium
npx playwright test --project=smoke

cd "$REPO_ROOT"
echo "=== All CI checks passed ==="
