"""FastAPI application factory."""

from fastapi import FastAPI

from app import __version__
from app.api import router
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    application = FastAPI(
        title="Agent Hub API",
        version=__version__,
        description="Local single-Master coding-agent orchestration API",
    )
    application.state.settings = resolved_settings
    application.include_router(router)
    return application


app = create_app()
