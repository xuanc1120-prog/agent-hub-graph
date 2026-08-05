"""Deterministic ChangeSet guard, test, and risk node handlers."""

from __future__ import annotations

import asyncio
import os
from hashlib import sha256
from pathlib import Path, PurePosixPath

from pydantic import Field

from protocol import (
    ArtifactRef,
    ArtifactType,
    ChangeSetStatus,
    FrozenStrictModel,
    NodeOutcome,
    NodeRunStatus,
    NodeType,
    RiskLevel,
    SecuritySeverity,
    TestKind,
    WorkflowNode,
    canonical_json,
)
from security.command_guard import CommandGuard, CommandGuardViolation
from security.patch_guard import PatchGuard, PatchGuardDecision
from security.risk_classifier import RiskClassifier
from security.test_runner import TestRunner, TestRunResult
from storage.artifact_repository import ArtifactRecord, ArtifactRepository
from storage.change_set_repository import ChangeSetRecord, ChangeSetRepository
from storage.errors import ConcurrencyConflict
from workflow.handlers.base import NodeExecutionContext, NodeHandlerResult
from workspace.git_manager import GitManager
from workspace.lock_manager import LockManager, WorkspaceOwnerKind
from workspace.transaction import (
    CapturedWorkspaceChangeSet,
    WorkspaceTransaction,
    WorkspaceTransactionError,
    materialize_workspace_preimages,
)


class PatchGuardArtifact(FrozenStrictModel):
    change_set_id: str = Field(min_length=1, max_length=128)
    decision: str = Field(pattern=r"^(passed|rejected|quarantined)$")
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=500)
    reasons: tuple[str, ...] = Field(default_factory=tuple, max_length=500)


class CommandGuardArtifact(FrozenStrictModel):
    change_set_id: str = Field(min_length=1, max_length=128)
    approved: bool
    template_id: str | None = Field(default=None, max_length=64)
    argv: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    reason: str | None = Field(default=None, max_length=1_000)


class TestArtifact(FrozenStrictModel):
    change_set_id: str = Field(min_length=1, max_length=128)
    test_kind: TestKind
    passed: bool
    runner: TestRunResult | None = None
    reasons: tuple[str, ...] = Field(default_factory=tuple, max_length=100)


class RiskArtifact(FrozenStrictModel):
    change_set_id: str = Field(min_length=1, max_length=128)
    runtime_risk: RiskLevel
    effective_risk: RiskLevel
    blocked: bool
    requires_approval: bool
    reasons: tuple[str, ...] = Field(default_factory=tuple, max_length=100)


class _ChangeSetHandler:
    def __init__(
        self,
        change_sets: ChangeSetRepository,
        artifacts: ArtifactRepository,
    ) -> None:
        self._change_sets = change_sets
        self._artifacts = artifacts

    async def _source(
        self,
        context: NodeExecutionContext,
    ) -> tuple[WorkflowNode, ChangeSetRecord]:
        source_id = context.node.source_node_id
        if source_id is None:
            raise ValueError("system ChangeSet node has no source_node_id")
        source = next(
            (node for node in context.run.compiled_snapshot.nodes if node.id == source_id),
            None,
        )
        if source is None or source.node_type != NodeType.AGENT_TASK:
            raise ValueError("system ChangeSet node source is not an AgentTask")
        record = await self._change_sets.get_for_source_node(
            workflow_run_id=context.run.workflow_run_id,
            source_node_id=source_id,
        )
        return source, record

    async def _report(
        self,
        *,
        context: NodeExecutionContext,
        record: ChangeSetRecord,
        prefix: str,
        artifact_type: ArtifactType,
        model: FrozenStrictModel,
    ) -> ArtifactRef:
        content = canonical_json(model)
        digest = sha256(
            prefix.encode() + b"\0" + context.node_run.node_run_id.encode() + b"\0" + content
        ).hexdigest()[:24]
        artifact_id = f"{prefix}-{digest}"
        try:
            stored = await self._artifacts.create(
                artifact_id=artifact_id,
                session_id=context.run.session_id,
                task_id=record.change_set.task_id,
                artifact_type=artifact_type,
                content=content,
                redacted=True,
            )
        except Exception as create_error:
            try:
                stored, existing = await self._artifacts.get_and_verify(
                    artifact_id,
                    expected_session_id=context.run.session_id,
                    expected_task_id=record.change_set.task_id,
                )
            except Exception:
                raise create_error from None
            if (
                stored.task_id != record.change_set.task_id
                or stored.artifact_type != artifact_type.value
                or not stored.redacted
                or existing != content
            ):
                raise create_error from None
        return _artifact_ref(stored)


class PatchGuardNodeHandler(_ChangeSetHandler):
    async def execute(self, value: object) -> NodeHandlerResult:
        context = _context(value)
        source, record = await self._source(context)
        record, patch = await self._change_sets.load_patch(record.change_set.change_set_id)
        report = PatchGuard(context.session.shared_repo_path).check(
            record.document.manifest,
            patch,
            allowed_existing_files=source.effective_allowed_files or [],
            allowed_new_files=source.effective_new_files or [],
            expected_patch_sha256=record.change_set.patch_sha256,
        )
        artifact = PatchGuardArtifact(
            change_set_id=record.change_set.change_set_id,
            decision=report.decision.value,
            patch_sha256=report.patch_sha256,
            checked_paths=report.checked_paths,
            reasons=report.reasons,
        )
        ref = await self._report(
            context=context,
            record=record,
            prefix="patch-guard",
            artifact_type=ArtifactType.REPORT,
            model=artifact,
        )
        if report.decision == PatchGuardDecision.PASSED:
            await self._change_sets.transition(
                record.change_set.change_set_id,
                expected=ChangeSetStatus.CAPTURED,
                target=ChangeSetStatus.GUARD_PASSED,
                master_lease=context.master_lease,
            )
            return _success("PatchGuard accepted the canonical ChangeSet.", ref)

        target = (
            ChangeSetStatus.QUARANTINED
            if report.decision == PatchGuardDecision.QUARANTINED
            else ChangeSetStatus.GUARD_REJECTED
        )
        severity = (
            SecuritySeverity.CRITICAL
            if target == ChangeSetStatus.QUARANTINED
            else SecuritySeverity.HIGH
        )
        await self._change_sets.transition(
            record.change_set.change_set_id,
            expected=ChangeSetStatus.CAPTURED,
            target=target,
            master_lease=context.master_lease,
            reason=";".join(report.reasons)[:1_000] or "PatchGuard rejected the ChangeSet",
            severity=severity,
        )
        return _blocked("patch_guard_rejected", "PatchGuard rejected the ChangeSet.", ref)


class CommandGuardNodeHandler(_ChangeSetHandler):
    def __init__(
        self,
        change_sets: ChangeSetRepository,
        artifacts: ArtifactRepository,
        command_guard: CommandGuard,
    ) -> None:
        super().__init__(change_sets, artifacts)
        self._guard = command_guard

    async def execute(self, value: object) -> NodeHandlerResult:
        context = _context(value)
        source, record = await self._source(context)
        test = _successor_test(context)
        approved = None
        reason = None
        try:
            if test.test_argv is None:
                raise CommandGuardViolation("compiled test argv is missing")
            approved = self._guard.validate(test.test_argv)
            templates = {tuple(command) for command in (source.effective_allowed_commands or [])}
            if approved.argv not in templates:
                raise CommandGuardViolation(
                    "compiled test argv is outside the source task command scope"
                )
        except CommandGuardViolation as error:
            reason = str(error)

        artifact = CommandGuardArtifact(
            change_set_id=record.change_set.change_set_id,
            approved=approved is not None,
            template_id=approved.template_id if approved else None,
            argv=approved.argv if approved else tuple(test.test_argv or ()),
            reason=reason,
        )
        ref = await self._report(
            context=context,
            record=record,
            prefix="command-guard",
            artifact_type=ArtifactType.REPORT,
            model=artifact,
        )
        if approved is not None:
            if record.change_set.status != ChangeSetStatus.GUARD_PASSED:
                raise ConcurrencyConflict("CommandGuard requires a guard_passed ChangeSet")
            return _success("CommandGuard accepted the compiled test argv.", ref)

        await self._change_sets.transition(
            record.change_set.change_set_id,
            expected=ChangeSetStatus.GUARD_PASSED,
            target=ChangeSetStatus.TEST_FAILED,
            master_lease=context.master_lease,
            reason=reason or "CommandGuard rejected the compiled test argv",
            severity=SecuritySeverity.HIGH,
        )
        return _blocked(
            "command_guard_rejected",
            "CommandGuard rejected the compiled test argv.",
            ref,
        )


class TestNodeHandler(_ChangeSetHandler):
    def __init__(
        self,
        change_sets: ChangeSetRepository,
        artifacts: ArtifactRepository,
        git: GitManager,
        locks: LockManager,
        runner: TestRunner,
        *,
        runtime_root: Path,
        workspace_lease_ttl_seconds: int = 30,
        workspace_heartbeat_seconds: float = 5,
        max_changed_paths: int = 500,
        max_patch_bytes: int = 20 * 1024 * 1024,
        max_created_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        super().__init__(change_sets, artifacts)
        self._git = git
        self._locks = locks
        self._runner = runner
        self._runtime_root = runtime_root.expanduser().resolve(strict=False)
        self._workspace_lease_ttl_seconds = workspace_lease_ttl_seconds
        self._workspace_heartbeat_seconds = workspace_heartbeat_seconds
        self._transaction_limits = {
            "max_changed_paths": max_changed_paths,
            "max_patch_bytes": max_patch_bytes,
            "max_created_bytes": max_created_bytes,
        }

    async def execute(self, value: object) -> NodeHandlerResult:
        context = _context(value)
        _source_node, record = await self._source(context)
        if record.change_set.status != ChangeSetStatus.GUARD_PASSED:
            raise ConcurrencyConflict("Test requires a guard_passed ChangeSet")
        kind = context.node.test_kind
        if kind == TestKind.DOCS_STATIC:
            reasons = _docs_static_reasons(record)
            result = None
        elif kind == TestKind.COMMAND:
            if context.node.test_argv is None:
                reasons = ("compiled_test_argv_missing",)
                result = None
            else:
                result, reasons = await self._run_command_test(
                    context=context,
                    record=record,
                )
        else:
            reasons = ("compiled_test_kind_missing",)
            result = None

        passed = not reasons and (result is None or result.passed)
        artifact = TestArtifact(
            change_set_id=record.change_set.change_set_id,
            test_kind=kind or TestKind.COMMAND,
            passed=passed,
            runner=result,
            reasons=tuple(reasons),
        )
        ref = await self._report(
            context=context,
            record=record,
            prefix="test-result",
            artifact_type=ArtifactType.TEST_RESULT,
            model=artifact,
        )
        if not passed:
            await self._change_sets.transition(
                record.change_set.change_set_id,
                expected=ChangeSetStatus.GUARD_PASSED,
                target=ChangeSetStatus.TEST_FAILED,
                master_lease=context.master_lease,
                reason=";".join(reasons)[:1_000] or "approved test failed",
                severity=SecuritySeverity.HIGH,
            )
            return _failed("test_failed", "The approved ChangeSet test failed.", ref)

        if _is_last_test(context):
            await self._change_sets.transition(
                record.change_set.change_set_id,
                expected=ChangeSetStatus.GUARD_PASSED,
                target=ChangeSetStatus.TEST_PASSED,
                master_lease=context.master_lease,
            )
        return _success("The approved ChangeSet test passed.", ref)

    async def _run_command_test(
        self,
        *,
        context: NodeExecutionContext,
        record: ChangeSetRecord,
    ) -> tuple[TestRunResult | None, tuple[str, ...]]:
        _stored, patch = await self._change_sets.load_patch(record.change_set.change_set_id)
        preimages = await self._change_sets.load_preimages(record.change_set.change_set_id)
        operation_error: BaseException | None = None
        result: TestRunResult | None = None
        async with self._locks.hold(
            session_id=context.run.session_id,
            owner_kind=WorkspaceOwnerKind.TEST,
            owner_operation_id=context.node_run.node_run_id,
            owner_process_id=os.getpid(),
            ttl_seconds=self._workspace_lease_ttl_seconds,
            heartbeat_seconds=self._workspace_heartbeat_seconds,
        ) as held:
            operation_root = self._runtime_root / context.node_run.node_run_id
            operation_root.mkdir(parents=True, exist_ok=True)
            validation_root = self._git.create_private_temporary_directory(prefix="ah-test-")
            validation_id = validation_root.name.removeprefix("ah-test-")
            shared_repo = Path(context.session.shared_repo_path).expanduser().resolve(strict=True)
            try:
                validation_common = Path(os.path.commonpath((shared_repo, validation_root)))
            except ValueError:
                validation_common = None
            if validation_common == shared_repo:
                self._git.remove_private_temporary_directory(validation_root)
                raise ValueError("test validation root must be outside the shared repository")
            validation_repo = validation_root / "workspace" / "repo"
            transaction: WorkspaceTransaction | None = None
            try:
                held.assert_healthy()
                source = self._git.inspect_source_repository(
                    shared_repo,
                    base_ref=record.change_set.base_commit,
                )
                self._git.create_session_repository(
                    source=source,
                    destination=validation_repo,
                    session_id=validation_id,
                )
                materialize_workspace_preimages(
                    validation_repo,
                    preimages,
                    max_paths=self._transaction_limits["max_changed_paths"],
                )
                self._git.apply_check(validation_repo, patch)
                self._git.apply_patch(validation_repo, patch)
                baseline = self._git.commit_validation_baseline(validation_repo)
                transaction = WorkspaceTransaction(
                    self._git,
                    validation_repo,
                    base_commit=baseline.commit,
                    expected_branch=baseline.branch,
                    temp_directory=operation_root / f"transaction-{validation_id}",
                    seal_git_objects=True,
                    **self._transaction_limits,
                )
                transaction.begin()
                try:
                    assert context.node.test_argv is not None
                    result = await self._runner.run(
                        context.node.test_argv,
                        repo=validation_repo,
                        runtime_directory=operation_root / f"process-{validation_id}",
                    )
                except BaseException as error:
                    operation_error = error
                try:
                    verification = transaction.capture_and_restore()
                except WorkspaceTransactionError as integrity_error:
                    if operation_error is not None:
                        integrity_error.add_note(
                            f"test operation also failed: {type(operation_error).__name__}"
                        )
                    return result, ("test_workspace_integrity_failed",)
                except BaseException as restore_error:
                    if operation_error is not None:
                        restore_error.add_note(
                            f"test operation also failed: {type(operation_error).__name__}"
                        )
                    raise
                held.assert_healthy()
                if _workspace_changed(verification):
                    return result, ("test_mutated_workspace",)
            finally:
                if transaction is not None:
                    transaction.close()
                self._git.remove_private_temporary_directory(validation_root)
            if operation_error is not None:
                if isinstance(
                    operation_error,
                    (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
                ):
                    raise operation_error
                return None, (f"test_runner_error:{type(operation_error).__name__}",)
        assert result is not None
        if result.timed_out:
            return result, ("test_timeout",)
        if result.exit_code != 0:
            return result, (f"test_exit_code:{result.exit_code}",)
        return result, ()


class RiskClassifierNodeHandler(_ChangeSetHandler):
    def __init__(
        self,
        change_sets: ChangeSetRepository,
        artifacts: ArtifactRepository,
        classifier: RiskClassifier,
    ) -> None:
        super().__init__(change_sets, artifacts)
        self._classifier = classifier

    async def execute(self, value: object) -> NodeHandlerResult:
        context = _context(value)
        source, record = await self._source(context)
        if record.change_set.status != ChangeSetStatus.TEST_PASSED:
            raise ConcurrencyConflict("RiskClassifier requires a test_passed ChangeSet")
        assessment = self._classifier.classify(
            record.document.manifest,
            policy_floor=source.policy_risk_floor or RiskLevel.L0,
            user_hint=source.risk_level_hint,
        )
        artifact = RiskArtifact(
            change_set_id=record.change_set.change_set_id,
            runtime_risk=assessment.runtime_risk,
            effective_risk=assessment.effective_risk,
            blocked=assessment.blocked,
            requires_approval=assessment.requires_approval,
            reasons=assessment.reasons,
        )
        ref = await self._report(
            context=context,
            record=record,
            prefix="risk",
            artifact_type=ArtifactType.REPORT,
            model=artifact,
        )
        if assessment.blocked:
            await self._change_sets.transition(
                record.change_set.change_set_id,
                expected=ChangeSetStatus.TEST_PASSED,
                target=ChangeSetStatus.POLICY_REJECTED,
                master_lease=context.master_lease,
                reason=";".join(assessment.reasons)[:1_000] or "L4 risk rejected",
                severity=SecuritySeverity.CRITICAL,
            )
            return _blocked("l4_risk_rejected", "L4 ChangeSet risk was rejected.", ref)
        return _success(
            f"ChangeSet risk classified as {assessment.effective_risk.value}.",
            ref,
        )


def _context(value: object) -> NodeExecutionContext:
    if not isinstance(value, NodeExecutionContext):
        raise TypeError("handler requires NodeExecutionContext")
    return value


def _success(summary: str, ref: ArtifactRef) -> NodeHandlerResult:
    return NodeHandlerResult(
        status=NodeRunStatus.COMPLETED,
        outcome=NodeOutcome.SUCCESS,
        summary=summary,
        artifact_refs=(ref,),
    )


def _failed(code: str, summary: str, ref: ArtifactRef) -> NodeHandlerResult:
    return NodeHandlerResult(
        status=NodeRunStatus.FAILED,
        outcome=NodeOutcome.FAILURE,
        summary=summary,
        artifact_refs=(ref,),
        error_code=code,
    )


def _blocked(code: str, summary: str, ref: ArtifactRef) -> NodeHandlerResult:
    return NodeHandlerResult(
        status=NodeRunStatus.BLOCKED_BY_GUARD,
        outcome=NodeOutcome.BLOCKED,
        summary=summary,
        artifact_refs=(ref,),
        error_code=code,
    )


def _artifact_ref(record: ArtifactRecord) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=record.artifact_id,
        artifact_type=ArtifactType(record.artifact_type),
        relative_path=record.relative_path,
        sha256=record.sha256,
        size_bytes=record.size_bytes,
    )


def _successor_test(context: NodeExecutionContext) -> WorkflowNode:
    successor_ids = {
        edge.to_node
        for edge in context.run.compiled_snapshot.edges
        if edge.from_node == context.node.id
    }
    candidates = [
        node
        for node in context.run.compiled_snapshot.nodes
        if node.id in successor_ids
        and node.node_type == NodeType.TEST
        and node.source_node_id == context.node.source_node_id
    ]
    if len(candidates) != 1:
        raise ValueError("CommandGuard must have exactly one compiled Test successor")
    return candidates[0]


def _is_last_test(context: NodeExecutionContext) -> bool:
    source_id = context.node.source_node_id
    tests = [
        node
        for node in context.run.compiled_snapshot.nodes
        if node.node_type == NodeType.TEST and node.source_node_id == source_id
    ]
    if context.node not in tests:
        raise ValueError("current node is not a source-bound Test")
    successors = {
        edge.to_node
        for edge in context.run.compiled_snapshot.edges
        if edge.from_node == context.node.id
    }
    return not any(
        node.id in successors and node.source_node_id == source_id
        for node in context.run.compiled_snapshot.nodes
        if node.node_type in {NodeType.COMMAND_GUARD, NodeType.TEST}
    )


def _docs_static_reasons(record: ChangeSetRecord) -> tuple[str, ...]:
    reasons: list[str] = []
    allowed = {".md", ".mdx", ".rst", ".txt"}
    for change in record.document.manifest.changes:
        for value in (change.old_path, change.path):
            if value is None:
                continue
            if PurePosixPath(value).suffix.casefold() not in allowed:
                reasons.append(f"non_documentation_path:{value}")
        if change.binary:
            reasons.append(f"binary_documentation_change:{change.path}")
    if not record.document.manifest.changes:
        reasons.append("documentation_changeset_empty")
    return tuple(dict.fromkeys(reasons))


def _workspace_changed(capture: CapturedWorkspaceChangeSet) -> bool:
    manifest = capture.manifest
    return (
        bool(capture.patch_bytes)
        or bool(manifest.changes)
        or bool(manifest.created_directories)
        or bool(manifest.ignored_files_touched)
        or manifest.pre_state_hash != manifest.post_state_hash
    )


__all__ = [
    "CommandGuardNodeHandler",
    "PatchGuardNodeHandler",
    "RiskClassifierNodeHandler",
    "TestNodeHandler",
]
