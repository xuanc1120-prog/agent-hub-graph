"""Tests for context.context_builder — scope safety, prompt budget."""

from __future__ import annotations

import pytest

from context.context_builder import (
    BudgetExceeded,
    ContextBuilder,
    ContextBuildResult,
)
from protocol import (
    ArtifactRef,
    ArtifactType,
    CommandTemplate,
    NodeRunStatus,
    NodeSummary,
    TaskKind,
)


def _make_task_package(
    *,
    task_id: str = "task-001",
    session_id: str = "sess-001",
    node_id: str = "node-001",
    allowed_files: list[str] | None = None,
    new_files: list[str] | None = None,
    allowed_commands: list[list[str]] | None = None,
) -> object:
    from protocol import TaskPackage

    return TaskPackage(
        task_id=task_id,
        session_id=session_id,
        workflow_run_id="run-001",
        node_run_id="nr-001",
        node_id=node_id,
        agent_id="opencode",
        task_kind=TaskKind.IMPLEMENT,
        instruction="Fix the bug",
        repo_path=".",
        base_commit="a" * 40,
        effective_allowed_files=allowed_files or [],
        effective_new_files=new_files or [],
        effective_allowed_commands=allowed_commands or [],
        runtime_policy_ref=ArtifactRef(
            artifact_id="rp-001",
            artifact_type=ArtifactType.RUNTIME_POLICY,
            relative_path="policies/rp-001",
            sha256="b" * 64,
            size_bytes=100,
        ),
        context_bundle_path="bundles/task-001",
        context_bundle_sha256="c" * 64,
    )


# --- Scope safety ----------------------------------------------------------


class TestScopeSafety:
    def test_effective_files_copied_verbatim(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package(
            allowed_files=["src/foo.py", "src/bar.py"],
            new_files=["src/baz.py"],
        )
        result = builder.build(
            task,
            session_goal="test goal",
            current_node_title="Fix bug",
            current_task="Fix the authentication bug",
        )
        assert result.pack.effective_allowed_files == [
            "src/foo.py",
            "src/bar.py",
        ]
        assert result.pack.effective_new_files == ["src/baz.py"]

    def test_effective_commands_copied_verbatim(self) -> None:
        builder = ContextBuilder()
        cmds: list[CommandTemplate] = [["pytest", "-q"]]
        task = _make_task_package(allowed_commands=cmds)
        result = builder.build(
            task,
            session_goal="test goal",
            current_node_title="Test",
            current_task="Run tests",
        )
        assert result.pack.effective_allowed_commands == cmds

    def test_assert_no_scope_widening_passes(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package(
            allowed_files=["a.py", "b.py"],
            new_files=["c.py"],
            allowed_commands=[["pytest"]],
        )
        result = builder.build(
            task,
            session_goal="test",
            current_node_title="T",
            current_task="Do",
        )
        ContextBuilder.assert_no_scope_widening(result.pack, task)

    def test_assert_scope_widening_detected(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package(allowed_files=["a.py"])
        result = builder.build(
            task,
            session_goal="test",
            current_node_title="T",
            current_task="Do",
        )
        widened = result.pack.model_copy(update={"effective_allowed_files": ["a.py", "extra.py"]})
        with pytest.raises(ValueError, match="widens"):
            ContextBuilder.assert_no_scope_widening(widened, task)

    def test_capability_grant_copied_verbatim(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package()
        task = task.model_copy(
            update={
                "active_capability_grant_id": "cg-001",
                "granted_existing_files": ["src/config.py"],
            }
        )
        result = builder.build(
            task,
            session_goal="test",
            current_node_title="T",
            current_task="Do",
        )
        assert result.pack.active_capability_grant_id == "cg-001"
        assert result.pack.granted_existing_files == ["src/config.py"]

    def test_granted_files_difference_detected(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package()
        task = task.model_copy(update={"granted_existing_files": ["src/config.py"]})
        result = builder.build(
            task,
            session_goal="test",
            current_node_title="T",
            current_task="Do",
        )
        widened = result.pack.model_copy(update={"granted_existing_files": ["src/other.py"]})
        with pytest.raises(ValueError, match="granted_existing_files"):
            ContextBuilder.assert_no_scope_widening(widened, task)


# --- Prompt budget enforcement ---------------------------------------------


class TestPromptBudget:
    def test_budget_enforced_total_chars(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package()
        # Use values that fit within budget including all pack fields
        goal = "A" * 100
        node_title = "Fix bug"
        task_desc = "C" * 100
        result = builder.build(
            task,
            session_goal=goal,
            current_node_title=node_title,
            current_task=task_desc,
            max_prompt_chars=5_000,
        )
        assert result.total_bytes <= 5_000

    def test_budget_trims_upstream(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package()
        summaries = [
            NodeSummary(
                node_run_id=f"nr-{i}",
                status=NodeRunStatus.COMPLETED,
                summary=f"Summary {i} " * 50,
            )
            for i in range(10)
        ]
        result = builder.build(
            task,
            session_goal="g",
            current_node_title="T",
            current_task="D",
            upstream_summaries=summaries,
            max_prompt_chars=2_000,
        )
        assert len(result.pack.upstream_summaries) < 10
        assert len(result.trimmed) > 0
        assert all(t.item_type == "upstream_summary" for t in result.trimmed)

    def test_budget_trims_artifact_refs(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package()
        refs = [
            ArtifactRef(
                artifact_id=f"art-{i}-" + "x" * 100,
                artifact_type=ArtifactType.LOG,
                relative_path=f"logs/art-{i}-" + "y" * 100,
                sha256="a" * 64,
                size_bytes=100,
            )
            for i in range(20)
        ]
        result = builder.build(
            task,
            session_goal="g",
            current_node_title="T",
            current_task="D",
            artifact_refs=refs,
            max_prompt_chars=2_000,
        )
        assert len(result.pack.artifact_refs) < 20
        assert len(result.trimmed) > 0

    def test_required_fields_exceed_budget_raises(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package()
        with pytest.raises((BudgetExceeded, ValueError)):
            builder.build(
                task,
                session_goal="A" * 5000,
                current_node_title="B" * 3000,
                current_task="C" * 3000,
                max_prompt_chars=1_000,
            )

    def test_budget_invalid_range_rejected(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package()
        with pytest.raises(ValueError, match="max_prompt_chars"):
            builder.build(
                task,
                session_goal="g",
                current_node_title="T",
                current_task="D",
                max_prompt_chars=500,
            )

    def test_empty_inputs_produce_empty_pack(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package()
        result = builder.build(
            task,
            session_goal="test",
            current_node_title="T",
            current_task="Do",
        )
        assert result.pack.upstream_summaries == []
        assert result.pack.artifact_refs == []
        assert result.trimmed == []


# --- ContextBuildResult structure ------------------------------------------


class TestBuildResult:
    def test_result_is_context_build_result(self) -> None:
        builder = ContextBuilder()
        task = _make_task_package()
        result = builder.build(
            task,
            session_goal="test",
            current_node_title="T",
            current_task="Do",
        )
        assert isinstance(result, ContextBuildResult)
        assert result.pack.task_id == "task-001"
        assert result.pack.node_id == "node-001"
        assert result.pack.task_kind == TaskKind.IMPLEMENT
        assert result.total_bytes > 0
