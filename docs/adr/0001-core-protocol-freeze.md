# ADR 0001: Core Protocol Freeze (v1)

- Status: Accepted (proposed by Claude Code, pending Codex ratification via `contracts-frozen-v1` tag)
- Date: 2026-07-11
- Task: HUB-010
- Deciders: Claude Code (architecture owner), Codex (integration owner / reviewer)

## Context

HUB-010 freezes the v1 cross-module data contract for Agent Hub. Every
downstream task (SQLite schema, Compiler, GraphExecutor, security chain,
adapters, API, GUI) depends on these shapes being stable. Chapter 18 of
`agent-hub-development-plan.md` specifies the models; this ADR records the
decisions made while turning that spec into the frozen `protocol/` package and
the frontend TypeScript mirror, and the deviations that require sign-off.

The plan mandates: all models inherit `StrictModel` (`extra="forbid"`);
statuses/types are `StrEnum`; `list`/`dict` use `Field(default_factory=...)`;
strings and collections are length-bounded; `AuthorGraph` and `CompiledGraph`
are strictly separate; the compiler-only node fields are rejected on author
input; identifiers/hashes/Git object ids use constrained aliases.

## Decision

### 1. Package layout

`protocol/` is the single source of truth. Modules mirror the chapter 17
directory (`task.py`, `context.py`, `result.py`, `event.py`, `workflow.py`,
`console.py`, `privilege.py`) plus three additions:

- `common.py` — `StrictModel`, the constrained scalar/text aliases, every
  shared enum, `ArtifactRef`, and `canonical_json`. These are referenced by
  nearly every other module, so a shared root avoids import cycles.
- `change_set.py` — the frozen `ChangeSet` data contract (chapter 18.7). The
  runtime capture/transaction logic will live in `workspace/change_set.py`
  (owned by Codex); this module only defines the shape.
- `api.py` — the cross-module HTTP request/response contracts (chapter 22).

`workflow/graph_model.py` re-exports the graph models from `protocol.workflow`
so Codex's `workflow/` modules import from the location named in the directory
structure while the definitions stay under the single owner (`protocol/`).

### 2. Constrained scalar aliases replace bare `str`

Per the chapter 18.1 note ("部分字段仍写作 `str`；实际实现中所有 `*_id` ...
必须使用 `EntityId` ..."), all identifiers use `EntityId`, all sha256 hashes
use `Sha256Hex`, and resolved Git commits use `GitObjectId`. User-visible text
uses length-bounded aliases (`TitleText`, `InstructionText`, `GoalText`,
`SummaryText`, `ReasonText`, `ShortReasonText`, `DescriptionText`). Repo paths
use `RepoRelativePath`, which bounds *shape* only (length); semantic path
validation (escape/symlink/reserved-name) is deferred to the runtime
`PathPolicy` — the regex is deliberately not used as a path validator.

### 3. AuthorGraph / CompiledGraph separation

One `WorkflowNode` / `WorkflowEdge` shape is shared, but `AuthorGraph` and
`CompiledGraph` are distinct wrappers. Compiler-only fields (`effective_*`,
`policy_risk_floor`, `requires_changeset_approval`, `test_kind`, `test_argv`)
live on the shared node as `Optional` so compiled nodes can carry them; the
model layer does **not** reject them (a `CompiledGraph` node must set them).
Rejection on author/Planner/API input is a DraftValidator responsibility
(HUB-110), which this ADR records as the agreed boundary. `CompiledGraph` has
larger node/edge ceilings (300/600 vs 100/300) to accommodate injected system
security nodes.

### 4. Cross-field invariants as model validators

- `CapabilityGrant`: `consumed_at`/`consumed_fencing_token` set together;
  `revoked_at`/`revocation_reason` set together; consumed and revoked mutually
  exclusive.
- `EventEnvelope`: `workflow_run_id`/`run_seq` set together; a run event must
  also carry `workflow_id`.
- `Artifact`: `task_id` and `planner_run_id` are mutually exclusive owners.
- `ConsoleChunk`: `artifact_ref.artifact_type` must be `console` and its
  `size_bytes` must match the chunk's declared size.

Registry-level rules (event_type → payload class mapping, 64 KiB canonical
payload cap) are runtime concerns and are intentionally *not* enforced in the
model; the model provides the `canonical_json` helper the runtime uses.

### 5. `Approval` discriminated union

`Approval = Annotated[ChangeSetApproval | PrivilegeApproval, Field(discriminator="subject_type")]`,
matching the spec verbatim. `subject_sha256` is stored but the manifest that
produces it is computed by the runtime.

### 6. Deterministic serialization

`canonical_json(model)` dumps with `sort_keys=True` and compact separators via
`model_dump(mode="json")`, giving identical bytes for logically equal contract
values regardless of field declaration order. This is the primitive the
Compiler and ApprovalManager will hash over; the model layer does not itself
compute graph or subject hashes.

### 7. Frontend TypeScript mirror

`web/frontend/src/api/protocol.ts` mirrors the Python contract. Enums are
string-literal union types, **not** TS `enum`, because the frontend tsconfig
sets `erasableSyntaxOnly` (TS `enum` emits runtime code and would fail the
build). The `Approval` union is discriminated on `subject_type`.

## Deviations from the plan's literal field types

These are type *tightenings* consistent with the chapter 18.1 directive, not
semantic field changes. They are called out here for Codex review:

1. Fields written as `str` in the abbreviated examples are frozen as
   `EntityId` / `Sha256Hex` / `GitObjectId` / bounded text as appropriate
   (e.g. `TaskPackage.task_id`, `ChangeSet.patch_sha256`,
   `CompiledGraph.integration_base_commit`).
2. Collections that had no explicit `max_length` in the example but hold
   bounded data were given defensive ceilings (e.g. `ChangeSet.*_files` at
   500, aligned with `config.max_changed_paths`; `AgentResult.risks` at 50).
   No field's *presence*, *name*, *default*, or *semantics* was changed.
3. `error_code`, `planner_model`, and `revocation_reason` were given length
   caps (200) consistent with the "strings set length limits" rule.

No core field was renamed, added, removed, or had its type's meaning changed.
If Codex considers any collection ceiling wrong, that is a one-line change
before the `contracts-frozen-v1` tag; after the tag it requires a new ADR.

## Consequences

- Downstream tasks import stable, strict, length-bounded models from
  `protocol` (Python) and `src/api/protocol.ts` (frontend).
- DraftValidator/Compiler (HUB-110) own the author-input rejection of
  compiler-only fields and the graph/subject hashing built on `canonical_json`.
- Any change to a frozen core field after `contracts-frozen-v1` must land as a
  new ADR proposed by Claude Code and ratified by Codex, per task-allocation
  rule 2.8.
