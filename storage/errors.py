"""Typed persistence and concurrency failures."""

from __future__ import annotations


class StorageError(RuntimeError):
    """Base class for expected storage-layer failures."""


class RecordNotFound(StorageError):
    """The requested durable record does not exist."""


class ConcurrencyConflict(StorageError):
    """A compare-and-swap precondition no longer matches."""


class IdempotencyConflict(StorageError):
    """An idempotency key was reused with a different request hash."""


class LeaseUnavailable(StorageError):
    """A non-expired lease is held by another owner."""


class LeaseLost(StorageError):
    """The caller's owner identity or fencing token is no longer current."""


class UnsupportedSchemaVersion(StorageError):
    """The database schema is newer than this process understands."""


class PathEscapeError(StorageError):
    """A path attempted to escape the allowed containment directory."""


class QuotaExceeded(StorageError):
    """A storage quota (per-artifact or per-session) would be exceeded."""


class ArtifactNotFound(StorageError):
    """The requested artifact does not exist in the repository."""


class ArtifactCleanupRequired(StorageError):
    """A failed artifact operation left filesystem cleanup to be retried."""


class ContainmentViolation(StorageError):
    """An artifact fails containment, hash, size, or redaction checks."""


class PermissionError_(StorageError):
    """Platform file permission enforcement failed."""


class EventPayloadError(StorageError):
    """An event payload failed registry validation or size checks."""


class BundleError(StorageError):
    """A TaskContextBundle operation failed."""
