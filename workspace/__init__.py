"""Session workspace, ChangeSet, and isolated Git management."""

from workspace.change_set import ChangeSetManifest, FileAction, FileChange
from workspace.git_manager import (
    CanonicalPatch,
    GitCommandError,
    GitEvidence,
    GitManager,
    GitManagerError,
    GitMetadataSeal,
    RepositoryState,
    SourceRepository,
)
from workspace.lock_manager import LockManager, WorkspaceOwnerKind
from workspace.transaction import (
    CapturedWorkspaceChangeSet,
    FilePreimage,
    FileSnapshot,
    WorkspaceNotClean,
    WorkspaceRestoreError,
    WorkspaceTransaction,
    WorkspaceTransactionError,
)

__all__ = [
    "CanonicalPatch",
    "CapturedWorkspaceChangeSet",
    "ChangeSetManifest",
    "FileAction",
    "FileChange",
    "FilePreimage",
    "FileSnapshot",
    "GitCommandError",
    "GitEvidence",
    "GitManager",
    "GitManagerError",
    "GitMetadataSeal",
    "LockManager",
    "RepositoryState",
    "SourceRepository",
    "WorkspaceNotClean",
    "WorkspaceOwnerKind",
    "WorkspaceRestoreError",
    "WorkspaceTransaction",
    "WorkspaceTransactionError",
]
