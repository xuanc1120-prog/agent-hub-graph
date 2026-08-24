"""Coding-agent adapter package."""

from adapters.base import BaseAgentAdapter, ConsoleSink
from adapters.cli import CliAgentRunner, CliAgentSpec, CliRunResult
from adapters.mock import MockAgentAdapter

__all__ = [
    "BaseAgentAdapter",
    "CliAgentRunner",
    "CliAgentSpec",
    "CliRunResult",
    "ConsoleSink",
    "MockAgentAdapter",
]
