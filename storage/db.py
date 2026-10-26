"""SQLite connection policy, migration and transaction boundaries."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from storage.errors import UnsupportedSchemaVersion

SCHEMA_VERSION = 4
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_MIGRATION_PATH = Path(__file__).resolve().parent.parent / "migrations" / "init.sql"
_MIGRATION_V2_PATH = (
    Path(__file__).resolve().parent.parent / "migrations" / "0002_agent_routing_state.sql"
)
_MIGRATION_V3_PATH = (
    Path(__file__).resolve().parent.parent / "migrations" / "0003_capability_resource_seals.sql"
)
_MIGRATION_V4_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "0004_approval_idempotency_request_hash.sql"
)
_MIGRATION_CONTROL = frozenset({"BEGIN", "COMMIT", "ROLLBACK", "PRAGMA"})


def _migration_statements(script: str) -> Iterator[str]:
    """Yield complete SQL statements while the caller owns the transaction."""

    pending: list[str] = []
    for line in script.splitlines(keepends=True):
        pending.append(line)
        candidate = "".join(pending)
        if not sqlite3.complete_statement(candidate):
            continue
        statement = candidate.strip()
        pending.clear()
        meaningful = "\n".join(
            item for item in statement.splitlines() if not item.lstrip().startswith("--")
        ).lstrip()
        if not meaningful:
            continue
        keyword = meaningful.split(None, 1)[0].rstrip(";").upper()
        if keyword not in _MIGRATION_CONTROL:
            yield statement
    if "".join(pending).strip():
        raise ValueError("migration script ends with an incomplete SQL statement")


def normalize_utc(value: datetime | None = None) -> datetime:
    """Return an aware UTC datetime and reject ambiguous local timestamps."""

    resolved = value or datetime.now(UTC)
    if resolved.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return resolved.astimezone(UTC)


def utc_now_text(value: datetime | None = None) -> str:
    """Return fixed-width UTC text whose lexical order matches time order."""

    return normalize_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Transaction:
    """Restricted SQL transaction surface with no commit/rollback methods."""

    __slots__ = ("__connection",)

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self.__connection = connection

    async def execute(self, query: str, parameters: tuple[object, ...] = ()) -> int:
        cursor = await self.__connection.execute(query, parameters)
        changed = cursor.rowcount
        await cursor.close()
        return changed

    async def fetch_one(
        self, query: str, parameters: tuple[object, ...] = ()
    ) -> aiosqlite.Row | None:
        cursor = await self.__connection.execute(query, parameters)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def fetch_all(
        self, query: str, parameters: tuple[object, ...] = ()
    ) -> list[aiosqlite.Row]:
        cursor = await self.__connection.execute(query, parameters)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows


class Database:
    """Creates consistently configured short-lived SQLite connections."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        if busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = path.expanduser().resolve(strict=False)
        self.busy_timeout_ms = busy_timeout_ms

    async def connect(self) -> aiosqlite.Connection:
        return await self._connect(configure_journal_mode=True)

    async def _connect(
        self,
        *,
        configure_journal_mode: bool,
    ) -> aiosqlite.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path, isolation_level=None)
        connection.row_factory = aiosqlite.Row
        statements = [
            "PRAGMA foreign_keys=ON",
            f"PRAGMA busy_timeout={self.busy_timeout_ms}",
        ]
        try:
            for statement in statements:
                cursor = await connection.execute(statement)
                try:
                    await cursor.fetchone()
                finally:
                    await cursor.close()
            if configure_journal_mode:
                await self._enable_wal(connection)
        except BaseException:
            await connection.close()
            raise
        return connection

    async def _enable_wal(self, connection: aiosqlite.Connection) -> None:
        # WAL selection must run outside the migration transaction. A second
        # initializer can acquire BEGIN IMMEDIATE after our commit, and SQLite
        # may return SQLITE_BUSY here without invoking its busy handler.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.busy_timeout_ms / 1_000
        while True:
            try:
                cursor = await connection.execute("PRAGMA journal_mode=WAL")
                try:
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
            except sqlite3.OperationalError as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
                remaining = deadline - loop.time()
                if code != sqlite3.SQLITE_BUSY or remaining <= 0:
                    raise
                await asyncio.sleep(min(0.01, remaining))
                continue
            if row is None or str(row[0]).lower() != "wal":
                raise sqlite3.OperationalError("SQLite did not enable WAL mode")
            return

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await self.connect()
        try:
            yield connection
        finally:
            await connection.close()

    @asynccontextmanager
    async def _migration_connection(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await self._connect(configure_journal_mode=False)
        try:
            yield connection
        finally:
            await connection.close()

    @asynccontextmanager
    async def immediate_transaction(self) -> AsyncIterator[Transaction]:
        """Own one BEGIN IMMEDIATE transaction and always close its connection."""

        async with self.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield Transaction(connection)
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()

    async def initialize(self) -> int:
        migration_v1 = _MIGRATION_PATH.read_text(encoding="utf-8")
        migration_v2 = _MIGRATION_V2_PATH.read_text(encoding="utf-8")
        migration_v3 = _MIGRATION_V3_PATH.read_text(encoding="utf-8")
        migration_v4 = _MIGRATION_V4_PATH.read_text(encoding="utf-8")
        async with self._migration_connection() as connection:
            cursor = await connection.execute("BEGIN IMMEDIATE")
            await cursor.close()
            try:
                version = await self._schema_version(connection)
                if version > SCHEMA_VERSION:
                    raise UnsupportedSchemaVersion(
                        f"database schema {version} is newer than supported version "
                        f"{SCHEMA_VERSION}"
                    )
                if version < 1:
                    await self._execute_migration(connection, migration_v1)
                    version = await self._schema_version(connection)
                    if version != 1:
                        raise UnsupportedSchemaVersion(
                            f"v1 migration recorded unexpected schema version {version}"
                        )
                if version < 2:
                    await self._execute_migration(connection, migration_v2)
                    version = await self._schema_version(connection)
                    if version != 2:
                        raise UnsupportedSchemaVersion(
                            f"v2 migration recorded unexpected schema version {version}"
                        )
                if version < 3:
                    await self._execute_migration(connection, migration_v3)
                    version = await self._schema_version(connection)
                    if version != 3:
                        raise UnsupportedSchemaVersion(
                            f"v3 migration recorded unexpected schema version {version}"
                        )
                if version < 4:
                    await self._execute_migration(connection, migration_v4)
                    version = await self._schema_version(connection)
                    if version != 4:
                        raise UnsupportedSchemaVersion(
                            f"v4 migration recorded unexpected schema version {version}"
                        )
                if version != SCHEMA_VERSION:
                    raise UnsupportedSchemaVersion(
                        f"database schema {version} does not match required version "
                        f"{SCHEMA_VERSION}"
                    )
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()
                await self._enable_wal(connection)
        return version

    @staticmethod
    async def _execute_migration(
        connection: aiosqlite.Connection,
        script: str,
    ) -> None:
        for statement in _migration_statements(script):
            cursor = await connection.execute(statement)
            await cursor.close()

    @staticmethod
    async def _schema_version(connection: aiosqlite.Connection) -> int:
        cursor = await connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        )
        exists = await cursor.fetchone()
        await cursor.close()
        if exists is None:
            return 0
        cursor = await connection.execute("SELECT MAX(version) FROM schema_migrations")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row and row[0] is not None else 0
