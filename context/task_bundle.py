"""TaskContextBundle: materialize redacted artifacts into a sealed read-only bundle.

A bundle is a directory at ``bundle_root/<task_id>/context/`` containing only
redacted artifacts that belong to the current session (and optionally task).
Each materialization re-verifies metadata, hash, size and ownership.  The
bundle includes a frozen strict manifest, a canonical hash, and supports
idempotent cleanup.

The bundle total byte limit is caller-injected (never a magic number).

Staged publication: all artifacts are written to a staging directory first;
only after complete verification is the staging directory renamed to the
final location.  Existing non-empty bundle directories are rejected.

Every directory level is checked for symlinks, junctions, and reparse points
before any I/O operation.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

from pydantic import Field, TypeAdapter

from protocol import (
    ArtifactRef,
    ArtifactType,
    EntityId,
    FrozenStrictModel,
    Sha256Hex,
    canonical_json,
)
from storage.artifact_repository import ArtifactRepository
from storage.db import normalize_utc, utc_now_text
from storage.errors import BundleError

_ENTITY_ID = TypeAdapter(EntityId)

# ---------------------------------------------------------------------------
# Containment helpers
# ---------------------------------------------------------------------------


def _is_reparse_or_symlink(path: Path) -> bool:
    """Detect symlinks and Windows reparse points (junctions, etc.)."""
    try:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            return True
        if os.name == "nt":
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
            if attrs != -1 and attrs & 0x400:
                return True
    except OSError:
        pass
    return False


def _check_path_safe(path: Path, base: Path, label: str) -> None:
    """Verify every component of path under base is not a symlink/reparse."""
    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise BundleError(f"{label} escapes containment: {path} not under {base}") from exc
    current = base
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse_or_symlink(current):
            raise BundleError(f"{label}: symlink/junction/reparse in path: {current}")


# ---------------------------------------------------------------------------
# Manifest models (frozen strict, no extra fields)
# ---------------------------------------------------------------------------


class ManifestEntry(FrozenStrictModel):
    """One item in the bundle manifest."""

    artifact_id: EntityId
    artifact_type: ArtifactType
    source_ref: ArtifactRef
    materialized_filename: str = Field(min_length=1, max_length=256)
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
    expires_at: str = Field(min_length=1, max_length=27)


class BundleManifest(FrozenStrictModel):
    """Frozen strict manifest for a TaskContextBundle."""

    task_id: EntityId
    session_id: EntityId
    entries: tuple[ManifestEntry, ...] = Field(default_factory=tuple)
    total_bytes: int = Field(ge=0)
    bundle_sha256: Sha256Hex


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BundleResult:
    """Result of :meth:`TaskContextBundle.materialize`."""

    manifest: BundleManifest
    bundle_dir: Path


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Result of :meth:`TaskContextBundle.cleanup`."""

    removed_files: int
    removed_dirs: int
    errors: list[str]


# ---------------------------------------------------------------------------
# Main bundle class
# ---------------------------------------------------------------------------


class TaskContextBundle:
    """Materialize redacted artifacts into a task-scoped sealed bundle.

    Parameters
    ----------
    repository:
        The artifact repository for metadata and content access.
    bundle_root:
        Root directory for bundles (typically ``DataPaths.agent_runs``).
    max_bundle_bytes:
        Caller-injected total byte limit for the entire bundle.
        Must be explicitly provided — no default.
    ttl_seconds:
        Time-to-live for manifest entries (used for expires_at).
    """

    def __init__(
        self,
        repository: ArtifactRepository,
        bundle_root: Path,
        *,
        max_bundle_bytes: int,
        ttl_seconds: int,
    ) -> None:
        if max_bundle_bytes < 1:
            raise ValueError("max_bundle_bytes must be positive")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        self._repo = repository
        self._bundle_root = bundle_root.expanduser().resolve(strict=False)
        self._max_bundle_bytes = max_bundle_bytes
        self._ttl_seconds = ttl_seconds

    def bundle_dir(self, task_id: str) -> Path:
        """Return the canonical bundle directory for a task."""
        resolved = _ENTITY_ID.validate_python(task_id)
        return self._bundle_root / resolved / "context"

    async def materialize(
        self,
        *,
        task_id: str,
        session_id: str,
        artifact_refs: list[ArtifactRef],
        now: str | None = None,
    ) -> BundleResult:
        """Materialize redacted artifacts into a sealed bundle.

        Every directory level is checked for symlinks/junctions before I/O.
        Duplicate artifact IDs are rejected.
        """
        resolved_task = _ENTITY_ID.validate_python(task_id)
        resolved_session = _ENTITY_ID.validate_python(session_id)

        resolved_now = normalize_utc(now)
        expires_at = utc_now_text(resolved_now + timedelta(seconds=self._ttl_seconds))

        final_dir = self.bundle_dir(resolved_task)

        # Containment check on bundle root
        _check_path_safe(self._bundle_root, self._bundle_root, "bundle_root")

        # Check final dir containment and state
        if final_dir.exists():
            # Verify no symlinks in path
            _check_path_safe(final_dir, self._bundle_root, "final_dir")
            if any(final_dir.iterdir()):
                raise BundleError(
                    f"bundle directory already exists and is non-empty: {resolved_task}/context"
                )
            final_dir.rmdir()

        # Verify parent chain is safe
        parent = final_dir.parent
        _check_path_safe(parent, self._bundle_root, "bundle_parent")

        # Create staging directory
        staging_parent = parent
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix=".bundle-staging-", dir=str(staging_parent)))

        # Verify staging is under our root
        _check_path_safe(staging_dir, self._bundle_root, "staging")

        entries: list[ManifestEntry] = []
        total_bytes = 0
        seen_ids: set[str] = set()

        try:
            for ref in artifact_refs:
                resolved_aid = _ENTITY_ID.validate_python(ref.artifact_id)

                # Duplicate artifact ID rejection
                if resolved_aid in seen_ids:
                    raise BundleError(f"duplicate artifact ref: {resolved_aid}")
                seen_ids.add(resolved_aid)

                # Fetch and verify ownership
                try:
                    record, content = await self._repo.get_and_verify(
                        resolved_aid,
                        expected_session_id=resolved_session,
                    )
                except Exception as exc:
                    raise BundleError(
                        f"artifact {resolved_aid} verification failed: {exc}"
                    ) from exc

                # Task ownership check
                if record.task_id is not None and record.task_id != resolved_task:
                    raise BundleError(
                        f"artifact {resolved_aid} belongs to task "
                        f"{record.task_id}, not {resolved_task}"
                    )

                # Must be redacted
                if not record.redacted:
                    raise BundleError(f"artifact {resolved_aid} is not redacted")

                # Verify ref fields match DB record
                if ref.sha256 != record.sha256:
                    raise BundleError(f"artifact {resolved_aid} sha256 mismatch")
                if ref.size_bytes != record.size_bytes:
                    raise BundleError(f"artifact {resolved_aid} size mismatch")
                if ref.artifact_type.value != record.artifact_type:
                    raise BundleError(f"artifact {resolved_aid} type mismatch")
                if ref.relative_path != record.relative_path:
                    raise BundleError(
                        f"artifact {resolved_aid} relative_path mismatch: "
                        f"ref={ref.relative_path}, db={record.relative_path}"
                    )

                # Build source_ref from verified DB record (not caller's ref)
                verified_ref = ArtifactRef(
                    artifact_id=resolved_aid,
                    artifact_type=ArtifactType(record.artifact_type),
                    relative_path=record.relative_path,
                    sha256=record.sha256,
                    size_bytes=record.size_bytes,
                )

                # Bundle size limit
                if total_bytes + record.size_bytes > self._max_bundle_bytes:
                    raise BundleError(
                        f"bundle size limit exceeded: "
                        f"{total_bytes} + {record.size_bytes} "
                        f"> {self._max_bundle_bytes}"
                    )

                # Write to staging
                filename = f"{resolved_aid}.{record.artifact_type}"
                dest = staging_dir / filename
                _check_path_safe(dest.parent, self._bundle_root, "staging_dest")
                dest.write_bytes(content)
                self._set_file_readonly(dest)

                total_bytes += record.size_bytes
                entries.append(
                    ManifestEntry(
                        artifact_id=resolved_aid,
                        artifact_type=ArtifactType(record.artifact_type),
                        source_ref=verified_ref,
                        materialized_filename=filename,
                        sha256=record.sha256,
                        size_bytes=record.size_bytes,
                        expires_at=expires_at,
                    )
                )

            # Compute manifest hash
            manifest_hash = self._compute_manifest_hash(
                entries, resolved_task, resolved_session, total_bytes
            )

            # Use tuple for entries (truly immutable)
            manifest = BundleManifest(
                task_id=resolved_task,
                session_id=resolved_session,
                entries=tuple(entries),
                total_bytes=total_bytes,
                bundle_sha256=manifest_hash,
            )

            # Write manifest to staging
            manifest_path = staging_dir / "manifest.json"
            manifest_bytes = canonical_json(manifest)
            manifest_path.write_bytes(manifest_bytes)
            self._set_file_readonly(manifest_path)

            # Set staging directory private and read-only
            self._set_dir_private(staging_dir)

            # Publish: rename staging → final
            if final_dir.exists():
                raise BundleError("bundle directory appeared during staging")
            staging_dir.rename(final_dir)

            # Seal parent directory
            try:
                self._set_dir_private(final_dir.parent)
            except BundleError:
                # ACL failed after rename — remove the incomplete final bundle
                self._force_remove_staging(final_dir)
                raise

        except BaseException:
            # If staging still exists (rename didn't happen), clean it up
            if staging_dir.exists():
                self._force_remove_staging(staging_dir)
            raise

        return BundleResult(manifest=manifest, bundle_dir=final_dir)

    async def cleanup(self, task_id: str) -> CleanupResult:
        """Idempotently remove a task's bundle directory.

        Uses top-down walk to filter out symlinks/junctions/reparse points
        from `dirs` before os.walk recurses into them.  Junction/symlink
        directories are removed as leaf entries (the link itself), never
        traversed.
        """
        resolved_task = _ENTITY_ID.validate_python(task_id)
        bdir = self.bundle_dir(resolved_task)
        if not bdir.exists():
            return CleanupResult(removed_files=0, removed_dirs=0, errors=[])

        # Verify containment before cleanup
        try:
            _check_path_safe(bdir, self._bundle_root, "cleanup_dir")
        except BundleError as exc:
            return CleanupResult(removed_files=0, removed_dirs=0, errors=[str(exc)])

        removed_files = 0
        removed_dirs = 0
        errors: list[str] = []

        # Top-down walk: filter out junctions/symlinks from dirs to prevent
        # os.walk from recursing into them.
        for root, dirs, files in os.walk(str(bdir), topdown=True):
            # Filter dirs: remove junctions/symlinks from traversal
            safe_dirs = []
            for name in dirs:
                dpath = Path(root) / name
                if _is_reparse_or_symlink(dpath):
                    # Remove the junction/symlink itself, don't recurse
                    try:
                        self._restore_write(dpath)
                        dpath.rmdir()  # rmdir removes the link, not target
                        removed_dirs += 1
                    except OSError:
                        errors.append(
                            f"failed to remove junction: {resolved_task}/context/{name}: junction"
                        )
                else:
                    safe_dirs.append(name)
            dirs[:] = safe_dirs

            for name in files:
                fpath = Path(root) / name
                try:
                    self._restore_write(fpath)
                    fpath.unlink()
                    removed_files += 1
                except OSError as exc:
                    errors.append(
                        f"failed to remove file: {resolved_task}/"
                        f"context/{name}: {type(exc).__name__}"
                    )

        # Now remove empty dirs bottom-up
        for root, dirs, _files in os.walk(str(bdir), topdown=False):
            for name in dirs:
                dpath = Path(root) / name
                try:
                    dpath.rmdir()
                    removed_dirs += 1
                except OSError as exc:
                    errors.append(
                        f"failed to remove directory: {resolved_task}/"
                        f"context/{name}: {type(exc).__name__}"
                    )

        try:
            bdir.rmdir()
            removed_dirs += 1
        except OSError as exc:
            errors.append(
                f"failed to remove bundle dir: {resolved_task}/context: {type(exc).__name__}"
            )

        return CleanupResult(
            removed_files=removed_files,
            removed_dirs=removed_dirs,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_manifest_hash(
        entries: list[ManifestEntry],
        task_id: str,
        session_id: str,
        total_bytes: int,
    ) -> str:
        """Compute deterministic hash over all manifest contents."""

        class _HashPayload(FrozenStrictModel):
            task_id: str
            session_id: str
            entries: tuple[ManifestEntry, ...]
            total_bytes: int

        payload = _HashPayload(
            task_id=task_id,
            session_id=session_id,
            entries=tuple(entries),
            total_bytes=total_bytes,
        )
        return sha256(canonical_json(payload)).hexdigest()

    @staticmethod
    def _set_file_readonly(path: Path) -> None:
        """Set a file to read-only for the current user."""
        if os.name == "nt":
            import subprocess

            user = os.environ.get("USERNAME", "")
            if not user:
                raise BundleError("cannot determine current user")
            result = subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(R)"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise BundleError("failed to set read-only permissions on file")
        else:
            try:
                path.chmod(stat.S_IREAD)
            except OSError as exc:
                raise BundleError(f"failed to set read-only: {exc}") from exc

    @staticmethod
    def _set_dir_private(path: Path) -> None:
        """Set directory to owner-only (0700 on POSIX)."""
        if os.name == "nt":
            import subprocess

            user = os.environ.get("USERNAME", "")
            if not user:
                raise BundleError("cannot determine current user")
            result = subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                raise BundleError("failed to set private permissions on directory")
        else:
            try:
                path.chmod(stat.S_IRWXU)
            except OSError as exc:
                raise BundleError(f"failed to set dir private: {exc}") from exc

    @staticmethod
    def _restore_write(path: Path) -> None:
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
                path.chmod(stat.S_IRWXU)

    @staticmethod
    def _force_remove_staging(staging_dir: Path) -> None:
        """Force-remove a staging directory tree."""
        if not staging_dir.exists():
            return
        for root, dirs, files in os.walk(str(staging_dir), topdown=False):
            for name in files:
                fpath = Path(root) / name
                if os.name == "nt":
                    import subprocess

                    user = os.environ.get("USERNAME", "")
                    if user:
                        subprocess.run(
                            ["icacls", str(fpath), "/inheritance:r", "/grant:r", f"{user}:(F)"],
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                else:
                    with contextlib.suppress(OSError):
                        fpath.chmod(stat.S_IRWXU)
                with contextlib.suppress(OSError):
                    fpath.unlink()
            for name in dirs:
                with contextlib.suppress(OSError):
                    (Path(root) / name).rmdir()
        with contextlib.suppress(OSError):
            staging_dir.rmdir()
