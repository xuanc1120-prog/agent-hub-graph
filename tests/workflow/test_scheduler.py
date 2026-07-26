from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from protocol import NodeRunStatus, WorkflowRunStatus
from workflow.scheduler import DurableScheduler


class _RunRepositoryStub:
    def __init__(self) -> None:
        self._claimed: set[str] = set()

    async def start_and_reconcile(self, workflow_run_id: str, *, lease: object):
        return SimpleNamespace(
            workflow_run_id=workflow_run_id,
            status=WorkflowRunStatus.RUNNING,
        )

    async def claim_next(self, workflow_run_id: str, *, lease: object):
        if workflow_run_id in self._claimed:
            return None
        self._claimed.add(workflow_run_id)
        return SimpleNamespace(
            workflow_run_id=workflow_run_id,
            status=NodeRunStatus.RUNNING,
        )


class _ExecutorStub:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def execute_claimed(self, claimed: object, *, lease: object) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1


@pytest.mark.asyncio
async def test_scheduler_serializes_handlers_across_concurrent_ticks() -> None:
    runs = _RunRepositoryStub()
    executor = _ExecutorStub()
    scheduler = DurableScheduler(runs, executor, poll_interval_seconds=0.01)  # type: ignore[arg-type]

    await asyncio.gather(
        scheduler.tick("run-a", lease=object()),  # type: ignore[arg-type]
        scheduler.tick("run-b", lease=object()),  # type: ignore[arg-type]
    )

    assert executor.max_active == 1
