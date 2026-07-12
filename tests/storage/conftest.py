from __future__ import annotations

from pathlib import Path

import pytest_asyncio

from storage.db import Database


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> Database:
    value = Database(tmp_path / "data" / "agent-hub.db")
    await value.initialize()
    return value
