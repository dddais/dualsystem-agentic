"""Scripted VLM planner for demos and offline regression tests."""

from __future__ import annotations

import json
from typing import Any

from dualsystem_agentic.core.types import AgenticPlannerInput


class ScriptedVLMPlanner:
    """Return configured planner outputs instead of calling a real VLM.

    This planner is intentionally small: it exists for offline demos and tests
    where the rest of the online robot loop should be exercised without network
    calls or local model weights.
    """

    def __init__(
        self,
        outputs: list[Any],
        *,
        repeat_last: bool = False,
        reset_on_new_task: bool = True,
    ) -> None:
        if not outputs:
            raise ValueError("ScriptedVLMPlanner requires at least one output")
        self.outputs = [_to_output_text(output) for output in outputs]
        self.repeat_last = repeat_last
        self.reset_on_new_task = reset_on_new_task
        self.calls: list[AgenticPlannerInput] = []
        self._index = 0
        self._active_task: str | None = None

    def generate(self, planner_input: AgenticPlannerInput) -> str:
        if self.reset_on_new_task and planner_input.step_index == 0 and planner_input.task != self._active_task:
            self._index = 0
            self._active_task = planner_input.task
        self.calls.append(planner_input)
        if self._index < len(self.outputs):
            output = self.outputs[self._index]
            self._index += 1
            return output
        if self.repeat_last:
            return self.outputs[-1]
        raise RuntimeError("scripted VLM outputs exhausted")


def _to_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False)
