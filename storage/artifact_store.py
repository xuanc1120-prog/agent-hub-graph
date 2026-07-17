"""Atomic artifact file storage with containment, quota and permission enforcement.

All artifact paths are server-generated from validated ``artifact_id`` and
``artifact_type`` components.  Writes use a staged approach:

1. Compute SHA-256 and size from the content bytes.
2. Write to a uniquely-named temp file, fsync, and set permissions.
3. Publish via atomic hardlink (no-overwrite) inside a ``BEGIN IMMEDIATE``
   transaction alongside the metadata INSERT.

Each :class:`ArtifactStore` instance creates its own private token.
:class:`TempWriteResult` handles are operation-bound and one-time:
STAGED → PUBLISHED/CONSUMED or STAGED → DISCARDED.  Cross-store, stale,
and replayed handles are rejected before any file I/O.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import stat
import tempfile
import threading
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter

from protocol import ArtifactType, EntityId
from storage.errors import (
    ArtifactCleanupRequired,
    PathEscapeError,
    PermissionError_,
    QuotaExceeded,
)

_ENTITY_ID = TypeAdapter(EntityId)
_ARTIFACT_TYPE = TypeAdapter(ArtifactType)

# Handle states (private, not exported)
# Valid transitions:
#   STAGED -> PUBLISHED  (via publish)
#   STAGED -> DISCARDED  (via discard)
#   PUBLISHED -> ROLLED_BACK (via rollback, removes published file)
#   PUBLISHED -> FINALIZED (via finalize, after DB commit — terminal)
#   ROLLED_BACK -> DISCARDED (via discard after rollback)
_STAGED = 0
_PUBLISHED = 1
_ROLLED_BACK = 2
_DISCARDED = 3
_FINALIZED = 4
_CLEANUP_REQUIRED = 5


def _validate_entity_id(value: str, label: str) -> str:
    try:
        return _ENTITY_ID.validate_python(value)
    except Exception as exc:
        raise PathEscapeError(f"invalid {label}: {exc}") from exc


def _validate_artifact_type(value: str) -> str:
    try:
        _ARTIFACT_TYPE.validate_python(value)
    except Exception as exc:
        raise PathEscapeError(f"invalid artifact_type: {exc}") from exc
    return value


def _is_reparse_or_symlink(path: Path) -> bool:
    """Detect symlinks and Windows reparse points (junctions, etc.)."""
    try:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            return True
        if os.name == "nt":
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
            if attrs != -1 and attrs & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                return True
    except OSError:
        pass
    return False


def _is_hardlink(path: Path) -> bool:
    """Detect hardlinks by comparing nlink count."""
    try:
        return path.stat().st_nlink > 1
    except OSError:
        return False


def _check_containment_path(path: Path, base: Path) -> None:
    """Verify every component of path under base is not a symlink/reparse."""
    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise PathEscapeError(f"path escapes containment: {path} not under {base}") from exc

    current = base
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse_or_symlink(current):
            raise PathEscapeError(f"symlink/reparse point in containment path: {current}")


def _set_private_permissions(path: Path, *, directory: bool = False) -> None:
    """Set current-user-only permissions.  POSIX: dirs 0o700, files 0o600."""
    if os.name == "nt":
        import subprocess

        user = os.environ.get("USERNAME", "")
        if not user:
            raise PermissionError_(f"cannot determine current user for permission set on {path}")
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise PermissionError_(
                f"failed to set Windows permissions on {path}: {result.stderr.strip()}"
            )
    else:
        mode = 0o700 if directory else 0o600
        try:
            os.chmod(path, mode)
        except OSError as exc:
            raise PermissionError_(f"failed to set POSIX permissions on {path}: {exc}") from exc


def _restore_write_permissions(path: Path) -> None:
    """Restore write permissions for cleanup."""
    if os.name == "nt":
        import subprocess

        user = os.environ.get("USERNAME", "")
        if not user:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IRWXU)


def _write_fully(fd: int, content: bytes) -> None:
    """Write all bytes, handling short writes."""
    offset = 0
    length = len(content)
    while offset < length:
        written = os.write(fd, content[offset:])
        if written == 0:
            raise OSError("short write: wrote 0 bytes")
        offset += written


def _unlink_path_entry(path: Path) -> bool:
    """Remove one path entry without following a symlink or reparse point."""
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False

    if _is_reparse_or_symlink(path):
        if stat.S_ISDIR(path_stat.st_mode):
            os.rmdir(path)
        else:
            path.unlink()
    else:
        _restore_write_permissions(path)
        path.unlink()
    return True


class ArtifactStore:
    """Manages atomic artifact file writes under a containment root.

    Each instance has a private token that binds :class:`TempWriteResult`
    handles to this store.  Handles from other stores are rejected.
    """

    def __init__(
        self,
        base_dir: Path,
        *,
        max_artifact_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self._base = base_dir.expanduser().resolve(strict=False)
        self._max_artifact_bytes = max_artifact_bytes
        self._token = object()  # per-instance, not guessable
        self._lock = threading.Lock()
        self._base.mkdir(parents=True, exist_ok=True)
        _set_private_permissions(self._base, directory=True)

    @property
    def base_dir(self) -> Path:
        return self._base

    def resolve(self, artifact_id: str, artifact_type: str) -> Path:
        """Compute the safe absolute path for an artifact."""
        _validate_entity_id(artifact_id, "artifact_id")
        _validate_artifact_type(artifact_type)
        target = (self._base / artifact_type / artifact_id).resolve(strict=False)
        if not self._is_contained(target):
            raise PathEscapeError(
                f"resolved path escapes containment: {target} not under {self._base}"
            )
        return target

    def _is_contained(self, target: Path) -> bool:
        try:
            target.relative_to(self._base)
            return True
        except ValueError:
            return False

    def exists(self, artifact_id: str, artifact_type: str) -> bool:
        return self.resolve(artifact_id, artifact_type).exists()

    def write_temp(
        self,
        artifact_id: str,
        artifact_type: str,
        content: bytes,
    ) -> TempWriteResult:
        """Stage a write.  Returns a one-time, store-bound handle."""
        size_bytes = len(content)
        if size_bytes > self._max_artifact_bytes:
            raise QuotaExceeded(
                f"artifact size {size_bytes} exceeds limit {self._max_artifact_bytes}"
            )

        content_hash = sha256(content).hexdigest()
        target = self.resolve(artifact_id, artifact_type)
        target.parent.mkdir(parents=True, exist_ok=True)
        _check_containment_path(target.parent, self._base)

        fd = -1
        tmp_path: Path | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=".artifact-",
                suffix=".tmp",
                dir=str(target.parent),
            )
            tmp_path = Path(tmp_name)
            _write_fully(fd, content)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            _set_private_permissions(tmp_path)

            actual_size = tmp_path.stat().st_size
            if actual_size != size_bytes:
                raise PathEscapeError(
                    f"temp file size mismatch: expected {size_bytes}, got {actual_size}"
                )
            actual_hash = sha256(tmp_path.read_bytes()).hexdigest()
            if actual_hash != content_hash:
                raise PathEscapeError(
                    f"temp file hash mismatch: expected {content_hash}, got {actual_hash}"
                )

        except BaseException:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    _restore_write_permissions(tmp_path)
                    tmp_path.unlink(missing_ok=True)
            raise

        return TempWriteResult(
            tmp_path=tmp_path,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            sha256=content_hash,
            size_bytes=size_bytes,
            _token=self._token,
        )

    def _validate_handle(self, staged: TempWriteResult, operation: str, allowed: set[int]) -> None:
        """Validate store binding and state."""
        if staged._token is not self._token:
            raise PathEscapeError(f"{operation}: handle not from this store")
        if staged._state not in allowed:
            raise PathEscapeError(f"{operation}: invalid handle state")

    def _verify_linked_pair_locked(self, staged: TempWriteResult, final: Path) -> None:
        """Verify the staged and final names still reference the expected bytes."""
        _check_containment_path(staged.tmp_path, self._base)
        _check_containment_path(final, self._base)
        if _is_reparse_or_symlink(staged.tmp_path):
            raise PathEscapeError(f"staged temp is symlink/reparse: {staged.tmp_path}")
        if _is_reparse_or_symlink(final):
            raise PathEscapeError(f"published final is symlink/reparse: {final}")

        try:
            temp_stat = staged.tmp_path.lstat()
            final_stat = final.lstat()
        except OSError as exc:
            raise PathEscapeError(f"cannot stat linked artifact: {exc}") from exc
        if not stat.S_ISREG(temp_stat.st_mode) or not stat.S_ISREG(final_stat.st_mode):
            raise PathEscapeError("staged temp and published final must be regular files")
        try:
            same_file = staged.tmp_path.samefile(final)
        except OSError as exc:
            raise PathEscapeError(f"cannot verify linked artifact identity: {exc}") from exc
        if not same_file:
            raise PathEscapeError("published final no longer references the staged file")

        final_data = final.read_bytes()
        if len(final_data) != staged.size_bytes:
            raise PathEscapeError(
                f"published size changed: expected {staged.size_bytes}, got {len(final_data)}"
            )
        actual_hash = sha256(final_data).hexdigest()
        if actual_hash != staged.sha256:
            raise PathEscapeError(
                f"published hash changed: expected {staged.sha256}, got {actual_hash}"
            )

        try:
            if not staged.tmp_path.samefile(final):
                raise PathEscapeError("published final identity changed during verification")
        except OSError as exc:
            raise PathEscapeError(
                f"published final disappeared during verification: {exc}"
            ) from exc

    def _verify_published_final_locked(self, staged: TempWriteResult, final: Path) -> None:
        _check_containment_path(final, self._base)
        if _is_reparse_or_symlink(final):
            raise PathEscapeError(f"published final is symlink/reparse: {final}")
        try:
            final_stat = final.lstat()
        except OSError as exc:
            raise PathEscapeError(f"published final is missing: {final}") from exc
        if not stat.S_ISREG(final_stat.st_mode):
            raise PathEscapeError(f"published final is not a regular file: {final}")
        if final_stat.st_nlink != 1:
            raise PathEscapeError(
                f"published final has unexpected link count: {final_stat.st_nlink}"
            )
        final_data = final.read_bytes()
        if len(final_data) != staged.size_bytes or sha256(final_data).hexdigest() != staged.sha256:
            raise PathEscapeError("published final content does not match staged metadata")

    def _mark_cleanup_required(
        self,
        staged: TempWriteResult,
        cleanup_error: BaseException,
        operation_error: BaseException,
    ) -> ArtifactCleanupRequired:
        object.__setattr__(staged, "_state", _CLEANUP_REQUIRED)
        error = ArtifactCleanupRequired(
            f"artifact cleanup required for {staged._artifact_type}/{staged._artifact_id}: "
            f"{cleanup_error}"
        )
        error.add_note(f"original artifact operation failed: {operation_error!r}")
        return error

    @staticmethod
    def _cleanup_error(
        staged: TempWriteResult,
        cleanup_error: BaseException,
        operation_error: BaseException,
    ) -> ArtifactCleanupRequired:
        error = ArtifactCleanupRequired(
            f"artifact cleanup required for {staged._artifact_type}/{staged._artifact_id}: "
            f"{cleanup_error}"
        )
        error.add_note(f"original artifact operation failed: {operation_error!r}")
        return error

    def _rollback_failed_publish_locked(
        self,
        staged: TempWriteResult,
        final: Path,
        operation_error: BaseException,
    ) -> None:
        try:
            _unlink_path_entry(final)
        except BaseException as cleanup_error:
            raise self._mark_cleanup_required(
                staged, cleanup_error, operation_error
            ) from cleanup_error
        object.__setattr__(staged, "_state", _ROLLED_BACK)

    def publish(self, staged: TempWriteResult) -> None:
        """Atomic no-overwrite publish: hardlink temp → final, unlink temp.

        The staged name is retained until the linked final passes identity,
        type, hash and size verification.  Any failure after linking removes
        the final or transitions the handle to CLEANUP_REQUIRED.
        Valid from: STAGED.  Transitions to: PUBLISHED.
        """
        if staged._token is not self._token:
            raise PathEscapeError("publish: handle not from this store")

        final = self.resolve(staged._artifact_id, staged._artifact_type)
        _check_containment_path(final, self._base)

        with self._lock:
            if staged._state != _STAGED:
                raise PathEscapeError("publish: invalid handle state")

            # Verify temp file INSIDE lock
            if not staged.tmp_path.exists():
                raise PathEscapeError(f"staged temp missing: {staged.tmp_path}")
            _check_containment_path(staged.tmp_path, self._base)
            if _is_reparse_or_symlink(staged.tmp_path):
                raise PathEscapeError(f"staged temp is symlink/reparse: {staged.tmp_path}")

            actual_data = staged.tmp_path.read_bytes()
            if len(actual_data) != staged.size_bytes:
                raise PathEscapeError(
                    f"temp size changed: expected {staged.size_bytes}, got {len(actual_data)}"
                )
            if sha256(actual_data).hexdigest() != staged.sha256:
                raise PathEscapeError(f"temp hash changed: expected {staged.sha256}")

            linked = False
            try:
                os.link(str(staged.tmp_path), str(final))
                linked = True
                self._verify_linked_pair_locked(staged, final)
                self._cleanup_temp(staged)
                self._verify_published_final_locked(staged, final)
            except FileExistsError:
                try:
                    self._cleanup_temp(staged)
                except BaseException as cleanup_error:
                    duplicate_error = PathEscapeError(
                        f"artifact already exists: {final}; immutable, cannot overwrite"
                    )
                    raise self._cleanup_error(staged, cleanup_error, duplicate_error) from (
                        cleanup_error
                    )
                object.__setattr__(staged, "_state", _DISCARDED)
                raise PathEscapeError(
                    f"artifact already exists: {final}; immutable, cannot overwrite"
                ) from None
            except BaseException as operation_error:
                if linked:
                    self._rollback_failed_publish_locked(staged, final, operation_error)
                if isinstance(operation_error, PathEscapeError):
                    raise
                raise PathEscapeError(f"publish failed: {operation_error}") from operation_error

            object.__setattr__(staged, "_state", _PUBLISHED)

    def rollback_publish(self, staged: TempWriteResult) -> None:
        """Remove a published final file.

        Uses a lock to make state-check + delete atomic, preventing
        concurrent rollbacks from both deleting the file.

        Valid from: PUBLISHED.  Transitions to: ROLLED_BACK.
        From STAGED: just transitions to ROLLED_BACK (no file to remove).
        """
        if staged._token is not self._token:
            raise PathEscapeError("rollback: handle not from this store")

        with self._lock:
            if staged._state == _PUBLISHED:
                final = self.resolve(staged._artifact_id, staged._artifact_type)
                if final.exists():
                    try:
                        _unlink_path_entry(final)
                    except BaseException as cleanup_error:
                        operation_error = PathEscapeError("artifact rollback failed")
                        raise self._mark_cleanup_required(
                            staged, cleanup_error, operation_error
                        ) from cleanup_error
                object.__setattr__(staged, "_state", _ROLLED_BACK)
            elif staged._state == _STAGED:
                object.__setattr__(staged, "_state", _ROLLED_BACK)
            else:
                raise PathEscapeError("rollback: invalid handle state")

    def finalize(self, staged: TempWriteResult) -> None:
        """Mark handle as finalized after successful DB commit.

        Uses lock to prevent concurrent rollback.  Valid from: PUBLISHED.
        Transitions to: FINALIZED (terminal).
        """
        if staged._token is not self._token:
            raise PathEscapeError("finalize: handle not from this store")
        with self._lock:
            if staged._state != _PUBLISHED:
                raise PathEscapeError("finalize: invalid handle state")
            final = self.resolve(staged._artifact_id, staged._artifact_type)
            self._verify_published_final_locked(staged, final)
            object.__setattr__(staged, "_state", _FINALIZED)

    def discard_staged(self, staged: TempWriteResult) -> None:
        """Clean up a staged temp.

        Valid from: STAGED, ROLLED_BACK.  Transitions to: DISCARDED.
        Uses lock to prevent concurrent publish from racing.
        """
        if staged._token is not self._token:
            raise PathEscapeError("discard: handle not from this store")
        with self._lock:
            if staged._state not in {_STAGED, _ROLLED_BACK}:
                raise PathEscapeError("discard: invalid handle state")
            try:
                self._cleanup_temp(staged)
            except BaseException as cleanup_error:
                operation_error = PathEscapeError("staged artifact discard failed")
                raise self._cleanup_error(staged, cleanup_error, operation_error) from cleanup_error
            object.__setattr__(staged, "_state", _DISCARDED)

    def abort(self, staged: TempWriteResult) -> None:
        """Clean a failed create operation according to its current state."""
        if staged._token is not self._token:
            raise PathEscapeError("abort: handle not from this store")
        with self._lock:
            if staged._state == _FINALIZED:
                raise PathEscapeError("abort: finalized handle cannot be aborted")
            if staged._state == _DISCARDED:
                return

            final_cleanup_error: BaseException | None = None
            if staged._state in {_PUBLISHED, _CLEANUP_REQUIRED}:
                try:
                    final = self.resolve(staged._artifact_id, staged._artifact_type)
                    _unlink_path_entry(final)
                except BaseException as cleanup_error:
                    final_cleanup_error = cleanup_error
                else:
                    object.__setattr__(staged, "_state", _ROLLED_BACK)

            try:
                self._cleanup_temp(staged)
            except BaseException as cleanup_error:
                operation_error = PathEscapeError("artifact abort failed")
                raise self._cleanup_error(staged, cleanup_error, operation_error) from cleanup_error

            if final_cleanup_error is not None:
                operation_error = PathEscapeError("artifact abort failed")
                raise self._mark_cleanup_required(
                    staged, final_cleanup_error, operation_error
                ) from final_cleanup_error

            object.__setattr__(staged, "_state", _DISCARDED)

    def _cleanup_temp(self, staged: TempWriteResult) -> None:
        """Remove temp file if it exists."""
        if staged.tmp_path.exists():
            _check_containment_path(staged.tmp_path, self._base)
            if _is_reparse_or_symlink(staged.tmp_path):
                raise PathEscapeError(f"staged temp is symlink/reparse: {staged.tmp_path}")
            _restore_write_permissions(staged.tmp_path)
            staged.tmp_path.unlink(missing_ok=True)

    def delete_orphan(self, artifact_id: str, artifact_type: str) -> bool:
        """Delete an unreferenced final after the repository checks metadata."""
        final = self.resolve(artifact_id, artifact_type)
        with self._lock:
            _check_containment_path(final, self._base)
            return _unlink_path_entry(final)

    def read_bytes(self, artifact_id: str, artifact_type: str) -> bytes:
        """Read artifact bytes after full containment verification."""
        target = self.resolve(artifact_id, artifact_type)
        if not target.exists():
            raise PathEscapeError(f"artifact file missing: {target}")
        _check_containment_path(target, self._base)
        if _is_reparse_or_symlink(target):
            raise PathEscapeError(f"symlink/reparse point detected: {target}")
        if _is_hardlink(target):
            raise PathEscapeError(f"hardlink detected: {target}")
        return target.read_bytes()

    def cleanup_temp(
        self,
        artifact_type: str,
        *,
        ttl_seconds: int,
        now: float | None = None,
    ) -> int:
        """Remove orphaned temp files older than TTL."""
        import time

        _validate_artifact_type(artifact_type)
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        type_dir = self._base / artifact_type
        if not type_dir.exists():
            return 0
        cutoff = (time.time() if now is None else now) - ttl_seconds
        removed = 0
        for tmp in type_dir.glob(".artifact-*.tmp"):
            try:
                _check_containment_path(tmp, self._base)
                if _is_reparse_or_symlink(tmp):
                    continue
                if tmp.stat().st_mtime >= cutoff:
                    continue
                _restore_write_permissions(tmp)
                tmp.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    def delete(self, artifact_id: str, artifact_type: str) -> bool:
        """Delete an artifact file."""
        target = self.resolve(artifact_id, artifact_type)
        if target.exists():
            _restore_write_permissions(target)
            target.unlink()
            return True
        return False


class TempWriteResult:
    """One-time, store-bound handle for a staged write.

    Can only be created by :meth:`ArtifactStore.write_temp`.  Each handle
    starts in STAGED state and transitions to CONSUMED on the first
    publish/rollback/discard.  Reuse raises ``PathEscapeError``.
    Handles from a different store are rejected.
    """

    __slots__ = (
        "_artifact_id",
        "_artifact_type",
        "_state",
        "_token",
        "sha256",
        "size_bytes",
        "tmp_path",
    )

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError(f"TempWriteResult is immutable: cannot set {name}")
        super().__setattr__(name, value)

    def __init__(
        self,
        tmp_path: Path,
        artifact_id: str,
        artifact_type: str,
        sha256: str,
        size_bytes: int,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is None:
            raise ValueError("TempWriteResult can only be created by ArtifactStore.write_temp()")
        self._token = _token
        self._state = _STAGED
        self.tmp_path = tmp_path
        self._artifact_id = artifact_id
        self._artifact_type = artifact_type
        self.sha256 = sha256
        self.size_bytes = size_bytes

    @property
    def artifact_id(self) -> str:
        return self._artifact_id

    @property
    def artifact_type(self) -> str:
        return self._artifact_type
