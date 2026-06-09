"""Downstream executor (VLA / robot) protocol."""

from __future__ import annotations

from typing import Protocol

from dualsystem_agentic.core.types import ExecutorInput, ExecutorOutput


class ExecutorClient(Protocol):
    """Protocol implemented by VLA or robot executor adapters."""

    def execute(self, executor_input: ExecutorInput) -> ExecutorOutput:
        """Execute one current subtask and return its result."""


class NoopExecutorClient:
    """Executor that does nothing, for setups where an MCP ``execute`` tool drives the robot."""

    def execute(self, executor_input: ExecutorInput) -> ExecutorOutput:
        return ExecutorOutput.success()
