"""Common in-process and CLI agent adapter boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from protocol import AgentResult, ContextPack, TaskPackage

ConsoleSink = Callable[[str], Awaitable[None]]


class BaseAgentAdapter(ABC):
    @property
    @abstractmethod
    def agent_id(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def is_available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def build_prompt(self, task_package: TaskPackage, context_pack: ContextPack) -> str:
        raise NotImplementedError

    @abstractmethod
    async def run(
        self,
        task_package: TaskPackage,
        context_pack: ContextPack,
        console_stream: ConsoleSink | None = None,
    ) -> AgentResult:
        raise NotImplementedError


__all__ = ["BaseAgentAdapter", "ConsoleSink"]
