"""Read-only AgentTask handler for the phase-one MockAgent slice."""

from __future__ import annotations

import logging
from hashlib import sha256
from typing import Literal

from pydantic import Field

from adapters.base import BaseAgentAdapter
from context.context_builder import ContextBuilder
from context.task_bundle import TaskContextBundle
from protocol import (
    AgentResultStatus,
    ArtifactRef,
    ArtifactType,
    FrozenStrictModel,
    NodeOutcome,
    NodeRunStatus,
    NodeSummary,
    RiskLevel,
    TaskPackage,
    TaskStatus,
    canonical_json,
)
from storage.artifact_repository import ArtifactRecord, ArtifactRepository
from storage.workflow_run_repository import (
    WorkflowRunRepository,
    task_id_for_node,
)
from workflow.handlers.base import NodeExecutionContext, NodeHandlerResult

_LOGGER = logging.getLogger(__name__)


class _ReadOnlyRuntimePolicy(FrozenStrictModel):
    mode: Literal["read_only"] = "read_only"
    agent_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    write: Literal[False] = False
    commands: Literal[False] = False
    network: Literal[False] = False


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
    ) -> None:
        self._runs = runs
        self._artifacts = artifacts
        self._bundles = bundles
        self._adapters = dict(adapters)
        self._context_builder = ContextBuilder()
        self._max_prompt_chars = max_prompt_chars

    async def execute(self, context: object) -> NodeHandlerResult:
        if not isinstance(context, NodeExecutionContext):
            raise TypeError("AgentTaskNodeHandler requires NodeExecutionContext")
        node = context.node
        if node.requires_write:
            return _blocked(
                "write_runtime_unavailable",
                "Write AgentTask execution requires HUB-200 workspace transactions.",
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
                content=canonical_json(
                    _ReadOnlyRuntimePolicy(
                        agent_id=node.resolved_agent_id,
                        node_id=node.id,
                    )
                ),
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
                repo_path=".",
                base_commit=context.run.current_commit,
                effective_allowed_files=[],
                effective_new_files=[],
                readonly_files=list(node.effective_allowed_files or []),
                effective_allowed_commands=[],
                forbidden_actions=[
                    "Do not modify files.",
                    "Do not execute commands.",
                    "Do not access secrets or Master credentials.",
                ],
                effective_risk=node.policy_risk_floor or RiskLevel.L0,
                requires_changeset_approval=False,
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
        except Exception as exc:
            if task_started:
                await self._finish_failed_if_running(task_id, context, exc)
            return NodeHandlerResult(
                status=NodeRunStatus.FAILED,
                outcome=NodeOutcome.FAILURE,
                summary="AgentTask handler failed without exposing subprocess or path details.",
                error_code=f"handler_{type(exc).__name__.lower()}",
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
                    error_code=f"handler_{type(error).__name__.lower()}",
                    lease=context.master_lease,
                )
        except Exception:
            return


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
