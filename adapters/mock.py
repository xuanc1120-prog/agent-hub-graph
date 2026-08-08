"""Deterministic adapter with an opt-in bounded demo write mode."""

from __future__ import annotations

from pathlib import Path

from adapters.base import BaseAgentAdapter, ConsoleSink
from protocol import AgentResult, AgentResultStatus, ContextPack, TaskPackage, canonical_json
from security.path_policy import PathPolicy


class MockAgentAdapter(BaseAgentAdapter):
    def __init__(self, *, demo_write_enabled: bool = False) -> None:
        self._demo_write_enabled = demo_write_enabled

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
        has_write_scope = bool(
            task_package.effective_allowed_files
            or task_package.effective_new_files
            or task_package.granted_existing_files
            or task_package.effective_allowed_commands
            or task_package.requires_changeset_approval
        )
        if has_write_scope and not self._demo_write_enabled:
            return AgentResult(
                task_id=task_package.task_id,
                node_run_id=task_package.node_run_id,
                agent_id=self.agent_id,
                status=AgentResultStatus.BLOCKED_BY_GUARD,
                summary="MockAgent refused a task with write or command capabilities.",
                error_code="mock_readonly_only",
                error_message="MockAgent is restricted to no-side-effect tasks",
            )
        if has_write_scope:
            if not task_package.effective_new_files:
                return AgentResult(
                    task_id=task_package.task_id,
                    node_run_id=task_package.node_run_id,
                    agent_id=self.agent_id,
                    status=AgentResultStatus.BLOCKED_BY_GUARD,
                    summary="Demo MockAgent only creates an explicitly allowed new file.",
                    error_code="mock_demo_requires_new_file",
                )
            repo = Path(task_package.repo_path).expanduser().resolve(strict=True)
            relative = task_package.effective_new_files[0]
            target = (
                PathPolicy(repo)
                .validate_captured_path(
                    relative,
                    must_exist=False,
                )
                .absolute_path
            )
            if target.exists():
                return AgentResult(
                    task_id=task_package.task_id,
                    node_run_id=task_package.node_run_id,
                    agent_id=self.agent_id,
                    status=AgentResultStatus.BLOCKED_BY_GUARD,
                    summary="Demo MockAgent refused to overwrite an existing path.",
                    error_code="mock_demo_target_exists",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_demo_content(relative), encoding="utf-8")
            if console_stream is not None:
                await console_stream(f"mock: created sealed path {relative}")
            return AgentResult(
                task_id=task_package.task_id,
                node_run_id=task_package.node_run_id,
                agent_id=self.agent_id,
                status=AgentResultStatus.SUCCEEDED,
                summary=f"MockAgent created the sealed demo file {relative}.",
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


def _demo_content(path: str) -> str:
    suffix = Path(path).suffix.casefold()
    if suffix in {".md", ".mdx", ".rst", ".txt"}:
        return "# Agent Hub demo change\n"
    if suffix == ".py":
        if Path(path).name.casefold().startswith("test_"):
            return "def test_agent_hub_demo():\n    assert True\n"
        return '"""Agent Hub demo change."""\n'
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return "export const agentHubDemo = true;\n"
    return "Agent Hub demo change\n"


__all__ = ["MockAgentAdapter"]
