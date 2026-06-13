"""Two-stage planner wrapper: visual scene extraction before action planning."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from dualsystem_agentic.core.types import AgenticPlannerInput, JsonDict, ensure_jsonable
from dualsystem_agentic.vlm.base import VLMPlanner, VisionTextGenerator

DEFAULT_VISUAL_SCENE_PROMPT = """You are the visual perception stage for a robot planner.
Inspect the attached robot camera images and return ONLY a JSON object.

Required JSON shape:
{
  "objects": [
    {
      "name": "<short visual name, e.g. pink cup>",
      "type": "<cup|bowl|plate|spoon|rack|other>",
      "color": "<visible color or unknown>",
      "location": "<brief spatial location>",
      "action_hint": "<what the robot should do with it for the user task>"
    }
  ],
  "target_locations": ["<visible destination such as dish rack>"],
  "summary": "<one sentence scene summary>"
}

Rules:
- Name concrete visible objects, not abstract task steps.
- Include likely task-relevant movable items.
- If uncertain, use "unknown" rather than inventing details.
- Do not include robot monitoring, status checks, or planning steps.

User task: {task}
"""


class VisualScenePrepassPlanner:
    """Add a model-generated visual scene JSON to planner input before planning."""

    def __init__(
        self,
        planner: VLMPlanner,
        *,
        prompt_template: str = DEFAULT_VISUAL_SCENE_PROMPT,
        environment_key: str = "visual_scene",
        sampling_params: dict[str, Any] | None = None,
    ) -> None:
        if not hasattr(planner, "generate_text"):
            raise TypeError("visual_scene_prepass requires a planner with generate_text()")
        self.planner = planner
        self.generator = planner  # type: ignore[assignment]
        self.prompt_template = prompt_template
        self.environment_key = environment_key
        self.sampling_params = sampling_params or {}
        self.last_visual_scene: JsonDict | None = None

    def generate(self, planner_input: AgenticPlannerInput) -> str:
        self.last_visual_scene = None
        if not planner_input.images:
            return self.planner.generate(planner_input)

        scene = self._generate_visual_scene(planner_input)
        self.last_visual_scene = scene
        enriched_environment = dict(planner_input.environment)
        enriched_environment[self.environment_key] = scene
        enriched_input = replace(planner_input, environment=enriched_environment)
        return self.planner.generate(enriched_input)

    def _generate_visual_scene(self, planner_input: AgenticPlannerInput) -> JsonDict:
        prompt = self.prompt_template.replace("{task}", planner_input.task)
        raw = self.generator.generate_text(
            prompt,
            images=planner_input.images,
            sampling_params=self.sampling_params,
        )
        parsed = _load_json_object(raw)
        if isinstance(parsed, dict):
            scene = ensure_jsonable(parsed)
            if isinstance(scene, dict):
                return scene  # type: ignore[return-value]
        return {
            "objects": [],
            "target_locations": [],
            "summary": "",
            "_raw_output": raw,
            "_parse_error": "visual scene prepass did not return a JSON object",
        }


def _load_json_object(text: str) -> Any:
    cleaned = _strip_code_fences(text or "")
    if not cleaned:
        return None
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\{.*\})", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()
