"""Tests for YAML config factories."""

from __future__ import annotations

import json

import pytest

from dualsystem_agentic.config import (
    AppConfig,
    InteractionConfig,
    LoopConfig,
    MCPConfig,
    VLMConfig,
    build_interaction,
    build_mcp_client,
    build_vlm,
)
from dualsystem_agentic.core.parser import parse_agentic_planner_output
from dualsystem_agentic.interaction import TuiInteractionLayer
from dualsystem_agentic.vlm.scripted import ScriptedVLMPlanner


def test_scripted_vlm_replays_script_for_each_new_task():
    planner = build_vlm(
        VLMConfig(
            provider="scripted",
            script=[
                {"current_subtask": "step one"},
                {"task_complete": True},
            ],
        )
    )

    assert isinstance(planner, ScriptedVLMPlanner)
    from dualsystem_agentic.core.types import AgenticPlannerInput

    first = planner.generate(AgenticPlannerInput(task="task a", step_index=0))
    second = planner.generate(AgenticPlannerInput(task="task a", step_index=1))
    replayed = planner.generate(AgenticPlannerInput(task="task b", step_index=0))

    assert json.loads(first)["current_subtask"] == "step one"
    assert json.loads(second)["task_complete"] is True
    assert json.loads(replayed)["current_subtask"] == "step one"


def test_fake_mcp_tools_can_be_declared_in_config():
    client = build_mcp_client(
        MCPConfig(
            provider="fake",
            tools=[
                {
                    "namespace": "mock",
                    "name": "monitor",
                    "description": "status",
                    "parameters": {"type": "object"},
                    "results": [{"status": "running"}, {"status": "success"}],
                    "echo_args": True,
                }
            ],
        )
    )

    tools = client.list_tools()
    assert tools[0]["name"] == "monitor"
    assert tools[0]["namespace"] == "mock"
    assert tools[0]["canonical_name"] == "mock___monitor"

    first = client.call_tool("monitor", {"subtask": "open drawer"})
    second = client.call_tool("monitor", {"subtask": "open drawer"})

    assert first.data == {"status": "running", "subtask": "open drawer"}
    assert second.data == {"status": "success", "subtask": "open drawer"}


def test_fake_mcp_sequence_can_cycle_for_repeated_online_tasks():
    client = build_mcp_client(
        MCPConfig(
            provider="fake",
            tools=[
                {
                    "name": "monitor",
                    "results": [{"status": "running"}, {"status": "success"}],
                    "cycle_results": True,
                }
            ],
        )
    )

    statuses = [client.call_tool("monitor", {}).data["status"] for _ in range(4)]

    assert statuses == ["running", "success", "running", "success"]


def test_parser_accepts_canonical_and_legacy_qualified_tool_names():
    canonical = parse_agentic_planner_output(
        json.dumps({"tool_calls": [{"name": "mock___monitor", "arguments": {}}]})
    )
    legacy = parse_agentic_planner_output(
        json.dumps({"tool_calls": [{"name": "mock/monitor", "arguments": {}}]})
    )

    assert canonical.tool_calls[0].namespace == "mock"
    assert canonical.tool_calls[0].name == "monitor"
    assert legacy.tool_calls[0].namespace == "mock"
    assert legacy.tool_calls[0].name == "monitor"


def test_build_tui_interaction_from_config():
    interaction = build_interaction(
        InteractionConfig(provider="tui", prompt="robot> ", max_log_lines=7)
    )

    assert isinstance(interaction, TuiInteractionLayer)
    assert interaction.prompt == "robot> "
    assert interaction.max_log_lines == 7


def test_config_expands_nested_environment_variables(monkeypatch):
    monkeypatch.setenv("ROBOT_URL", "http://robot.local")

    config = AppConfig.from_dict(
        {
            "mcp": {
                "provider": "fake",
                "tools": [
                    {
                        "name": "fetch_env",
                        "result": {"bridge": "${ROBOT_URL}"},
                    }
                ],
            }
        }
    )

    assert config.mcp.tools[0]["result"]["bridge"] == "http://robot.local"


def test_loop_tool_roles_are_optional_defaults():
    config = AppConfig.from_dict({"loop": {"max_steps": 7}})

    assert config.loop.max_steps == 7
    assert config.loop.monitor_tool_name == "monitor"
    assert config.loop.execute_tool_name == "execute"
    assert config.loop.fetch_env_tool_name == "fetch_env"


def test_loop_tool_roles_can_override_nonstandard_tool_names():
    loop = LoopConfig(
        tool_roles={
            "monitor": "check_status",
            "execute": "run_subtask",
            "fetch_env": "observe_scene",
        }
    )

    assert loop.monitor_tool_name == "check_status"
    assert loop.execute_tool_name == "run_subtask"
    assert loop.fetch_env_tool_name == "observe_scene"


def test_unknown_config_keys_raise_clear_error():
    with pytest.raises(TypeError):
        AppConfig.from_dict({"vlm": {"provider": "scripted", "bogus": True}})
