"""Tests for JSONL online run logging."""

from __future__ import annotations

import base64
import json

from dualsystem_agentic.config import LoggingConfig, build_run_logger
from dualsystem_agentic.core.types import (
    AgenticPlannerInput,
    AgenticPlannerOutput,
    AgenticStepResult,
    ExecutorOutput,
    ImageInput,
    ToolResult,
)
from dualsystem_agentic.run_logger import JsonlRunLogger, NullRunLogger


def _step_result(image_payload: str) -> AgenticStepResult:
    planner_input = AgenticPlannerInput(
        task="tidy the desk",
        step_index=0,
        current_subtask=None,
        subtasks=[],
        available_tools=[
            {
                "namespace": "demo",
                "name": "monitor",
                "description": "report status",
                "parameters": {},
            }
        ],
        images={
            "main": ImageInput(
                type="base64",
                data=image_payload,
                mime_type="image/png",
            )
        },
        metadata={"robot": "demo"},
    )
    planner_output = AgenticPlannerOutput(
        raw_output='{"current_subtask": "clear cups"}',
        current_subtask="clear cups",
    )
    return AgenticStepResult(
        task="tidy the desk",
        step_index=0,
        planner_input=planner_input,
        planner_output=planner_output,
        tool_results=[ToolResult.success("monitor", {"status": "running"}, namespace="demo")],
        executor_output=ExecutorOutput.success({"accepted": True}),
        current_subtask="clear cups",
    )


def _read_events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_jsonl_logger_records_step_without_embedding_base64_images(tmp_path):
    raw_image = b"not really a png, but logger only stores bytes"
    image_payload = base64.b64encode(raw_image).decode("ascii")
    logger = JsonlRunLogger(tmp_path, run_id="test-run", save_images=True)

    logger.start_run()
    logger.start_session("tidy the desk", "session_0001")
    logger.log_step("session_0001", _step_result(image_payload))
    logger.finish_session("session_0001", stop_reason="max_steps", task_complete=False, steps=1)
    logger.close()

    events_path = tmp_path / "test-run" / "events.jsonl"
    text = events_path.read_text(encoding="utf-8")
    assert image_payload not in text

    events = _read_events(events_path)
    step_event = next(event for event in events if event["event"] == "step")
    assert step_event["planner_input"]["task"] == "tidy the desk"
    assert "You are the high-level planner" in step_event["planner_prompt"]
    assert step_event["vlm_raw_output"] == '{"current_subtask": "clear cups"}'
    assert step_event["planner_output"]["current_subtask"] == "clear cups"
    assert step_event["tool_results"][0]["tool_name"] == "monitor"
    assert step_event["executor_output"]["data"] == {"accepted": True}
    assert step_event["task_complete"] is False

    image_ref = step_event["planner_input"]["images"]["main"]
    assert image_ref["mime_type"] == "image/png"
    assert image_ref["size_bytes"] == len(raw_image)
    assert "sha256" in image_ref
    assert "data" not in image_ref
    saved_path = tmp_path / "test-run" / image_ref["path"]
    assert saved_path.read_bytes() == raw_image


def test_jsonl_logger_omits_image_payload_when_image_saving_disabled(tmp_path):
    image_payload = base64.b64encode(b"image bytes").decode("ascii")
    logger = JsonlRunLogger(tmp_path, run_id="test-run", save_images=False)

    logger.start_session("task", "session_0001")
    logger.log_step("session_0001", _step_result(image_payload))
    logger.close()

    events_path = tmp_path / "test-run" / "events.jsonl"
    text = events_path.read_text(encoding="utf-8")
    assert image_payload not in text
    step_event = next(event for event in _read_events(events_path) if event["event"] == "step")
    assert step_event["planner_input"]["images"]["main"]["data_omitted"] is True
    assert not (tmp_path / "test-run" / "session_0001" / "step_0000" / "images").exists()


def test_disabled_logging_uses_null_logger_and_creates_no_files(tmp_path):
    logger = build_run_logger(
        LoggingConfig(enabled=False, root_dir=str(tmp_path / "runs"), save_images=True)
    )

    assert isinstance(logger, NullRunLogger)
    logger.start_run()
    logger.start_session("task", "session_0001")
    logger.close()

    assert not (tmp_path / "runs").exists()
