"""SQLite connection policy, migration and transaction boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from storage.errors import UnsupportedSchemaVersion

SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000
_MIGRATION_PATH = Path(__file__).resolve().parent.parent / "migrations" / "init.sql"


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.path, isolation_level=None)
        connection.row_factory = aiosqlite.Row
        for statement in (
            "PRAGMA foreign_keys=ON",
            f"PRAGMA busy_timeout={self.busy_timeout_ms}",
            "PRAGMA journal_mode=WAL",
        ):
            cursor = await connection.execute(statement)
            await cursor.fetchone()
            await cursor.close()
        return connection

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await self.connect()
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
        migration_sql = _MIGRATION_PATH.read_text(encoding="utf-8")
        async with self.connection() as connection:
            existing_version = await self._schema_version(connection)
            if existing_version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    f"database schema {existing_version} is newer than supported version "
                    f"{SCHEMA_VERSION}"
                )
            try:
                cursor = await connection.executescript(migration_sql)
                await cursor.close()
            except BaseException:
                await connection.rollback()
                raise

            cursor = await connection.execute("SELECT MAX(version) FROM schema_migrations")
            row = await cursor.fetchone()
            await cursor.close()

        version = int(row[0]) if row and row[0] is not None else 0
        if version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"database schema {version} is newer than supported version {SCHEMA_VERSION}"
            )
        if version != SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                f"database schema {version} does not match required version {SCHEMA_VERSION}"
            )
        return version

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
