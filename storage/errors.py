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
