"""AgentTask handler for read-only and demo shared-write execution."""

from __future__ import annotations

import logging
import os
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field

from adapters.base import BaseAgentAdapter, ConsoleSink
from context.context_builder import ContextBuilder
from context.task_bundle import TaskContextBundle
from protocol import (
    AgentResult,
    AgentResultStatus,
    ArtifactRef,
    ArtifactType,
    ChangeSetStatus,
    ContextPack,
    FrozenStrictModel,
    NodeOutcome,
    NodeRunStatus,
    NodeSummary,
    RiskLevel,
    TaskPackage,
    TaskStatus,
    WorkflowNode,
    canonical_json,
)
from storage.artifact_repository import ArtifactRecord, ArtifactRepository
from storage.change_set_repository import ChangeSetRepository
from storage.errors import ChangeSetReconciliationRequired, LeaseLost
from storage.workflow_run_repository import (
    WorkflowRunRepository,
    task_id_for_node,
)
from workflow.handlers.base import NodeExecutionContext, NodeHandlerResult
from workspace.git_manager import GitManager
from workspace.lock_manager import LockManager, WorkspaceOwnerKind
from workspace.transaction import WorkspaceTransaction

_LOGGER = logging.getLogger(__name__)


class _AgentTaskError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ReadOnlyRuntimePolicy(FrozenStrictModel):
    mode: Literal["read_only"] = "read_only"
    agent_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    write: Literal[False] = False
    commands: Literal[False] = False
    network: Literal[False] = False


class _WriteRuntimePolicy(FrozenStrictModel):
    mode: Literal["shared_write"] = "shared_write"
    agent_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    allowed_existing_files: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    allowed_new_files: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    allowed_commands: tuple[tuple[str, ...], ...] = Field(
        default_factory=tuple,
        max_length=20,
    )
    write: Literal[True] = True
    network: Literal[False] = False
    git_push: Literal[False] = False
    git_merge: Literal[False] = False


class _MockOutput(FrozenStrictModel):
    task_id: str = Field(min_length=1, max_length=128)
    summary: str = Field(max_length=8_000)
    console: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    risks: tuple[str, ...] = Field(default_factory=tuple, max_length=50)


class AgentTaskNodeHandler:
    def __init__(
        self,
        runs: WorkflowRunRepository,
        artifacts: ArtifactRepository,
        bundles: TaskContextBundle,
        adapters: dict[str, BaseAgentAdapter],
        *,
        max_prompt_chars: int = 12_000,
        change_sets: ChangeSetRepository | None = None,
        git: GitManager | None = None,
        locks: LockManager | None = None,
        agent_runs_dir: Path | None = None,
        workspace_lease_ttl_seconds: int = 30,
        workspace_heartbeat_seconds: float = 5,
        max_changed_paths: int = 500,
        max_patch_bytes: int = 20 * 1024 * 1024,
        max_created_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        self._runs = runs
        self._artifacts = artifacts
        self._bundles = bundles
        self._adapters = dict(adapters)
        self._context_builder = ContextBuilder()
        self._max_prompt_chars = max_prompt_chars
        dependencies = (change_sets, git, locks, agent_runs_dir)
        if any(item is None for item in dependencies) and any(
            item is not None for item in dependencies
        ):
            raise ValueError("write runtime dependencies must be configured together")
        if workspace_heartbeat_seconds >= workspace_lease_ttl_seconds:
            raise ValueError("workspace heartbeat must be shorter than its lease TTL")
        self._change_sets = change_sets
        self._git = git
        self._locks = locks
        self._agent_runs_dir = (
            agent_runs_dir.expanduser().resolve(strict=False) if agent_runs_dir else None
        )
        self._workspace_lease_ttl_seconds = workspace_lease_ttl_seconds
        self._workspace_heartbeat_seconds = workspace_heartbeat_seconds
        self._transaction_limits = {
            "max_changed_paths": max_changed_paths,
            "max_patch_bytes": max_patch_bytes,
            "max_created_bytes": max_created_bytes,
        }

    @property
    def _write_runtime_available(self) -> bool:
        return all(
            item is not None
            for item in (
                self._change_sets,
                self._git,
                self._locks,
                self._agent_runs_dir,
            )
        )

    @staticmethod
    def _runtime_policy(node: WorkflowNode) -> FrozenStrictModel:
        if not node.requires_write:
            assert node.resolved_agent_id is not None
            return _ReadOnlyRuntimePolicy(
                agent_id=node.resolved_agent_id,
                node_id=node.id,
            )
        if (
            node.resolved_agent_id is None
            or node.effective_allowed_files is None
            or node.effective_new_files is None
            or node.effective_allowed_commands is None
        ):
            raise ValueError("compiled write task is missing effective policy fields")
        return _WriteRuntimePolicy(
            agent_id=node.resolved_agent_id,
            node_id=node.id,
            allowed_existing_files=tuple(node.effective_allowed_files),
            allowed_new_files=tuple(node.effective_new_files),
            allowed_commands=tuple(tuple(command) for command in node.effective_allowed_commands),
        )

    async def execute(self, context: object) -> NodeHandlerResult:
        if not isinstance(context, NodeExecutionContext):
            raise TypeError("AgentTaskNodeHandler requires NodeExecutionContext")
        node = context.node
        if node.requires_write and not self._write_runtime_available:
            return _blocked(
                "write_runtime_unavailable",
                "Write AgentTask execution requires configured workspace transactions.",
            )
        if node.task_kind is None or node.instruction is None:
            return _blocked("compiled_task_invalid", "Compiled AgentTask is incomplete.")
        if node.resolved_agent_id is None:
            return _blocked("compiled_agent_unresolved", "Compiled AgentTask has no agent.")
        adapter = self._adapters.get(node.resolved_agent_id)
        if adapter is None or not await adapter.is_available():
            return _blocked("agent_unavailable", "Compiled Agent adapter is unavailable.")

        task_id = task_id_for_node(context.node_run.node_run_id)
        bundle_created = False
        task_started = False
        try:
            policy_record = await self._artifacts.create(
                artifact_id=_artifact_id("policy", context.node_run.node_run_id),
                session_id=context.run.session_id,
                artifact_type=ArtifactType.RUNTIME_POLICY,
                content=canonical_json(self._runtime_policy(node)),
                redacted=True,
            )
            policy_ref = _artifact_ref(policy_record)
            try:
                await self._runs.create_and_start_task(
                    task_id=task_id,
                    node_run_id_value=context.node_run.node_run_id,
                    agent_id=node.resolved_agent_id,
                    base_commit=context.run.current_commit,
                    runtime_policy_artifact_id=policy_record.artifact_id,
                    lease=context.master_lease,
                )
            except BaseException:
                await self._delete_unbound_policy(policy_record.artifact_id)
                raise
            task_started = True
            predecessor_refs = await self._predecessor_refs(context, task_id)
            selected_refs = list(predecessor_refs.values())
            bundle = await self._bundles.materialize(
                task_id=task_id,
                session_id=context.run.session_id,
                artifact_refs=selected_refs,
            )
            bundle_created = True
            task = TaskPackage(
                task_id=task_id,
                session_id=context.run.session_id,
                workflow_run_id=context.run.workflow_run_id,
                node_run_id=context.node_run.node_run_id,
                node_id=node.id,
                agent_id=node.resolved_agent_id,
                task_kind=node.task_kind,
                instruction=node.instruction,
                repo_path=(str(context.session.shared_repo_path) if node.requires_write else "."),
                base_commit=context.run.current_commit,
                effective_allowed_files=(
                    list(node.effective_allowed_files or []) if node.requires_write else []
                ),
                effective_new_files=(
                    list(node.effective_new_files or []) if node.requires_write else []
                ),
                readonly_files=(
                    [] if node.requires_write else list(node.effective_allowed_files or [])
                ),
                effective_allowed_commands=(
                    list(node.effective_allowed_commands or []) if node.requires_write else []
                ),
                forbidden_actions=(
                    [
                        "Do not access secrets or Master credentials.",
                        "Do not git push, merge, commit, checkout, or change branches.",
                        "Do not modify files outside the sealed write scope.",
                    ]
                    if node.requires_write
                    else [
                        "Do not modify files.",
                        "Do not execute commands.",
                        "Do not access secrets or Master credentials.",
                    ]
                ),
                effective_risk=node.policy_risk_floor or RiskLevel.L0,
                requires_changeset_approval=bool(node.requires_changeset_approval),
                runtime_policy_ref=policy_ref,
                context_bundle_path=f"{task_id}/context",
                context_bundle_sha256=bundle.manifest.bundle_sha256,
            )
            summaries = [
                NodeSummary(
                    node_run_id=item.node_run_id,
                    status=item.status,
                    summary=(
                        f"{item.node_id}: {item.status.value}"
                        + (f"/{item.outcome.value}" if item.outcome else "")
                    ),
                    artifact_refs=(
                        [predecessor_refs[item.node_run_id]]
                        if item.node_run_id in predecessor_refs
                        else []
                    ),
                )
                for item in context.predecessors
            ]
            context_result = self._context_builder.build(
                task,
                session_goal=context.session.goal,
                current_node_title=node.title,
                current_task=node.instruction,
                upstream_summaries=summaries,
                artifact_refs=selected_refs,
                max_prompt_chars=self._max_prompt_chars,
            )
            self._context_builder.assert_no_scope_widening(context_result.pack, task)
            console: list[str] = []

            async def collect(message: str) -> None:
                if len(console) < 100:
                    console.append(message[:1_000])

            if node.requires_write:
                result = await self._execute_write_task(
                    context=context,
                    adapter=adapter,
                    task=task,
                    context_pack=context_result.pack,
                    collect=collect,
                    console=console,
                )
                bundle_created = False
                return result

            agent_result = await adapter.run(task, context_result.pack, collect)
            if (
                agent_result.task_id != task_id
                or agent_result.node_run_id != context.node_run.node_run_id
                or agent_result.agent_id != node.resolved_agent_id
            ):
                raise ValueError("AgentResult identity does not match the sealed task")
            cleanup = await self._bundles.cleanup(task_id)
            if cleanup.errors:
                await self._runs.finish_task(
                    task_id,
                    target=TaskStatus.FAILED,
                    error_code="context_cleanup_failed",
                    lease=context.master_lease,
                )
                return NodeHandlerResult(
                    status=NodeRunStatus.FAILED,
                    outcome=NodeOutcome.FAILURE,
                    summary="Read-only context bundle cleanup failed.",
                    error_code="context_cleanup_failed",
                )
            bundle_created = False
            if agent_result.status == AgentResultStatus.SUCCEEDED:
                output = _MockOutput(
                    task_id=task_id,
                    summary=agent_result.summary,
                    console=tuple(console),
                    risks=tuple(agent_result.risks),
                )
                output_record = await self._artifacts.create(
                    artifact_id=_artifact_id("mock-output", context.node_run.node_run_id),
                    session_id=context.run.session_id,
                    task_id=task_id,
                    artifact_type=ArtifactType.REPORT,
                    content=canonical_json(output),
                    redacted=True,
                )
                await self._runs.finish_task(
                    task_id,
                    target=TaskStatus.SUCCEEDED,
                    lease=context.master_lease,
                )
                return NodeHandlerResult(
                    status=NodeRunStatus.COMPLETED,
                    outcome=NodeOutcome.SUCCESS,
                    summary=agent_result.summary,
                    artifact_refs=(_artifact_ref(output_record),),
                )

            target, code = _task_failure(agent_result.status, agent_result.error_code)
            await self._runs.finish_task(
                task_id,
                target=target,
                error_code=code,
                lease=context.master_lease,
            )
            if target == TaskStatus.BLOCKED_BY_GUARD:
                return _blocked(code, agent_result.summary or "MockAgent blocked the task.")
            return NodeHandlerResult(
                status=NodeRunStatus.FAILED,
                outcome=NodeOutcome.FAILURE,
                summary=agent_result.summary or "MockAgent execution failed.",
                error_code=code,
            )
        except (ChangeSetReconciliationRequired, LeaseLost):
            raise
        except Exception as exc:
            if task_started and not node.requires_write:
                await self._finish_failed_if_running(task_id, context, exc)
            return NodeHandlerResult(
                status=NodeRunStatus.FAILED,
                outcome=NodeOutcome.FAILURE,
                summary="AgentTask handler failed without exposing subprocess or path details.",
                error_code=_handler_error_code(exc),
            )
        finally:
            if bundle_created:
                try:
                    retry = await self._bundles.cleanup(task_id)
                except Exception:
                    _LOGGER.warning("context bundle cleanup retry raised", exc_info=True)
                else:
                    if retry.errors:
                        _LOGGER.warning(
                            "context bundle cleanup retry failed for %s: %s",
                            task_id,
                            retry.errors,
                        )

    async def _execute_write_task(
        self,
        *,
        context: NodeExecutionContext,
        adapter: BaseAgentAdapter,
        task: TaskPackage,
        context_pack: ContextPack,
        collect: ConsoleSink,
        console: list[str],
    ) -> NodeHandlerResult:
        assert self._change_sets is not None
        assert self._git is not None
        assert self._locks is not None
        assert self._agent_runs_dir is not None
        capture = None
        has_changes = False
        async with self._locks.hold(
            session_id=context.run.session_id,
            owner_kind=WorkspaceOwnerKind.AGENT_TASK,
            owner_operation_id=task.task_id,
            owner_process_id=os.getpid(),
            ttl_seconds=self._workspace_lease_ttl_seconds,
            heartbeat_seconds=self._workspace_heartbeat_seconds,
        ) as held:
            try:
                transaction = WorkspaceTransaction(
                    self._git,
                    Path(context.session.shared_repo_path),
                    base_commit=context.run.current_commit,
                    expected_branch=context.session.integration_branch,
                    temp_directory=(self._agent_runs_dir / task.task_id / "workspace-transaction"),
                    seal_git_objects=True,
                    **self._transaction_limits,
                )
                await self._locks.run_fenced(
                    held,
                    session_id=context.run.session_id,
                    ttl_seconds=self._workspace_lease_ttl_seconds,
                    operation=transaction.begin,
                )
                execution_error: BaseException | None = None
                agent_result: AgentResult | None = None
                try:
                    agent_result = await adapter.run(task, context_pack, collect)
                    if (
                        agent_result.task_id != task.task_id
                        or agent_result.node_run_id != task.node_run_id
                        or agent_result.agent_id != task.agent_id
                    ):
                        execution_error = _AgentTaskError("agent_identity_mismatch")
                except BaseException as error:
                    execution_error = error

                try:
                    held.assert_healthy()
                    capture = await self._locks.run_fenced(
                        held,
                        session_id=context.run.session_id,
                        ttl_seconds=self._workspace_lease_ttl_seconds,
                        operation=transaction.capture_and_restore,
                    )
                except BaseException as capture_error:
                    if execution_error is not None:
                        capture_error.add_note(
                            f"Agent execution also failed: {type(execution_error).__name__}"
                        )
                    raise

                has_changes = bool(
                    capture.manifest.changes
                    or capture.manifest.ignored_files_touched
                    or capture.manifest.created_directories
                )
                held.assert_healthy()
                if execution_error is not None:
                    raise execution_error
                assert agent_result is not None
                if agent_result.status == AgentResultStatus.SUCCEEDED and not has_changes:
                    raise _AgentTaskError("write_agent_no_changes")

                cleanup = await self._bundles.cleanup(task.task_id)
                if cleanup.errors:
                    raise _AgentTaskError("context_cleanup_failed")

                if agent_result.status == AgentResultStatus.SUCCEEDED:
                    output = _MockOutput(
                        task_id=task.task_id,
                        summary=agent_result.summary,
                        console=tuple(console),
                        risks=tuple(agent_result.risks),
                    )
                    output_record = await self._artifacts.create(
                        artifact_id=_artifact_id(
                            "mock-output",
                            context.node_run.node_run_id,
                        ),
                        session_id=context.run.session_id,
                        task_id=task.task_id,
                        artifact_type=ArtifactType.REPORT,
                        content=canonical_json(output),
                        redacted=True,
                    )
                    assert capture is not None
                    await self._change_sets.persist_capture(
                        session_id=context.run.session_id,
                        workflow_run_id=context.run.workflow_run_id,
                        node_run_id=context.node_run.node_run_id,
                        task_id=task.task_id,
                        capture=capture,
                        master_lease=context.master_lease,
                        workspace_lease=held.lease,
                        task_target=TaskStatus.SUCCEEDED,
                        status=ChangeSetStatus.CAPTURED,
                    )
                    return NodeHandlerResult(
                        status=NodeRunStatus.COMPLETED,
                        outcome=NodeOutcome.SUCCESS,
                        summary=agent_result.summary,
                        artifact_refs=(_artifact_ref(output_record),),
                    )

                target, code = _task_failure(
                    agent_result.status,
                    agent_result.error_code,
                )
                if has_changes:
                    assert capture is not None
                    await self._change_sets.persist_capture(
                        session_id=context.run.session_id,
                        workflow_run_id=context.run.workflow_run_id,
                        node_run_id=context.node_run.node_run_id,
                        task_id=task.task_id,
                        capture=capture,
                        master_lease=context.master_lease,
                        workspace_lease=held.lease,
                        task_target=target,
                        task_error_code=code,
                        status=ChangeSetStatus.ABANDONED_PARTIAL,
                        reason="Agent execution did not complete successfully",
                    )
                else:
                    await self._runs.finish_task(
                        task.task_id,
                        target=target,
                        error_code=code,
                        lease=context.master_lease,
                        workspace_lease=held.lease,
                    )
                if target == TaskStatus.BLOCKED_BY_GUARD:
                    return _blocked(
                        code,
                        agent_result.summary or "Agent blocked the write task.",
                    )
                return NodeHandlerResult(
                    status=NodeRunStatus.FAILED,
                    outcome=NodeOutcome.FAILURE,
                    summary=agent_result.summary or "Agent execution failed.",
                    error_code=code,
                )
            except LeaseLost:
                raise
            except BaseException as error:
                if isinstance(error, ChangeSetReconciliationRequired):
                    raise
                current = await self._runs.get_task(task.task_id)
                if current.status == TaskStatus.RUNNING:
                    code = _handler_error_code(error)
                    if capture is not None and has_changes:
                        try:
                            await self._change_sets.persist_capture(
                                session_id=context.run.session_id,
                                workflow_run_id=context.run.workflow_run_id,
                                node_run_id=context.node_run.node_run_id,
                                task_id=task.task_id,
                                capture=capture,
                                master_lease=context.master_lease,
                                workspace_lease=held.lease,
                                task_target=TaskStatus.FAILED,
                                task_error_code=code,
                                status=ChangeSetStatus.ABANDONED_PARTIAL,
                                reason=f"Write task failed after capture: {code}",
                            )
                        except BaseException as finalization_error:
                            finalization_error.add_note(f"original write task error: {error!r}")
                            raise
                    else:
                        await self._runs.finish_task(
                            task.task_id,
                            target=TaskStatus.FAILED,
                            error_code=code,
                            lease=context.master_lease,
                            workspace_lease=held.lease,
                        )
                raise

    async def _predecessor_refs(
        self,
        context: NodeExecutionContext,
        task_id: str,
    ) -> dict[str, ArtifactRef]:
        refs: dict[str, ArtifactRef] = {}
        for predecessor in context.predecessors:
            if predecessor.output_artifact_id is None:
                continue
            record, content = await self._artifacts.get_and_verify(
                predecessor.output_artifact_id,
                expected_session_id=context.run.session_id,
            )
            if not record.redacted:
                raise ValueError("unredacted predecessor artifact cannot enter a task bundle")
            if record.task_id is not None and record.task_id != task_id:
                record = await self._copy_for_task(task_id, record, content)
            refs[predecessor.node_run_id] = _artifact_ref(record)
        return refs

    async def _copy_for_task(
        self,
        task_id: str,
        source: ArtifactRecord,
        content: bytes,
    ) -> ArtifactRecord:
        artifact_id = _context_copy_artifact_id(task_id, source)
        try:
            return await self._artifacts.create(
                artifact_id=artifact_id,
                session_id=source.session_id,
                task_id=task_id,
                artifact_type=ArtifactType(source.artifact_type),
                content=content,
                redacted=True,
            )
        except Exception as error:
            try:
                existing, existing_content = await self._artifacts.get_and_verify(
                    artifact_id,
                    expected_session_id=source.session_id,
                    expected_task_id=task_id,
                )
            except Exception:
                raise error from None
            if existing_content != content or existing.artifact_type != source.artifact_type:
                raise error from None
            return existing

    async def _delete_unbound_policy(self, artifact_id: str) -> None:
        try:
            await self._artifacts.delete(artifact_id)
        except Exception:
            return

    async def _finish_failed_if_running(
        self,
        task_id: str,
        context: NodeExecutionContext,
        error: Exception,
    ) -> None:
        try:
            current = await self._runs.get_task(task_id)
            if current.status == TaskStatus.RUNNING:
                await self._runs.finish_task(
                    task_id,
                    target=TaskStatus.FAILED,
                    error_code=_handler_error_code(error),
                    lease=context.master_lease,
                )
        except Exception:
            return


def _handler_error_code(error: Exception) -> str:
    if isinstance(error, _AgentTaskError):
        return error.code
    return f"handler_{type(error).__name__.lower()}"


def _artifact_id(prefix: str, node_run_id: str) -> str:
    digest = sha256(f"{prefix}\x00{node_run_id}".encode()).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _context_copy_artifact_id(task_id: str, source: ArtifactRecord) -> str:
    digest = sha256(
        f"context-copy\x00{task_id}\x00{source.artifact_id}\x00{source.sha256}".encode()
    ).hexdigest()[:24]
    return f"context-copy-{digest}"


def _artifact_ref(record: ArtifactRecord) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=record.artifact_id,
        artifact_type=ArtifactType(record.artifact_type),
        relative_path=record.relative_path,
        sha256=record.sha256,
        size_bytes=record.size_bytes,
    )


def _blocked(code: str, summary: str) -> NodeHandlerResult:
    return NodeHandlerResult(
        status=NodeRunStatus.BLOCKED_BY_GUARD,
        outcome=NodeOutcome.BLOCKED,
        summary=summary,
        error_code=code,
    )


def _task_failure(
    status: AgentResultStatus,
    error_code: str | None,
) -> tuple[TaskStatus, str]:
    mapping = {
        AgentResultStatus.BLOCKED_BY_GUARD: TaskStatus.BLOCKED_BY_GUARD,
        AgentResultStatus.PARSE_FAILED: TaskStatus.PARSE_FAILED,
        AgentResultStatus.TIMED_OUT: TaskStatus.TIMED_OUT,
        AgentResultStatus.CANCELLED: TaskStatus.CANCELLED,
        AgentResultStatus.PRIVILEGE_REQUESTED: TaskStatus.PRIVILEGE_REQUESTED,
        AgentResultStatus.FAILED: TaskStatus.FAILED,
    }
    return mapping.get(status, TaskStatus.FAILED), error_code or f"agent_{status.value}"


__all__ = ["AgentTaskNodeHandler"]
