"""Application services shared by CLI now and HTTP routes in HUB-400."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from pydantic import TypeAdapter

from adapters.mock import MockAgentAdapter
from app.config import DataPaths, Settings, ensure_data_directories
from context.planner_bundle import PlannerContextBundle
from context.task_bundle import TaskContextBundle
from master.planner import PlannerInput, RuleBasedPlanner, TemplateKind
from protocol import (
    AssignmentMode,
    AuthorGraph,
    EdgeCondition,
    EntityId,
    PlannerRunStatus,
    RiskLevel,
    SessionStatus,
    ValidateResponse,
    ValidationIssue,
    WorkflowEdge,
    WorkflowLayout,
)
from security.command_guard import CommandGuard
from security.path_policy import PathPolicy
from security.risk_classifier import RiskClassifier
from security.test_runner import TestRunner
from storage.agent_repository import AgentRepository
from storage.approval_repository import ApprovalRepository
from storage.artifact_repository import ArtifactRepository
from storage.artifact_store import ArtifactStore
from storage.change_set_repository import ChangeSetRepository
from storage.db import Database
from storage.errors import LeaseLost
from storage.event_repository import EventRecord, EventRepository
from storage.leases import (
    MasterLease,
    MasterLeaseRepository,
    WorkspaceLease,
    WorkspaceLeaseRepository,
)
from storage.planner_repository import PlannerRunRepository
from storage.repositories import (
    NewSession,
    NewWorkflow,
    SessionRecord,
    SessionRepository,
    WorkflowRecord,
    WorkflowRepository,
)
from storage.workflow_run_repository import (
    NewWorkflowRun,
    WorkflowRunRecord,
    WorkflowRunRepository,
)
from workflow.approval_manager import ApprovalManager
from workflow.capability_broker import CapabilityBroker
from workflow.compiler import WorkflowCompiler
from workflow.events import build_runtime_event_registry
from workflow.executable_validator import ExecutableValidator
from workflow.executor import GraphExecutor
from workflow.handlers.agent_task import AgentTaskNodeHandler
from workflow.handlers.approval import ApprovalNodeHandler
from workflow.handlers.factory import build_node_registry
from workflow.handlers.guards import (
    CommandGuardNodeHandler,
    PatchGuardNodeHandler,
    RiskClassifierNodeHandler,
    TestNodeHandler,
)
from workflow.handlers.merge_patch import MergePatchNodeHandler
from workflow.recovery import CancellationManager, RecoveryManager
from workflow.registry import NodeRegistry
from workflow.scheduler import DurableScheduler
from workspace.git_manager import GitManager
from workspace.lock_manager import LockManager, WorkspaceOwnerKind

_ENTITY_ID = TypeAdapter(EntityId)


@dataclass(frozen=True, slots=True)
class PlanResult:
    planner_run_id: str
    workflow_id: str
    semantic_version: int
    demo_read_only: bool


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    paths: DataPaths
    database: Database
    git: GitManager
    agents: AgentRepository
    sessions: SessionRepository
    workflows: WorkflowRepository
    planner_runs: PlannerRunRepository
    events: EventRepository
    leases: MasterLeaseRepository
    workspace_leases: WorkspaceLeaseRepository
    locks: LockManager
    artifacts: ArtifactRepository
    change_sets: ChangeSetRepository
    approvals: ApprovalRepository
    approval_manager: ApprovalManager
    capabilities: CapabilityBroker
    cancellation: CancellationManager
    recovery: RecoveryManager
    runs: WorkflowRunRepository
    registry: NodeRegistry
    scheduler: DurableScheduler


class WorkflowApplication:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.services = _build_services(settings)

    async def initialize(self) -> None:
        ensure_data_directories(self._settings)
        await self.services.database.initialize()

    @asynccontextmanager
    async def temporary_master(self):
        lease = await self.services.leases.acquire(
            instance_id=f"cli-{uuid.uuid4().hex}",
            process_id=os.getpid(),
            ttl_seconds=self._settings.master_lease_ttl_seconds,
        )
        current = lease
        stop = asyncio.Event()
        heartbeat_task: asyncio.Task[None] | None = None

        async def heartbeat() -> None:
            nonlocal current
            interval = max(1, self._settings.master_lease_ttl_seconds // 3)
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    current = await self.services.leases.heartbeat(
                        current,
                        ttl_seconds=self._settings.master_lease_ttl_seconds,
                    )

        try:
            # Recovery must be covered by the same release finally as normal
            # Master operation.  Keep the lease alive while startup verifies
            # Git, artifacts, and durable state; heartbeat preserves the same
            # fencing token even though it refreshes the expiry snapshot.
            heartbeat_task = asyncio.create_task(heartbeat())
            await self.services.recovery.recover(lease=current)
            yield current
        finally:
            stop.set()
            if heartbeat_task is not None:
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            with suppress(LeaseLost):
                await self.services.leases.release(current)

    @asynccontextmanager
    async def temporary_workspace(
        self,
        session_id: str,
        *,
        owner_kind: WorkspaceOwnerKind,
    ):
        lease = await self.services.locks.acquire(
            session_id=session_id,
            owner_kind=owner_kind,
            owner_operation_id=f"{owner_kind.value}-{uuid.uuid4().hex}",
            owner_process_id=os.getpid(),
            ttl_seconds=self._settings.workspace_lease_ttl_seconds,
        )
        current = lease
        stop = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal current
            interval = max(1, self._settings.workspace_lease_ttl_seconds // 3)
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    current = await self.services.locks.heartbeat(
                        current,
                        ttl_seconds=self._settings.workspace_lease_ttl_seconds,
                    )

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            yield lease
        finally:
            stop.set()
            try:
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            finally:
                with suppress(LeaseLost):
                    await self.services.locks.release(current)

    async def register_mock_agent(self) -> None:
        await self.services.agents.register_mock()

    async def create_session(
        self,
        *,
        repo: Path,
        goal: str,
        session_id: str | None = None,
    ) -> SessionRecord:
        resolved_repo = repo.expanduser().resolve(strict=True)
        source = self.services.git.inspect_source_repository(resolved_repo)
        if source.dirty:
            raise ValueError("source repository must be clean before session creation")
        if not goal or len(goal) > 20_000:
            raise ValueError("goal must contain 1..20000 characters")
        resolved_id = _ENTITY_ID.validate_python(session_id or f"session-{uuid.uuid4().hex}")
        directory_id = sha256(resolved_id.encode("utf-8")).hexdigest()[:32]
        shared_parent = self.services.paths.shared_workspaces / f"session-{directory_id}"
        shared = shared_parent / "repo"
        try:
            state = self.services.git.create_session_repository(
                source=source,
                destination=shared,
                session_id=resolved_id,
            )
            return await self.services.sessions.create(
                NewSession(
                    session_id=resolved_id,
                    goal=goal,
                    source_repo_path=resolved_repo,
                    shared_repo_path=shared,
                    base_commit=source.commit,
                    integration_branch=state.branch,
                    integration_head_commit=state.commit,
                )
            )
        except BaseException as operation_error:
            try:
                self.services.git.remove_session_repository(
                    shared,
                    allowed_root=self.services.paths.shared_workspaces,
                )
            except Exception as cleanup_error:
                operation_error.add_note(f"session clone cleanup failed: {cleanup_error!r}")
            raise

    async def plan(
        self,
        session_id: str,
        *,
        task_family: TemplateKind,
        lease: MasterLease,
        full_template: bool = False,
        planner_run_id: str | None = None,
        workflow_id: str | None = None,
    ) -> PlanResult:
        session = await self.services.sessions.get(session_id)
        planner = RuleBasedPlanner()
        resolved_run_id = planner_run_id or f"planner-{uuid.uuid4().hex}"
        resolved_workflow_id = workflow_id or f"workflow-{uuid.uuid4().hex}"
        await self.services.planner_runs.create(
            planner_run_id=resolved_run_id,
            session_id=session.session_id,
            planner_id=planner.planner_id,
            planner_type=planner.planner_type,
            integration_base_commit=session.integration_head_commit,
            lease=lease,
        )
        await self.services.planner_runs.transition(
            resolved_run_id,
            expected=PlannerRunStatus.PENDING,
            target=PlannerRunStatus.RUNNING,
            lease=lease,
        )
        try:
            context = PlannerContextBundle.create(
                session_id=session.session_id,
                goal=session.goal,
                integration_base_commit=session.integration_head_commit,
            )
            output = await planner.plan(
                PlannerInput(context_bundle=context, task_family=task_family)
            )
            graph = AuthorGraph(nodes=output.draft.nodes, edges=output.draft.edges)
            if not full_template:
                graph = _readonly_demo_graph(graph)
            _planner_run, workflow = await self.services.planner_runs.succeed_with_workflow(
                resolved_run_id,
                workflow_repository=self.services.workflows,
                workflow=NewWorkflow(
                    workflow_id=resolved_workflow_id,
                    session_id=session.session_id,
                    source_planner_run_id=resolved_run_id,
                    author_graph=graph,
                    layout=WorkflowLayout(),
                ),
                lease=lease,
            )
        except Exception as error:
            await self.services.planner_runs.transition(
                resolved_run_id,
                expected=PlannerRunStatus.RUNNING,
                target=PlannerRunStatus.FAILED,
                error_code=f"planning_{type(error).__name__.lower()}",
                lease=lease,
            )
            raise
        return PlanResult(
            planner_run_id=resolved_run_id,
            workflow_id=workflow.workflow_id,
            semantic_version=workflow.semantic_version,
            demo_read_only=not full_template,
        )

    async def validate(self, workflow_id: str) -> ValidateResponse:
        workflow = await self.services.workflows.get(workflow_id)
        session = await self.services.sessions.get(workflow.session_id)
        async with self.temporary_workspace(
            session.session_id,
            owner_kind=WorkspaceOwnerKind.VALIDATE,
        ) as workspace_lease:
            return await self._validate_loaded(
                workflow,
                session,
                workspace_lease=workspace_lease,
            )

    async def _validate_loaded(
        self,
        workflow: WorkflowRecord,
        session: SessionRecord,
        *,
        workspace_lease: WorkspaceLease,
    ) -> ValidateResponse:
        state = self.services.git.state(session.shared_repo_path)
        preflight_errors: list[ValidationIssue] = []
        if session.status != SessionStatus.ACTIVE:
            preflight_errors.append(
                ValidationIssue(
                    code="session_not_active",
                    message="only active sessions can be validated or run",
                )
            )
        if state.dirty:
            preflight_errors.append(
                ValidationIssue(
                    code="shared_repo_dirty",
                    message="session integration repository is not clean",
                )
            )
        if state.commit != session.integration_head_commit:
            preflight_errors.append(
                ValidationIssue(
                    code="integration_head_changed",
                    message="session integration HEAD differs from the durable session head",
                )
            )
        catalog = await self.services.agents.catalog()
        compilation = WorkflowCompiler(
            catalog,
            path_policy=PathPolicy(session.shared_repo_path),
            command_guard=CommandGuard(),
        ).compile(
            workflow.author_graph,
            integration_base_commit=session.integration_head_commit,
        )
        errors = [*preflight_errors, *compilation.errors]
        warnings = list(compilation.warnings)
        if compilation.graph is not None:
            executable = ExecutableValidator(
                self.services.registry,
                write_runtime_enabled=True,
            ).validate(compilation.graph)
            errors.extend(executable.errors)
            warnings.extend(executable.warnings)
        final_state = self.services.git.state(session.shared_repo_path)
        if final_state != state:
            errors.append(
                ValidationIssue(
                    code="shared_repo_changed_during_validation",
                    message="session integration repository changed while compiling",
                )
            )
        ok = compilation.graph is not None and not errors
        if ok:
            assert compilation.graph is not None
            await self.services.workflows.save_compiled_preview(
                workflow.workflow_id,
                compilation.graph,
                expected_semantic_version=workflow.semantic_version,
                workspace_lease=workspace_lease,
                workspace_leases=self.services.workspace_leases,
            )
        return ValidateResponse(
            ok=ok,
            errors=errors,
            warnings=warnings,
            compiled_graph=compilation.graph,
            compiled_hash=compilation.compiled_hash,
            integration_base_commit=session.integration_head_commit,
            agent_catalog_hash=catalog.catalog_hash,
            policy_version=(
                compilation.graph.policy_version if compilation.graph is not None else "demo-v1"
            ),
            source_semantic_version=workflow.semantic_version,
        )

    async def run(
        self,
        workflow_id: str,
        *,
        lease: MasterLease,
        confirmed_compiled_hash: str | None = None,
        workflow_run_id: str | None = None,
    ) -> WorkflowRunRecord:
        initial_workflow = await self.services.workflows.get(workflow_id)
        initial_session = await self.services.sessions.get(initial_workflow.session_id)
        async with self.temporary_workspace(
            initial_session.session_id,
            owner_kind=WorkspaceOwnerKind.RUN,
        ) as workspace_lease:
            workflow = await self.services.workflows.get(workflow_id)
            session = await self.services.sessions.get(workflow.session_id)
            validation = await self._validate_loaded(
                workflow,
                session,
                workspace_lease=workspace_lease,
            )
            if (
                not validation.ok
                or validation.compiled_graph is None
                or validation.compiled_hash is None
            ):
                codes = ", ".join(issue.code for issue in validation.errors[:5])
                raise ValueError(f"workflow is not executable: {codes}")
            if (
                confirmed_compiled_hash is not None
                and confirmed_compiled_hash != validation.compiled_hash
            ):
                raise ValueError("confirmed compiled hash does not match current compilation")
            final_state = self.services.git.state(session.shared_repo_path)
            if final_state.dirty or final_state.commit != validation.integration_base_commit:
                raise ValueError(
                    "session integration repository changed before run snapshot creation"
                )
            if workflow.semantic_version != validation.source_semantic_version:
                raise ValueError("workflow changed before run snapshot creation")
            catalog = await self.services.agents.catalog()
            if catalog.catalog_hash != validation.agent_catalog_hash:
                raise ValueError("agent catalog changed before run snapshot creation")
            planner_run = (
                await self.services.planner_runs.get(workflow.source_planner_run_id)
                if workflow.source_planner_run_id is not None
                else None
            )
            if planner_run is not None and planner_run.result_workflow_id != workflow.workflow_id:
                raise ValueError("workflow planner lineage is inconsistent")
            run = await self.services.runs.create(
                NewWorkflowRun(
                    workflow_run_id=workflow_run_id or f"run-{uuid.uuid4().hex}",
                    workflow=workflow,
                    compiled_graph=validation.compiled_graph,
                    agent_catalog=catalog,
                    current_commit=validation.integration_base_commit,
                    planner_run_id=workflow.source_planner_run_id,
                    planner_id=planner_run.planner_id if planner_run is not None else None,
                    planner_model=(planner_run.planner_model if planner_run is not None else None),
                ),
                lease=lease,
                workspace_lease=workspace_lease,
            )
        return await self.services.scheduler.run_until_stable(
            run.workflow_run_id,
            lease=lease,
        )

    async def show_workflow(self, workflow_id: str) -> WorkflowRecord:
        return await self.services.workflows.get(workflow_id)

    async def show_run(self, workflow_run_id: str) -> WorkflowRunRecord:
        return await self.services.runs.get(workflow_run_id)

    async def show_events(
        self,
        *,
        workflow_run_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[EventRecord]:
        if (workflow_run_id is None) == (session_id is None):
            raise ValueError("provide exactly one of workflow_run_id or session_id")
        if workflow_run_id is not None:
            return await self.services.events.list_all_by_run(workflow_run_id, page_size=limit)
        assert session_id is not None
        return await self.services.events.list_all_by_session(session_id, page_size=limit)


def _readonly_demo_graph(graph: AuthorGraph) -> AuthorGraph:
    input_node = next(node for node in graph.nodes if node.node_type.value == "input")
    output_node = next(node for node in graph.nodes if node.node_type.value == "output")
    task = next(node for node in graph.nodes if node.node_type.value == "agent_task")
    task = task.model_copy(
        update={
            "assigned_agent": "mock",
            "assignment_mode": AssignmentMode.LOCKED,
            "requires_write": False,
            "new_files_candidate": [],
            "allowed_commands_candidate": [],
            "risk_level_hint": RiskLevel.L0,
        },
        deep=True,
    )
    return AuthorGraph(
        nodes=[input_node, task, output_node],
        edges=[
            WorkflowEdge(
                id="demo-edge-input-task",
                from_node=input_node.id,
                to_node=task.id,
                condition=EdgeCondition.SUCCESS,
            ),
            WorkflowEdge(
                id="demo-edge-task-output",
                from_node=task.id,
                to_node=output_node.id,
                condition=EdgeCondition.SUCCESS,
            ),
        ],
    )


def _build_services(settings: Settings) -> RuntimeServices:
    paths = DataPaths.from_settings(settings)
    database = Database(paths.database)
    git = GitManager(
        paths.profiles / "git",
        max_tracked_paths=settings.max_source_tracked_paths,
        max_git_bytes=settings.max_source_git_bytes,
    )
    agents = AgentRepository(database)
    sessions = SessionRepository(database)
    leases = MasterLeaseRepository(database)
    workspace_leases = WorkspaceLeaseRepository(database)
    locks = LockManager(workspace_leases)
    workflows = WorkflowRepository(database)
    events = EventRepository(database, build_runtime_event_registry())
    planner_runs = PlannerRunRepository(database, events, leases)
    runs = WorkflowRunRepository(database, events, leases, workspace_leases)
    artifacts = ArtifactRepository(
        database,
        ArtifactStore(paths.artifacts),
        max_artifact_bytes=settings.max_artifact_bytes,
        max_session_artifact_bytes=settings.max_session_artifact_bytes,
    )
    command_guard = CommandGuard()
    change_sets = ChangeSetRepository(database, artifacts, events, leases, locks)
    approvals = ApprovalRepository(
        database,
        events,
        leases,
        runs,
        grant_ttl_seconds=settings.capability_grant_ttl_seconds,
    )
    approval_manager = ApprovalManager(
        approvals,
        change_sets,
        runs,
        ttl_seconds=settings.changeset_approval_ttl_seconds,
        artifacts=artifacts,
    )
    capabilities = CapabilityBroker(
        approvals,
        ttl_seconds=settings.privilege_approval_ttl_seconds,
        grant_ttl_seconds=settings.capability_grant_ttl_seconds,
    )
    test_runner = TestRunner(
        command_guard,
        timeout_seconds=settings.agent_default_timeout_seconds,
        max_output_bytes=settings.max_console_bytes_per_run,
    )
    bundles = TaskContextBundle(
        artifacts,
        paths.agent_runs,
        max_bundle_bytes=settings.max_artifact_bytes,
        ttl_seconds=settings.agent_default_timeout_seconds,
    )
    agent_handler = AgentTaskNodeHandler(
        runs,
        artifacts,
        bundles,
        {"mock": MockAgentAdapter(demo_write_enabled=True)},
        change_sets=change_sets,
        git=git,
        locks=locks,
        capabilities=capabilities,
        agent_runs_dir=paths.agent_runs,
        workspace_lease_ttl_seconds=settings.workspace_lease_ttl_seconds,
        workspace_heartbeat_seconds=settings.lease_heartbeat_seconds,
        max_changed_paths=settings.max_changed_paths,
        max_patch_bytes=settings.max_patch_bytes,
        max_created_bytes=settings.max_task_created_bytes,
    )
    registry = build_node_registry(
        agent_handler,
        patch_guard_handler=PatchGuardNodeHandler(change_sets, artifacts, approvals),
        command_guard_handler=CommandGuardNodeHandler(
            change_sets,
            artifacts,
            command_guard,
        ),
        test_handler=TestNodeHandler(
            change_sets,
            artifacts,
            git,
            locks,
            test_runner,
            runtime_root=paths.agent_runs,
            workspace_lease_ttl_seconds=settings.workspace_lease_ttl_seconds,
            workspace_heartbeat_seconds=settings.lease_heartbeat_seconds,
            max_changed_paths=settings.max_changed_paths,
            max_patch_bytes=settings.max_patch_bytes,
            max_created_bytes=settings.max_task_created_bytes,
        ),
        approval_handler=ApprovalNodeHandler(approval_manager),
        merge_patch_handler=MergePatchNodeHandler(
            runs,
            change_sets,
            approvals,
            git,
            locks,
            artifacts=artifacts,
            workspace_lease_ttl_seconds=settings.workspace_lease_ttl_seconds,
            workspace_heartbeat_seconds=settings.lease_heartbeat_seconds,
        ),
        risk_handler=RiskClassifierNodeHandler(
            change_sets,
            artifacts,
            RiskClassifier(),
        ),
    )
    cancellation = CancellationManager(runs, approvals, capabilities, events)
    recovery = RecoveryManager(
        runs,
        sessions,
        approvals,
        change_sets,
        events,
        git,
        locks,
        artifacts,
        capabilities=capabilities,
    )
    executor = GraphExecutor(runs, sessions, artifacts, registry)
    scheduler = DurableScheduler(
        runs,
        executor,
        poll_interval_seconds=settings.scheduler_poll_ms / 1_000,
    )
    return RuntimeServices(
        paths=paths,
        database=database,
        git=git,
        agents=agents,
        sessions=sessions,
        workflows=workflows,
        planner_runs=planner_runs,
        events=events,
        leases=leases,
        workspace_leases=workspace_leases,
        locks=locks,
        artifacts=artifacts,
        change_sets=change_sets,
        approvals=approvals,
        approval_manager=approval_manager,
        capabilities=capabilities,
        cancellation=cancellation,
        recovery=recovery,
        runs=runs,
        registry=registry,
        scheduler=scheduler,
    )


__all__ = ["PlanResult", "RuntimeServices", "WorkflowApplication"]
