"""FastAPI application factory with local API security boundaries."""

import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from app import __version__
from app.api import fallback_router, protected_router, router
from app.config import Settings, get_settings
from storage.errors import (
    ArtifactNotFound,
    ConcurrencyConflict,
    ContainmentViolation,
    IdempotencyConflict,
    LeaseLost,
    LeaseUnavailable,
    PathEscapeError,
    QuotaExceeded,
    RecordNotFound,
    StorageError,
)
from web.backend.security import ApiBoundaryMiddleware


def create_app(
    settings: Settings | None = None,
    *,
    workflow_application: object | None = None,
    master_lease: object | None = None,
    api_token: str | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    application = FastAPI(
        title="Agent Hub API",
        version=__version__,
        description="Local single-Master coding-agent orchestration API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.settings = resolved_settings
    application.state.workflow_application = workflow_application
    application.state.master_lease = master_lease
    application.state.api_token = api_token or secrets.token_urlsafe(32)
    application.include_router(router)
    application.include_router(protected_router)
    application.include_router(fallback_router)
    frontend_dist = Path(__file__).resolve().parents[1] / "web" / "frontend" / "dist"
    if frontend_dist.is_dir() and not frontend_dist.is_symlink():
        application.mount(
            "/",
            StaticFiles(directory=str(frontend_dist), html=True, check_dir=True),
            name="frontend",
        )
    application.add_middleware(
        ApiBoundaryMiddleware,
        host=resolved_settings.api_host,
        port=resolved_settings.api_port,
        max_body_bytes=resolved_settings.max_http_body_bytes,
    )

    @application.exception_handler(RequestValidationError)
    async def invalid_request_handler(
        _request: Request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        return _error_response(422, "invalid_request", "The request is invalid.")

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        _request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        detail = error.detail
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            code = detail["code"]
            message = detail.get("message")
            if not isinstance(message, str):
                message = "The request could not be completed."
        else:
            code = "not_found" if error.status_code == 404 else "request_rejected"
            message = (
                "The requested resource was not found."
                if error.status_code == 404
                else ("The request could not be completed.")
            )
        return _error_response(error.status_code, code, message, headers=error.headers)

    @application.exception_handler(StorageError)
    async def storage_error_handler(_request: Request, error: StorageError) -> JSONResponse:
        if isinstance(error, (RecordNotFound, ArtifactNotFound)):
            return _error_response(404, "not_found", "The requested resource was not found.")
        if isinstance(error, (ConcurrencyConflict, IdempotencyConflict)):
            return _error_response(409, "conflict", "The request conflicts with current state.")
        if isinstance(error, (LeaseLost, LeaseUnavailable)):
            return _error_response(409, "lease_conflict", "The runtime lease is not available.")
        if isinstance(error, ContainmentViolation):
            return _error_response(
                409, "artifact_integrity_conflict", "Artifact verification failed."
            )
        if isinstance(error, PathEscapeError):
            return _error_response(422, "invalid_resource", "The requested resource is invalid.")
        if isinstance(error, QuotaExceeded):
            return _error_response(
                413, "quota_exceeded", "The configured resource limit was exceeded."
            )
        return _error_response(500, "storage_error", "The request could not be completed.")

    @application.exception_handler(ValueError)
    async def value_error_handler(_request: Request, _error: ValueError) -> JSONResponse:
        return _error_response(422, "invalid_request", "The request is invalid.")

    @application.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _error: Exception) -> JSONResponse:
        return _error_response(500, "internal_error", "The request could not be completed.")

    return application


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


app = create_app()
