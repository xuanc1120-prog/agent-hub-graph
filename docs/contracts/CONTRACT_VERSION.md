# Agent Hub Contract Version

- Current contract version: **1** (`protocol.CONTRACT_VERSION == "1"`,
  `CONTRACT_VERSION === '1'` in `web/frontend/src/api/protocol.ts`)
- Frozen by: HUB-010
- Base commit: `badd9489c5c7e560da3cc27ec817c9fdd95ddc1d` (`hub-000-complete`)
- Governing spec: `agent-hub-development-plan.md` chapters 18 (models), 22
  (API), 24 (frontend); `agent-hub-task-allocation.md` section 2.

## What this version covers

The `protocol/` package and its TypeScript mirror define the frozen v1
cross-module data contract:

| Area | Python module | TS section |
|---|---|---|
| Strict base, scalar/text aliases, all shared enums, `ArtifactRef`, `canonical_json` | `protocol/common.py` | Enums + `ArtifactRef` |
| Author/Compiled graph, Node/Edge, Draft, Layout, `IfCondition` | `protocol/workflow.py` | Workflow graph |
| `TaskPackage` | `protocol/task.py` | Task |
| `ContextPack`, `NodeSummary` | `protocol/context.py` | Context |
| `AgentResult`, `NextSuggestion` | `protocol/result.py` | Result |
| `ChangeSet` + `ChangeSetStatus` | `protocol/change_set.py` | ChangeSet |
| `Approval` union, `PrivilegeRequest`, `CapabilityGrant`, `AgentOutputEnvelope` | `protocol/privilege.py` | Approval/privilege |
| `Artifact`, `EventEnvelope` | `protocol/event.py` | Event/artifact |
| `ConsoleChunk` | `protocol/console.py` | Console |
| HTTP request/response DTOs | `protocol/api.py` | API DTOs |

## Invariants guaranteed by v1

1. Every model inherits `StrictModel` — `extra="forbid"`,
   `validate_assignment=True`, `allow_inf_nan=False`.
2. All status/type fields are `StrEnum`; no free-form status strings.
3. Identifiers use `EntityId`, sha256 hashes use `Sha256Hex`, resolved Git
   commits use `GitObjectId`; user text and repo paths are length-bounded.
4. All `list`/`dict` fields use `Field(default_factory=...)` with a
   `max_length` ceiling.
5. `AuthorGraph` and `CompiledGraph` are separate; `CompiledGraph` is
   constructed only by the Compiler and is never accepted from a client.
6. Compiler-only node fields exist on the shared node but are author-rejected
   downstream (DraftValidator, HUB-110).
7. `canonical_json` produces order-independent bytes for hashing.
8. Models with a cross-field `model_validator` (`CapabilityGrant`, `Artifact`,
   `EventEnvelope`, `ConsoleChunk`) and the value object they embed
   (`ArtifactRef`) are `frozen=True`: a rejected assignment can never leave a
   record in an illegal state, and nested mutation cannot bypass the invariant.
9. Command templates are bounded in both token length and token count;
   `IfCondition.value` operands are short bounded tokens with a bounded list.

## Phase-0 OpenAPI draft

`docs/contracts/openapi.draft.json` is generated from the frozen `protocol/api.py`
DTOs (`scripts/generate_openapi.py`). It has no `paths` — the FastAPI routes are
added by HUB-400. `tests/test_openapi_draft.py` guards it against drift.

## `schema_version` fields

`WorkflowDraft`, `AuthorGraph`, and `CompiledGraph` each carry a
`schema_version` string defaulting to `CONTRACT_VERSION`. Persisted graphs
record the version they were written under so a future v2 can migrate them.

## Change control

Per task-allocation rule 2.8, after the `contracts-frozen-v1` tag any change to
a frozen core field (name, type semantics, default, or an enum member) requires
a new ADR under `docs/adr/`, proposed by Claude Code and ratified by Codex.
Additive, non-breaking changes (a brand-new optional model, a new enum member
that no state machine rejects) may bump a minor descriptor but still require an
ADR entry noting the compatibility impact.

See `docs/adr/0001-core-protocol-freeze.md` for the freeze decisions and the
type-tightening deviations submitted for Codex review.
