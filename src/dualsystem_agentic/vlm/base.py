"""VLM planner protocol and a callable adapter for tests/custom planners."""

from __future__ import annotations

from typing import Callable, Protocol

from dualsystem_agentic.core.types import AgenticPlannerInput


class VLMPlanner(Protocol):
    """Protocol implemented by high-level VLM planner adapters."""

    def generate(self, planner_input: AgenticPlannerInput) -> str:
        """Return one raw planner output (expected to be a JSON object string)."""


class CallablePlanner:
    """Wrap any ``callable(AgenticPlannerInput) -> str`` as a ``VLMPlanner``."""

    def __init__(self, fn: Callable[[AgenticPlannerInput], str]) -> None:
        self._fn = fn
        self.calls: list[AgenticPlannerInput] = []

    def generate(self, planner_input: AgenticPlannerInput) -> str:
        self.calls.append(planner_input)
        return self._fn(planner_input)
