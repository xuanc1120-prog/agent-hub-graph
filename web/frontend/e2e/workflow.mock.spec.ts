import { execFileSync, spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { once } from 'node:events'
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createInterface } from 'node:readline'
import { fileURLToPath } from 'node:url'
import { expect, test as base } from '@playwright/test'
import type { Page } from '@playwright/test'
import type { AuthorGraph } from '../src/api/protocol'

const repoRoot = fileURLToPath(new URL('../../../', import.meta.url))
const git = (cwd: string, ...args: string[]) =>
  execFileSync('git', args, { cwd, encoding: 'utf8', windowsHide: true }).trim()

async function reservePort() {
  const server = createServer().listen(0, '127.0.0.1')
  await once(server, 'listening')
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('No local TCP port')
  await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
  return address.port
}

const test = base.extend<object, { backend: { origin: string; token: string; folder: string } }>({
  // Playwright requires destructured fixture dependencies, even when empty.
  // eslint-disable-next-line eslint/no-empty-pattern
  backend: [async ({}, provide) => {
    if (!existsSync(join(repoRoot, 'web', 'frontend', 'dist', 'index.html'))) {
      throw new Error('Run npm run build before the real Mock browser tests.')
    }
    const folder = mkdtempSync(join(tmpdir(), 'agent-hub-410-'))
    const port = await reservePort()
    const origin = `http://127.0.0.1:${port}`
    const child = spawn(process.env.HUB410_PYTHON || 'python', [
      '-m', 'app', 'serve', '--host', '127.0.0.1', '--port', String(port), '--data-dir', join(folder, 'data'),
    ], { cwd: repoRoot, stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true })
    const lines = createInterface({ input: child.stderr })
    let token = ''
    let exited = false
    let spawnFailed = false
    let startupError = ''
    child.on('exit', () => { exited = true })
    child.on('error', () => { spawnFailed = true })
    lines.on('line', (line) => {
      const match = /^Agent Hub API bearer token \(shown once\): (\S+)$/.exec(line.trim())
      if (match) token = match[1]
      else if (/^(PermissionError|ModuleNotFoundError|OSError|ERROR:)/.test(line)) startupError = line
    })
    try {
      const deadline = Date.now() + 30_000
      while (!exited && !spawnFailed && Date.now() < deadline) {
        if (token && await fetch(`${origin}/healthz`, { signal: AbortSignal.timeout(1000) })
          .then((response) => response.ok, () => false)) break
        await new Promise((resolve) => setTimeout(resolve, 100))
      }
      if (!token || exited || spawnFailed || Date.now() >= deadline) {
        throw new Error(`Local backend did not become ready. ${startupError || 'Check HUB410_PYTHON and runtime permissions.'}`)
      }
      await provide({ origin, token, folder })
    } finally {
      lines.close()
      if (!exited && child.pid) {
        const closed = once(child, 'exit')
        if (process.platform === 'win32') {
          try {
            execFileSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
              windowsHide: true, stdio: 'ignore', timeout: 5000,
            })
          } catch {
            child.kill()
          }
        } else {
          child.kill('SIGTERM')
        }
        let timer: ReturnType<typeof setTimeout> | undefined
        try {
          await Promise.race([closed, new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error('Local backend did not stop.')), 10000)
          })])
        } finally {
          clearTimeout(timer)
        }
      }
    }
  }, { scope: 'worker', timeout: 45_000 }],
})

function fixture(folder: string, name: string) {
  const path = join(folder, name)
  mkdirSync(path)
  writeFileSync(join(path, 'README.md'), '# Local test repository\n')
  git(path, 'init', '-b', 'main')
  git(path, 'add', 'README.md')
  git(path, '-c', 'user.name=Agent Hub Test', '-c', 'user.email=test@example.invalid',
    'commit', '-m', 'fixture baseline')
  return path
}

async function enterToken(page: Page, token: string) {
  await page.getByLabel('Bearer token').fill(token, { timeout: 5000 }).catch(() => {
    throw new Error('The bearer token field could not be filled.')
  })
}

for (const decision of ['Approve', 'Reject'] as const) {
  test(`real Mock workflow: ${decision.toLowerCase()} change`, async ({ page, backend }) => {
    const repository = fixture(backend.folder, decision.toLowerCase())
    const baseCommit = git(repository, 'rev-parse', 'HEAD')
    const file = 'docs/hub410-demo.md'
    await page.goto(backend.origin)
    await enterToken(page, backend.token)
    await page.getByLabel('Repository path').fill(repository)
    await page.getByRole('button', { name: 'Create session' }).click()
    await expect(page.getByTestId('session-id')).toHaveText(/^session-/)
    const sessionId = await page.getByTestId('session-id').innerText()
    const directory = createHash('sha256').update(sessionId).digest('hex').slice(0, 32)
    const sharedRepo = join(backend.folder, 'data', 'workspaces', 'shared', `session-${directory}`, 'repo')

    const submittedPlan = page.waitForResponse((response) => response.url().endsWith('/plans') &&
      response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Generate plan' }).click()
    expect((await submittedPlan).status()).toBe(202)
    await expect(page.getByText('Plan: succeeded')).toBeVisible()
    await expect(page.locator('.react-flow__node')).toHaveCount(3)
    await page.getByLabel('Node title').fill('Write the demo guide')
    await page.getByLabel('Create documentation file').check()
    await page.getByLabel('New file path').fill(file)
    await page.getByRole('button', { name: 'Save edits' }).click()
    await expect(page.getByText('v2', { exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Validate', exact: true }).click()
    await expect(page.getByText('Validated', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Compiled', exact: true })).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByText('Read only')).toBeVisible()
    const submittedRun = page.waitForResponse((response) => response.url().endsWith('/runs') &&
      response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Run', exact: true }).click()
    expect((await submittedRun).status()).toBe(202)
    await expect(page.getByTestId('run-status')).toHaveText('waiting_approval')
    expect(git(sharedRepo, 'rev-parse', 'HEAD')).toBe(baseCommit)
    expect(existsSync(join(sharedRepo, file))).toBe(false)
    await expect(page.getByRole('button', { name: decision, exact: true })).toHaveCount(0)
    await page.getByRole('button', { name: 'View diff' }).click()
    await expect(page.getByLabel('Change diff')).toContainText(file)
    await expect(page.getByLabel('Change diff')).toContainText('+# Agent Hub demo change')
    await page.getByRole('button', { name: decision, exact: true }).click()
    if (decision === 'Approve') {
      await expect(page.getByTestId('run-status')).toHaveText('completed')
      const commit = await page.getByTestId('current-commit').innerText()
      expect(commit).not.toBe(baseCommit)
      expect(git(sharedRepo, 'rev-parse', 'HEAD')).toBe(commit)
      expect(git(sharedRepo, 'diff', '--name-only', baseCommit, commit)).toBe(file)
      expect(readFileSync(join(sharedRepo, file), 'utf8').trim()).toBe('# Agent Hub demo change')
    } else {
      await expect(page.getByText('Decision: rejected')).toBeVisible()
      await expect(page.getByTestId('run-status')).toHaveText(/^(blocked|failed|completed|cancelled)$/)
      expect(git(sharedRepo, 'rev-parse', 'HEAD')).toBe(baseCommit)
      expect(existsSync(join(sharedRepo, file))).toBe(false)
    }
    expect(git(sharedRepo, 'status', '--porcelain')).toBe('')
    expect(git(repository, 'rev-parse', 'HEAD')).toBe(baseCommit)
    expect(git(repository, 'status', '--porcelain')).toBe('')
    await expect(page).toHaveURL(`${backend.origin}/`)
    expect(await page.evaluate(() => localStorage.length + sessionStorage.length)).toBe(0)
  })
}

test('stale Author save stops and requires an explicit reload', async ({ page, request, backend }) => {
  const repository = fixture(backend.folder, 'conflict')
  await page.goto(backend.origin)
  await enterToken(page, backend.token)
  await page.getByLabel('Repository path').fill(repository)
  await page.getByRole('button', { name: 'Create session' }).click()
  const loaded = page.waitForResponse((response) =>
    /\/workflows\/[^/]+$/.test(response.url()) && response.request().method() === 'GET')
  await page.getByRole('button', { name: 'Generate plan' }).click()
  const response = await loaded
  const workflow = await response.json() as { author_graph: AuthorGraph; semantic_version: number }
  const task = workflow.author_graph.nodes.find((node) => node.node_type === 'agent_task')!
  task.title = 'External edit'
  const update = await request.put(response.url(), {
    headers: { Authorization: `Bearer ${backend.token}`, 'Idempotency-Key': 'hub410-external-edit' },
    data: { author_graph: workflow.author_graph, expected_semantic_version: workflow.semantic_version },
  })
  expect(update.status()).toBe(200)
  await page.getByLabel('Node title').fill('My stale edit')
  await page.getByRole('button', { name: 'Save edits' }).click()
  await expect(page.getByRole('alert')).toContainText('Version or evidence conflict')
  await expect(page.getByLabel('Node title')).toHaveValue('My stale edit')
  await expect(page.getByRole('button', { name: 'Validate', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: 'Reload workflow' }).click()
  await expect(page.getByLabel('Node title')).toHaveValue('External edit')
})
