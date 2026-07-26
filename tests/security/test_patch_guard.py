from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from security.patch_guard import PatchGuard, PatchGuardDecision
from workspace.change_set import ChangeSetManifest, FileAction, FileChange

_ZERO = "0" * 64
_ONE = "1" * 64
_COMMIT = "a" * 40


def _manifest(*changes: FileChange, ignored: tuple[str, ...] = ()) -> ChangeSetManifest:
    return ChangeSetManifest(
        base_commit=_COMMIT,
        pre_state_hash=_ZERO,
        post_state_hash=_ONE,
        changes=changes,
        ignored_files_touched=ignored,
        staged_evidence_sha256=_ZERO,
        unstaged_evidence_sha256=_ZERO,
        status_evidence_sha256=_ZERO,
    )


def _modified(path: str) -> FileChange:
    return FileChange(
        action=FileAction.MODIFIED,
        path=path,
        before_sha256=_ZERO,
        after_sha256=_ONE,
        before_size=1,
        after_size=2,
    )


def _created(path: str) -> FileChange:
    return FileChange(
        action=FileAction.CREATED,
        path=path,
        after_sha256=_ONE,
        after_size=2,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("before\n", encoding="utf-8")
    (repo / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    return repo


def test_allows_action_specific_existing_and_new_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    patch = b"canonical patch"
    report = PatchGuard(repo).check(
        _manifest(_modified("src/app.py"), _created("src/new.py")),
        patch,
        allowed_existing_files=["src/app.py"],
        allowed_new_files=["src/new.py"],
        expected_patch_sha256=sha256(patch).hexdigest(),
    )

    assert report.decision == PatchGuardDecision.PASSED


def test_rejects_modified_path_outside_existing_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = PatchGuard(repo).check(
        _manifest(_modified("src/app.py")),
        b"patch",
        allowed_existing_files=[],
        allowed_new_files=["src/new.py"],
    )

    assert report.decision == PatchGuardDecision.REJECTED
    assert "existing_scope_violation:src/app.py" in report.reasons


def test_rejects_created_path_outside_new_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = PatchGuard(repo).check(
        _manifest(_created("src/new.py")),
        b"patch",
        allowed_existing_files=["src/app.py"],
        allowed_new_files=[],
    )

    assert report.decision == PatchGuardDecision.REJECTED
    assert "new_scope_violation:src/new.py" in report.reasons


def test_quarantines_sensitive_scope_or_patch_material(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    scope_report = PatchGuard(repo).check(
        _manifest(_created(".env")),
        b"patch",
        allowed_existing_files=[],
        allowed_new_files=[".env"],
    )
    secret_report = PatchGuard(repo).check(
        _manifest(_modified("src/app.py")),
        b"-----BEGIN PRIVATE KEY-----\nsecret\n",
        allowed_existing_files=["src/app.py"],
        allowed_new_files=[],
    )

    assert scope_report.decision == PatchGuardDecision.QUARANTINED
    assert secret_report.decision == PatchGuardDecision.QUARANTINED


def test_rejects_git_control_files_even_when_in_candidate_scope(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = PatchGuard(repo).check(
        _manifest(_modified(".gitattributes")),
        b"patch",
        allowed_existing_files=[".gitattributes"],
        allowed_new_files=[],
    )

    assert report.decision == PatchGuardDecision.REJECTED
    assert any(reason.startswith("invalid_effective_scope:") for reason in report.reasons)


def test_rename_requires_existing_source_and_new_destination(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    change = FileChange(
        action=FileAction.RENAMED,
        old_path="src/app.py",
        path="src/renamed.py",
        before_sha256=_ZERO,
        after_sha256=_ONE,
        before_size=7,
        after_size=7,
    )

    passed = PatchGuard(repo).check(
        _manifest(change),
        b"patch",
        allowed_existing_files=["src/app.py"],
        allowed_new_files=["src/renamed.py"],
    )
    rejected = PatchGuard(repo).check(
        _manifest(change),
        b"patch",
        allowed_existing_files=["src/app.py"],
        allowed_new_files=["src/other.py"],
    )

    assert passed.decision == PatchGuardDecision.PASSED
    assert rejected.decision == PatchGuardDecision.REJECTED


def test_rejects_ignored_mutation_and_patch_hash_mismatch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = PatchGuard(repo).check(
        _manifest(_modified("src/app.py"), ignored=("build/cache.bin",)),
        b"patch",
        allowed_existing_files=["src/app.py"],
        allowed_new_files=[],
        expected_patch_sha256=_ZERO,
    )

    assert report.decision == PatchGuardDecision.REJECTED
    assert "ignored_files_touched" in report.reasons
    assert "patch_hash_mismatch" in report.reasons
