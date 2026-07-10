// Frozen v1 Agent Hub contract types (HUB-010).
//
// This file mirrors the Python `protocol` package. It is the single source of
// truth for the frontend's view of the contract. Enums are expressed as string
// literal union types (not TS `enum`) so the module is fully erasable under the
// project's `erasableSyntaxOnly` tsconfig.
//
// After the `contracts-frozen-v1` tag, these shapes may only change via an
// accepted ADR, in lockstep with the Python models.

export const CONTRACT_VERSION = '1'

// --- Enums (string literal unions) ---------------------------------------------

export type RiskLevel = 'L0' | 'L1' | 'L2' | 'L3' | 'L4'

export type PlannerType = 'rule_based' | 'open_code'

export type SessionStatus = 'active' | 'blocked' | 'archived'

export type PlannerRunStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'timed_out'
  | 'cancelled'
  | 'orphaned'

export type TaskStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'privilege_requested'
  | 'failed'
  | 'timed_out'
  | 'cancelled'
  | 'blocked_by_guard'
  | 'parse_failed'
  | 'orphaned'

export type SecuritySeverity = 'info' | 'warning' | 'high' | 'critical'

export type ActorType = 'user' | 'master' | 'agent' | 'system' | 'local_cli'

export type ConsoleStreamKind = 'stdout' | 'stderr' | 'system'

export type ConsoleOwnerType = 'task' | 'planner_run'

export type ArtifactType =
  | 'log'
  | 'console'
  | 'diff'
  | 'patch'
  | 'report'
  | 'test_result'
  | 'runtime_policy'
  | 'change_preimage'

export type CapabilityType = 'modify_dependency' | 'modify_config'

export type PrivilegeAction = 'edit_dependency_manifest' | 'edit_project_config'

export type NodeType =
  | 'input'
  | 'agent_task'
  | 'context_builder'
  | 'patch_guard'
  | 'command_guard'
  | 'test'
  | 'risk_classifier'
  | 'approval'
  | 'merge_patch'
  | 'output'
  | 'if'

export type TaskKind = 'analyze' | 'implement' | 'review' | 'docs' | 'test_fix'

export type TestKind = 'command' | 'docs_static'

export type IfOperator = 'eq' | 'ne' | 'in' | 'is_true' | 'is_false'

export type AssignmentMode = 'auto' | 'manual' | 'locked'

export type NodeRunStatus =
  | 'pending'
  | 'ready'
  | 'running'
  | 'waiting_approval'
  | 'blocked_by_guard'
  | 'failed'
  | 'completed'
  | 'skipped'
  | 'superseded'
  | 'cancelled'
  | 'orphaned'

export type WorkflowRunStatus =
  | 'pending'
  | 'running'
  | 'waiting_approval'
  | 'paused'
  | 'blocked'
  | 'failed'
  | 'completed'
  | 'cancelled'
  | 'orphaned'

export type NodeOutcome =
  | 'success'
  | 'failure'
  | 'matched'
  | 'not_matched'
  | 'approved'
  | 'rejected'
  | 'blocked'
  | 'cancelled'

export type EdgeCondition =
  | 'success'
  | 'failure'
  | 'matched'
  | 'not_matched'
  | 'approved'
  | 'rejected'

export type ChangeSetStatus =
  | 'captured'
  | 'guard_passed'
  | 'guard_rejected'
  | 'test_passed'
  | 'test_failed'
  | 'policy_rejected'
  | 'pending_approval'
  | 'approved'
  | 'rejected'
  | 'cancelled'
  | 'stale'
  | 'merged'
  | 'abandoned_partial'
  | 'quarantined'

export type ApprovalStatus =
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'expired'
  | 'invalidated'

export type PrivilegeRequestStatus =
  | 'pending'
  | 'waiting_approval'
  | 'approved'
  | 'rejected'
  | 'consumed'
  | 'expired'
  | 'denied'

export type AgentResultStatus =
  | 'succeeded'
  | 'privilege_requested'
  | 'failed'
  | 'timed_out'
  | 'cancelled'
  | 'blocked_by_guard'
  | 'parse_failed'

// --- Shared value objects ------------------------------------------------------

export interface ArtifactRef {
  artifact_id: string
  artifact_type: ArtifactType
  relative_path: string
  sha256: string
  size_bytes: number
}

// --- Workflow graph ------------------------------------------------------------

export type CommandTemplate = string[]

export interface NodePosition {
  x: number
  y: number
}

export interface NodeLayout {
  node_id: string
  position: NodePosition
}

export interface WorkflowLayout {
  nodes: NodeLayout[]
}

export interface AgentRecommendation {
  agent_id: string
  score: number
  reason: string
}

export interface IfCondition {
  upstream_node_id: string
  field: 'status' | 'outcome' | 'effective_risk' | 'tests_passed'
  operator: IfOperator
  value?: string | boolean | string[] | null
}

// Compiler-only fields (effective_*, policy_risk_floor,
// requires_changeset_approval, test_kind, test_argv) are present on the shared
// node so CompiledGraph nodes can carry them. They are read-only in the GUI and
// are rejected by DraftValidator on author input.
export interface WorkflowNode {
  id: string
  node_type: NodeType
  task_kind?: TaskKind | null
  title: string
  description?: string | null
  instruction?: string | null
  assigned_agent?: string | null
  assignment_mode: AssignmentMode
  resolved_agent_id?: string | null
  resolved_agent_spec_sha256?: string | null
  recommended_agents: AgentRecommendation[]
  allowed_files_candidate: string[]
  new_files_candidate: string[]
  allowed_commands_candidate: CommandTemplate[]
  effective_allowed_files?: string[] | null
  effective_new_files?: string[] | null
  effective_allowed_commands?: CommandTemplate[] | null
  policy_risk_floor?: RiskLevel | null
  requires_changeset_approval?: boolean | null
  test_kind?: TestKind | null
  test_argv?: string[] | null
  if_condition?: IfCondition | null
  risk_level_hint: RiskLevel
  requires_write: boolean
  system_managed: boolean
  source_node_id?: string | null
  system_rule_id?: string | null
}

export interface WorkflowEdge {
  id: string
  from_node: string
  to_node: string
  condition: EdgeCondition
  system_managed: boolean
}

export interface WorkflowDraft {
  schema_version: string
  session_id: string
  goal: string
  planner_id: string
  planner_type: PlannerType
  planner_model?: string | null
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  assumptions: string[]
  risks: string[]
  required_user_inputs: string[]
}

export interface AuthorGraph {
  schema_version: string
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
}

export interface CompiledGraph {
  schema_version: string
  source_author_hash: string
  integration_base_commit: string
  policy_version: string
  agent_catalog_snapshot_hash: string
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
}

// --- Task / context / result ---------------------------------------------------

export interface TaskPackage {
  task_id: string
  session_id: string
  workflow_run_id: string
  node_run_id: string
  node_id: string
  agent_id: string
  task_kind: TaskKind
  instruction: string
  repo_path: string
  base_commit: string
  effective_allowed_files: string[]
  effective_new_files: string[]
  active_capability_grant_id?: string | null
  granted_existing_files: string[]
  readonly_files: string[]
  effective_allowed_commands: CommandTemplate[]
  workspace_ephemeral_paths: string[]
  forbidden_actions: string[]
  acceptance_criteria: string[]
  effective_risk: RiskLevel
  requires_changeset_approval: boolean
  runtime_policy_ref: ArtifactRef
  context_bundle_path: string
  context_bundle_sha256: string
  timeout_seconds: number
}

export interface NodeSummary {
  node_run_id: string
  status: NodeRunStatus
  summary: string
  artifact_refs: ArtifactRef[]
}

export interface ContextPack {
  task_id: string
  node_id: string
  task_kind: TaskKind
  session_goal: string
  current_node_title: string
  current_task: string
  upstream_summaries: NodeSummary[]
  artifact_refs: ArtifactRef[]
  effective_allowed_files: string[]
  effective_new_files: string[]
  active_capability_grant_id?: string | null
  granted_existing_files: string[]
  effective_allowed_commands: CommandTemplate[]
  forbidden_paths: string[]
  acceptance_criteria: string[]
  max_prompt_chars: number
}

export interface NextSuggestion {
  suggested_agent?: string | null
  reason: string
}

export interface AgentResult {
  task_id: string
  node_run_id: string
  agent_id: string
  status: AgentResultStatus
  summary: string
  raw_output_ref?: ArtifactRef | null
  change_set_id?: string | null
  artifact_refs: ArtifactRef[]
  risks: string[]
  privilege_request_ids: string[]
  error_code?: string | null
  error_message?: string | null
}

// --- ChangeSet -----------------------------------------------------------------

export interface ChangeSet {
  change_set_id: string
  session_id: string
  workflow_run_id: string
  node_run_id: string
  task_id: string
  base_commit: string
  pre_state_hash: string
  post_state_hash: string
  patch_sha256: string
  status: ChangeSetStatus
  canonical_patch_ref: ArtifactRef
  evidence_refs: ArtifactRef[]
  created_files: string[]
  created_directories: string[]
  modified_files: string[]
  deleted_files: string[]
  renamed_files: string[]
  untracked_files: string[]
  ignored_files_touched: string[]
  preimage_refs: ArtifactRef[]
}

// --- Approval / privilege / capability -----------------------------------------

export interface ChangeSetApproval {
  subject_type: 'change_set'
  approval_id: string
  workflow_run_id: string
  node_run_id: string
  subject_sha256: string
  effective_risk: RiskLevel
  scope: string[]
  status: ApprovalStatus
  version: number
  expires_at: string
  change_set_id: string
  base_commit: string
  patch_sha256: string
  evidence_sha256: string
}

export interface PrivilegeApproval {
  subject_type: 'privilege_request'
  approval_id: string
  workflow_run_id: string
  node_run_id: string
  subject_sha256: string
  effective_risk: RiskLevel
  scope: string[]
  status: ApprovalStatus
  version: number
  expires_at: string
  privilege_request_id: string
  evidence_sha256: string
}

// Discriminated union on `subject_type`.
export type Approval = ChangeSetApproval | PrivilegeApproval

export interface PrivilegeRequestProposal {
  requested_capability: CapabilityType
  requested_action: PrivilegeAction
  requested_resource?: string | null
  reason: string
  expected_impact: string[]
  related_files: string[]
  rollback_plan?: string | null
  risk_level_hint: RiskLevel
}

export interface PrivilegeRequest extends PrivilegeRequestProposal {
  request_id: string
  session_id: string
  task_id: string
  node_run_id: string
  agent_id: string
  requested_resource: string
  effective_risk: RiskLevel
  status: PrivilegeRequestStatus
}

export interface AgentOutputEnvelope {
  summary: string
  risk_hints: string[]
  next_suggestion?: NextSuggestion | null
  privilege_requests: PrivilegeRequestProposal[]
}

export interface CapabilityGrant {
  grant_id: string
  request_id: string
  target_task_id: string
  action: PrivilegeAction
  resource: string
  expires_at: string
  consumed_at?: string | null
  consumed_fencing_token?: number | null
  revoked_at?: string | null
  revocation_reason?: string | null
}

// --- Event / artifact / console ------------------------------------------------

export interface Artifact {
  artifact_id: string
  session_id: string
  task_id?: string | null
  planner_run_id?: string | null
  artifact_type: ArtifactType
  relative_path: string
  sha256: string
  size_bytes: number
  redacted: boolean
  created_at: string
}

export interface EventEnvelope<PayloadT = unknown> {
  event_id: number
  session_id: string
  workflow_id?: string | null
  workflow_run_id?: string | null
  run_seq?: number | null
  event_type: string
  actor_type: ActorType
  actor_id?: string | null
  payload: PayloadT
  created_at: string
}

export interface ConsoleChunk {
  console_session_id: string
  seq: number
  stream: ConsoleStreamKind
  artifact_ref: ArtifactRef
  size_bytes: number
  created_at: string
}

// --- HTTP API data contracts ---------------------------------------------------

export interface ValidationIssue {
  code: string
  message: string
  node_id?: string | null
  edge_id?: string | null
}

export interface PageInfo {
  next_cursor?: string | null
  limit: number
}

export interface WorkflowSaveRequest {
  author_graph: AuthorGraph
  expected_semantic_version: number
}

export interface WorkflowSaveResponse {
  workflow_id: string
  semantic_version: number
}

export interface LayoutSaveRequest {
  layout: WorkflowLayout
  expected_layout_version: number
}

export interface LayoutSaveResponse {
  workflow_id: string
  layout_version: number
}

export interface ValidateRequest {
  expected_semantic_version: number
}

export interface ValidateResponse {
  ok: boolean
  errors: ValidationIssue[]
  warnings: ValidationIssue[]
  compiled_graph?: CompiledGraph | null
  compiled_hash?: string | null
  integration_base_commit?: string | null
  agent_catalog_hash?: string | null
  policy_version?: string | null
  source_semantic_version?: number | null
}

export interface RunRequest {
  expected_semantic_version: number
  confirmed_compiled_hash: string
}

export interface RunResponse {
  workflow_run_id: string
}

export interface AssignAgentRequest {
  agent_id: string
  expected_semantic_version: number
}

export interface LockNodeRequest {
  locked: boolean
  expected_semantic_version: number
}

export interface MutationVersionResponse {
  workflow_id: string
  semantic_version: number
}

export interface ApprovalDecisionRequest {
  approval_version: number
  confirm_subject_hash: string
}

export interface ApprovalRenewRequest {
  approval_version: number
  confirm_subject_hash: string
}

export interface PlanRequest {
  planner_mode: 'open_code' | 'rule_based'
  parent_workflow_id?: string | null
}

export interface PlanResponse {
  planner_run_id: string
}

export interface RecoverWorkspaceRequest {
  expected_fencing_token: number
  resolution: 'retry' | 'cancel'
}

export interface WsTicketResponse {
  ticket: string
  expires_in_seconds: number
}

export interface RiskFinding {
  risk_level: RiskLevel
  reason: string
  path?: string | null
}
