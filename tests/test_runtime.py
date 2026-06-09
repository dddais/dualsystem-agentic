"""Tests for the online runtime and console interaction layer."""

from __future__ import annotations

import io
import json
import base64
from collections.abc import Iterable

from dualsystem_agentic import (
    AgenticRobotLoop,
    AgenticStepResult,
    CallablePlanner,
    ConsoleInteractionLayer,
    ExecutorInput,
    ExecutorOutput,
    FakeMCPToolClient,
    OnlineAgentRuntime,
    OnlineTaskSummary,
)
from dualsystem_agentic.io.dataloader import StaticDataLoader
from dualsystem_agentic.core.types import ImageInput
from dualsystem_agentic.run_logger import JsonlRunLogger


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[ExecutorInput] = []

    def execute(self, executor_input: ExecutorInput) -> ExecutorOutput:
        self.calls.append(executor_input)
        return ExecutorOutput.success({"ack": executor_input.subtask})


class ScriptedInteraction:
    def __init__(self, tasks: Iterable[str | None]) -> None:
        self._tasks = iter(tasks)
        self.started: list[tuple[str, str]] = []
        self.steps: list[AgenticStepResult] = []
        self.summaries: list[OnlineTaskSummary] = []
        self.errors: list[tuple[str, BaseException]] = []
        self.startup_count = 0
        self.shutdown_count = 0

    def read_task(self) -> str | None:
        return next(self._tasks)

    def show_startup(self) -> None:
        self.startup_count += 1

    def show_task_started(self, task: str, session_id: str) -> None:
        self.started.append((task, session_id))

    def show_step(self, result: AgenticStepResult) -> None:
        self.steps.append(result)

    def show_task_finished(self, summary: OnlineTaskSummary) -> None:
        self.summaries.append(summary)

    def show_error(self, task: str, error: BaseException) -> None:
        self.errors.append((task, error))

    def show_shutdown(self) -> None:
        self.shutdown_count += 1


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def start_run(self) -> None:
        self.events.append(("start_run",))

    def start_session(self, task: str, session_id: str) -> None:
        self.events.append(("start_session", task, session_id))

    def log_step(self, session_id: str, result: AgenticStepResult) -> None:
        self.events.append(("step", session_id, result.step_index))

    def finish_session(
        self,
        session_id: str,
        *,
        stop_reason: str,
        task_complete: bool,
        steps: int,
    ) -> None:
        self.events.append(("finish_session", session_id, stop_reason, task_complete, steps))

    def log_error(self, session_id: str, task: str, error: BaseException) -> None:
        self.events.append(("error", session_id, task, type(error).__name__, str(error)))

    def close(self) -> None:
        self.events.append(("close",))


def _tool_client() -> FakeMCPToolClient:
    client = FakeMCPToolClient()
    client.register("monitor", lambda args: {"status": "running"}, namespace="demo")
    return client


def _loop(script_fn) -> AgenticRobotLoop:
    return AgenticRobotLoop(CallablePlanner(script_fn), _tool_client(), RecordingExecutor())


def test_online_runtime_resets_session_state_between_tasks():
    first_step_inputs: list[tuple[str, list[str], int]] = []

    def planner(planner_input):
        if planner_input.step_index == 0:
            first_step_inputs.append(
                (planner_input.task, list(planner_input.subtasks), planner_input.step_index)
            )
            return json.dumps(
                {
                    "subtasks": [f"{planner_input.task} part"],
                    "subtask_index": 0,
                }
            )
        return json.dumps({"task_complete": True})

    interaction = ScriptedInteraction(["first task", "second task", None])
    runtime = OnlineAgentRuntime(
        _loop(planner),
        interaction=interaction,
        max_steps=3,
    )

    summaries = runtime.serve_forever()

    assert [summary.task for summary in summaries] == ["first task", "second task"]
    assert all(summary.task_complete for summary in summaries)
    assert first_step_inputs == [("first task", [], 0), ("second task", [], 0)]
    assert interaction.startup_count == 1
    assert interaction.shutdown_count == 1


def test_online_runtime_marks_max_steps_and_keeps_waiting():
    def planner(planner_input):
        return json.dumps({"current_subtask": f"work on {planner_input.task}"})

    interaction = ScriptedInteraction(["never done", "next task", None])
    runtime = OnlineAgentRuntime(
        _loop(planner),
        interaction=interaction,
        max_steps=1,
    )

    summaries = runtime.serve_forever()

    assert [summary.task for summary in summaries] == ["never done", "next task"]
    assert all(summary.stop_reason == "max_steps" for summary in summaries)
    assert all(not summary.task_complete for summary in summaries)
    assert len(interaction.steps) == 2


def test_online_runtime_logs_and_reports_task_errors_then_continues():
    calls: list[str] = []

    def planner(planner_input):
        calls.append(planner_input.task)
        if planner_input.task == "bad task":
            raise RuntimeError("planner exploded")
        return json.dumps({"task_complete": True})

    interaction = ScriptedInteraction(["bad task", "good task", None])
    logger = RecordingLogger()
    runtime = OnlineAgentRuntime(
        _loop(planner),
        interaction=interaction,
        logger=logger,
        max_steps=2,
    )

    summaries = runtime.serve_forever()

    assert calls == ["bad task", "good task"]
    assert summaries[0].stop_reason == "error"
    assert summaries[0].task_complete is False
    assert summaries[1].stop_reason == "task_complete"
    assert interaction.errors[0][0] == "bad task"
    assert any(event[:4] == ("error", summaries[0].session_id, "bad task", "RuntimeError") for event in logger.events)


def test_console_interaction_ignores_empty_input_and_quits():
    output = io.StringIO()
    interaction = ConsoleInteractionLayer(
        input_stream=io.StringIO("\n  \n/quit\n"),
        output_stream=output,
    )

    assert interaction.read_task() is None


def test_console_interaction_reads_task_after_empty_lines():
    output = io.StringIO()
    interaction = ConsoleInteractionLayer(
        input_stream=io.StringIO("\n  \nmove the cup\n"),
        output_stream=output,
    )

    assert interaction.read_task() == "move the cup"


def test_online_runtime_jsonl_logger_records_each_task_session(tmp_path):
    image_payload = base64.b64encode(b"frame bytes").decode("ascii")

    def planner(planner_input):
        if planner_input.step_index == 0:
            return json.dumps({"current_subtask": f"work on {planner_input.task}"})
        return json.dumps({"task_complete": True})

    loop = AgenticRobotLoop(
        CallablePlanner(planner),
        _tool_client(),
        RecordingExecutor(),
        dataloader=StaticDataLoader(
            {"main": ImageInput(type="base64", data=image_payload, mime_type="image/jpeg")}
        ),
    )
    logger = JsonlRunLogger(tmp_path, run_id="runtime-test", save_images=True)
    interaction = ScriptedInteraction(["task one", "task two", None])
    runtime = OnlineAgentRuntime(loop, interaction=interaction, logger=logger, max_steps=2)

    summaries = runtime.serve_forever()

    assert [summary.stop_reason for summary in summaries] == ["task_complete", "task_complete"]
    events_path = tmp_path / "runtime-test" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert [event["event"] for event in events].count("session_started") == 2
    step_events = [event for event in events if event["event"] == "step"]
    assert len(step_events) == 4
    assert all("vlm_raw_output" in event for event in step_events)
    assert step_events[0]["planner_input"]["images"]["main"]["path"].endswith(".jpg")
