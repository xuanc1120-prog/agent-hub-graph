import { useEffect, useMemo, useRef, useState } from 'react'
import { Bot, Check, GitPullRequest, Network, Play, RefreshCw, X } from 'lucide-react'
import { active, ApiError, createApi, sessionPath } from './api/client'
import type { Approval, PlanStatus, RunStatus, Session, Workflow } from './api/client'
import type {
  AuthorGraph, ValidateResponse, WorkflowNode, WorkflowRunStatus, WorkflowSaveResponse,
} from './api/protocol'
import { WorkflowGraph } from './WorkflowGraph'
import './App.css'

type Operation = { execute: () => Promise<void>; conflict?: () => void }
type ReviewedPatch = { approval: Approval; text: string }

function message(error: unknown) {
  return error instanceof ApiError ? error.message : 'The request could not be completed.'
}

export default function App() {
  const [token, setToken] = useState('')
  const api = useMemo(() => createApi(token.trim()), [token])
  const [repository, setRepository] = useState('')
  const [goal, setGoal] = useState('Create demo documentation')
  const [session, setSession] = useState<Session>()
  const [plan, setPlan] = useState<PlanStatus>()
  const [workflow, setWorkflow] = useState<Workflow>()
  const [draft, setDraft] = useState<AuthorGraph>()
  const [selection, setSelection] = useState<string>()
  const [validation, setValidation] = useState<ValidateResponse>()
  const [view, setView] = useState<'author' | 'compiled'>('author')
  const [runId, setRunId] = useState<string>()
  const [queuedStatus, setQueuedStatus] = useState<WorkflowRunStatus>()
  const [run, setRun] = useState<RunStatus>()
  const [review, setReview] = useState<ReviewedPatch>()
  const [decision, setDecision] = useState<Approval>()
  const [stale, setStale] = useState(false)
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const [revision, setRevision] = useState(0)
  const [busy, setBusy] = useState(false)
  const [retry, setRetry] = useState<Operation>()
  const inFlight = useRef(false)
  const root = session ? sessionPath(session.session_id) : ''
  const workflowPath = workflow ? `${root}/workflows/${encodeURIComponent(workflow.workflow_id)}` : ''
  const planId = workflow ? undefined : plan?.planner_run_id
  const selected = draft?.nodes.find((node) => node.id === selection)
  const dirty = !!draft && draft !== workflow?.author_graph
  const incomplete = draft?.nodes.some((node) => !node.title.trim() ||
    (node.requires_write && !node.new_files_candidate[0]?.trim()))
  const blocked = busy || !!retry || !token.trim()
  const runStatus = run?.status ?? queuedStatus
  const pendingApprovals = run?.status === 'waiting_approval'
    ? run.approvals.filter((approval) => approval.status === 'pending') : []
  const reviewed = review && pendingApprovals.some((approval) =>
    approval.approval_id === review.approval.approval_id &&
    approval.version === review.approval.version &&
    approval.subject_sha256 === review.approval.subject_sha256 &&
    approval.patch_sha256 === review.approval.patch_sha256
  )

  function loadWorkflow(value: Workflow) {
    setWorkflow(value)
    setDraft(value.author_graph)
    setSelection(value.author_graph.nodes.find((node) => node.node_type === 'agent_task')?.id)
    setValidation(undefined)
    setView('author')
    setStale(false)
  }

  useEffect(() => {
    if (!token.trim() || (!planId && !runId)) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(15000)])
      try {
        if (runId) {
          const value = await api.get<RunStatus>(`${root}/workflow-runs/${encodeURIComponent(runId)}`, signal)
          if (controller.signal.aborted) return
          setRun(value)
          if (active(value.status) || value.status === 'waiting_approval') {
            timer = setTimeout(poll, value.status === 'waiting_approval' ? 1500 : 500)
          }
        } else if (planId) {
          const value = await api.get<PlanStatus>(`${root}/planner-runs/${encodeURIComponent(planId)}`, signal)
          if (controller.signal.aborted) return
          if (value.status === 'succeeded' && value.result_workflow_id) {
            const graph = await api.get<Workflow>(
              `${root}/workflows/${encodeURIComponent(value.result_workflow_id)}`, signal,
            )
            if (controller.signal.aborted) return
            loadWorkflow(graph)
          }
          setPlan(value)
          if (active(value.status)) timer = setTimeout(poll, 500)
        }
        setPollError('')
      } catch (failure) {
        if (!controller.signal.aborted) setPollError(`Status refresh stopped. ${message(failure)}`)
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [api, token, root, planId, runId, revision])

  async function perform(operation: Operation, mutation = true) {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError('')
    try {
      await operation.execute()
      setRetry(undefined)
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409 && operation.conflict) {
        operation.conflict()
        setError('Version or evidence conflict. Reload the current state before continuing.')
      } else {
        setError(message(failure))
      }
      const uncertain = !(failure instanceof ApiError) || failure.status >= 500
      setRetry(mutation && uncertain ? operation : undefined)
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  function mutate<T>(
    method: 'POST' | 'PUT', path: string, body: object, accept: (value: T) => void,
    conflict?: () => void,
  ) {
    if (blocked || inFlight.current) return
    const request = api.mutation<T>(method, path, body)
    void perform({ execute: async () => accept(await request()), conflict })
  }

  function edit(update: Partial<WorkflowNode>) {
    if (!draft || !selected) return
    setDraft({ ...draft, nodes: draft.nodes.map((node) => node.id === selected.id
      ? { ...node, ...update } : node) })
    setValidation(undefined)
  }

  function conflict() {
    setStale(true)
    setValidation(undefined)
    setView('author')
  }

  function decide(action: 'approve' | 'reject') {
    if (!reviewed || !review) return
    const approval = review.approval
    mutate<Approval>('POST', `${root}/approvals/${encodeURIComponent(approval.approval_id)}/${action}`, {
      approval_version: approval.version, confirm_subject_hash: approval.subject_sha256,
    }, (value) => {
      setDecision(value)
      setReview(undefined)
      setRevision((n) => n + 1)
    }, () => {
      setReview(undefined)
      setRevision((n) => n + 1)
    })
  }

  return (
    <main className="app-shell">
      <header className="app-header">
        <strong><Network size={21} aria-hidden="true" /> Agent Hub</strong>
        <span className="tag"><Bot size={15} aria-hidden="true" /> Mock Agent</span>
        <label className="token">Bearer token
          <input type="password" autoComplete="off" spellCheck={false} value={token}
            disabled={busy || !!retry} onChange={(event) => setToken(event.target.value)} />
        </label>
      </header>

      <section className="session-bar" aria-label="Session">
        <form onSubmit={(event) => {
          event.preventDefault()
          mutate<Session>('POST', '/sessions', { repository: repository.trim(), goal: goal.trim() }, setSession)
        }}>
          <label>Repository path
            <input required maxLength={4096} value={repository} disabled={blocked || !!session}
              onChange={(event) => setRepository(event.target.value)} placeholder="Absolute path to a clean Git repository" />
          </label>
          <label>Goal
            <input required maxLength={20000} value={goal} disabled={blocked || !!session}
              onChange={(event) => setGoal(event.target.value)} />
          </label>
          <button disabled={blocked || !!session || !repository.trim() || !goal.trim()}>Create session</button>
        </form>
        {session && <div className="session-summary">
          <code data-testid="session-id">{session.session_id}</code>
          <button disabled={blocked || !!workflow || (plan && active(plan.status))}
            onClick={() => mutate<PlanStatus>('POST', `${root}/plans`, {}, setPlan)}>Generate plan</button>
          {plan && <span role="status">Plan: {plan.status}{plan.error_code ? ` · ${plan.error_code}` : ''}</span>}
          {runStatus && !active(runStatus) && runStatus !== 'waiting_approval' &&
            <button disabled={blocked} onClick={() => {
              setSession(undefined); setPlan(undefined); setWorkflow(undefined); setDraft(undefined)
              setSelection(undefined); setValidation(undefined); setView('author'); setRunId(undefined)
              setRun(undefined); setQueuedStatus(undefined); setReview(undefined); setDecision(undefined)
              setError(''); setPollError(''); setStale(false)
            }}>New session</button>}
        </div>}
      </section>

      {(error || pollError || retry) && <div className="notice" role="alert">
        {error || pollError}
        {retry && <button disabled={busy} onClick={() => void perform(retry)}>Retry same request</button>}
        {pollError && <button onClick={() => setRevision((n) => n + 1)}>Refresh status</button>}
      </div>}

      <section className="workspace" aria-label="Workflow workspace">
        <div className="graph-area">
          <div className="toolbar">
            <h1>Workflow</h1>
            <div className="segmented" aria-label="Graph view">
              <button aria-pressed={view === 'author'} onClick={() => setView('author')}>Author</button>
              <button aria-pressed={view === 'compiled'} disabled={!validation?.compiled_graph}
                onClick={() => setView('compiled')}>Compiled</button>
            </div>
            {workflow && <span className="tag">v{workflow.semantic_version}{dirty ? ' · Unsaved' : ''}</span>}
          </div>
          <div className="canvas">
            {view === 'author' && draft
              ? <WorkflowGraph key="author" graph={draft} layout={workflow?.layout}
                  selected={selection} onSelect={setSelection} />
              : view === 'compiled' && validation?.compiled_graph
                ? <WorkflowGraph key="compiled" graph={validation.compiled_graph}
                    statuses={Object.fromEntries(run?.nodes.map((node) => [node.node_id, node.status]) ?? [])} />
                : <div className="empty"><Network size={29} aria-hidden="true" /> No workflow yet</div>}
          </div>
          {workflow && <div className="actions">
            <button disabled={blocked || !dirty || incomplete || stale || !!runId || view !== 'author'} onClick={() =>
              mutate<WorkflowSaveResponse>('PUT', workflowPath, {
                expected_semantic_version: workflow.semantic_version, author_graph: draft,
              }, (value) => {
                setWorkflow({ ...workflow, author_graph: draft!, semantic_version: value.semantic_version })
                setValidation(undefined)
              }, conflict)}>Save edits</button>
            <button disabled={blocked || dirty || stale || !!runId} onClick={() =>
              mutate<ValidateResponse>('POST', `${workflowPath}/validate`, {
                expected_semantic_version: workflow.semantic_version,
              }, (value) => {
                setValidation(value)
                setView(value.ok && value.compiled_graph ? 'compiled' : 'author')
              }, conflict)}>Validate</button>
            <button className="primary" disabled={blocked || dirty || stale || !!runId ||
                !validation?.ok || !validation.compiled_hash ||
                validation.source_semantic_version !== workflow.semantic_version} onClick={() =>
              mutate<{ workflow_run_id: string; status: WorkflowRunStatus }>('POST', `${workflowPath}/runs`, {
                expected_semantic_version: workflow.semantic_version,
                confirmed_compiled_hash: validation?.compiled_hash,
              }, (value) => { setQueuedStatus(value.status); setRunId(value.workflow_run_id) }, conflict)}>
              <Play size={15} aria-hidden="true" /> Run
            </button>
            {stale && <button disabled={blocked} onClick={() => void perform({
              execute: async () => loadWorkflow(await api.get<Workflow>(workflowPath)),
            }, false)}><RefreshCw size={15} aria-hidden="true" /> Reload workflow</button>}
          </div>}
          {validation && <div className="validation" role="status">
            <strong>{validation.ok ? 'Validated' : 'Validation failed'}</strong>
            {[...validation.errors, ...validation.warnings].map((issue, index) =>
              <div key={index}>{issue.message}</div>)}
          </div>}
        </div>

        <aside className="inspector">
          <h2>{view === 'author' ? 'Node configuration' : 'Compiled workflow'}</h2>
          {view === 'compiled' ? <p className="muted">Read only</p> : selected ? <>
            <span className="tag">{selected.node_type}</span>
            <fieldset disabled={blocked || stale || !!runId}>
              <label>Node
                <select value={selected.id} onChange={(event) => setSelection(event.target.value)}>
                  {draft?.nodes.map((node) => <option key={node.id} value={node.id}>{node.title}</option>)}
                </select>
              </label>
              <label>Node title
                <input required maxLength={500} value={selected.title}
                  onChange={(event) => edit({ title: event.target.value })} />
              </label>
              {selected.node_type === 'agent_task' && <>
                <label>Instruction
                  <textarea rows={3} maxLength={20000} value={selected.instruction ?? ''}
                    onChange={(event) => edit({ instruction: event.target.value || null })} />
                </label>
                <label className="check"><input type="checkbox" checked={selected.requires_write}
                  onChange={(event) => edit(event.target.checked
                    ? { requires_write: true, task_kind: 'docs', risk_level_hint: 'L1' }
                    : { requires_write: false, new_files_candidate: [], risk_level_hint: 'L0' })} />
                  Create documentation file
                </label>
                {selected.requires_write && <label>New file path
                  <input required value={selected.new_files_candidate[0] ?? ''} maxLength={1024}
                    placeholder="docs/demo.md"
                    onChange={(event) => edit({
                      new_files_candidate: event.target.value ? [event.target.value] : [],
                    })} />
                </label>}
                <span className="muted">Agent: {selected.assigned_agent === 'mock' ? 'Mock' : selected.assigned_agent}</span>
              </>}
            </fieldset>
          </> : <p className="muted">No node selected</p>}
        </aside>
      </section>

      <section className="run-area" aria-label="Run and approval">
        <h2><GitPullRequest size={17} aria-hidden="true" /> Run &amp; approval</h2>
        {runStatus ? <>
          <div className="run-summary">
            <strong role="status" data-testid="run-status">{runStatus}</strong>
            {run && <code title={run.current_commit} data-testid="current-commit">{run.current_commit}</code>}
            {decision && <span className="tag">Decision: {decision.status}</span>}
            <button disabled={busy} onClick={() => setRevision((n) => n + 1)}>Refresh run</button>
          </div>
          {run?.nodes.some((node) => node.error_code) && <ul>
            {run.nodes.filter((node) => node.error_code).map((node) =>
              <li key={node.node_id}>{node.node_type}: {node.error_code}</li>)}
          </ul>}
          {pendingApprovals.filter((approval) => decision?.approval_id !== approval.approval_id)
            .map((approval) => <div className="approval" key={approval.approval_id}>
              <div><strong>Pending approval</strong> · {approval.effective_risk}</div>
              <div className="paths">{approval.scope.join(', ')}</div>
              <button disabled={blocked} onClick={() => {
                setReview(undefined)
                void perform({ execute: async () => {
                  const text = await api.diff(
                    `${root}/change-sets/${encodeURIComponent(approval.change_set_id)}/diff`, approval.patch_sha256,
                  )
                  setReview({ approval, text })
                } }, false)
              }}>View diff</button>
            </div>)}
          {review && reviewed && decision?.approval_id !== review.approval.approval_id && <>
            <pre aria-label="Change diff" tabIndex={0}>{review.text}</pre>
            <div className="actions decision">
              <button className="primary" disabled={blocked} onClick={() => decide('approve')}>
                <Check size={15} aria-hidden="true" /> Approve
              </button>
              <button disabled={blocked} onClick={() => decide('reject')}>
                <X size={15} aria-hidden="true" /> Reject
              </button>
            </div>
          </>}
        </> : <p className="muted">No run yet</p>}
      </section>
    </main>
  )
}
