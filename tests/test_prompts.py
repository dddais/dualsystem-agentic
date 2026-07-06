"""Tests for planner prompt construction."""

from __future__ import annotations

from dualsystem_agentic.core.prompts import build_agentic_prompt
from dualsystem_agentic.core.types import (
    ActiveExecution,
    AgenticEvent,
    AgenticPhase,
    AgenticPlannerInput,
    ImageInput,
    SubtaskStatus,
    ToolResult,
)


def test_prompt_describes_async_execute_monitor_contract():
    prompt = build_agentic_prompt(
        AgenticPlannerInput(
            task="pick up the cup",
            phase=AgenticPhase.REASON,
            active_execution=ActiveExecution(
                subtask="pick up the cup",
                execution_id="exec-1",
                monitor_id="mon-1",
                namespace="robot",
                status="success",
            ),
            events=[
                AgenticEvent(
                    event_type="monitor_success",
                    data={
                        "execution_id": "exec-1",
                        "monitor_id": "mon-1",
                        "subtask": "pick up the cup",
                        "status": "success",
                    },
                )
            ],
            environment={"objects": {"cup_1": {"class": "cup"}}},
            images={
                "front": ImageInput(type="base64", data="abc", mime_type="image/jpeg"),
                "wrist": ImageInput(type="base64", data="def", mime_type="image/jpeg"),
            },
            metadata={"robot_type": "dual_franka"},
            subtasks=["pick cup", "place cup"],
            subtask_index=1,
            subtask_statuses=[SubtaskStatus.SUCCESS, SubtaskStatus.PENDING],
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

    assert "Execute starts an asynchronous action" in prompt
    assert "Core protocol:" in prompt
    assert "concrete physical robot action" in prompt
    assert "observing" in prompt
    assert "checking" in prompt
    assert "monitoring" in prompt
    assert "analyzing" in prompt
    assert 'set decision="execute"' in prompt
    assert "controller fills" in prompt
    assert "Do not write an execute tool_call yourself" in prompt
    assert "Executable subtask constraints:" in prompt
    assert 'Use only tools from "Available tools"' in prompt
    assert '"tool_calls": [' in prompt
    assert "Use the attached images to name visible objects" in prompt
    assert "Step guidance:" in prompt
    assert "The previous action succeeded" in prompt
    assert "Do not execute a subtask already marked success" in prompt
    assert "one object or one tightly coupled" in prompt
    assert "object group" in prompt
    assert '"grip", "pick",' in prompt
    assert "pink cup" not in prompt
    assert "conditional or vague subtasks" in prompt
    assert "monitor_success" in prompt
    assert "Keep successful subtasks" in prompt
    assert "0. [success] pick cup" in prompt
    assert "1. [pending] place cup <- current" in prompt
    assert '"decision": "plan|execute|observe|wait|replan|cancel|complete|ask_user|noop"' in prompt
    assert "should_execute" not in prompt
    assert "Scene graph" in prompt
    assert "Visual observations:" in prompt
    assert "front, wrist" in prompt
    assert "Use Scene graph only when present" in prompt
    assert "Session memory / Runtime state:" in prompt
    assert "Controller phase:" not in prompt
    assert "Step index:" not in prompt
    assert "Reason requested:" not in prompt
    assert "Active execution:" in prompt
    assert '"subtask": "pick up the cup"' in prompt
    assert '"status": "success"' in prompt
    assert "exec-1" not in prompt
    assert "mon-1" not in prompt
    assert "execution_id" not in prompt
    assert "monitor_id" not in prompt
    assert "namespace" not in prompt
    assert "started_at" not in prompt
    assert "Pending events:" in prompt
    assert "Scene graph:" in prompt
    assert "Environment state:" not in prompt
    assert "Planner-visible metadata:" in prompt


def test_prompt_guidance_for_active_execution_forbids_execute():
    prompt = build_agentic_prompt(
        AgenticPlannerInput(
            task="pick up the cup",
            active_execution=ActiveExecution(
                subtask="pick up the cup",
                subtask_index=0,
                execution_id="exec-1",
            ),
            subtasks=["pick up the cup", "place the cup"],
            subtask_index=0,
            subtask_statuses=[SubtaskStatus.RUNNING, SubtaskStatus.PENDING],
        )
    )

    assert "Active execution is running" in prompt
    assert 'Do not use decision="execute"' in prompt
    assert "Keep subtask_index on the active execution" in prompt


def test_prompt_guidance_for_initial_plan_is_short_and_action_oriented():
    prompt = build_agentic_prompt(AgenticPlannerInput(task="clear the table"))

    assert "No plan exists yet" in prompt
    assert "Return concrete physical subtasks and set subtask_index=0" in prompt
    assert 'use decision="execute"' in prompt


def test_prompt_hides_runtime_monitor_identity_arguments_from_tool_list():
    prompt = build_agentic_prompt(
        AgenticPlannerInput(
            task="clear the table",
            available_tools=[
                {
                    "namespace": "dual_franka",
                    "name": "monitor",
                    "canonical_name": "dual_franka___monitor",
                    "description": "Check current dual-Franka subtask status over HTTP; returns running / success / failed.",
                    "service_description": "Dual-Franka runtime tools exposed as dual_franka___<tool_name>.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "subtask": {"type": "string"},
                            "subtask_index": {"type": "integer"},
                            "execution_id": {"type": "string"},
                            "monitor_id": {"type": "string"},
                            "task_id": {"type": "string"},
                        },
                    },
                },
                {
                    "namespace": "dual_franka",
                    "name": "execute",
                    "canonical_name": "dual_franka___execute",
                    "description": "Start one dual-Franka subtask and return initial monitor status/ids.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "subtask": {"type": "string"},
                            "task": {"type": "string"},
                            "metadata": {"type": "object"},
                        },
                        "required": ["subtask"],
                    },
                },
                {
                    "namespace": "dual_franka",
                    "name": "stop_task",
                    "canonical_name": "dual_franka___stop_task",
                    "description": "Stop the current dual-Franka task over HTTP.",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "namespace": "dual_franka",
                    "name": "emergency_stop",
                    "canonical_name": "dual_franka___emergency_stop",
                    "description": "Emergency stop the dual-Franka runtime over HTTP.",
                    "service_description": "Dual-Franka runtime tools exposed as dual_franka___<tool_name>.",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        )
    )

    assert "dual_franka___monitor()" in prompt
    assert "check active action status" in prompt
    assert "dual_franka___execute" not in prompt
    assert "start the selected action" not in prompt
    assert "dual_franka___stop_task()" in prompt
    assert "dual_franka___emergency_stop()" in prompt
    assert "stop current task" in prompt
    assert "emergency stop" in prompt
    assert "Dual-Franka runtime tools exposed" not in prompt
    assert "over HTTP" not in prompt
    assert "execution_id?: string" not in prompt
    assert "monitor_id?: string" not in prompt
    assert "task_id?: string" not in prompt
    assert "subtask?: string" not in prompt


def test_prompt_hides_runtime_identity_fields_from_events_and_tool_results():
    prompt = build_agentic_prompt(
        AgenticPlannerInput(
            task="clear the table",
            events=[
                AgenticEvent(
                    event_type="monitor_running",
                    data={
                        "status": "running",
                        "subtask": "pick cup",
                        "subtask_index": 0,
                        "execution_id": "exec-1",
                        "monitor_id": "mon-1",
                    },
                )
            ],
            tool_results=[
                ToolResult.success(
                    "execute",
                    {
                        "executed": True,
                        "status": "running",
                        "execution_id": "exec-2",
                        "monitor_id": "mon-2",
                        "subtask": "pick cup",
                    },
                    namespace="dual_franka",
                )
            ],
        )
    )

    assert "monitor_running" in prompt
    assert "pick cup" in prompt
    assert "exec-1" not in prompt
    assert "exec-2" not in prompt
    assert "mon-1" not in prompt
    assert "mon-2" not in prompt
    assert "execution_id" not in prompt
    assert "monitor_id" not in prompt


def test_prompt_preserves_scene_graph_object_ids_in_tool_results():
    prompt = build_agentic_prompt(
        AgenticPlannerInput(
            task="clear the table",
            tool_results=[
                ToolResult.success(
                    "fetch_env",
                    {
                        "scene_graph": {
                            "objects": [
                                {"id": "cup_1", "class": "cup"},
                            ]
                        },
                        "execution_id": "exec-ignored",
                    },
                    namespace="dual_franka",
                )
            ],
        )
    )

    assert "cup_1" in prompt
    assert "exec-ignored" not in prompt
    assert "execution_id" not in prompt
