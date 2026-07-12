"""Persistent singleton and workspace leases with fencing tokens."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from aiosqlite import Row
from pydantic import TypeAdapter

from protocol import EntityId
from storage.db import Database, Transaction, normalize_utc, utc_now_text
from storage.errors import LeaseLost, LeaseUnavailable

_ENTITY_ID = TypeAdapter(EntityId)


@dataclass(frozen=True, slots=True)
class MasterLease:
    lease_key: str
    instance_id: str
    process_id: int
    fencing_token: int
    heartbeat_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    resource_key: str
    owner_kind: str
    owner_operation_id: str
    owner_process_id: int
    fencing_token: int
    acquired_at: str
    heartbeat_at: str
    expires_at: str


async def _one(
    transaction: Transaction,
    query: str,
    parameters: tuple[object, ...],
) -> Row | None:
    return await transaction.fetch_one(query, parameters)


def _owner_id(value: str) -> str:
    return _ENTITY_ID.validate_python(value)


def _process_id(value: int) -> int:
    if value < 1:
        raise ValueError("process_id must be positive")
    return value


def _ttl(value: int) -> int:
    if value < 1:
        raise ValueError("ttl_seconds must be positive")
    return value


class MasterLeaseRepository:
    def __init__(self, database: Database, *, lease_key: str = "scheduler") -> None:
        if not lease_key or len(lease_key) > 128:
            raise ValueError("lease_key must contain 1..128 characters")
        self._database = database
        self._lease_key = lease_key

    async def acquire(
        self,
        *,
        instance_id: str,
        process_id: int,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> MasterLease:
        owner = _owner_id(instance_id)
        pid = _process_id(process_id)
        resolved_now = normalize_utc(now)
        now_text = utc_now_text(resolved_now)
        expires = utc_now_text(resolved_now + timedelta(seconds=_ttl(ttl_seconds)))

        async with self._database.immediate_transaction() as connection:
            row = await _one(
                connection, "SELECT * FROM master_leases WHERE lease_key = ?", (self._lease_key,)
            )
            if row is None:
                token = 1
                await connection.execute(
                    """
                    INSERT INTO master_leases(
                        lease_key, instance_id, process_id, fencing_token,
                        heartbeat_at, lease_expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (self._lease_key, owner, pid, token, now_text, expires),
                )
            elif str(row["lease_expires_at"]) > now_text:
                if row["instance_id"] != owner or int(row["process_id"]) != pid:
                    raise LeaseUnavailable(
                        f"master lease {self._lease_key} is held by another live instance"
                    )
                token = int(row["fencing_token"])
                await connection.execute(
                    """
                    UPDATE master_leases
                    SET heartbeat_at = ?, lease_expires_at = ?
                    WHERE lease_key = ? AND instance_id = ? AND process_id = ?
                      AND fencing_token = ? AND lease_expires_at > ?
                    """,
                    (now_text, expires, self._lease_key, owner, pid, token, now_text),
                )
            else:
                token = int(row["fencing_token"]) + 1
                await connection.execute(
                    """
                    UPDATE master_leases
                    SET instance_id = ?, process_id = ?, fencing_token = ?,
                        heartbeat_at = ?, lease_expires_at = ?
                    WHERE lease_key = ? AND fencing_token = ? AND lease_expires_at <= ?
                    """,
                    (
                        owner,
                        pid,
                        token,
                        now_text,
                        expires,
                        self._lease_key,
                        int(row["fencing_token"]),
                        now_text,
                    ),
                )
            current = await _one(
                connection, "SELECT * FROM master_leases WHERE lease_key = ?", (self._lease_key,)
            )
        assert current is not None
        return self._master(current)

    async def heartbeat(
        self,
        lease: MasterLease,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> MasterLease:
        resolved_now = normalize_utc(now)
        now_text = utc_now_text(resolved_now)
        expires = utc_now_text(resolved_now + timedelta(seconds=_ttl(ttl_seconds)))
        async with self._database.immediate_transaction() as connection:
            changed = await connection.execute(
                """
                UPDATE master_leases
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE lease_key = ? AND instance_id = ? AND process_id = ?
                  AND fencing_token = ? AND lease_expires_at > ?
                """,
                (
                    now_text,
                    expires,
                    lease.lease_key,
                    lease.instance_id,
                    lease.process_id,
                    lease.fencing_token,
                    now_text,
                ),
            )
            if changed != 1:
                raise LeaseLost("master lease heartbeat rejected by fencing check")
            row = await _one(
                connection, "SELECT * FROM master_leases WHERE lease_key = ?", (lease.lease_key,)
            )
        assert row is not None
        return self._master(row)

    async def release(self, lease: MasterLease, *, now: datetime | None = None) -> None:
        now_text = utc_now_text(now)
        async with self._database.immediate_transaction() as connection:
            changed = await connection.execute(
                """
                UPDATE master_leases
                SET heartbeat_at = ?, lease_expires_at = ?
                WHERE lease_key = ? AND instance_id = ? AND process_id = ?
                  AND fencing_token = ? AND lease_expires_at > ?
                """,
                (
                    now_text,
                    now_text,
                    lease.lease_key,
                    lease.instance_id,
                    lease.process_id,
                    lease.fencing_token,
                    now_text,
                ),
            )
            if changed != 1:
                raise LeaseLost("master lease release rejected by fencing check")

    async def assert_valid(self, lease: MasterLease, *, now: datetime | None = None) -> None:
        async with self._database.connection() as connection:
            await self.assert_valid_in(Transaction(connection), lease, now=now)

    async def assert_valid_in(
        self,
        transaction: Transaction,
        lease: MasterLease,
        *,
        now: datetime | None = None,
    ) -> None:
        """Fence a Master-owned mutation inside the mutation's transaction."""

        now_text = utc_now_text(now)
        row = await _one(
            transaction,
            """
            SELECT 1 FROM master_leases
            WHERE lease_key = ? AND instance_id = ? AND process_id = ?
              AND fencing_token = ? AND lease_expires_at > ?
            """,
            (
                lease.lease_key,
                lease.instance_id,
                lease.process_id,
                lease.fencing_token,
                now_text,
            ),
        )
        if row is None:
            raise LeaseLost("master lease is no longer valid")

    @staticmethod
    def _master(row: Row) -> MasterLease:
        return MasterLease(
            lease_key=str(row["lease_key"]),
            instance_id=str(row["instance_id"]),
            process_id=int(row["process_id"]),
            fencing_token=int(row["fencing_token"]),
            heartbeat_at=str(row["heartbeat_at"]),
            expires_at=str(row["lease_expires_at"]),
        )


class WorkspaceLeaseRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def acquire(
        self,
        *,
        resource_key: str,
        owner_kind: str,
        owner_operation_id: str,
        owner_process_id: int,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> WorkspaceLease:
        if not resource_key or len(resource_key) > 1024:
            raise ValueError("resource_key must contain 1..1024 characters")
        if not owner_kind or len(owner_kind) > 64:
            raise ValueError("owner_kind must contain 1..64 characters")
        operation_id = _owner_id(owner_operation_id)
        pid = _process_id(owner_process_id)
        resolved_now = normalize_utc(now)
        now_text = utc_now_text(resolved_now)
        expires = utc_now_text(resolved_now + timedelta(seconds=_ttl(ttl_seconds)))

        async with self._database.immediate_transaction() as connection:
            row = await _one(
                connection, "SELECT * FROM file_locks WHERE resource_key = ?", (resource_key,)
            )
            active = (
                row is not None
                and row["released_at"] is None
                and str(row["lease_expires_at"]) > now_text
            )
            if row is None:
                token = 1
                await connection.execute(
                    """
                    INSERT INTO file_locks(
                        resource_key, owner_kind, owner_operation_id, owner_process_id,
                        fencing_token, acquired_at, heartbeat_at, lease_expires_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        resource_key,
                        owner_kind,
                        operation_id,
                        pid,
                        token,
                        now_text,
                        now_text,
                        expires,
                    ),
                )
            elif active:
                same_owner = (
                    row["owner_kind"] == owner_kind
                    and row["owner_operation_id"] == operation_id
                    and int(row["owner_process_id"]) == pid
                )
                if not same_owner:
                    raise LeaseUnavailable(f"workspace resource is already leased: {resource_key}")
                token = int(row["fencing_token"])
                await connection.execute(
                    """
                    UPDATE file_locks SET heartbeat_at = ?, lease_expires_at = ?
                    WHERE resource_key = ? AND owner_kind = ? AND owner_operation_id = ?
                      AND owner_process_id = ? AND fencing_token = ?
                      AND released_at IS NULL AND lease_expires_at > ?
                    """,
                    (
                        now_text,
                        expires,
                        resource_key,
                        owner_kind,
                        operation_id,
                        pid,
                        token,
                        now_text,
                    ),
                )
            else:
                token = int(row["fencing_token"]) + 1
                await connection.execute(
                    """
                    UPDATE file_locks
                    SET owner_kind = ?, owner_operation_id = ?, owner_process_id = ?,
                        fencing_token = ?, acquired_at = ?, heartbeat_at = ?,
                        lease_expires_at = ?, released_at = NULL
                    WHERE resource_key = ? AND fencing_token = ?
                      AND (released_at IS NOT NULL OR lease_expires_at <= ?)
                    """,
                    (
                        owner_kind,
                        operation_id,
                        pid,
                        token,
                        now_text,
                        now_text,
                        expires,
                        resource_key,
                        int(row["fencing_token"]),
                        now_text,
                    ),
                )
            current = await _one(
                connection, "SELECT * FROM file_locks WHERE resource_key = ?", (resource_key,)
            )
        assert current is not None
        return self._workspace(current)

    async def heartbeat(
        self,
        lease: WorkspaceLease,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> WorkspaceLease:
        resolved_now = normalize_utc(now)
        now_text = utc_now_text(resolved_now)
        expires = utc_now_text(resolved_now + timedelta(seconds=_ttl(ttl_seconds)))
        async with self._database.immediate_transaction() as connection:
            changed = await connection.execute(
                """
                UPDATE file_locks SET heartbeat_at = ?, lease_expires_at = ?
                WHERE resource_key = ? AND owner_kind = ? AND owner_operation_id = ?
                  AND owner_process_id = ? AND fencing_token = ?
                  AND released_at IS NULL AND lease_expires_at > ?
                """,
                (
                    now_text,
                    expires,
                    lease.resource_key,
                    lease.owner_kind,
                    lease.owner_operation_id,
                    lease.owner_process_id,
                    lease.fencing_token,
                    now_text,
                ),
            )
            if changed != 1:
                raise LeaseLost("workspace lease heartbeat rejected by fencing check")
            row = await _one(
                connection,
                "SELECT * FROM file_locks WHERE resource_key = ?",
                (lease.resource_key,),
            )
        assert row is not None
        return self._workspace(row)

    async def release(self, lease: WorkspaceLease, *, now: datetime | None = None) -> None:
        now_text = utc_now_text(now)
        async with self._database.immediate_transaction() as connection:
            changed = await connection.execute(
                """
                UPDATE file_locks
                SET heartbeat_at = ?, lease_expires_at = ?, released_at = ?
                WHERE resource_key = ? AND owner_kind = ? AND owner_operation_id = ?
                  AND owner_process_id = ? AND fencing_token = ?
                  AND released_at IS NULL AND lease_expires_at > ?
                """,
                (
                    now_text,
                    now_text,
                    now_text,
                    lease.resource_key,
                    lease.owner_kind,
                    lease.owner_operation_id,
                    lease.owner_process_id,
                    lease.fencing_token,
                    now_text,
                ),
            )
            if changed != 1:
                raise LeaseLost("workspace lease release rejected by fencing check")

    async def assert_valid(self, lease: WorkspaceLease, *, now: datetime | None = None) -> None:
        async with self._database.connection() as connection:
            await self.assert_valid_in(Transaction(connection), lease, now=now)

    async def assert_valid_in(
        self,
        transaction: Transaction,
        lease: WorkspaceLease,
        *,
        now: datetime | None = None,
    ) -> None:
        """Fence a workspace mutation inside the mutation's transaction."""

        now_text = utc_now_text(now)
        row = await _one(
            transaction,
            """
            SELECT 1 FROM file_locks
            WHERE resource_key = ? AND owner_kind = ? AND owner_operation_id = ?
              AND owner_process_id = ? AND fencing_token = ?
              AND released_at IS NULL AND lease_expires_at > ?
            """,
            (
                lease.resource_key,
                lease.owner_kind,
                lease.owner_operation_id,
                lease.owner_process_id,
                lease.fencing_token,
                now_text,
            ),
        )
        if row is None:
            raise LeaseLost("workspace lease is no longer valid")

    @staticmethod
    def _workspace(row: Row) -> WorkspaceLease:
        return WorkspaceLease(
            resource_key=str(row["resource_key"]),
            owner_kind=str(row["owner_kind"]),
            owner_operation_id=str(row["owner_operation_id"]),
            owner_process_id=int(row["owner_process_id"]),
            fencing_token=int(row["fencing_token"]),
            acquired_at=str(row["acquired_at"]),
            heartbeat_at=str(row["heartbeat_at"]),
            expires_at=str(row["lease_expires_at"]),
        )
