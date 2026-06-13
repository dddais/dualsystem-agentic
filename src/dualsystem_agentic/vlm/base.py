"""VLM planner protocol and a callable adapter for tests/custom planners."""

from __future__ import annotations

from typing import Callable, Protocol

from dualsystem_agentic.core.types import AgenticPlannerInput, ImageInput


class VLMPlanner(Protocol):
    """Protocol implemented by high-level VLM planner adapters."""

    def generate(self, planner_input: AgenticPlannerInput) -> str:
        """Return one raw planner output (expected to be a JSON object string)."""


class VisionTextGenerator(Protocol):
    """Optional extension for direct image+text prompts."""

    def generate_text(
        self,
        prompt: str,
        *,
        images: dict[str, ImageInput] | None = None,
        sampling_params: dict[str, object] | None = None,
    ) -> str:
        """Return raw model text for a custom multimodal prompt."""


class CallablePlanner:
    """Wrap any ``callable(AgenticPlannerInput) -> str`` as a ``VLMPlanner``."""

    def __init__(self, fn: Callable[[AgenticPlannerInput], str]) -> None:
        self._fn = fn
        self.calls: list[AgenticPlannerInput] = []

    def generate(self, planner_input: AgenticPlannerInput) -> str:
        self.calls.append(planner_input)
        return self._fn(planner_input)
