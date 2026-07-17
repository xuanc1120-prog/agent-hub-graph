"""Deterministic no-side-effect adapter for the phase-one runtime gate."""

from __future__ import annotations

from adapters.base import BaseAgentAdapter, ConsoleSink
from protocol import AgentResult, AgentResultStatus, ContextPack, TaskPackage, canonical_json


class MockAgentAdapter(BaseAgentAdapter):
    @property
    def agent_id(self) -> str:
        return "mock"

    async def is_available(self) -> bool:
        return True

    def build_prompt(self, task_package: TaskPackage, context_pack: ContextPack) -> str:
        if context_pack.task_id != task_package.task_id:
            raise ValueError("ContextPack does not belong to TaskPackage")
        return canonical_json(context_pack).decode("utf-8")

    async def run(
        self,
        task_package: TaskPackage,
        context_pack: ContextPack,
        console_stream: ConsoleSink | None = None,
    ) -> AgentResult:
        if task_package.agent_id != self.agent_id:
            raise ValueError("TaskPackage agent_id does not match MockAgent")
        if context_pack.task_id != task_package.task_id:
            raise ValueError("ContextPack does not belong to TaskPackage")
        if (
            task_package.effective_allowed_files
            or task_package.effective_new_files
            or task_package.granted_existing_files
            or task_package.effective_allowed_commands
            or task_package.requires_changeset_approval
        ):
            return AgentResult(
                task_id=task_package.task_id,
                node_run_id=task_package.node_run_id,
                agent_id=self.agent_id,
                status=AgentResultStatus.BLOCKED_BY_GUARD,
                summary="MockAgent refused a task with write or command capabilities.",
                error_code="mock_readonly_only",
                error_message="HUB-110 MockAgent is restricted to no-side-effect tasks",
            )
        if console_stream is not None:
            await console_stream(f"mock: executing read-only task {task_package.task_id}")
        return AgentResult(
            task_id=task_package.task_id,
            node_run_id=task_package.node_run_id,
            agent_id=self.agent_id,
            status=AgentResultStatus.SUCCEEDED,
            summary=(
                f"MockAgent completed read-only {task_package.task_kind.value} task "
                f"for node {task_package.node_id}."
            ),
        )


__all__ = ["MockAgentAdapter"]
