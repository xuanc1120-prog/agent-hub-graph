"""Atomic persistent idempotency for retryable application mutations."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import TypeAdapter

from protocol import Sha256Hex
from storage.db import Database, Transaction, normalize_utc, utc_now_text
from storage.errors import IdempotencyConflict

_SHA256 = TypeAdapter(Sha256Hex)


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status_code: int
    response_json: str

    def __post_init__(self) -> None:
        if not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be in 100..599")
        parsed = json.loads(self.response_json)
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        object.__setattr__(self, "response_json", canonical)


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    response: StoredResponse
    replayed: bool


MutationContext = Transaction


Mutation = Callable[[MutationContext], Awaitable[StoredResponse]]


class IdempotencyRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def execute(
        self,
        *,
        actor_scope: str,
        operation_scope: str,
        idempotency_key: str,
        request_sha256: str,
        mutation: Mutation,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> IdempotencyResult:
        self._validate_text("actor_scope", actor_scope, 256)
        self._validate_text("operation_scope", operation_scope, 512)
        self._validate_text("idempotency_key", idempotency_key, 256)
        resolved_hash = _SHA256.validate_python(request_sha256)
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        resolved_now = normalize_utc(now)
        created_at = utc_now_text(resolved_now)
        expires_at = utc_now_text(resolved_now + timedelta(seconds=ttl_seconds))

        async with self._database.immediate_transaction() as transaction:
            row = await transaction.fetch_one(
                """
                SELECT request_sha256, response_status, response_json, expires_at
                FROM idempotency_keys
                WHERE actor_scope = ? AND operation_scope = ? AND idempotency_key = ?
                """,
                (actor_scope, operation_scope, idempotency_key),
            )
            if row is not None and str(row["expires_at"]) > created_at:
                if row["request_sha256"] != resolved_hash:
                    raise IdempotencyConflict(
                        "idempotency key already belongs to a different request"
                    )
                return IdempotencyResult(
                    response=StoredResponse(
                        status_code=int(row["response_status"]),
                        response_json=str(row["response_json"]),
                    ),
                    replayed=True,
                )

            if row is not None:
                await transaction.execute(
                    """
                    DELETE FROM idempotency_keys
                    WHERE actor_scope = ? AND operation_scope = ? AND idempotency_key = ?
                    """,
                    (actor_scope, operation_scope, idempotency_key),
                )

            response = await mutation(transaction)
            if not isinstance(response, StoredResponse):
                raise TypeError("mutation must return StoredResponse")
            await transaction.execute(
                """
                INSERT INTO idempotency_keys(
                    actor_scope, operation_scope, idempotency_key, request_sha256,
                    response_status, response_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_scope,
                    operation_scope,
                    idempotency_key,
                    resolved_hash,
                    response.status_code,
                    response.response_json,
                    created_at,
                    expires_at,
                ),
            )
            return IdempotencyResult(response=response, replayed=False)

    async def delete_expired(self, *, now: datetime | None = None) -> int:
        timestamp = utc_now_text(now)
        async with self._database.immediate_transaction() as transaction:
            deleted = await transaction.execute(
                "DELETE FROM idempotency_keys WHERE expires_at <= ?", (timestamp,)
            )
        return deleted

    @staticmethod
    def _validate_text(name: str, value: str, max_length: int) -> None:
        if not value or len(value) > max_length:
            raise ValueError(f"{name} must contain 1..{max_length} characters")
