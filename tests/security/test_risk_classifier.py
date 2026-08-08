from __future__ import annotations

from protocol.common import RiskLevel
from security.risk_classifier import RiskClassifier
from workspace.change_set import ChangeSetManifest, FileAction, FileChange

_ZERO = "0" * 64
_ONE = "1" * 64


def _change(
    path: str,
    *,
    action: FileAction = FileAction.MODIFIED,
    binary: bool = False,
) -> FileChange:
    if action == FileAction.CREATED:
        return FileChange(
            action=action,
            path=path,
            after_sha256=_ONE,
            after_size=1,
            binary=binary,
        )
    if action == FileAction.DELETED:
        return FileChange(
            action=action,
            path=path,
            before_sha256=_ZERO,
            before_size=1,
            binary=binary,
        )
    return FileChange(
        action=action,
        path=path,
        before_sha256=_ZERO,
        after_sha256=_ONE,
        before_size=1,
        after_size=1,
        binary=binary,
    )


def _manifest(*changes: FileChange, ignored: tuple[str, ...] = ()) -> ChangeSetManifest:
    return ChangeSetManifest(
        base_commit="a" * 40,
        pre_state_hash=_ZERO,
        post_state_hash=_ONE,
        changes=changes,
        ignored_files_touched=ignored,
        staged_evidence_sha256=_ZERO,
        unstaged_evidence_sha256=_ZERO,
        status_evidence_sha256=_ZERO,
    )


def test_no_changes_is_l0() -> None:
    result = RiskClassifier().classify(_manifest())

    assert result.runtime_risk == RiskLevel.L0
    assert result.effective_risk == RiskLevel.L0
    assert result.requires_approval is False


def test_ordinary_small_change_is_l1() -> None:
    result = RiskClassifier().classify(_manifest(_change("src/app.py")))

    assert result.effective_risk == RiskLevel.L1


def test_configuration_tests_and_six_paths_raise_l2() -> None:
    changes = tuple(_change(f"src/file_{index}.py") for index in range(6))
    count_result = RiskClassifier().classify(_manifest(*changes))
    config_result = RiskClassifier().classify(_manifest(_change("pyproject.toml")))

    assert count_result.effective_risk == RiskLevel.L2
    assert config_result.effective_risk == RiskLevel.L2
    assert config_result.requires_approval is True


def test_delete_binary_and_security_paths_raise_l3() -> None:
    delete_result = RiskClassifier().classify(
        _manifest(_change("src/app.py", action=FileAction.DELETED))
    )
    binary_result = RiskClassifier().classify(_manifest(_change("assets/app.bin", binary=True)))
    auth_result = RiskClassifier().classify(_manifest(_change("src/auth/login.py")))
    auth_file_result = RiskClassifier().classify(_manifest(_change("src/auth.py")))

    assert delete_result.effective_risk == RiskLevel.L3
    assert binary_result.effective_risk == RiskLevel.L3
    assert auth_result.effective_risk == RiskLevel.L3
    assert auth_file_result.effective_risk == RiskLevel.L3


def test_sensitive_ignored_and_bypass_are_l4() -> None:
    sensitive = RiskClassifier().classify(_manifest(_change(".env")))
    ignored = RiskClassifier().classify(
        _manifest(_change("src/app.py"), ignored=("build/cache.bin",))
    )
    bypass = RiskClassifier().classify(
        _manifest(_change("src/app.py")),
        bypass_intent=True,
    )

    assert sensitive.blocked is True
    assert ignored.blocked is True
    assert bypass.blocked is True


def test_hints_can_raise_but_cannot_lower_runtime_risk() -> None:
    raised = RiskClassifier().classify(
        _manifest(_change("src/app.py")),
        user_hint=RiskLevel.L3,
    )
    not_lowered = RiskClassifier().classify(
        _manifest(_change("src/auth/login.py")),
        user_hint=RiskLevel.L0,
        planner_hint=RiskLevel.L1,
    )

    assert raised.effective_risk == RiskLevel.L3
    assert not_lowered.runtime_risk == RiskLevel.L3
    assert not_lowered.effective_risk == RiskLevel.L3
