"""Tests for planner JSON parsing."""

from __future__ import annotations

import json

from dualsystem_agentic.core.parser import parse_agentic_planner_output


def test_parser_preserves_wait_decision_without_requiring_a_subtask():
    output = parse_agentic_planner_output(json.dumps({"decision": "wait"}))

    assert output.parse_ok is True
    assert output.decision == "wait"
    assert output.should_execute is False
    assert output.should_execute_explicit is False


def test_parser_defaults_explicit_plan_decision_to_no_execute():
    output = parse_agentic_planner_output(
        json.dumps(
            {
                "decision": "plan",
                "subtasks": ["inspect the cup", "pick up the cup"],
                "subtask_index": 0,
            }
        )
    )

    assert output.parse_ok is True
    assert output.decision == "plan"
    assert output.current_subtask == "inspect the cup"
    assert output.should_execute is False


def test_parser_treats_complete_decision_as_task_complete():
    output = parse_agentic_planner_output(json.dumps({"decision": "complete"}))

    assert output.parse_ok is True
    assert output.decision == "complete"
    assert output.task_complete is True
    assert output.should_execute is False
