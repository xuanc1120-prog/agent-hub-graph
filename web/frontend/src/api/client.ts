import type {
  ApprovalStatus, AuthorGraph, NodeRunStatus, PlannerRunStatus, RiskLevel,
  WorkflowLayout, WorkflowRunStatus,
} from './protocol'

export interface Session {
  session_id: string
  integration_head_commit: string
}

export interface Workflow {
  workflow_id: string
  semantic_version: number
  author_graph: AuthorGraph
  layout: WorkflowLayout
}

export interface PlanStatus {
  planner_run_id: string
  status: PlannerRunStatus
  result_workflow_id?: string | null
  error_code?: string | null
}

export interface Approval {
  approval_id: string
  change_set_id: string
  version: number
  subject_sha256: string
  patch_sha256: string
  status: ApprovalStatus
  effective_risk: RiskLevel
  expires_at: string
  scope: string[]
}

export interface RunStatus {
  workflow_run_id: string
  status: WorkflowRunStatus
  current_commit: string
  approvals: Approval[]
  nodes: { node_id: string; node_type: string; status: NodeRunStatus; error_code?: string | null }[]
}

export class ApiError extends Error {
  status: number

  constructor(status: number) {
    super(status === 401 ? 'Bearer token was rejected.' : `Request failed (HTTP ${status}).`)
    this.status = status
  }
}

export const sessionPath = (id: string) => `/sessions/${encodeURIComponent(id)}`
export const active = (status: string) => status === 'pending' || status === 'running'

export function createApi(token: string) {
  async function request(path: string, init?: RequestInit): Promise<Response> {
    const headers = new Headers(init?.headers)
    headers.set('Authorization', `Bearer ${token}`)
    const response = await fetch(`/api/v1${path}`, {
      ...init, headers, cache: 'no-store', redirect: 'error', credentials: 'omit',
      signal: init?.signal ?? AbortSignal.timeout(15000),
    })
    if (!response.ok) throw new ApiError(response.status)
    return response
  }

  return {
    async get<T>(path: string, signal?: AbortSignal): Promise<T> {
      return (await request(path, { signal })).json()
    },
    async diff(path: string, expectedHash: string): Promise<string> {
      const response = await request(path)
      if (response.headers.get('X-Content-SHA256') !== expectedHash) {
        throw new Error('Diff does not match the approval patch hash.')
      }
      return response.text()
    },
    mutation<T>(method: 'POST' | 'PUT', path: string, body: object) {
      const key = crypto.randomUUID()
      const encoded = JSON.stringify(body)
      return async (): Promise<T> => (await request(path, {
        method, body: encoded,
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      })).json()
    },
  }
}
