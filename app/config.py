"""Validated application settings and runtime data paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "AgentHub"
        return Path.home() / "AppData" / "Local" / "AgentHub"

    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "agent-hub"
    return Path.home() / ".local" / "share" / "agent-hub"


class Settings(BaseSettings):
    """Single source of truth for demo configuration."""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_HUB_",
        env_file=None,
        extra="forbid",
        validate_default=True,
    )

    data_dir: Path = Field(default_factory=_default_data_dir)
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8765, ge=1, le=65535)

    scheduler_poll_ms: int = Field(default=250, ge=50, le=60_000)
    master_lease_ttl_seconds: int = Field(default=15, ge=5, le=300)
    workspace_lease_ttl_seconds: int = Field(default=30, ge=5, le=600)
    lease_heartbeat_seconds: int = Field(default=5, ge=1, le=60)
    agent_default_timeout_seconds: int = Field(default=900, ge=1, le=3600)
    changeset_approval_ttl_seconds: int = Field(default=86_400, ge=60)
    privilege_approval_ttl_seconds: int = Field(default=600, ge=30)
    capability_grant_ttl_seconds: int = Field(default=300, ge=30)
    ws_ticket_ttl_seconds: int = Field(default=30, ge=5, le=300)
    idempotency_ttl_seconds: int = Field(default=86_400, ge=60)

    max_graph_json_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    max_http_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    max_console_bytes_per_run: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_console_chunk_bytes: int = Field(default=64 * 1024, ge=1024, le=64 * 1024)
    max_patch_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    max_changed_paths: int = Field(default=500, ge=1)
    max_task_created_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    max_artifact_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    max_session_artifact_bytes: int = Field(default=1024 * 1024 * 1024, ge=1024)
    max_source_tracked_paths: int = Field(default=50_000, ge=1)
    max_source_git_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1024)

    @field_validator("data_dir", mode="after")
    @classmethod
    def normalize_data_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve(strict=False)


@dataclass(frozen=True, slots=True)
class DataPaths:
    root: Path
    database: Path
    artifacts: Path
    logs: Path
    diffs: Path
    patches: Path
    reports: Path
    test_results: Path
    workspaces: Path
    shared_workspaces: Path
    agent_runs: Path
    profiles: Path
    opencode_profile: Path

    @classmethod
    def from_settings(cls, settings: Settings) -> DataPaths:
        root = settings.data_dir
        artifacts = root / "artifacts"
        workspaces = root / "workspaces"
        profiles = root / "profiles"
        return cls(
            root=root,
            database=root / "agent-hub.db",
            artifacts=artifacts,
            logs=artifacts / "logs",
            diffs=artifacts / "diffs",
            patches=artifacts / "patches",
            reports=artifacts / "reports",
            test_results=artifacts / "test-results",
            workspaces=workspaces,
            shared_workspaces=workspaces / "shared",
            agent_runs=workspaces / "agent-runs",
            profiles=profiles,
            opencode_profile=profiles / "opencode",
        )

    def directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.artifacts,
            self.logs,
            self.diffs,
            self.patches,
            self.reports,
            self.test_results,
            self.workspaces,
            self.shared_workspaces,
            self.agent_runs,
            self.profiles,
            self.opencode_profile,
        )


def ensure_data_directories(settings: Settings) -> DataPaths:
    paths = DataPaths.from_settings(settings)
    for directory in paths.directories():
        directory.mkdir(parents=True, exist_ok=True)
    return paths


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
