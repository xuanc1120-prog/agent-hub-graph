"""Pydantic models for strict manifest validation."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProbeStatus(BaseModel):
    """Status of CLI probes."""

    model_config = ConfigDict(strict=True, extra="forbid")

    binary_found: bool
    version_detected: bool
    success_rate: float = Field(ge=0.0, le=1.0)


class BinaryInfo(BaseModel):
    """Binary information with hashes."""

    model_config = ConfigDict(strict=True, extra="forbid")

    name: str
    version: str
    launcher_sha256: str
    entrypoint_sha256: str
    native_binary_sha256: str
    startup_chain_complete: bool
    probe_status: ProbeStatus

    @field_validator("launcher_sha256", "entrypoint_sha256", "native_binary_sha256")
    @classmethod
    def validate_hash_format(cls, v: str) -> str:
        """Validate SHA-256 hash format (64 hex chars or 'unknown')."""
        if v == "unknown":
            return v
        if len(v) != 64:
            raise ValueError(f"Hash must be 64 hex chars, got {len(v)}: {v}")
        if not all(c in "0123456789abcdef" for c in v):
            raise ValueError(f"Hash must be lowercase hex, got: {v}")
        return v

    @field_validator("version")
    @classmethod
    def validate_version_format(cls, v: str) -> str:
        """Validate semantic version format (X.Y.Z)."""
        parts = v.split(".")
        if len(parts) != 3:
            raise ValueError(f"Version must have 3 parts, got: {v}")
        if not all(p.isdigit() for p in parts):
            raise ValueError(f"Version parts must be digits, got: {v}")
        return v

    @model_validator(mode="after")
    def validate_startup_chain_consistency(self) -> "BinaryInfo":
        """Validate startup chain consistency."""
        # If startup_chain_complete is True, all hashes must be valid (not unknown)
        if self.startup_chain_complete:
            unknown_hashes = []
            if self.launcher_sha256 == "unknown":
                unknown_hashes.append("launcher_sha256")
            if self.entrypoint_sha256 == "unknown":
                unknown_hashes.append("entrypoint_sha256")
            if self.native_binary_sha256 == "unknown":
                unknown_hashes.append("native_binary_sha256")
            if unknown_hashes:
                raise ValueError(
                    f"startup_chain_complete=True but hashes are unknown: {unknown_hashes}"
                )
        return self


class CapabilitiesProbeStatus(BaseModel):
    """Status of capability probes."""

    model_config = ConfigDict(strict=True, extra="forbid")

    binary_found: bool
    version_detected: bool
    help_parsed: bool
    run_help_parsed: bool
    json_support_detected: bool
    pure_support_detected: bool
    dir_support_detected: bool


class Capabilities(BaseModel):
    """CLI capabilities."""

    model_config = ConfigDict(strict=True, extra="forbid")

    json_output: bool
    pure_mode: bool
    directory_selection: bool
    legacy_mode: bool
    requires_explicit_opt_in: bool
    probe_status: CapabilitiesProbeStatus

    @model_validator(mode="after")
    def validate_requires_explicit_opt_in(self) -> "Capabilities":
        """Legacy mode always requires explicit opt-in."""
        if not self.requires_explicit_opt_in:
            raise ValueError("requires_explicit_opt_in must be True")
        return self


class Command(BaseModel):
    """CLI command definition."""

    model_config = ConfigDict(strict=True, extra="forbid")

    command: str
    description: str


class Option(BaseModel):
    """CLI option definition."""

    model_config = ConfigDict(strict=True, extra="forbid")

    option: str
    description: str
    type: str = ""
    default: str = ""


class Options(BaseModel):
    """CLI options grouped by scope."""

    model_config = ConfigDict(strict=True, extra="forbid", populate_by_name=True)

    global_options: list[Option] = Field(alias="global")
    run: list[Option]


class OutputFraming(BaseModel):
    """Output framing configuration."""

    model_config = ConfigDict(strict=True, extra="forbid")

    json_events: bool
    default_format: str


class ExitBehavior(BaseModel):
    """Exit behavior configuration."""

    model_config = ConfigDict(strict=True, extra="forbid")

    timeout_seconds: int = Field(gt=0)
    cancel_support: str
    process_tree_kill: str

    @model_validator(mode="after")
    def validate_process_capabilities(self) -> "ExitBehavior":
        """Process capabilities must be 'unverified' until tested."""
        valid_statuses = ("unverified", "supported", "unsupported")
        if self.cancel_support not in valid_statuses:
            raise ValueError(f"Invalid cancel_support: {self.cancel_support}")
        if self.process_tree_kill not in valid_statuses:
            raise ValueError(f"Invalid process_tree_kill: {self.process_tree_kill}")
        return self


class JsonRunEvidence(BaseModel):
    """Evidence of JSON run attempt."""

    model_config = ConfigDict(strict=True, extra="forbid")

    attempted: bool
    success: bool
    skip_reason: str | None = None
    is_synthetic: bool
    event_count: int = Field(ge=0)


class Compatibility(BaseModel):
    """Compatibility information."""

    model_config = ConfigDict(strict=True, extra="forbid")

    version: str
    pure_support: bool
    legacy_support: bool
    recommended_mode: str
    requires_explicit_opt_in: bool

    @model_validator(mode="after")
    def validate_requires_explicit_opt_in(self) -> "Compatibility":
        """Legacy mode always requires explicit opt-in."""
        if not self.requires_explicit_opt_in:
            raise ValueError("requires_explicit_opt_in must be True")
        return self

    @model_validator(mode="after")
    def validate_recommended_mode_consistency(self) -> "Compatibility":
        """Validate recommended mode consistency."""
        valid_modes = ("none", "pure", "legacy")
        if self.recommended_mode not in valid_modes:
            raise ValueError(f"Invalid recommended_mode: {self.recommended_mode}")

        # Legacy can NEVER be recommended - it must be explicitly chosen
        if self.recommended_mode == "legacy":
            raise ValueError(
                "recommended_mode cannot be 'legacy' - legacy requires explicit opt-in"
            )

        # Pure can only be recommended if pure_support is True
        if self.recommended_mode == "pure" and not self.pure_support:
            raise ValueError("recommended_mode='pure' requires pure_support=True")

        # If legacy_support is False, recommended_mode cannot be "legacy"
        if not self.legacy_support and self.recommended_mode == "legacy":
            raise ValueError("recommended_mode cannot be 'legacy' when legacy_support is False")
        return self


class Manifest(BaseModel):
    """Complete manifest schema with strict validation."""

    model_config = ConfigDict(strict=True, extra="forbid")

    version: str
    tool: str
    binary: BinaryInfo
    capabilities: Capabilities
    commands: list[Command]
    options: Options
    output_framing: OutputFraming
    exit_behavior: ExitBehavior
    json_run_evidence: JsonRunEvidence
    compatibility: Compatibility

    @field_validator("version")
    @classmethod
    def validate_version_format(cls, v: str) -> str:
        """Validate semantic version format (X.Y.Z)."""
        parts = v.split(".")
        if len(parts) != 3:
            raise ValueError(f"Version must have 3 parts, got: {v}")
        if not all(p.isdigit() for p in parts):
            raise ValueError(f"Version parts must be digits, got: {v}")
        return v

    @model_validator(mode="after")
    def validate_version_consistency(self) -> "Manifest":
        """Validate version consistency across manifest."""
        if self.binary.version != self.compatibility.version:
            raise ValueError(
                f"Version mismatch: binary.version={self.binary.version} "
                f"!= compatibility.version={self.compatibility.version}"
            )
        return self

    @model_validator(mode="after")
    def validate_tool_name(self) -> "Manifest":
        """Validate tool name."""
        if self.tool != "opencode":
            raise ValueError(f"Invalid tool name: {self.tool}")
        return self


def validate_manifest_strict(manifest_dict: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    Validate manifest using strict Pydantic model.

    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    try:
        Manifest.model_validate(manifest_dict)
        return True, []
    except Exception as e:
        return False, [str(e)]
