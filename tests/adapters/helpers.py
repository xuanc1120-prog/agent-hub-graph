"""tests/adapters 内部辅助: 事件收集与断言工具。"""

from __future__ import annotations

import asyncio

from adapters.cli.events import CliEvent


class EventCollector:
    """Collect events from the runner's bounded, non-executable sink."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[CliEvent] = asyncio.Queue()

    @property
    def items(self) -> list[CliEvent]:
        return list(self.queue._queue)  # test-only queue snapshot


def payloads(events: list[CliEvent] | tuple[CliEvent, ...]) -> list[dict]:
    return [event.payload for event in events]


def types(events: list[CliEvent] | tuple[CliEvent, ...]) -> list[str]:
    return [str(event.payload.get("event_type")) for event in events]
