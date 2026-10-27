from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.services import WorkflowApplication
from protocol import (
    ArtifactType,
    AssignmentMode,
    AuthorGraph,
    NodeType,
    RiskLevel,
    TaskKind,
    WorkflowEdge,
    WorkflowNode,
    WorkflowRunStatus,
)
from protocol.api import ApprovalDecisionRequest, RunRequest, ValidateRequest

API_TOKEN = "hub400-test-token-not-for-production"
API_HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}
API_PORT = 9876


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject", "expired"])
async def test_mock_http_product_loop_and_decision_conflict(
    fixture_source_repo: Path,
    tmp_path: Path,
    decision: str,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "hub400-data",
        api_port=API_PORT,
        scheduler_poll_ms=50,
        max_http_body_bytes=64 * 1024,
    )
    service = WorkflowApplication(settings)
    await service.initialize()
    await service.register_mock_agent()

    async with service.temporary_master() as lease:
        application = create_app(
            settings,
            workflow_application=service,
            master_lease=lease,
            api_token=API_TOKEN,
        )
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url=f"http://127.0.0.1:{API_PORT}",
            headers=API_HEADERS,
        ) as client:
            session_request = {
                "repository": str(fixture_source_repo),
                "goal": "Create one passing test.",
            }
            created = await client.post(
                "/api/v1/sessions",
                json=session_request,
                headers={"Idempotency-Key": "session-create-1"},
            )
            assert created.status_code == 201, created.text
            session = created.json()
            replay = await client.post(
                "/api/v1/sessions",
                json=session_request,
                headers={"Idempotency-Key": "session-create-1"},
            )
            assert replay.status_code == 201
            assert replay.json() == session
            drift = await client.post(
                "/api/v1/sessions",
                json={**session_request, "goal": "A different request."},
                headers={"Idempotency-Key": "session-create-1"},
            )
            assert drift.status_code == 409
            assert drift.json()["error"]["code"] == "conflict"

            session_id = session["session_id"]
            planned = await client.post(
                f"/api/v1/sessions/{session_id}/plans",
                json={},
                headers={"Idempotency-Key": "plan-1"},
            )
            assert planned.status_code == 202, planned.text
            plan_replay = await client.post(
                f"/api/v1/sessions/{session_id}/plans",
                json={},
                headers={"Idempotency-Key": "plan-1"},
            )
            assert plan_replay.status_code == 202
            assert plan_replay.json() == planned.json()
            await service.services.scheduler.tick_next(lease=lease)
            planner_run = await client.get(
                f"/api/v1/sessions/{session_id}/planner-runs/{planned.json()['planner_run_id']}"
            )
            assert planner_run.status_code == 200, planner_run.text
            assert planner_run.json()["status"] == "succeeded"
            workflow_id = planner_run.json()["result_workflow_id"]

            workflow_response = await client.get(
                f"/api/v1/sessions/{session_id}/workflows/{workflow_id}"
            )
            assert workflow_response.status_code == 200
            workflow = workflow_response.json()
            updated = await client.put(
                f"/api/v1/sessions/{session_id}/workflows/{workflow_id}",
                json={
                    "expected_semantic_version": workflow["semantic_version"],
                    "author_graph": _write_test_graph().model_dump(mode="json"),
                },
                headers={"Idempotency-Key": "workflow-edit-1"},
            )
            assert updated.status_code == 200, updated.text
            semantic_version = updated.json()["semantic_version"]
            stale = await client.put(
                f"/api/v1/sessions/{session_id}/workflows/{workflow_id}",
                json={
                    "expected_semantic_version": workflow["semantic_version"],
                    "author_graph": _write_test_graph().model_dump(mode="json"),
                },
                headers={"Idempotency-Key": "workflow-edit-stale"},
            )
            assert stale.status_code == 409

            validation_body = ValidateRequest(expected_semantic_version=semantic_version)
            validation = await client.post(
                f"/api/v1/sessions/{session_id}/workflows/{workflow_id}/validate",
                json=validation_body.model_dump(mode="json"),
                headers={"Idempotency-Key": "validate-1"},
            )
            assert validation.status_code == 200, validation.text
            validation_data = validation.json()
            assert validation_data["ok"] is True
            validate_replay = await client.post(
                f"/api/v1/sessions/{session_id}/workflows/{workflow_id}/validate",
                json=validation_body.model_dump(mode="json"),
                headers={"Idempotency-Key": "validate-1"},
            )
            assert validate_replay.json() == validation_data

            run_body = RunRequest(
                expected_semantic_version=semantic_version,
                confirmed_compiled_hash=validation_data["compiled_hash"],
            )
            queued = await client.post(
                f"/api/v1/sessions/{session_id}/workflows/{workflow_id}/runs",
                json=run_body.model_dump(mode="json"),
                headers={"Idempotency-Key": "run-1"},
            )
            assert queued.status_code == 202, queued.text
            run_id = queued.json()["workflow_run_id"]
            run_replay = await client.post(
                f"/api/v1/sessions/{session_id}/workflows/{workflow_id}/runs",
                json=run_body.model_dump(mode="json"),
                headers={"Idempotency-Key": "run-1"},
            )
            assert run_replay.status_code == 202
            assert run_replay.json() == queued.json()

            status_path = f"/api/v1/sessions/{session_id}/workflow-runs/{run_id}"
            for _ in range(30):
                await service.services.scheduler.tick_next(lease=lease)
                status = await client.get(status_path)
                assert status.status_code == 200, status.text
                if status.json()["status"] == "waiting_approval":
                    break
            assert status.json()["status"] == "waiting_approval"
            approval = status.json()["approvals"][0]
            change_set_id = approval["change_set_id"]

            change_set_response = await client.get(
                f"/api/v1/sessions/{session_id}/change-sets/{change_set_id}"
            )
            assert change_set_response.status_code == 200
            change_set = change_set_response.json()
            diff = await client.get(
                f"/api/v1/sessions/{session_id}/change-sets/{change_set_id}/diff"
            )
            assert diff.status_code == 200
            assert diff.content

            artifact_list = await client.get(f"/api/v1/sessions/{session_id}/artifacts")
            assert artifact_list.status_code == 200
            assert change_set["patch_artifact_id"] in {
                item["artifact_id"] for item in artifact_list.json()["items"]
            }
            artifact_path = (
                f"/api/v1/sessions/{session_id}/artifacts/{change_set['patch_artifact_id']}"
            )
            artifact = await client.get(artifact_path)
            assert artifact.status_code == 200
            assert artifact.content == diff.content

            other_session = await client.post(
                "/api/v1/sessions",
                json={"repository": str(fixture_source_repo), "goal": "Another session."},
                headers={"Idempotency-Key": "session-create-2"},
            )
            assert other_session.status_code == 201, other_session.text
            cross_session = await client.get(
                f"/api/v1/sessions/{other_session.json()['session_id']}"
                f"/artifacts/{change_set['patch_artifact_id']}"
            )
            assert cross_session.status_code == 404
            cross_change_set = await client.get(
                f"/api/v1/sessions/{other_session.json()['session_id']}/change-sets/{change_set_id}"
            )
            assert cross_change_set.status_code == 404
            cross_diff = await client.get(
                f"/api/v1/sessions/{other_session.json()['session_id']}"
                f"/change-sets/{change_set_id}/diff"
            )
            assert cross_diff.status_code == 404

            decision_body = ApprovalDecisionRequest(
                approval_version=approval["version"],
                confirm_subject_hash=approval["subject_sha256"],
            )
            if decision == "expired":
                async with service.services.database.immediate_transaction() as transaction:
                    await transaction.execute(
                        "UPDATE approvals SET expires_at = ? WHERE id = ?",
                        ("2000-01-01T00:00:00.000000Z", approval["approval_id"]),
                    )
            decision_response = await client.post(
                f"/api/v1/sessions/{session_id}/approvals/{approval['approval_id']}/"
                f"{'approve' if decision == 'expired' else decision}",
                json=decision_body.model_dump(mode="json"),
                headers={"Idempotency-Key": f"approval-{decision}-1"},
            )
            if decision == "expired":
                assert decision_response.status_code == 409
                assert decision_response.json()["error"]["code"] == "conflict"
            else:
                assert decision_response.status_code == 200, decision_response.text
                expected_status = "approved" if decision == "approve" else "rejected"
                assert decision_response.json()["status"] == expected_status
                decision_replay = await client.post(
                    f"/api/v1/sessions/{session_id}/approvals/{approval['approval_id']}/{decision}",
                    json=decision_body.model_dump(mode="json"),
                    headers={"Idempotency-Key": f"approval-{decision}-1"},
                )
                assert decision_replay.status_code == 200
                assert decision_replay.json() == decision_response.json()

                opposite = "reject" if decision == "approve" else "approve"
                conflict = await client.post(
                    f"/api/v1/sessions/{session_id}/approvals/{approval['approval_id']}/{opposite}",
                    json=decision_body.model_dump(mode="json"),
                    headers={"Idempotency-Key": f"approval-{opposite}-conflict"},
                )
                assert conflict.status_code == 409
                assert conflict.json()["error"]["code"] == "conflict"

            if decision == "approve":
                for _ in range(30):
                    await service.services.scheduler.tick_next(lease=lease)
                    status = await client.get(status_path)
                    if status.json()["status"] in {
                        WorkflowRunStatus.COMPLETED.value,
                        WorkflowRunStatus.FAILED.value,
                        WorkflowRunStatus.ORPHANED.value,
                    }:
                        break
                assert status.json()["status"] == WorkflowRunStatus.COMPLETED.value
                assert (
                    service.services.git.state(
                        (await service.services.sessions.get(session_id)).shared_repo_path
                    ).dirty
                    is False
                )
            session_record = await service.services.sessions.get(session_id)
            repository_state = service.services.git.state(session_record.shared_repo_path)
            assert repository_state.dirty is False
            assert repository_state.commit == session_record.integration_head_commit
            if decision != "approve":
                assert repository_state.commit == session["integration_head_commit"]

            metadata = await service.services.artifacts.get(change_set["patch_artifact_id"])
            artifact_file = service.services.artifacts._store.resolve(
                metadata.artifact_id,
                metadata.artifact_type,
            )
            artifact_file.chmod(0o600)
            artifact_file.write_bytes(artifact.content + b"tampered")
            drifted = await client.get(artifact_path)
            assert drifted.status_code == 409
            assert drifted.json()["error"]["code"] == "artifact_integrity_conflict"


@pytest.mark.asyncio
async def test_api_auth_host_origin_and_url_token_boundaries(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, api_port=API_PORT)
    application = create_app(settings, api_token=API_TOKEN)
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url=f"http://127.0.0.1:{API_PORT}",
    ) as client:
        unauthenticated = await client.post(
            "/api/v1/sessions",
            json={"repository": "C:/private/repo", "goal": "demo"},
            headers={"Idempotency-Key": "auth-fail"},
        )
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["error"]["code"] == "unauthorized"

        non_ascii_request = client.build_request(
            "GET",
            "/api/v1/unknown",
            headers=[(b"Authorization", b"Bearer \xe9")],
        )
        non_ascii_bearer = await client.send(non_ascii_request)
        assert non_ascii_bearer.status_code == 401
        assert non_ascii_bearer.json()["error"]["code"] == "unauthorized"

        invalid_host = await client.get(
            "/healthz",
            headers={"Host": f"attacker.example:{API_PORT}"},
        )
        assert invalid_host.status_code == 421

        invalid_origin = await client.get(
            "/api/v1/unknown",
            headers={
                **API_HEADERS,
                "Origin": f"http://attacker.example:{API_PORT}",
            },
        )
        assert invalid_origin.status_code == 403

        unknown_unauthenticated = await client.get("/api/v1/not-an-endpoint")
        assert unknown_unauthenticated.status_code == 401
        unknown_authenticated = await client.get(
            "/api/v1/not-an-endpoint",
            headers=API_HEADERS,
        )
        assert unknown_authenticated.status_code == 404

        websocket_path = await client.get("/ws/events")
        assert websocket_path.status_code == 404

        token_in_url = await client.get(f"/api/v1/unknown?access_token={API_TOKEN}")
        assert token_in_url.status_code == 400
        assert API_TOKEN not in token_in_url.text

        docs = await client.get("/openapi.json")
        assert docs.status_code == 404


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "agent-hub.local"])
def test_settings_refuse_non_loopback_api_host(host: str) -> None:
    with pytest.raises(ValueError):
        Settings(api_host=host)


@pytest.mark.asyncio
async def test_actual_streamed_body_limit_without_content_length(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, api_port=API_PORT, max_http_body_bytes=1024)
    application = create_app(settings, api_token=API_TOKEN)
    messages = [
        {"type": "http.request", "body": b"x" * 600, "more_body": True},
        {"type": "http.request", "body": b"y" * 600, "more_body": False},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/sessions",
        "raw_path": b"/api/v1/sessions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", f"127.0.0.1:{API_PORT}".encode()),
            (b"authorization", f"Bearer {API_TOKEN}".encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", API_PORT),
        "state": {},
    }
    await application(scope, receive, send)

    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["error"]["code"] == "request_body_too_large"


@pytest.mark.asyncio
async def test_artifact_read_limit_caps_disk_content_growth(
    fixture_source_repo: Path,
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "hub400-artifact-limit",
        api_port=API_PORT,
        max_http_artifact_bytes=1024,
    )
    service = WorkflowApplication(settings)
    await service.initialize()
    session = await service.create_session(
        repo=fixture_source_repo,
        goal="Verify bounded artifact reads.",
        session_id="session-api-limit",
    )
    artifact = await service.services.artifacts.create(
        artifact_id="artifact-api-limit",
        session_id=session.session_id,
        artifact_type=ArtifactType.LOG,
        content=b"small original artifact",
    )
    path = service.services.artifacts._store.resolve(
        artifact.artifact_id,
        artifact.artifact_type,
    )
    path.chmod(0o600)
    path.write_bytes(b"x" * 4096)
    application = create_app(
        settings,
        workflow_application=service,
        api_token=API_TOKEN,
    )
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url=f"http://127.0.0.1:{API_PORT}",
        headers=API_HEADERS,
    ) as client:
        response = await client.get(
            f"/api/v1/sessions/{session.session_id}/artifacts/{artifact.artifact_id}"
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "quota_exceeded"
    assert len(response.content) < 1024


def _write_test_graph() -> AuthorGraph:
    return AuthorGraph(
        nodes=[
            WorkflowNode(id="input", node_type=NodeType.INPUT, title="Input"),
            WorkflowNode(
                id="write-test",
                node_type=NodeType.AGENT_TASK,
                task_kind=TaskKind.TEST_FIX,
                title="Create a passing test",
                instruction="Create and run the exact approved test file.",
                assigned_agent="mock",
                assignment_mode=AssignmentMode.LOCKED,
                new_files_candidate=["tests/test_api_generated.py"],
                allowed_commands_candidate=[["pytest", "-q", "tests/test_api_generated.py"]],
                risk_level_hint=RiskLevel.L1,
                requires_write=True,
            ),
            WorkflowNode(id="output", node_type=NodeType.OUTPUT, title="Output"),
        ],
        edges=[
            WorkflowEdge(id="edge-input-write", from_node="input", to_node="write-test"),
            WorkflowEdge(id="edge-write-output", from_node="write-test", to_node="output"),
        ],
    )
