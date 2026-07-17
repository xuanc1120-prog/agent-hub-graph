"""Database-driven single-handler durable scheduler."""

from __future__ import annotations

import asyncio

from protocol import WorkflowRunStatus
from storage.leases import MasterLease
from storage.workflow_run_repository import WorkflowRunRecord, WorkflowRunRepository
from workflow.executor import GraphExecutor

_STOP_STATUSES = {
    WorkflowRunStatus.WAITING_APPROVAL,
    WorkflowRunStatus.PAUSED,
    WorkflowRunStatus.BLOCKED,
    WorkflowRunStatus.FAILED,
    WorkflowRunStatus.COMPLETED,
    WorkflowRunStatus.CANCELLED,
    WorkflowRunStatus.ORPHANED,
}


class DurableScheduler:
    def __init__(
        self,
        runs: WorkflowRunRepository,
        executor: GraphExecutor,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._runs = runs
        self._executor = executor
        self._poll_interval = poll_interval_seconds
        self._handler_lock = asyncio.Lock()

    async def tick(self, workflow_run_id: str, *, lease: MasterLease) -> WorkflowRunRecord:
        async with self._handler_lock:
            run = await self._runs.start_and_reconcile(workflow_run_id, lease=lease)
            if run.status in _STOP_STATUSES:
                return run
            claimed = await self._runs.claim_next(workflow_run_id, lease=lease)
            if claimed is not None:
                await self._executor.execute_claimed(claimed, lease=lease)
            return await self._runs.start_and_reconcile(workflow_run_id, lease=lease)

    async def tick_next(self, *, lease: MasterLease) -> WorkflowRunRecord | None:
        """Poll SQLite and execute at most one globally serialized handler."""

        workflow_run_id = await self._runs.next_schedulable_run_id(lease=lease)
        if workflow_run_id is None:
            return None
        return await self.tick(workflow_run_id, lease=lease)

    async def run_until_stable(
        self,
        workflow_run_id: str,
        *,
        lease: MasterLease,
        max_ticks: int = 1_000,
    ) -> WorkflowRunRecord:
        if max_ticks < 1:
            raise ValueError("max_ticks must be positive")
        for _ in range(max_ticks):
            run = await self.tick(workflow_run_id, lease=lease)
            if run.status in _STOP_STATUSES:
                return run
        raise RuntimeError("scheduler exceeded max_ticks without reaching a stable state")

    async def poll_run(
        self,
        workflow_run_id: str,
        *,
        lease: MasterLease,
        stop: asyncio.Event,
    ) -> WorkflowRunRecord:
        while not stop.is_set():
            run = await self.tick(workflow_run_id, lease=lease)
            if run.status in _STOP_STATUSES:
                return run
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue
        return await self._runs.get(workflow_run_id)

    async def poll(
        self,
        *,
        lease: MasterLease,
        stop: asyncio.Event,
    ) -> None:
        """Poll durable state until stopped; in-memory wakeups are optional only."""

        while not stop.is_set():
            await self.tick_next(lease=lease)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue


__all__ = ["DurableScheduler"]
