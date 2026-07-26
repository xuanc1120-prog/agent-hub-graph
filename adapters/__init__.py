"""Coding-agent adapter package."""

from adapters.base import BaseAgentAdapter, ConsoleSink
from adapters.mock import MockAgentAdapter

__all__ = ["BaseAgentAdapter", "ConsoleSink", "MockAgentAdapter"]
