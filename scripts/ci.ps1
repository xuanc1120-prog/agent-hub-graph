param(
    [string]$Python = "python3.11"
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Invoke-Checked {
    param([scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command"
    }
}

$VenvDir = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

# ── Python ──────────────────────────────────────────────────────────────────
Write-Host "=== Create project venv (.venv) ==="
Invoke-Checked { uv venv $VenvDir --python $Python --clear }

Write-Host "=== Sync locked dependencies ==="
Invoke-Checked { uv pip sync requirements.lock --python $VenvPython }

Write-Host "=== Install project (editable, no-deps) ==="
Invoke-Checked { uv pip install -e . --no-deps --python $VenvPython }

Write-Host "=== Ruff check ==="
Invoke-Checked { & $VenvPython -m ruff check . }

Write-Host "=== Ruff format ==="
Invoke-Checked { & $VenvPython -m ruff format --check . }

Write-Host "=== Pytest ==="
Invoke-Checked { & $VenvPython -m pytest }

Write-Host "=== pip-audit ==="
Invoke-Checked { & $VenvPython -m pip_audit -r requirements.lock --strict --desc }

# ── Frontend ────────────────────────────────────────────────────────────────
Write-Host "=== Frontend: npm ci ==="
Set-Location (Join-Path $RepoRoot "web\frontend")
Invoke-Checked { npm ci }

Write-Host "=== Frontend: lint ==="
Invoke-Checked { npm run lint }

Write-Host "=== Frontend: test ==="
Invoke-Checked { npm run test }

Write-Host "=== Frontend: build ==="
Invoke-Checked { npm run build }

Write-Host "=== Frontend: npm audit ==="
Invoke-Checked { npm audit --audit-level=high }

Write-Host "=== Frontend: Playwright smoke ==="
Invoke-Checked { npx playwright install chromium }
Invoke-Checked { npx playwright test --project=smoke }

Set-Location $RepoRoot
Write-Host "=== All CI checks passed ==="
