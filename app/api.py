"""Bearer-protected local HTTP adapter for the durable application services."""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app import __version__
from app.config import Settings
from app.services import WorkflowApplication
from protocol import ArtifactType, ChangeSetApproval
from protocol.api import RunRequest, ValidateRequest, ValidateResponse
from storage.db import Transaction
from storage.errors import (
    ArtifactNotFound,
    ConcurrencyConflict,
    ContainmentViolation,
    RecordNotFound,
)
from storage.idempotency_repository import IdempotencyResult, StoredResponse
from storage.leases import MasterLease
from web.backend.dto import (
    ApprovalDecisionRequest,
    ApprovalResponse,
    ArtifactListResponse,
    ArtifactMetadataResponse,
    ChangeSetResponse,
    NodeStatusResponse,
    PlanAcceptedResponse,
    PlannerRunResponse,
    PlanRequest,
    RunAcceptedResponse,
    SessionCreateRequest,
    SessionResponse,
    WorkflowResponse,
    WorkflowRunResponse,
    WorkflowUpdateRequest,
    WorkflowUpdateResponse,
)

router = APIRouter()


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    version: str
    runtime_mode: str


@router.get("/healthz", response_model=HealthResponse, tags=["system"])
async def health(request: Request) -> HealthResponse:
    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        version=__version__,
        runtime_mode=f"single-master@{settings.api_host}:{settings.api_port}",
    )


async def require_bearer(request: Request) -> None:
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied = authorization.partition(" ")
    expected = getattr(request.app.state, "api_token", "")
    valid_token = (
        isinstance(expected, str)
        and bool(supplied)
        and bool(expected)
        and secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    )
    if not separator or scheme.casefold() != "bearer" or not valid_token:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "A valid bearer token is required."},
            headers={"WWW-Authenticate": "Bearer"},
        )


protected_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_bearer)])
fallback_router = APIRouter(prefix="/api", dependencies=[Depends(require_bearer)])


def _service(request: Request) -> WorkflowApplication:
    service = getattr(request.app.state, "workflow_application", None)
    if not isinstance(service, WorkflowApplication):
        raise HTTPException(
            status_code=503,
            detail={"code": "runtime_unavailable", "message": "The runtime is unavailable."},
        )
    return service


def _master_lease(request: Request) -> MasterLease:
    lease = getattr(request.app.state, "master_lease", None)
    if not isinstance(lease, MasterLease):
        raise HTTPException(
            status_code=503,
            detail={"code": "runtime_unavailable", "message": "The runtime is unavailable."},
        )
    return lease


def _canonical_request_hash(request: Request, body: BaseModel) -> str:
    encoded = json.dumps(
        {
            "method": request.method.upper(),
            "path": request.url.path,
            "body": body.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _stored(status_code: int, body: BaseModel) -> StoredResponse:
    return StoredResponse(
        status_code=status_code,
        response_json=json.dumps(
            body.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    )


def _validate_idempotency_key(key: str) -> None:
    if not key or len(key) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_idempotency_key", "message": "Invalid Idempotency-Key."},
        )


async def _idempotency_replay(
    request: Request,
    *,
    operation_scope: str,
    key: str,
    body: BaseModel,
) -> Response | None:
    _validate_idempotency_key(key)
    service = _service(request)
    replay = await service.services.idempotency.lookup(
        actor_scope="local-api",
        operation_scope=operation_scope,
        idempotency_key=key,
        request_sha256=_canonical_request_hash(request, body),
    )
    return _json_response(replay) if replay is not None else None


async def _execute_idempotent(
    request: Request,
    *,
    operation_scope: str,
    key: str,
    body: BaseModel,
    mutate: Callable[[Transaction, WorkflowApplication, MasterLease], Awaitable[StoredResponse]],
) -> Response:
    _validate_idempotency_key(key)
    service = _service(request)
    lease = _master_lease(request)
    request_hash = _canonical_request_hash(request, body)
    repository = service.services.idempotency
    replay = await _idempotency_replay(
        request,
        operation_scope=operation_scope,
        key=key,
        body=body,
    )
    if replay is not None:
        return replay

    async def fenced_mutation(transaction: Transaction) -> StoredResponse:
        await service.services.leases.assert_valid_in(transaction, lease)
        return await mutate(transaction, service, lease)

    result = await repository.execute(
        actor_scope="local-api",
        operation_scope=operation_scope,
        idempotency_key=key,
        request_sha256=request_hash,
        mutation=fenced_mutation,
        ttl_seconds=request.app.state.settings.idempotency_ttl_seconds,
    )
    return _json_response(result)


def _json_response(result: IdempotencyResult | StoredResponse) -> JSONResponse:
    stored = result.response if isinstance(result, IdempotencyResult) else result
    return JSONResponse(
        status_code=stored.status_code,
        content=json.loads(stored.response_json),
        headers={"Cache-Control": "no-store"},
    )


def _session_response(record: Any) -> SessionResponse:
    return SessionResponse(
        session_id=record.session_id,
        status=record.status,
        base_commit=record.base_commit,
        integration_head_commit=record.integration_head_commit,
    )


def _approval_response(record: Any) -> ApprovalResponse:
    approval = record.approval
    if not isinstance(approval, ChangeSetApproval):
        raise RecordNotFound("ChangeSet approval not found")
    return ApprovalResponse(
        approval_id=approval.approval_id,
        workflow_run_id=approval.workflow_run_id,
        subject_sha256=approval.subject_sha256,
        status=approval.status,
        version=approval.version,
        expires_at=approval.expires_at,
        effective_risk=approval.effective_risk,
        change_set_id=approval.change_set_id,
        base_commit=approval.base_commit,
        patch_sha256=approval.patch_sha256,
        evidence_sha256=approval.evidence_sha256,
        scope=[str(path) for path in approval.scope],
    )


async def _require_session(service: WorkflowApplication, session_id: str) -> Any:
    return await service.services.sessions.get(session_id)


async def _require_workflow(
    service: WorkflowApplication,
    session_id: str,
    workflow_id: str,
) -> Any:
    workflow = await service.services.workflows.get(workflow_id)
    if workflow.session_id != session_id:
        raise RecordNotFound("workflow not found")
    return workflow


async def _require_run(service: WorkflowApplication, session_id: str, run_id: str) -> Any:
    run = await service.services.runs.get(run_id)
    if run.session_id != session_id:
        raise RecordNotFound("workflow run not found")
    return run


@protected_router.post(
    "/sessions",
    response_model=SessionResponse,
    status_code=201,
    tags=["sessions"],
)
async def create_session(
    request: Request,
    body: SessionCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    if idempotency_key is None:
        raise HTTPException(
            status_code=400,
            detail={"code": "idempotency_key_required", "message": "Idempotency-Key is required."},
        )
    service = _service(request)
    key = idempotency_key
    _validate_idempotency_key(key)
    request_hash = _canonical_request_hash(request, body)
    repository = service.services.idempotency
    replay = await repository.lookup(
        actor_scope="local-api",
        operation_scope="POST:/api/v1/sessions",
        idempotency_key=key,
        request_sha256=request_hash,
    )
    if replay is not None:
        return _json_response(replay)

    prepared = service.prepare_session(repo=Path(body.repository), goal=body.goal)

    async def mutation(transaction: Transaction) -> StoredResponse:
        await service.services.leases.assert_valid_in(transaction, _master_lease(request))
        record = await service.persist_prepared_session(transaction, prepared)
        return _stored(201, _session_response(record))

    try:
        result = await repository.execute(
            actor_scope="local-api",
            operation_scope="POST:/api/v1/sessions",
            idempotency_key=key,
            request_sha256=request_hash,
            mutation=mutation,
            ttl_seconds=request.app.state.settings.idempotency_ttl_seconds,
        )
    except BaseException:
        # Never delete a clone if an ambiguous DB commit may already reference it.
        try:
            await service.services.sessions.get(prepared.session_id)
        except RecordNotFound:
            service.discard_prepared_session(prepared)
        raise
    if result.replayed:
        service.discard_prepared_session(prepared)
    return _json_response(result)


@protected_router.post(
    "/sessions/{session_id}/plans",
    response_model=PlanAcceptedResponse,
    status_code=202,
    tags=["planning"],
)
async def enqueue_plan(
    session_id: str,
    request: Request,
    body: PlanRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail=_error("idempotency_key_required"))
    service = _service(request)
    await _require_session(service, session_id)

    async def mutate(transaction: Transaction, app: WorkflowApplication, lease: MasterLease):
        record = await app.enqueue_plan(
            session_id,
            lease=lease,
            transaction=transaction,
        )
        return _stored(
            202,
            PlanAcceptedResponse(planner_run_id=record.planner_run_id, status=record.status),
        )

    return await _execute_idempotent(
        request,
        operation_scope="POST:/api/v1/sessions/{session_id}/plans",
        key=idempotency_key,
        body=body,
        mutate=mutate,
    )


@protected_router.get(
    "/sessions/{session_id}/planner-runs/{planner_run_id}",
    response_model=PlannerRunResponse,
    tags=["planning"],
)
async def get_planner_run(
    session_id: str,
    planner_run_id: str,
    request: Request,
) -> PlannerRunResponse:
    service = _service(request)
    await _require_session(service, session_id)
    record = await service.services.planner_runs.get(planner_run_id)
    if record.session_id != session_id:
        raise RecordNotFound("planner run not found")
    return PlannerRunResponse(
        planner_run_id=record.planner_run_id,
        session_id=record.session_id,
        status=record.status,
        result_workflow_id=record.result_workflow_id,
        result_semantic_version=record.result_semantic_version,
        error_code=record.error_code,
    )


@protected_router.get(
    "/sessions/{session_id}/workflows/{workflow_id}",
    response_model=WorkflowResponse,
    tags=["workflows"],
)
async def get_workflow(session_id: str, workflow_id: str, request: Request) -> WorkflowResponse:
    workflow = await _require_workflow(_service(request), session_id, workflow_id)
    return WorkflowResponse(
        workflow_id=workflow.workflow_id,
        session_id=workflow.session_id,
        semantic_version=workflow.semantic_version,
        layout_version=workflow.layout_version,
        author_graph=workflow.author_graph,
        layout=workflow.layout,
    )


@protected_router.put(
    "/sessions/{session_id}/workflows/{workflow_id}",
    response_model=WorkflowUpdateResponse,
    tags=["workflows"],
)
async def update_workflow(
    session_id: str,
    workflow_id: str,
    request: Request,
    body: WorkflowUpdateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail=_error("idempotency_key_required"))
    service = _service(request)
    await _require_workflow(service, session_id, workflow_id)

    async def mutate(transaction: Transaction, app: WorkflowApplication, _lease: MasterLease):
        current = await transaction.fetch_one(
            "SELECT session_id FROM workflows WHERE id = ?", (workflow_id,)
        )
        if current is None or str(current["session_id"]) != session_id:
            raise RecordNotFound("workflow not found")
        record = await app.services.workflows.update_author_graph_in(
            transaction,
            workflow_id,
            body.author_graph,
            expected_semantic_version=body.expected_semantic_version,
        )
        return _stored(
            200,
            WorkflowUpdateResponse(
                workflow_id=record.workflow_id,
                semantic_version=record.semantic_version,
            ),
        )

    return await _execute_idempotent(
        request,
        operation_scope="PUT:/api/v1/sessions/{session_id}/workflows/{workflow_id}",
        key=idempotency_key,
        body=body,
        mutate=mutate,
    )


@protected_router.post(
    "/sessions/{session_id}/workflows/{workflow_id}/validate",
    response_model=ValidateResponse,
    tags=["workflows"],
)
async def validate_workflow(
    session_id: str,
    workflow_id: str,
    request: Request,
    body: ValidateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail=_error("idempotency_key_required"))
    service = _service(request)
    replay = await _idempotency_replay(
        request,
        operation_scope="POST:/api/v1/sessions/{session_id}/workflows/{workflow_id}/validate",
        key=idempotency_key,
        body=body,
    )
    if replay is not None:
        return replay
    workflow = await _require_workflow(service, session_id, workflow_id)
    if workflow.semantic_version != body.expected_semantic_version:
        raise ConcurrencyConflict("workflow semantic version is stale")
    async with service.prepare_validation(workflow_id) as prepared:
        if prepared.workflow.session_id != session_id:
            raise RecordNotFound("workflow not found")
        if prepared.workflow.semantic_version != body.expected_semantic_version:
            raise ConcurrencyConflict("workflow semantic version is stale")

        async def mutate(transaction: Transaction, app: WorkflowApplication, _lease: MasterLease):
            if prepared.response.ok and prepared.response.compiled_graph is not None:
                await app.services.workflows.save_compiled_preview_in(
                    transaction,
                    workflow_id,
                    prepared.response.compiled_graph,
                    expected_semantic_version=prepared.workflow.semantic_version,
                    workspace_lease=prepared.workspace_lease,
                    workspace_leases=app.services.workspace_leases,
                )
            return _stored(200, prepared.response)

        return await _execute_idempotent(
            request,
            operation_scope="POST:/api/v1/sessions/{session_id}/workflows/{workflow_id}/validate",
            key=idempotency_key,
            body=body,
            mutate=mutate,
        )


@protected_router.post(
    "/sessions/{session_id}/workflows/{workflow_id}/runs",
    response_model=RunAcceptedResponse,
    status_code=202,
    tags=["runs"],
)
async def enqueue_run(
    session_id: str,
    workflow_id: str,
    request: Request,
    body: RunRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail=_error("idempotency_key_required"))
    service = _service(request)
    replay = await _idempotency_replay(
        request,
        operation_scope="POST:/api/v1/sessions/{session_id}/workflows/{workflow_id}/runs",
        key=idempotency_key,
        body=body,
    )
    if replay is not None:
        return replay
    workflow = await _require_workflow(service, session_id, workflow_id)
    if workflow.semantic_version != body.expected_semantic_version:
        raise ConcurrencyConflict("workflow semantic version is stale")
    workflow_run_id = f"run-{secrets.token_hex(16)}"
    async with service.prepare_run(
        workflow_id,
        expected_semantic_version=body.expected_semantic_version,
        confirmed_compiled_hash=body.confirmed_compiled_hash,
        workflow_run_id=workflow_run_id,
    ) as prepared:
        if prepared.value.workflow.session_id != session_id:
            raise RecordNotFound("workflow not found")

        async def mutate(transaction: Transaction, app: WorkflowApplication, lease: MasterLease):
            await app.services.workflows.save_compiled_preview_in(
                transaction,
                workflow_id,
                prepared.value.compiled_graph,
                expected_semantic_version=prepared.value.workflow.semantic_version,
                workspace_lease=prepared.workspace_lease,
                workspace_leases=app.services.workspace_leases,
            )
            record = await app.services.runs.create_in(
                transaction,
                prepared.value,
                lease=lease,
                workspace_lease=prepared.workspace_lease,
            )
            return _stored(
                202,
                RunAcceptedResponse(
                    workflow_run_id=record.workflow_run_id,
                    status=record.status,
                ),
            )

        return await _execute_idempotent(
            request,
            operation_scope="POST:/api/v1/sessions/{session_id}/workflows/{workflow_id}/runs",
            key=idempotency_key,
            body=body,
            mutate=mutate,
        )


@protected_router.get(
    "/sessions/{session_id}/workflow-runs/{workflow_run_id}",
    response_model=WorkflowRunResponse,
    tags=["runs"],
)
async def get_workflow_run(
    session_id: str,
    workflow_run_id: str,
    request: Request,
) -> WorkflowRunResponse:
    service = _service(request)
    run = await _require_run(service, session_id, workflow_run_id)
    nodes = await service.services.runs.list_nodes(workflow_run_id)
    approvals = await service.services.approvals.list_for_workflow_run(workflow_run_id)
    changeset_approvals = [
        _approval_response(record)
        for record in approvals
        if isinstance(record.approval, ChangeSetApproval)
    ]
    return WorkflowRunResponse(
        workflow_run_id=run.workflow_run_id,
        workflow_id=run.workflow_id,
        session_id=run.session_id,
        status=run.status,
        current_commit=run.current_commit,
        compiled_snapshot_hash=run.compiled_snapshot_hash,
        nodes=[
            NodeStatusResponse(
                node_run_id=node.node_run_id,
                node_id=node.node_id,
                node_type=node.node_type,
                attempt=node.attempt,
                status=node.status,
                outcome=node.outcome,
                error_code=node.error_code,
                output_artifact_id=node.output_artifact_id,
            )
            for node in nodes
        ],
        approvals=changeset_approvals,
    )


@protected_router.get(
    "/sessions/{session_id}/artifacts",
    response_model=ArtifactListResponse,
    tags=["artifacts"],
)
async def list_artifacts(session_id: str, request: Request) -> ArtifactListResponse:
    service = _service(request)
    await _require_session(service, session_id)
    records = await service.services.artifacts.list_by_session(session_id, limit=500)
    return ArtifactListResponse(
        items=[
            ArtifactMetadataResponse(
                artifact_id=record.artifact_id,
                artifact_type=ArtifactType(record.artifact_type),
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                redacted=record.redacted,
            )
            for record in records
        ]
    )


@protected_router.get(
    "/sessions/{session_id}/artifacts/{artifact_id}",
    tags=["artifacts"],
)
async def read_artifact(session_id: str, artifact_id: str, request: Request) -> Response:
    service = _service(request)
    await _require_session(service, session_id)
    metadata = await service.services.artifacts.get(artifact_id)
    if metadata.session_id != session_id:
        raise ArtifactNotFound("artifact not found")
    if metadata.size_bytes > request.app.state.settings.max_http_artifact_bytes:
        raise HTTPException(
            status_code=413,
            detail={"code": "artifact_too_large", "message": "Artifact exceeds the read limit."},
        )
    verified, content = await service.services.artifacts.get_and_verify(
        artifact_id,
        expected_session_id=session_id,
        expected_task_id=metadata.task_id,
        max_bytes=request.app.state.settings.max_http_artifact_bytes,
    )
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "ETag": f'"{verified.sha256}"',
            "X-Content-SHA256": verified.sha256,
        },
    )


@protected_router.get(
    "/sessions/{session_id}/change-sets/{change_set_id}",
    response_model=ChangeSetResponse,
    tags=["change-sets"],
)
async def get_change_set(
    session_id: str, change_set_id: str, request: Request
) -> ChangeSetResponse:
    service = _service(request)
    await _require_session(service, session_id)
    record = await service.services.change_sets.get(
        change_set_id,
        expected_session_id=session_id,
        max_artifact_bytes=request.app.state.settings.max_http_artifact_bytes,
        max_total_artifact_bytes=request.app.state.settings.max_http_artifact_bytes * 4,
    )
    change_set = record.change_set
    if change_set.session_id != session_id:
        raise RecordNotFound("ChangeSet not found")
    patch = change_set.canonical_patch_ref
    return ChangeSetResponse(
        change_set_id=change_set.change_set_id,
        session_id=change_set.session_id,
        workflow_run_id=change_set.workflow_run_id,
        status=change_set.status,
        base_commit=change_set.base_commit,
        patch_sha256=change_set.patch_sha256,
        patch_artifact_id=patch.artifact_id,
        patch_size_bytes=patch.size_bytes,
        created_files=[str(path) for path in change_set.created_files],
        modified_files=[str(path) for path in change_set.modified_files],
        deleted_files=[str(path) for path in change_set.deleted_files],
        renamed_files=[str(path) for path in change_set.renamed_files],
    )


@protected_router.get(
    "/sessions/{session_id}/change-sets/{change_set_id}/diff",
    tags=["change-sets"],
)
async def read_change_set_diff(
    session_id: str,
    change_set_id: str,
    request: Request,
) -> Response:
    service = _service(request)
    await _require_session(service, session_id)
    record = await service.services.change_sets.get(
        change_set_id,
        expected_session_id=session_id,
        max_artifact_bytes=request.app.state.settings.max_http_artifact_bytes,
        max_total_artifact_bytes=request.app.state.settings.max_http_artifact_bytes * 4,
    )
    change_set = record.change_set
    if change_set.session_id != session_id:
        raise RecordNotFound("ChangeSet not found")
    patch_ref = change_set.canonical_patch_ref
    if patch_ref.size_bytes > request.app.state.settings.max_http_artifact_bytes:
        raise HTTPException(
            status_code=413,
            detail={"code": "artifact_too_large", "message": "Diff exceeds the read limit."},
        )
    metadata, content = await service.services.artifacts.get_and_verify(
        patch_ref.artifact_id,
        expected_session_id=session_id,
        expected_task_id=change_set.task_id,
        max_bytes=request.app.state.settings.max_http_artifact_bytes,
    )
    if metadata.artifact_type != ArtifactType.PATCH.value:
        raise ContainmentViolation("canonical patch artifact has an unexpected type")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "ETag": f'"{metadata.sha256}"',
            "X-Content-SHA256": metadata.sha256,
        },
    )


@protected_router.get(
    "/sessions/{session_id}/approvals/{approval_id}",
    response_model=ApprovalResponse,
    tags=["approvals"],
)
async def get_approval(session_id: str, approval_id: str, request: Request) -> ApprovalResponse:
    service = _service(request)
    await _require_session(service, session_id)
    record = await service.services.approvals.get(approval_id)
    await _require_run(service, session_id, record.approval.workflow_run_id)
    return _approval_response(record)


async def _decide_approval(
    session_id: str,
    approval_id: str,
    request: Request,
    body: ApprovalDecisionRequest,
    idempotency_key: str | None,
    *,
    approve: bool,
) -> ApprovalResponse:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail=_error("idempotency_key_required"))
    _validate_idempotency_key(idempotency_key)
    service = _service(request)
    await _require_session(service, session_id)
    current = await service.services.approvals.get(approval_id)
    await _require_run(service, session_id, current.approval.workflow_run_id)
    if not isinstance(current.approval, ChangeSetApproval):
        raise RecordNotFound("ChangeSet approval not found")
    decided = await service.services.approval_manager.decide(
        approval_id,
        expected_version=body.approval_version,
        confirm_subject_hash=body.confirm_subject_hash,
        approved=approve,
        actor_id="local-api-user",
        idempotency_key=idempotency_key,
        master_lease=_master_lease(request),
    )
    return _approval_response(decided)


@protected_router.post(
    "/sessions/{session_id}/approvals/{approval_id}/approve",
    response_model=ApprovalResponse,
    tags=["approvals"],
)
async def approve(
    session_id: str,
    approval_id: str,
    request: Request,
    body: ApprovalDecisionRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ApprovalResponse:
    return await _decide_approval(
        session_id,
        approval_id,
        request,
        body,
        idempotency_key,
        approve=True,
    )


@protected_router.post(
    "/sessions/{session_id}/approvals/{approval_id}/reject",
    response_model=ApprovalResponse,
    tags=["approvals"],
)
async def reject(
    session_id: str,
    approval_id: str,
    request: Request,
    body: ApprovalDecisionRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ApprovalResponse:
    return await _decide_approval(
        session_id,
        approval_id,
        request,
        body,
        idempotency_key,
        approve=False,
    )


@fallback_router.api_route(
    "",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
@fallback_router.api_route(
    "/{unmatched:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def unknown_api(_request: Request) -> Response:
    raise HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": "API endpoint not found."},
    )


def _error(code: str) -> dict[str, str]:
    return {"code": code, "message": "The request could not be completed."}


__all__ = ["fallback_router", "health", "protected_router", "router"]
