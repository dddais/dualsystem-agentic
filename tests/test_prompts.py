"""Tests for planner prompt construction."""

from __future__ import annotations

from dualsystem_agentic.core.prompts import build_agentic_prompt
from dualsystem_agentic.core.types import (
    ActiveExecution,
    AgenticEvent,
    AgenticPhase,
    AgenticPlannerInput,
    ImageInput,
)


def test_prompt_describes_async_execute_monitor_contract():
    prompt = build_agentic_prompt(
        AgenticPlannerInput(
            task="pick up the cup",
            phase=AgenticPhase.REASON,
            active_execution=ActiveExecution(subtask="pick up the cup", execution_id="exec-1"),
            events=[AgenticEvent(event_type="monitor_success", data={"execution_id": "exec-1"})],
            environment={"objects": {"cup_1": {"class": "cup"}}},
            images={
                "front": ImageInput(type="base64", data="abc", mime_type="image/jpeg"),
                "wrist": ImageInput(type="base64", data="def", mime_type="image/jpeg"),
            },
            metadata={"robot_type": "dual_franka"},
            available_tools=[
                {
                    "namespace": "robot",
                    "name": "execute",
                    "canonical_name": "robot___execute",
                    "description": "start an action",
                    "parameters": {"type": "object"},
                }
            ],
        )
    )

    assert "Treat execute as STARTING an asynchronous robot action" in prompt
    assert "Executable subtask constraints:" in prompt
    assert "concrete physical robot action" in prompt
    assert "Do NOT create subtasks for checking status" in prompt
    assert "Pick up the pink cup and place it in the dish rack." in prompt
    assert "Analyze the image to identify all items." in prompt
    assert "If Active execution is running" in prompt
    assert "monitor_success" in prompt
    assert "monitor_timeout" in prompt
    assert '"decision": "plan|execute|observe|wait|replan|cancel|complete|noop"' in prompt
    assert '"should_execute": false' in prompt
    assert "scene_graph" in prompt
    assert "Visual observations:" in prompt
    assert "front, wrist" in prompt
    assert "Use Scene graph only when it is present" in prompt
    assert "Session memory / Runtime state:" in prompt
    assert "Step index: 0" in prompt
    assert "Reason requested: false" in prompt
    assert "Active execution:" in prompt
    assert "Pending events:" in prompt
    assert "Scene graph:" in prompt
    assert "Environment state:" not in prompt
    assert "Planner-visible metadata:" in prompt
