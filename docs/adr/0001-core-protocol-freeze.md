# ADR 0001: Core Protocol Freeze (v1)

- Status: Accepted and ratified by Codex via the `contracts-frozen-v1` tag
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

### 4. Cross-field invariants as model validators, on frozen models

- `CapabilityGrant`: `consumed_at`/`consumed_fencing_token` set together;
  `revoked_at`/`revocation_reason` set together; consumed and revoked mutually
  exclusive.
- `EventEnvelope`: `workflow_run_id`/`run_seq` set together; a run event must
  also carry `workflow_id`.
- `Artifact`: `task_id` and `planner_run_id` are mutually exclusive owners.
- `ConsoleChunk`: `artifact_ref.artifact_type` must be `console` and its
  `size_bytes` must match the chunk's declared size.

**These four models are frozen (`FrozenStrictModel`, `frozen=True`).** Reason
(this is the fix for the first-round review): the strict base sets
`validate_assignment=True`, and Pydantic v2 mutates the target field *before*
running `model_validator(mode="after")`. A single-field assignment that breaks a
cross-field invariant therefore raises **but leaves the object mutated and
illegal** — the assignment is not rolled back. Freezing blocks assignment
outright, so an invariant established at construction can never be broken in
place; state transitions reconstruct a new instance through the validating
constructor (the runtime records are immutable audit rows anyway).

`ArtifactRef` is **also** frozen. It is a nested value object referenced by
`ConsoleChunk`'s size/type invariant; if it stayed mutable, the invariant could
be broken from underneath the frozen chunk by mutating the nested ref. Freezing
the leaf value object closes that nested-mutation hole. Freezing preserves the
strict config (`extra="forbid"` etc.) — verified by test.

Registry-level rules (event_type → payload class mapping, 64 KiB canonical
payload cap) are runtime concerns and are intentionally *not* enforced in the
model; the model provides the `canonical_json` helper the runtime uses.

### 4a. Required vs defaulted text fields

Per the first-round review, `NodeSummary.summary`, `NextSuggestion.reason`,
`PrivilegeRequestProposal.reason` and `AgentOutputEnvelope.summary` are
**required** (no `= ""` default): the spec declares them as plain
length-bounded text, and an empty planner/agent reason or summary is a producer
bug we want surfaced at construction, not silently accepted. `AgentResult.summary`
keeps its `""` default because chapter 18.4 declares it `default=""`.

### 4b. Bounded command templates and condition values

`CommandTemplate` is `Annotated[list[ArgvToken], Field(min_length=1, max_length=64)]`:
a template must have at least the executable token and is capped at 64 tokens,
so neither a single token (`ArgvToken` ≤ 4096 chars) nor the vector can grow
unbounded. `TaskPackage.effective_allowed_commands` and
`ContextPack.effective_allowed_commands` use this alias instead of the previous
untyped `list[list[str]]`. `IfCondition.value` scalars and `in`-list members use
`IfValueToken` (≤ 256 chars) and the list is capped at 50 members, so a draft
cannot smuggle unbounded text through a branch condition.

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

### 8. Phase-0 OpenAPI draft (scoped to frozen DTOs)

Chapter 25 phase-0 asks for an OpenAPI draft. The FastAPI routes live in
`web/backend/` (Codex, HUB-400) and do not exist yet, so a full route-level
spec is deliberately deferred. What *is* frozen now is the set of
request/response DTOs in `protocol/api.py`, so the phase-0 draft is generated
directly from those models:

- `scripts/generate_openapi.py` builds an OpenAPI 3.1 document whose
  `components.schemas` are the JSON schemas of every `protocol/api.py` DTO plus
  the graph models they embed. It emits no `paths` — those are added by HUB-400
  when the routes exist. The document carries an `x-agent-hub-contract-version`
  extension equal to `CONTRACT_VERSION`. Run `python -m scripts.generate_openapi`
  to print it or `--write` to rewrite the committed file.
- The generated document is committed at `docs/contracts/openapi.draft.json`.
- `tests/test_openapi_draft.py` regenerates in-memory and asserts byte-equality
  with the committed file, so the draft cannot drift from the frozen DTOs
  without the test failing (regenerate with
  `python -m scripts.generate_openapi --write`).

This is the verifiable phase-0 boundary: the data contract half of the API is
frozen and machine-checked now; the path/security half is added by the route
owner without changing these schemas.

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
4. `CommandTemplate` is frozen as `Annotated[list[ArgvToken], Field(min_length=1,
   max_length=64)]`: a command has at least one token (the executable) and at
   most 64. `IfCondition.value` uses a bounded `IfValueToken` (<=256 chars) and
   the list form is capped at 50 members, so a draft cannot smuggle unbounded
   text through a condition. `TaskPackage`/`ContextPack.effective_allowed_commands`
   now use `CommandTemplate` instead of bare `list[list[str]]`.

## Corrections applied after the first HUB-010 freeze review by Codex

1. **Required text restored.** `NodeSummary.summary`,
   `NextSuggestion.reason`, `PrivilegeRequestProposal.reason` and
   `AgentOutputEnvelope.summary` had been given `= ""` defaults in the first
   pass; the spec treats them as required. They are required again in Python,
   in the TS mirror, and asserted in tests. (`AgentResult.summary` keeps its
   `""` default, which is explicit in chapter 18.4.)
2. **Assignment can no longer corrupt an invariant-bearing model.** With
   `validate_assignment=True`, Pydantic mutates the field *then* runs the
   after-validator and does not roll back on failure, so a rejected assignment
   left the object mutated and illegal. Every model with a cross-field
   after-validator (`CapabilityGrant`, `Artifact`, `EventEnvelope`,
   `ConsoleChunk`) and the value object embedded in one of those invariants
   (`ArtifactRef`) now inherit `FrozenStrictModel` (`frozen=True`). Assignment
   is blocked outright and nested mutation of the embedded `ArtifactRef` can no
   longer break `ConsoleChunk`'s size/type invariant. Regression tests cover
   both top-level assignment and nested mutation.

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
