"""Persistent agent catalog snapshots used by deterministic compilation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from pydantic import TypeAdapter

from master.router import AgentCapability, AgentCatalog, AgentSpec
from protocol import EntityId
from storage.db import Database, Transaction, utc_now_text
from storage.errors import ConcurrencyConflict, RecordNotFound

_ENTITY_ID = TypeAdapter(EntityId)


@dataclass(frozen=True, slots=True)
class AgentRegistration:
    agent_id: str
    display_name: str
    adapter_type: str
    capabilities: frozenset[AgentCapability]
    enabled: bool = True
    available: bool = True
    auto_assignable: bool = False
    unavailable_reason: str | None = None


class AgentRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def register(
        self,
        registration: AgentRegistration,
        *,
        now: datetime | None = None,
    ) -> AgentSpec:
        spec = _to_spec(registration)
        capabilities_json = json.dumps(
            sorted(capability.value for capability in registration.capabilities),
            separators=(",", ":"),
        )
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            existing = await transaction.fetch_one(
                "SELECT * FROM agents WHERE id = ?",
                (spec.agent_id,),
            )
            if existing is None:
                await transaction.execute(
                    """
                    INSERT INTO agents(
                        id, display_name, adapter_type, enabled, available,
                        auto_assignable, unavailable_reason, capabilities_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec.agent_id,
                        spec.display_name,
                        spec.adapter_type,
                        1 if spec.enabled else 0,
                        1 if spec.available else 0,
                        1 if spec.auto_assignable else 0,
                        spec.unavailable_reason,
                        capabilities_json,
                        timestamp,
                    ),
                )
            else:
                persisted = _row_to_registration(existing)
                if persisted != registration:
                    raise ConcurrencyConflict(
                        f"agent {spec.agent_id} is already registered with different settings"
                    )
        return spec

    async def get(self, agent_id: str) -> AgentSpec:
        resolved_id = _ENTITY_ID.validate_python(agent_id)
        async with self._database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM agents WHERE id = ?", (resolved_id,))
            row = await cursor.fetchone()
            await cursor.close()
        if row is None:
            raise RecordNotFound(f"agent not found: {resolved_id}")
        return _to_spec(_row_to_registration(row))

    async def catalog(self) -> AgentCatalog:
        async with self._database.connection() as connection:
            cursor = await connection.execute("SELECT * FROM agents ORDER BY id")
            rows = await cursor.fetchall()
            await cursor.close()
        return _catalog_from_rows(rows)

    async def catalog_in(self, transaction: Transaction) -> AgentCatalog:
        """Load the authoritative catalog inside a caller-owned transaction."""

        rows = await transaction.fetch_all("SELECT * FROM agents ORDER BY id")
        return _catalog_from_rows(rows)

    async def register_mock(self, *, now: datetime | None = None) -> AgentSpec:
        return await self.register(mock_agent_registration(), now=now)


def _catalog_from_rows(rows: list[object]) -> AgentCatalog:
    specs = tuple(_to_spec(_row_to_registration(row)) for row in rows)
    return AgentCatalog(agents=specs, catalog_hash=compute_agent_catalog_hash(specs))


def mock_agent_registration() -> AgentRegistration:
    return AgentRegistration(
        agent_id="mock",
        display_name="Mock Agent",
        adapter_type="mock",
        capabilities=frozenset(
            {
                AgentCapability.READ_CODE,
                AgentCapability.ANALYZE,
                AgentCapability.IMPLEMENT,
                AgentCapability.REVIEW,
                AgentCapability.DOCS,
                AgentCapability.GENERATE_PATCH,
                AgentCapability.RUN_TESTS,
                AgentCapability.WRITE_FILES,
            }
        ),
        enabled=True,
        available=True,
        auto_assignable=False,
    )


def _row_to_registration(row: object) -> AgentRegistration:
    capabilities = json.loads(str(row["capabilities_json"]))  # type: ignore[index]
    return AgentRegistration(
        agent_id=str(row["id"]),  # type: ignore[index]
        display_name=str(row["display_name"]),  # type: ignore[index]
        adapter_type=str(row["adapter_type"]),  # type: ignore[index]
        capabilities=frozenset(AgentCapability(value) for value in capabilities),
        enabled=bool(row["enabled"]),  # type: ignore[index]
        available=bool(row["available"]),  # type: ignore[index]
        auto_assignable=bool(row["auto_assignable"]),  # type: ignore[index]
        unavailable_reason=(
            str(row["unavailable_reason"])  # type: ignore[index]
            if row["unavailable_reason"] is not None  # type: ignore[index]
            else None
        ),
    )


def _to_spec(registration: AgentRegistration) -> AgentSpec:
    payload = {
        "agent_id": _ENTITY_ID.validate_python(registration.agent_id),
        "display_name": registration.display_name,
        "adapter_type": registration.adapter_type,
        "capabilities": sorted(item.value for item in registration.capabilities),
        "enabled": registration.enabled,
        "available": registration.available,
        "auto_assignable": registration.auto_assignable,
        "unavailable_reason": registration.unavailable_reason,
    }
    digest = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AgentSpec(
        agent_id=payload["agent_id"],
        display_name=registration.display_name,
        adapter_type=registration.adapter_type,
        capabilities=registration.capabilities,
        enabled=registration.enabled,
        available=registration.available,
        auto_assignable=registration.auto_assignable,
        unavailable_reason=registration.unavailable_reason,
        spec_sha256=digest,
    )


def compute_agent_catalog_hash(specs: tuple[AgentSpec, ...]) -> str:
    payload: list[dict[str, object]] = []
    for spec in sorted(specs, key=lambda item: item.agent_id):
        value = spec.model_dump(mode="json")
        value["capabilities"] = sorted(capability.value for capability in spec.capabilities)
        payload.append(value)
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "AgentRegistration",
    "AgentRepository",
    "compute_agent_catalog_hash",
    "mock_agent_registration",
]
