"""Infrastructure-only API routes for the project baseline."""

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from app import __version__
from app.config import Settings

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
