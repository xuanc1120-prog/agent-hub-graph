# Agent Hub: HUB-410 Mock GUI

Base: `1778c7b2c67c48732eed2cd49fc0c958713b16e2` (HUB-400, PR #9).
This slice uses the existing same-origin HTTP API and the built-in Mock Agent.

## Run the demo

From the HUB-410 worktree, with Python project dependencies already installed:

```powershell
npm --prefix web/frontend ci
npm --prefix web/frontend run build
$python = 'E:\agent_hub_graph\.venv\Scripts\python.exe'
$demo = Join-Path ([System.IO.Path]::GetTempPath()) ("agent-hub-demo-" + [guid]::NewGuid())
$repo = Join-Path $demo 'source'
New-Item -ItemType Directory -Path $repo -Force | Out-Null
Set-Content -LiteralPath (Join-Path $repo 'README.md') -Value '# Demo repository'
git -C $repo init -b main
git -C $repo add README.md
git -C $repo -c user.name=Demo -c user.email=demo@example.invalid commit -m 'Demo baseline'
$repo
& $python -m app serve --host 127.0.0.1 --port 8765 --data-dir (Join-Path $demo 'data')
```

Open [127.0.0.1:8765](http://127.0.0.1:8765/) and enter the token shown once
in the server terminal. Enter the printed repository path, create the session,
and generate the plan. Select the task in the Author graph, enable **Create
documentation file**, enter `docs/demo.md`, and save. Validate, review the
read-only Compiled graph, and select **Run**. When approval is pending, select
**View diff**, then approve or reject.

Approval creates the commit in the session's integration checkout under
`data/workspaces/shared`. The source repository is unchanged. Rejection does
not apply the patch. A new session can be started after the run stops.

The token and current session are held in page memory. Reloading the page clears
both. An interrupted mutation offers **Retry same request**, which resends the
original body and idempotency key. A version conflict requires an explicit
reload; that reload discards the stale draft.

## Verify

From `web/frontend`, using the existing Playwright Chromium installation
(install with `npx playwright install chromium` if needed):

```powershell
npm run lint
npm test
npm run build
npm run test:e2e
$env:HUB410_PYTHON = 'E:\agent_hub_graph\.venv\Scripts\python.exe'
npm run test:e2e:mock
git diff --check
```

The Mock suite starts a real `python -m app serve` on a temporary loopback
port, with isolated repositories and runtime data. It exercises approval,
rejection, and a concurrent Author edit. It verifies actual Git commits,
file contents, and clean working trees. It does not use browser request mocks.
Trace, video, and screenshots are disabled for this authenticated suite.
On Windows, run the suite with permissions to create the backend's private
artifact directories. Temporary `agent-hub-410-*` directories remain available
for inspecting the Git evidence after the server stops.

Verified on Windows: lint and production build passed, Vitest 9 passed,
static Playwright smoke 1 passed, and real Mock Playwright 3 passed.
The Windows Vitest and browser runs were verified outside the filesystem
sandbox. Full repository CI was not rerun for this frontend slice.

This slice does not add run history, draft recovery across reloads, persisted
graph positions, WebSocket/Console, or real adapters. The real-backend suite
is a separate command from the existing static smoke test.
