from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


@pytest.mark.asyncio
async def test_health_endpoint_reports_single_master_mode(tmp_path: Path) -> None:
    application = create_app(Settings(data_dir=tmp_path, api_port=9876))

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.1.0",
        "runtime_mode": "single-master@127.0.0.1:9876",
    }
