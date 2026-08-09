"""Durable approval, privilege-request, and capability-grant persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import aiosqlite

from protocol import (
    ActorType,
    Approval,
    ApprovalStatus,
    CapabilityGrant,
    ChangeSetApproval,
    ChangeSetStatus,
    PrivilegeAction,
    PrivilegeApproval,
    PrivilegeRequest,
    PrivilegeRequestStatus,
    RiskLevel,
)
from storage.db import Database, Transaction, utc_now_text
from storage.errors import ConcurrencyConflict, RecordNotFound
from storage.event_repository import EventRepository
from storage.leases import MasterLease, MasterLeaseRepository, WorkspaceLease
from workflow.events import (
    APPROVAL_STATE_CHANGED,
    CAPABILITY_GRANT_STATE_CHANGED,
    PRIVILEGE_REQUEST_STATE_CHANGED,
    ApprovalEventPayload,
    CapabilityGrantEventPayload,
    PrivilegeRequestEventPayload,
)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval: Approval
    created_at: str
    decided_at: str | None
    decision_actor: str | None
    decision_idempotency_key: str | None


@dataclass(frozen=True, slots=True)
class CapabilityGrantRecord:
    grant: CapabilityGrant
    workflow_run_id: str
    node_run_id: str
    request_status: PrivilegeRequestStatus


async def _conn_one(
    connection: aiosqlite.Connection,
    query: str,
    params: tuple[object, ...],
) -> aiosqlite.Row | None:
    cursor = await connection.execute(query, params)
    row = await cursor.fetchone()
    await cursor.close()
    return row


async def _workflow_id(tx: Transaction, run_id: str) -> str:
    row = await tx.fetch_one("SELECT workflow_id FROM workflow_runs WHERE id = ?", (run_id,))
    if row is None:
        raise RecordNotFound(f"workflow run not found: {run_id}")
    return str(row["workflow_id"])


async def _session_id(tx: Transaction, run_id: str) -> str:
    row = await tx.fetch_one("SELECT session_id FROM workflow_runs WHERE id = ?", (run_id,))
    if row is None:
        raise RecordNotFound(f"workflow run not found: {run_id}")
    return str(row["session_id"])


def _approval_record(row: aiosqlite.Row) -> ApprovalRecord:
    common = {
        "approval_id": str(row["id"]),
        "workflow_run_id": str(row["workflow_run_id"]),
        "node_run_id": str(row["node_run_id"]),
        "subject_sha256": str(row["subject_sha256"]),
        "effective_risk": RiskLevel(str(row["effective_risk"])),
        "scope": json.loads(str(row["scope_json"])),
        "status": ApprovalStatus(str(row["status"])),
        "version": int(row["version"]),
        "expires_at": str(row["expires_at"]),
    }
    if str(row["subject_type"]) == "change_set":
        value: Approval = ChangeSetApproval(
            **common,
            change_set_id=str(row["change_set_id"]),
            base_commit=str(row["base_commit"]),
            patch_sha256=str(row["patch_sha256"]),
            evidence_sha256=str(row["evidence_sha256"]),
        )
    else:
        value = PrivilegeApproval(
            **common,
            privilege_request_id=str(row["privilege_request_id"]),
            evidence_sha256=str(row["evidence_sha256"]),
        )
    return ApprovalRecord(
        approval=value,
        created_at=str(row["created_at"]),
        decided_at=str(row["decided_at"]) if row["decided_at"] else None,
        decision_actor=str(row["decision_actor"]) if row["decision_actor"] else None,
        decision_idempotency_key=(
            str(row["decision_idempotency_key"]) if row["decision_idempotency_key"] else None
        ),
    )


def _grant_record(
    row: aiosqlite.Row,
    *,
    workflow_run_id: str,
    node_run_id: str,
) -> CapabilityGrantRecord:
    return CapabilityGrantRecord(
        grant=CapabilityGrant(
            grant_id=str(row["id"]),
            request_id=str(row["request_id"]),
            target_task_id=str(row["target_task_id"]),
            action=str(row["action"]),
            resource=str(row["resource"]),
            expires_at=str(row["expires_at"]),
            consumed_at=str(row["consumed_at"]) if row["consumed_at"] else None,
            consumed_fencing_token=(
                int(row["consumed_fencing_token"])
                if row["consumed_fencing_token"] is not None
                else None
            ),
            revoked_at=str(row["revoked_at"]) if row["revoked_at"] else None,
            revocation_reason=str(row["revocation_reason"]) if row["revocation_reason"] else None,
        ),
        workflow_run_id=workflow_run_id,
        node_run_id=node_run_id,
        request_status=PrivilegeRequestStatus(
            str(row["request_status"]) if "request_status" in row else "approved"
        ),
    )


class ApprovalRepository:
    def __init__(
        self,
        database: Database,
        events: EventRepository,
        master_leases: MasterLeaseRepository,
    ) -> None:
        self._database = database
        self._events = events
        self._master_leases = master_leases

    async def get(self, approval_id: str) -> ApprovalRecord:
        async with self._database.connection() as connection:
            row = await _conn_one(
                connection, "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            )
        if row is None:
            raise RecordNotFound(f"approval not found: {approval_id}")
        return _approval_record(row)

    async def get_for_change_set(self, change_set_id: str) -> ApprovalRecord | None:
        async with self._database.connection() as connection:
            row = await _conn_one(
                connection,
                "SELECT * FROM approvals WHERE change_set_id = ? ORDER BY created_at DESC LIMIT 1",
                (change_set_id,),
            )
        return _approval_record(row) if row is not None else None

    async def get_pending_for_change_set(self, change_set_id: str) -> ApprovalRecord | None:
        async with self._database.connection() as connection:
            row = await _conn_one(
                connection,
                "SELECT * FROM approvals WHERE change_set_id = ? AND status = 'pending' "
                "ORDER BY created_at DESC LIMIT 1",
                (change_set_id,),
            )
        return _approval_record(row) if row is not None else None

    async def get_for_privilege_request(self, request_id: str) -> ApprovalRecord | None:
        async with self._database.connection() as connection:
            row = await _conn_one(
                connection,
                "SELECT * FROM approvals WHERE privilege_request_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (request_id,),
            )
        return _approval_record(row) if row is not None else None

    async def create(
        self,
        *,
        approval: Approval,
        master_lease: MasterLease,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        if approval.status != ApprovalStatus.PENDING or approval.version != 1:
            raise ValueError("new approvals must start pending at version 1")
        if approval.effective_risk == RiskLevel.L4:
            raise ValueError("L4 cannot create an approval")
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as tx:
            await self._master_leases.assert_valid_in(tx, master_lease, now=now)
            existing = await tx.fetch_one(
                """
                SELECT * FROM approvals
                WHERE (change_set_id = ? OR privilege_request_id = ?)
                  AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    approval.change_set_id if isinstance(approval, ChangeSetApproval) else None,
                    approval.privilege_request_id
                    if isinstance(approval, PrivilegeApproval)
                    else None,
                ),
            )
            if existing is not None:
                current = _approval_record(existing)
                if current.approval.subject_sha256 != approval.subject_sha256:
                    raise ConcurrencyConflict("pending approval subject drifted")
                return current
            owner = await tx.fetch_one(
                "SELECT workflow_run_id FROM node_runs WHERE id = ?",
                (approval.node_run_id,),
            )
            if owner is None:
                raise RecordNotFound(f"approval node run not found: {approval.node_run_id}")
            if str(owner["workflow_run_id"]) != approval.workflow_run_id:
                raise ValueError("approval node does not belong to the workflow run")
            if isinstance(approval, ChangeSetApproval):
                row = await tx.fetch_one(
                    "SELECT nr.workflow_run_id, t.node_run_id, cs.status, "
                    "wr.status AS workflow_status, wr.cancel_requested_at "
                    "FROM change_sets cs JOIN tasks t ON t.id = cs.task_id "
                    "JOIN node_runs nr ON nr.id = t.node_run_id "
                    "JOIN workflow_runs wr ON wr.id = nr.workflow_run_id "
                    "WHERE cs.id = ?",
                    (approval.change_set_id,),
                )
                if row is None:
                    raise RecordNotFound(f"change set not found: {approval.change_set_id}")
                if str(row["workflow_run_id"]) != approval.workflow_run_id or str(
                    row["status"]
                ) not in {
                    ChangeSetStatus.TEST_PASSED.value,
                    ChangeSetStatus.PENDING_APPROVAL.value,
                }:
                    raise ConcurrencyConflict("ChangeSet is not ready for approval")
                if row["cancel_requested_at"] is not None or str(row["workflow_status"]) in {
                    "cancelled",
                    "completed",
                    "failed",
                    "orphaned",
                }:
                    raise ConcurrencyConflict("workflow cancellation prevents approval")
                if str(row["status"]) == ChangeSetStatus.TEST_PASSED.value:
                    changed = await tx.execute(
                        "UPDATE change_sets SET status = ?, updated_at = ? "
                        "WHERE id = ? AND status = ?",
                        (
                            ChangeSetStatus.PENDING_APPROVAL.value,
                            timestamp,
                            approval.change_set_id,
                            ChangeSetStatus.TEST_PASSED.value,
                        ),
                    )
                    if changed != 1:
                        raise ConcurrencyConflict("ChangeSet approval transition lost CAS")
                columns = (
                    approval.change_set_id,
                    None,
                    approval.base_commit,
                    approval.patch_sha256,
                )
            else:
                row = await tx.fetch_one(
                    "SELECT workflow_run_id, node_run_id, "
                    "privilege_requests.task_id AS task_id, status, "
                    "wr.status AS workflow_status, wr.cancel_requested_at "
                    "FROM privilege_requests "
                    "JOIN node_runs ON node_runs.id = privilege_requests.node_run_id "
                    "JOIN workflow_runs wr ON wr.id = node_runs.workflow_run_id "
                    "WHERE privilege_requests.id = ?",
                    (approval.privilege_request_id,),
                )
                if row is None:
                    raise RecordNotFound(
                        f"privilege request not found: {approval.privilege_request_id}"
                    )
                if str(row["node_run_id"]) != approval.node_run_id:
                    raise ValueError("privilege approval node does not own request")
                if str(row["workflow_run_id"]) != approval.workflow_run_id:
                    raise ValueError("privilege approval workflow does not own request")
                if row["cancel_requested_at"] is not None or str(row["workflow_status"]) in {
                    "cancelled",
                    "completed",
                    "failed",
                    "orphaned",
                }:
                    raise ConcurrencyConflict("workflow cancellation prevents approval")
                changed = await tx.execute(
                    "UPDATE privilege_requests SET status = 'waiting_approval' "
                    "WHERE id = ? AND status IN ('pending', 'waiting_approval')",
                    (approval.privilege_request_id,),
                )
                if changed != 1:
                    raise ConcurrencyConflict("privilege request is not awaiting approval")
                await self._append_privilege_request_event(
                    tx,
                    workflow_run_id=str(row["workflow_run_id"]),
                    node_run_id=str(row["node_run_id"]),
                    task_id=str(row["task_id"]),
                    request_id=approval.privilege_request_id,
                    previous=str(row["status"]),
                    status=PrivilegeRequestStatus.WAITING_APPROVAL.value,
                    master_lease=master_lease,
                    now=now,
                    reason="approval_created",
                )
                columns = (None, approval.privilege_request_id, None, None)
            await tx.execute(
                """
                INSERT INTO approvals(
                    id, workflow_run_id, node_run_id, subject_type, change_set_id,
                    privilege_request_id, subject_sha256, base_commit, patch_sha256,
                    evidence_sha256, effective_risk, scope_json, status, version,
                    decision_actor, decision_idempotency_key, expires_at, decided_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1,
                          NULL, ?, ?, NULL, ?)
                """,
                (
                    approval.approval_id,
                    approval.workflow_run_id,
                    approval.node_run_id,
                    approval.subject_type,
                    columns[0],
                    columns[1],
                    approval.subject_sha256,
                    columns[2],
                    columns[3],
                    approval.evidence_sha256,
                    approval.effective_risk.value,
                    json.dumps(approval.scope, separators=(",", ":"), ensure_ascii=False),
                    idempotency_key,
                    utc_now_text(approval.expires_at),
                    timestamp,
                ),
            )
            await self._append_approval_event(
                tx, approval=approval, previous=None, master_lease=master_lease, now=now
            )
            row = await tx.fetch_one(
                "SELECT * FROM approvals WHERE id = ?", (approval.approval_id,)
            )
        assert row is not None
        return _approval_record(row)

    async def decide(
        self,
        approval_id: str,
        *,
        expected_version: int,
        confirm_subject_hash: str,
        target: Literal[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED],
        actor_id: str,
        idempotency_key: str,
        master_lease: MasterLease,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        if expected_version < 1 or not idempotency_key:
            raise ValueError("approval version and idempotency key are required")
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as tx:
            await self._master_leases.assert_valid_in(tx, master_lease, now=now)
            row = await tx.fetch_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            if row is None:
                raise RecordNotFound(f"approval not found: {approval_id}")
            current = _approval_record(row)
            if current.approval.subject_sha256 != confirm_subject_hash:
                raise ConcurrencyConflict("approval subject hash does not match")
            if (
                current.approval.status == target
                and current.decision_idempotency_key == idempotency_key
            ):
                return current
            workflow = await tx.fetch_one(
                "SELECT status, cancel_requested_at FROM workflow_runs WHERE id = ?",
                (current.approval.workflow_run_id,),
            )
            if workflow is None:
                raise RecordNotFound(f"workflow run not found: {current.approval.workflow_run_id}")
            if workflow["cancel_requested_at"] is not None or str(workflow["status"]) in {
                "cancelled",
                "completed",
                "failed",
                "orphaned",
            }:
                raise ConcurrencyConflict("workflow cancellation prevents approval decision")
            if current.approval.status != ApprovalStatus.PENDING:
                raise ConcurrencyConflict(f"approval is already {current.approval.status.value}")
            if current.approval.version != expected_version:
                raise ConcurrencyConflict("approval version CAS failed")
            if utc_now_text(current.approval.expires_at) <= timestamp:
                await self._expire_in(tx, current, master_lease=master_lease, now=now)
                raise ConcurrencyConflict("approval expired before decision")
            changed = await tx.execute(
                """
                UPDATE approvals
                SET status = ?, version = version + 1, decision_actor = ?,
                    decision_idempotency_key = ?, decided_at = ?
                WHERE id = ? AND status = 'pending' AND version = ?
                  AND subject_sha256 = ?
                """,
                (
                    target.value,
                    actor_id,
                    idempotency_key,
                    timestamp,
                    approval_id,
                    expected_version,
                    confirm_subject_hash,
                ),
            )
            if changed != 1:
                raise ConcurrencyConflict("approval decision lost CAS")
            if isinstance(current.approval, ChangeSetApproval):
                target_status = (
                    ChangeSetStatus.APPROVED
                    if target == ApprovalStatus.APPROVED
                    else ChangeSetStatus.REJECTED
                )
                changed = await tx.execute(
                    "UPDATE change_sets SET status = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'pending_approval'",
                    (target_status.value, timestamp, current.approval.change_set_id),
                )
            else:
                request_status = (
                    PrivilegeRequestStatus.APPROVED
                    if target == ApprovalStatus.APPROVED
                    else PrivilegeRequestStatus.REJECTED
                )
                changed = await tx.execute(
                    "UPDATE privilege_requests SET status = ? "
                    "WHERE id = ? AND status IN ('pending', 'waiting_approval')",
                    (request_status.value, current.approval.privilege_request_id),
                )
            if changed != 1:
                raise ConcurrencyConflict("approval subject transition lost CAS")
            if isinstance(current.approval, PrivilegeApproval):
                request_row = await tx.fetch_one(
                    "SELECT pr.status, pr.task_id, pr.node_run_id, nr.workflow_run_id "
                    "FROM privilege_requests pr JOIN node_runs nr ON nr.id = pr.node_run_id "
                    "WHERE pr.id = ?",
                    (current.approval.privilege_request_id,),
                )
                assert request_row is not None
                await self._append_privilege_request_event(
                    tx,
                    workflow_run_id=str(request_row["workflow_run_id"]),
                    node_run_id=str(request_row["node_run_id"]),
                    task_id=str(request_row["task_id"]),
                    request_id=current.approval.privilege_request_id,
                    previous=PrivilegeRequestStatus.WAITING_APPROVAL.value,
                    status=str(request_row["status"]),
                    master_lease=master_lease,
                    now=now,
                    reason=f"approval_decision:{target.value}",
                )
            fresh = await tx.fetch_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            assert fresh is not None
            result = _approval_record(fresh)
            await self._append_approval_event(
                tx,
                approval=result.approval,
                previous=current.approval.status,
                master_lease=master_lease,
                now=now,
                reason=f"decision:{target.value}",
            )
        return result

    async def expire_due(
        self,
        *,
        master_lease: MasterLease,
        workflow_run_id: str | None = None,
        now: datetime | None = None,
    ) -> list[ApprovalRecord]:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as tx:
            await self._master_leases.assert_valid_in(tx, master_lease, now=now)
            query = "SELECT * FROM approvals WHERE status = 'pending' AND expires_at <= ?"
            params: list[object] = [timestamp]
            if workflow_run_id is not None:
                query += " AND workflow_run_id = ?"
                params.append(workflow_run_id)
            rows = await tx.fetch_all(query, tuple(params))
            results: list[ApprovalRecord] = []
            for row in rows:
                current = _approval_record(row)
                await self._expire_in(tx, current, master_lease=master_lease, now=now)
                fresh = await tx.fetch_one(
                    "SELECT * FROM approvals WHERE id = ?", (current.approval.approval_id,)
                )
                assert fresh is not None
                results.append(_approval_record(fresh))
        return results

    async def renew_changeset(
        self,
        *,
        old_approval_id: str,
        expected_version: int,
        confirm_subject_hash: str,
        new_approval: ChangeSetApproval,
        master_lease: MasterLease,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as tx:
            await self._master_leases.assert_valid_in(tx, master_lease, now=now)
            old_row = await tx.fetch_one("SELECT * FROM approvals WHERE id = ?", (old_approval_id,))
            if old_row is None:
                raise RecordNotFound(f"approval not found: {old_approval_id}")
            old = _approval_record(old_row)
            if not isinstance(old.approval, ChangeSetApproval):
                raise ValueError("only ChangeSetApproval can be renewed")
            if old.approval.status != ApprovalStatus.EXPIRED:
                raise ConcurrencyConflict("approval is not expired")
            if old.approval.version != expected_version:
                raise ConcurrencyConflict("approval version CAS failed")
            if old.approval.subject_sha256 != confirm_subject_hash:
                raise ConcurrencyConflict("approval subject hash does not match")
            run = await tx.fetch_one(
                "SELECT status, cancel_requested_at FROM workflow_runs WHERE id = ?",
                (old.approval.workflow_run_id,),
            )
            if (
                run is None
                or run["cancel_requested_at"] is not None
                or str(run["status"])
                in {
                    "cancelled",
                    "completed",
                    "failed",
                    "orphaned",
                }
            ):
                raise ConcurrencyConflict("workflow cancellation prevents approval renewal")
            if new_approval.change_set_id != old.approval.change_set_id:
                raise ValueError("renewal changed the ChangeSet")
            if (
                new_approval.workflow_run_id != old.approval.workflow_run_id
                or new_approval.base_commit != old.approval.base_commit
                or new_approval.patch_sha256 != old.approval.patch_sha256
                or new_approval.evidence_sha256 != old.approval.evidence_sha256
                or new_approval.effective_risk != old.approval.effective_risk
                or new_approval.scope != old.approval.scope
            ):
                raise ValueError("renewal changed the immutable ChangeSet subject")
            if new_approval.subject_sha256 == old.approval.subject_sha256:
                raise ValueError("renewal must bind its new expiry")
            if new_approval.status != ApprovalStatus.PENDING or new_approval.version != 1:
                raise ValueError("renewal must create a pending version-one approval")
            if new_approval.effective_risk == RiskLevel.L4:
                raise ValueError("L4 cannot renew an approval")
            if utc_now_text(new_approval.expires_at) <= timestamp:
                raise ValueError("renewal must expire in the future")
            owner = await tx.fetch_one(
                "SELECT workflow_run_id FROM node_runs WHERE id = ?",
                (new_approval.node_run_id,),
            )
            if owner is None or str(owner["workflow_run_id"]) != new_approval.workflow_run_id:
                raise ValueError("renewal node does not belong to the workflow run")
            change_set = await tx.fetch_one(
                "SELECT status FROM change_sets WHERE id = ?",
                (old.approval.change_set_id,),
            )
            if (
                change_set is None
                or str(change_set["status"]) != ChangeSetStatus.PENDING_APPROVAL.value
            ):
                raise ConcurrencyConflict("ChangeSet is no longer renewable")
            await tx.execute(
                """
                INSERT INTO approvals(
                    id, workflow_run_id, node_run_id, subject_type, change_set_id,
                    privilege_request_id, subject_sha256, base_commit, patch_sha256,
                    evidence_sha256, effective_risk, scope_json, status, version,
                    decision_actor, decision_idempotency_key, expires_at, decided_at, created_at
                ) VALUES (?, ?, ?, 'change_set', ?, NULL, ?, ?, ?, ?, ?, ?, 'pending', 1,
                          NULL, NULL, ?, NULL, ?)
                """,
                (
                    new_approval.approval_id,
                    new_approval.workflow_run_id,
                    new_approval.node_run_id,
                    new_approval.change_set_id,
                    new_approval.subject_sha256,
                    new_approval.base_commit,
                    new_approval.patch_sha256,
                    new_approval.evidence_sha256,
                    new_approval.effective_risk.value,
                    json.dumps(new_approval.scope, separators=(",", ":"), ensure_ascii=False),
                    utc_now_text(new_approval.expires_at),
                    timestamp,
                ),
            )
            await self._append_approval_event(
                tx,
                approval=new_approval,
                previous=None,
                master_lease=master_lease,
                now=now,
                reason=f"renewed_from:{old_approval_id}",
            )
            row = await tx.fetch_one(
                "SELECT * FROM approvals WHERE id = ?", (new_approval.approval_id,)
            )
        assert row is not None
        return _approval_record(row)

    async def create_privilege_request(
        self,
        *,
        request: PrivilegeRequest,
        master_lease: MasterLease,
        now: datetime | None = None,
    ) -> PrivilegeRequest:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as tx:
            await self._master_leases.assert_valid_in(tx, master_lease, now=now)
            existing = await tx.fetch_one(
                "SELECT * FROM privilege_requests WHERE id = ?", (request.request_id,)
            )
            if existing is not None:
                immutable = {
                    "task_id": request.task_id,
                    "node_run_id": request.node_run_id,
                    "capability": request.requested_capability.value,
                    "action": request.requested_action.value,
                    "resource": request.requested_resource,
                    "effective_risk": request.effective_risk.value,
                }
                if any(str(existing[field]) != expected for field, expected in immutable.items()):
                    raise ConcurrencyConflict("privilege request subject drifted")
                return request.model_copy(
                    update={"status": PrivilegeRequestStatus(str(existing["status"]))}
                )
            await tx.execute(
                """
                INSERT INTO privilege_requests(
                    id, task_id, node_run_id, capability, action, resource,
                    effective_risk, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.task_id,
                    request.node_run_id,
                    request.requested_capability.value,
                    request.requested_action.value,
                    request.requested_resource,
                    request.effective_risk.value,
                    request.status.value,
                    timestamp,
                ),
            )
            lineage = await tx.fetch_one(
                "SELECT workflow_run_id FROM node_runs WHERE id = ?",
                (request.node_run_id,),
            )
            assert lineage is not None
            await self._append_privilege_request_event(
                tx,
                workflow_run_id=str(lineage["workflow_run_id"]),
                node_run_id=request.node_run_id,
                task_id=request.task_id,
                request_id=request.request_id,
                previous=None,
                status=request.status.value,
                master_lease=master_lease,
                now=now,
                reason="created",
            )
        return request

    async def create_grant(
        self,
        *,
        grant: CapabilityGrant,
        workflow_run_id: str,
        source_task_id: str,
        master_lease: MasterLease,
        now: datetime | None = None,
    ) -> CapabilityGrantRecord:
        async with self._database.immediate_transaction() as tx:
            await self._master_leases.assert_valid_in(tx, master_lease, now=now)
            request = await tx.fetch_one(
                """
                SELECT pr.*, source_nr.node_id, source_nr.attempt AS source_attempt,
                       source_nr.workflow_run_id
                FROM privilege_requests pr
                JOIN tasks source_task ON source_task.id = pr.task_id
                JOIN node_runs source_nr ON source_nr.id = pr.node_run_id
                WHERE pr.id = ?
                """,
                (grant.request_id,),
            )
            if request is None:
                raise RecordNotFound(f"privilege request not found: {grant.request_id}")
            if str(request["task_id"]) != source_task_id:
                raise ValueError("grant source task mismatch")
            if str(request["workflow_run_id"]) != workflow_run_id:
                raise ValueError("grant workflow mismatch")
            if (
                str(request["action"]) != grant.action.value
                or str(request["resource"]) != grant.resource
            ):
                raise ValueError("grant action/resource mismatch")
            if str(request["status"]) != PrivilegeRequestStatus.APPROVED.value:
                raise ConcurrencyConflict("privilege request is not approved")
            approval = await tx.fetch_one(
                "SELECT status FROM approvals WHERE privilege_request_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (grant.request_id,),
            )
            if approval is None or str(approval["status"]) != ApprovalStatus.APPROVED.value:
                raise ConcurrencyConflict("grant requires an approved PrivilegeApproval")
            target = await tx.fetch_one(
                """
                SELECT t.id, nr.node_id, nr.attempt, nr.workflow_run_id, nr.id AS node_run_id
                FROM tasks t JOIN node_runs nr ON nr.id = t.node_run_id
                WHERE t.id = ?
                """,
                (grant.target_task_id,),
            )
            if target is None:
                raise RecordNotFound(f"grant target task not found: {grant.target_task_id}")
            if (
                str(target["workflow_run_id"]) != workflow_run_id
                or str(target["node_id"]) != str(request["node_id"])
                or int(target["attempt"]) != int(request["source_attempt"]) + 1
            ):
                raise ValueError("grant target must be same node attempt+1")
            existing = await tx.fetch_one(
                "SELECT * FROM capability_grants WHERE request_id = ?", (grant.request_id,)
            )
            if existing is not None:
                return _grant_record(
                    existing,
                    workflow_run_id=workflow_run_id,
                    node_run_id=str(request["node_run_id"]),
                )
            await tx.execute(
                """
                INSERT INTO capability_grants(
                    id, request_id, target_task_id, action, resource, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    grant.grant_id,
                    grant.request_id,
                    grant.target_task_id,
                    grant.action.value,
                    grant.resource,
                    utc_now_text(grant.expires_at),
                ),
            )
            row = await tx.fetch_one(
                "SELECT * FROM capability_grants WHERE id = ?", (grant.grant_id,)
            )
            assert row is not None
            await self._append_grant_event(
                tx,
                row=row,
                workflow_run_id=workflow_run_id,
                node_run_id=str(request["node_run_id"]),
                master_lease=master_lease,
                now=now,
                reason="created",
            )
        return _grant_record(
            row, workflow_run_id=workflow_run_id, node_run_id=str(request["node_run_id"])
        )

    async def consume_grant(
        self,
        *,
        grant_id: str,
        target_task_id: str,
        action: PrivilegeAction,
        resource: str,
        master_lease: MasterLease,
        workspace_lease: WorkspaceLease | None = None,
        now: datetime | None = None,
    ) -> CapabilityGrantRecord:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as tx:
            await self._master_leases.assert_valid_in(tx, master_lease, now=now)
            row = await tx.fetch_one(
                """
                SELECT cg.*, pr.status AS request_status, pr.node_run_id, pr.task_id,
                       nr.workflow_run_id
                FROM capability_grants cg
                JOIN privilege_requests pr ON pr.id = cg.request_id
                JOIN node_runs nr ON nr.id = pr.node_run_id
                WHERE cg.id = ?
                """,
                (grant_id,),
            )
            if row is None:
                raise RecordNotFound(f"capability grant not found: {grant_id}")
            if (
                str(row["target_task_id"]) != target_task_id
                or str(row["action"]) != action.value
                or str(row["resource"]) != resource
            ):
                raise ValueError("grant target/action/resource mismatch")
            if str(row["request_status"]) != PrivilegeRequestStatus.APPROVED.value:
                raise ConcurrencyConflict("privilege request is no longer approved")
            if workspace_lease is None:
                raise ValueError("workspace lease is required to consume a grant")
            lock = await tx.fetch_one(
                """
                SELECT resource_key, fencing_token
                FROM file_locks
                WHERE resource_key = ? AND fencing_token = ?
                  AND lease_expires_at > ?
                """,
                (workspace_lease.resource_key, workspace_lease.fencing_token, timestamp),
            )
            if lock is None:
                raise ConcurrencyConflict("grant consumption lacks the bound workspace fence")
            changed = await tx.execute(
                """
                UPDATE capability_grants
                SET consumed_at = ?, consumed_fencing_token = ?
                WHERE id = ? AND target_task_id = ? AND action = ? AND resource = ?
                  AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?
                """,
                (
                    timestamp,
                    workspace_lease.fencing_token,
                    grant_id,
                    target_task_id,
                    action.value,
                    resource,
                    timestamp,
                ),
            )
            if changed != 1:
                raise ConcurrencyConflict("grant is expired, revoked, or already consumed")
            changed_request = await tx.execute(
                """
                UPDATE privilege_requests SET status = 'consumed'
                WHERE id = ? AND status = 'approved'
                """,
                (str(row["request_id"]),),
            )
            if changed_request != 1:
                raise ConcurrencyConflict("privilege request consumption lost CAS")
            fresh = await tx.fetch_one(
                """
                SELECT cg.*, pr.status AS request_status, pr.node_run_id,
                       nr.workflow_run_id
                FROM capability_grants cg
                JOIN privilege_requests pr ON pr.id = cg.request_id
                JOIN node_runs nr ON nr.id = pr.node_run_id
                WHERE cg.id = ?
                """,
                (grant_id,),
            )
            assert fresh is not None
            await self._append_grant_event(
                tx,
                row=fresh,
                workflow_run_id=str(fresh["workflow_run_id"]),
                node_run_id=str(fresh["node_run_id"]),
                master_lease=master_lease,
                now=now,
                reason="consumed",
            )
        return _grant_record(
            fresh,
            workflow_run_id=str(fresh["workflow_run_id"]),
            node_run_id=str(fresh["node_run_id"]),
        )

    async def revoke_unconsumed_for_run(
        self,
        *,
        workflow_run_id: str,
        master_lease: MasterLease,
        reason: str,
        now: datetime | None = None,
    ) -> int:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as tx:
            await self._master_leases.assert_valid_in(tx, master_lease, now=now)
            rows = await tx.fetch_all(
                """
                SELECT cg.*, pr.status AS request_status, pr.node_run_id,
                       nr.workflow_run_id
                FROM capability_grants cg
                JOIN privilege_requests pr ON pr.id = cg.request_id
                JOIN node_runs nr ON nr.id = pr.node_run_id
                WHERE nr.workflow_run_id = ?
                  AND cg.consumed_at IS NULL AND cg.revoked_at IS NULL
                """,
                (workflow_run_id,),
            )
            count = 0
            for row in rows:
                changed = await tx.execute(
                    """
                    UPDATE capability_grants
                    SET revoked_at = ?, revocation_reason = ?
                    WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                    """,
                    (timestamp, reason, str(row["id"])),
                )
                if changed == 1:
                    count += 1
                    changed_request = await tx.execute(
                        "UPDATE privilege_requests SET status = 'denied' "
                        "WHERE id = ? AND status = 'approved'",
                        (str(row["request_id"]),),
                    )
                    fresh = await tx.fetch_one(
                        """
                        SELECT cg.*, pr.status AS request_status, pr.node_run_id, pr.task_id,
                               nr.workflow_run_id
                        FROM capability_grants cg
                        JOIN privilege_requests pr ON pr.id = cg.request_id
                        JOIN node_runs nr ON nr.id = pr.node_run_id
                        WHERE cg.id = ?
                        """,
                        (str(row["id"]),),
                    )
                    assert fresh is not None
                    if changed_request == 1:
                        await self._append_privilege_request_event(
                            tx,
                            workflow_run_id=str(fresh["workflow_run_id"]),
                            node_run_id=str(fresh["node_run_id"]),
                            task_id=str(row["task_id"]),
                            request_id=str(row["request_id"]),
                            previous=PrivilegeRequestStatus.APPROVED.value,
                            status=PrivilegeRequestStatus.DENIED.value,
                            master_lease=master_lease,
                            now=now,
                            reason=f"revoked:{reason}",
                        )
                    await self._append_grant_event(
                        tx,
                        row=fresh,
                        workflow_run_id=str(fresh["workflow_run_id"]),
                        node_run_id=str(fresh["node_run_id"]),
                        master_lease=master_lease,
                        now=now,
                        reason=f"revoked:{reason}",
                    )
        return count

    async def invalidate_for_run(
        self,
        *,
        workflow_run_id: str,
        master_lease: MasterLease,
        reason: str = "workflow_cancelled",
        now: datetime | None = None,
    ) -> int:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as tx:
            await self._master_leases.assert_valid_in(tx, master_lease, now=now)
            rows = await tx.fetch_all(
                "SELECT * FROM approvals WHERE workflow_run_id = ? AND status = 'pending'",
                (workflow_run_id,),
            )
            count = 0
            for row in rows:
                current = _approval_record(row)
                changed = await tx.execute(
                    """
                    UPDATE approvals
                    SET status = 'invalidated', version = version + 1, decided_at = ?
                    WHERE id = ? AND status = 'pending' AND version = ?
                    """,
                    (timestamp, current.approval.approval_id, current.approval.version),
                )
                if changed != 1:
                    raise ConcurrencyConflict("approval invalidation lost CAS")
                if isinstance(current.approval, ChangeSetApproval):
                    await tx.execute(
                        """
                        UPDATE change_sets SET status = 'cancelled', updated_at = ?
                        WHERE id = ? AND status = 'pending_approval'
                        """,
                        (timestamp, current.approval.change_set_id),
                    )
                else:
                    changed_request = await tx.execute(
                        """
                        UPDATE privilege_requests SET status = 'denied'
                        WHERE id = ? AND status IN ('pending', 'waiting_approval')
                        """,
                        (current.approval.privilege_request_id,),
                    )
                    if changed_request == 1:
                        request_row = await tx.fetch_one(
                            "SELECT pr.task_id, pr.node_run_id, nr.workflow_run_id "
                            "FROM privilege_requests pr "
                            "JOIN node_runs nr ON nr.id = pr.node_run_id "
                            "WHERE pr.id = ?",
                            (current.approval.privilege_request_id,),
                        )
                        assert request_row is not None
                        await self._append_privilege_request_event(
                            tx,
                            workflow_run_id=str(request_row["workflow_run_id"]),
                            node_run_id=str(request_row["node_run_id"]),
                            task_id=str(request_row["task_id"]),
                            request_id=current.approval.privilege_request_id,
                            previous=PrivilegeRequestStatus.WAITING_APPROVAL.value,
                            status=PrivilegeRequestStatus.DENIED.value,
                            master_lease=master_lease,
                            now=now,
                            reason=reason,
                        )
                fresh = await tx.fetch_one(
                    "SELECT * FROM approvals WHERE id = ?", (current.approval.approval_id,)
                )
                assert fresh is not None
                await self._append_approval_event(
                    tx,
                    approval=_approval_record(fresh).approval,
                    previous=current.approval.status,
                    master_lease=master_lease,
                    now=now,
                    reason=reason,
                )
                count += 1
            await tx.execute(
                """
                UPDATE change_sets SET status = 'cancelled', updated_at = ?
                WHERE status IN ('pending_approval', 'approved')
                  AND task_id IN (
                      SELECT t.id FROM tasks t
                      JOIN node_runs nr ON nr.id = t.node_run_id
                      WHERE nr.workflow_run_id = ?
                  )
                """,
                (timestamp, workflow_run_id),
            )
        return count

    async def _append_privilege_request_event(
        self,
        tx: Transaction,
        *,
        workflow_run_id: str,
        node_run_id: str,
        task_id: str,
        request_id: str,
        previous: str | None,
        status: str,
        master_lease: MasterLease,
        now: datetime | None,
        reason: str | None,
    ) -> None:
        await self._events.append_in(
            tx,
            session_id=await _session_id(tx, workflow_run_id),
            workflow_id=await _workflow_id(tx, workflow_run_id),
            workflow_run_id=workflow_run_id,
            event_type=PRIVILEGE_REQUEST_STATE_CHANGED,
            actor_type=ActorType.MASTER,
            actor_id=master_lease.instance_id,
            payload=PrivilegeRequestEventPayload(
                master_fencing_token=master_lease.fencing_token,
                workflow_run_id=workflow_run_id,
                node_run_id=node_run_id,
                task_id=task_id,
                request_id=request_id,
                previous_status=previous,
                status=status,
                reason=reason,
            ),
            now=now,
        )

    async def _expire_in(
        self,
        tx: Transaction,
        current: ApprovalRecord,
        *,
        master_lease: MasterLease,
        now: datetime | None = None,
    ) -> None:
        timestamp = utc_now_text(now)
        changed = await tx.execute(
            """
            UPDATE approvals
            SET status = 'expired', version = version + 1, decided_at = ?
            WHERE id = ? AND status = 'pending' AND version = ?
            """,
            (timestamp, current.approval.approval_id, current.approval.version),
        )
        if changed != 1:
            raise ConcurrencyConflict("approval expiry lost CAS")
        if isinstance(current.approval, PrivilegeApproval):
            changed_request = await tx.execute(
                """
                UPDATE privilege_requests SET status = 'expired'
                WHERE id = ? AND status IN ('pending', 'waiting_approval')
                """,
                (current.approval.privilege_request_id,),
            )
            if changed_request == 1:
                request_row = await tx.fetch_one(
                    "SELECT pr.task_id, pr.node_run_id, nr.workflow_run_id "
                    "FROM privilege_requests pr JOIN node_runs nr ON nr.id = pr.node_run_id "
                    "WHERE pr.id = ?",
                    (current.approval.privilege_request_id,),
                )
                assert request_row is not None
                await self._append_privilege_request_event(
                    tx,
                    workflow_run_id=str(request_row["workflow_run_id"]),
                    node_run_id=str(request_row["node_run_id"]),
                    task_id=str(request_row["task_id"]),
                    request_id=current.approval.privilege_request_id,
                    previous=PrivilegeRequestStatus.WAITING_APPROVAL.value,
                    status=PrivilegeRequestStatus.EXPIRED.value,
                    master_lease=master_lease,
                    now=now,
                    reason="expired",
                )
        fresh = await tx.fetch_one(
            "SELECT * FROM approvals WHERE id = ?", (current.approval.approval_id,)
        )
        assert fresh is not None
        await self._append_approval_event(
            tx,
            approval=_approval_record(fresh).approval,
            previous=current.approval.status,
            master_lease=master_lease,
            now=now,
            reason="expired",
        )

    async def _append_approval_event(
        self,
        tx: Transaction,
        *,
        approval: Approval,
        previous: ApprovalStatus | None,
        master_lease: MasterLease,
        now: datetime | None,
        reason: str | None = None,
    ) -> None:
        await self._events.append_in(
            tx,
            session_id=await _session_id(tx, approval.workflow_run_id),
            workflow_id=await _workflow_id(tx, approval.workflow_run_id),
            workflow_run_id=approval.workflow_run_id,
            event_type=APPROVAL_STATE_CHANGED,
            actor_type=ActorType.MASTER,
            actor_id=master_lease.instance_id,
            payload=ApprovalEventPayload(
                master_fencing_token=master_lease.fencing_token,
                workflow_run_id=approval.workflow_run_id,
                node_run_id=approval.node_run_id,
                approval_id=approval.approval_id,
                subject_type=approval.subject_type,
                previous_status=previous,
                status=approval.status,
                version=approval.version,
                subject_sha256=approval.subject_sha256,
                reason=reason,
            ),
            now=now,
        )

    async def _append_grant_event(
        self,
        tx: Transaction,
        *,
        row: aiosqlite.Row,
        workflow_run_id: str,
        node_run_id: str,
        master_lease: MasterLease,
        now: datetime | None,
        reason: str,
    ) -> None:
        await self._events.append_in(
            tx,
            session_id=await _session_id(tx, workflow_run_id),
            workflow_id=await _workflow_id(tx, workflow_run_id),
            workflow_run_id=workflow_run_id,
            event_type=CAPABILITY_GRANT_STATE_CHANGED,
            actor_type=ActorType.MASTER,
            actor_id=master_lease.instance_id,
            payload=CapabilityGrantEventPayload(
                master_fencing_token=master_lease.fencing_token,
                workflow_run_id=workflow_run_id,
                node_run_id=node_run_id,
                request_id=str(row["request_id"]),
                grant_id=str(row["id"]),
                target_task_id=str(row["target_task_id"]),
                action=str(row["action"]),
                resource=str(row["resource"]),
                consumed_fencing_token=(
                    int(row["consumed_fencing_token"])
                    if row["consumed_fencing_token"] is not None
                    else None
                ),
                reason=reason,
            ),
            now=now,
        )


__all__ = [
    "ApprovalRecord",
    "ApprovalRepository",
    "CapabilityGrantRecord",
]
