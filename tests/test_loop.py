"""End-to-end loop tests using a scripted planner, fake MCP, and fake executor."""

from __future__ import annotations

import json
import time

from dualsystem_agentic import (
    ActiveExecution,
    AgenticRobotLoop,
    AgenticPhase,
    AgenticSessionState,
    CallablePlanner,
    ExecutorInput,
    ExecutorOutput,
    FakeMCPToolClient,
    MonitorStatus,
    SubtaskStatus,
)
from dualsystem_agentic.vlm.visual_scene_prepass import VisualScenePrepassPlanner


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[ExecutorInput] = []

    def execute(self, executor_input: ExecutorInput) -> ExecutorOutput:
        self.calls.append(executor_input)
        return ExecutorOutput.success({"ack": executor_input.subtask})


def _tool_client() -> FakeMCPToolClient:
    client = FakeMCPToolClient()
    client.register("fetch_env", lambda args: {"objects": ["radio"]}, namespace="demo_robot")
    client.register(
        "monitor",
        lambda args: {"status": "running", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )
    client.register("execute", lambda args: {"executed": True}, namespace="demo_robot")
    return client


def _planner(script: list[str]) -> CallablePlanner:
    outputs = iter(script)

    def fn(_planner_input) -> str:
        return next(outputs)

    return CallablePlanner(fn)


class VisualScenePlanner:
    environment_key = "visual_scene"

    def __init__(self) -> None:
        self.last_visual_scene = None

    def generate(self, planner_input) -> str:
        self.last_visual_scene = {
            "objects": [{"name": "pink cup", "type": "cup"}],
            "target_locations": ["dish rack"],
        }
        return json.dumps(
            {
                "decision": "plan",
                "subtasks": ["Pick up the pink cup and place it in the dish rack."],
                "subtask_index": 0,
            }
        )


def test_planner_visual_scene_is_recorded_in_step_input_and_state():
    planner = VisualScenePlanner()
    loop = AgenticRobotLoop(planner, _tool_client(), RecordingExecutor())

    result, state = loop.step("organize the desk")

    assert "visual_scene" not in result.planner_input.environment
    assert state.environment["visual_scene"]["target_locations"] == ["dish rack"]


class TextGeneratingScenePlanner:
    def generate_text(self, prompt, *, images=None, sampling_params=None):
        return json.dumps(
            {
                "objects": [{"name": "pink cup", "type": "cup"}],
                "target_locations": ["dish rack"],
                "summary": "pink cup near dish rack",
            }
        )

    def generate(self, planner_input):
        return json.dumps(
            {
                "decision": "plan",
                "subtasks": ["Pick up the pink cup and place it in the dish rack."],
                "subtask_index": 0,
            }
        )


def test_visual_scene_prepass_records_actual_enriched_planner_input():
    planner = VisualScenePrepassPlanner(TextGeneratingScenePlanner())
    loop = AgenticRobotLoop(planner, _tool_client(), RecordingExecutor())

    result, state = loop.step(
        "organize the desk",
        images={"front": {"type": "base64", "data": "abc", "mime_type": "image/jpeg"}},
    )

    assert result.planner_input.environment["visual_scene"]["objects"][0]["name"] == "pink cup"
    assert state.environment["visual_scene"]["target_locations"] == ["dish rack"]


def test_execute_decision_autocalls_mcp_execute_with_fetch_env_and_monitor():
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "tool_calls": [
                        {"namespace": "demo_robot", "name": "fetch_env", "arguments": {}},
                        {
                            "namespace": "demo_robot",
                            "name": "monitor",
                            "arguments": {"subtask": "turn on the radio"},
                        },
                    ],
                    "subtasks": ["turn on the radio"],
                    "subtask_index": 0,
                    "task_complete": False,
                }
            )
        ]
    )
    executor = RecordingExecutor()
    loop = AgenticRobotLoop(planner, _tool_client(), executor)

    result, state = loop.step("turn on the radio and tidy up")

    assert result.current_subtask == "turn on the radio"
    assert result.monitor_status is MonitorStatus.RUNNING
    assert state.environment["objects"] == ["radio"]
    assert [tool_result.tool_name for tool_result in result.tool_results] == [
        "fetch_env",
        "execute",
        "monitor",
    ]
    assert executor.calls == []


def test_plan_only_output_selects_first_pending_subtask_and_requests_reason():
    planner = _planner(
        [
            json.dumps(
                {
                    "subtasks": [
                        "Pick up the blue bowl",
                        "Pick up the pink bowl",
                        "Place all items into the metal basket",
                    ],
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, _tool_client(), RecordingExecutor())

    result, state = loop.step("clean the table")

    assert result.parse_ok is True
    assert result.current_subtask == "Pick up the blue bowl"
    assert result.subtask_index == 0
    assert result.subtask_statuses == [
        SubtaskStatus.PENDING,
        SubtaskStatus.PENDING,
        SubtaskStatus.PENDING,
    ]
    assert result.tool_results == []
    assert result.reason_requested is True
    assert state.current_subtask == "Pick up the blue bowl"
    assert state.subtask_index == 0
    assert state.reason_requested is True


def test_monitor_feedback_flows_into_next_planner_input():
    client = FakeMCPToolClient()
    client.register(
        "monitor",
        lambda args: {"status": "success", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )
    planner_inputs = []

    def fn(planner_input) -> str:
        planner_inputs.append(planner_input)
        if len(planner_inputs) > 1:
            return json.dumps({"task_complete": True})
        return json.dumps(
            {
                "tool_calls": [
                    {"namespace": "demo_robot", "name": "monitor", "arguments": {}}
                ],
                "subtask_index": 0,
            }
        )

    planner = CallablePlanner(fn)
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())
    state = AgenticSessionState(
        task="task",
        subtasks=["grasp cup"],
        subtask_statuses=[SubtaskStatus.RUNNING],
        current_subtask="grasp cup",
        subtask_index=0,
        monitor_status=MonitorStatus.RUNNING,
        awaiting_monitor=True,
        monitor_namespace="demo_robot",
        active_execution=ActiveExecution(
            subtask="grasp cup",
            subtask_index=0,
            execution_id="exec-1",
            monitor_id="mon-1",
            namespace="demo_robot",
            status=MonitorStatus.RUNNING.value,
        ),
    )

    _, state = loop.step("task", state, reason_interval_s=0)
    assert state.monitor_status is MonitorStatus.SUCCESS

    loop.step("task", state, reason_interval_s=0)
    assert planner_inputs[-1].events[-1].event_type == "monitor_success"


def test_task_complete_short_circuits_tools_and_executor():
    planner = _planner([json.dumps({"task_complete": True})])
    executor = RecordingExecutor()
    loop = AgenticRobotLoop(planner, _tool_client(), executor)

    result, _ = loop.step("done task")

    assert result.task_complete is True
    assert result.tool_results == []
    assert executor.calls == []


def test_task_complete_with_tool_calls_is_rejected_without_calling_tools():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "task_complete": True,
                    "tool_calls": [
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"subtask": "push button"}}
                    ],
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("done task")

    assert result.task_complete is False
    assert result.parse_ok is False
    assert "task_complete=true with tool_calls" in (result.parse_error or "")
    assert result.tool_results == []
    assert execute_calls == []
    assert state.phase is AgenticPhase.ERROR


def test_task_complete_with_execute_decision_is_rejected():
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "task_complete": True,
                    "subtasks": ["pick cup"],
                    "subtask_index": 0,
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, _tool_client(), RecordingExecutor())

    result, state = loop.step("clean the table")

    assert result.task_complete is False
    assert result.parse_ok is False
    assert "cannot combine task_complete=true" in (result.parse_error or "")
    assert result.tool_results == []
    assert state.phase is AgenticPhase.ERROR


def test_mcp_execute_skips_downstream_executor():
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "push button",
                }
            )
        ]
    )
    executor = RecordingExecutor()
    loop = AgenticRobotLoop(planner, _tool_client(), executor)

    result, state = loop.step("task")

    assert any(tr.tool_name == "execute" for tr in result.tool_results)
    assert executor.calls == []
    assert state.awaiting_monitor is True
    assert state.active_execution is not None
    assert state.active_execution.status == "running"


def test_planner_monitor_call_without_active_execution_is_rejected_before_tool_call():
    client = FakeMCPToolClient()
    monitor_calls = []
    client.register(
        "monitor",
        lambda args: monitor_calls.append(dict(args)) or {"status": "failed"},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"namespace": "demo_robot", "name": "monitor", "arguments": {}}],
                    "subtasks": [
                        "Pick up the red cup and place it on the black desk.",
                        "Pick up the blue bowl and place it on the black desk.",
                    ],
                    "subtask_index": 0,
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table")

    assert result.parse_ok is False
    assert "no active execution is running" in (result.parse_error or "")
    assert result.tool_results == []
    assert monitor_calls == []
    assert state.subtasks == []
    assert state.subtask_statuses == []
    assert result.planner_output.subtasks == [
        "Pick up the red cup and place it on the black desk.",
        "Pick up the blue bowl and place it on the black desk.",
    ]


def test_unavailable_planner_tool_is_rejected_before_mcp_call():
    client = FakeMCPToolClient()
    client.register(
        "monitor",
        lambda args: {"status": "running", "subtask": args.get("subtask")},
        namespace="dual_franka",
    )
    client.register(
        "execute",
        lambda args: {"executed": True},
        namespace="dual_franka",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {
                            "namespace": "dual_franka",
                            "name": "init",
                            "arguments": {},
                        }
                    ],
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("organize the table")

    assert result.parse_ok is False
    assert "unavailable tool 'dual_franka___init'" in (result.parse_error or "")
    assert result.tool_results == []
    assert state.phase is AgenticPhase.ERROR


def test_multiple_execute_tool_calls_are_rejected_before_robot_calls():
    client = FakeMCPToolClient()
    execute_calls = []
    monitor_calls = []
    client.register(
        "execute",
        lambda args: (
            execute_calls.append(dict(args))
            or {"executed": True, "status": "running", "subtask": args.get("subtask")}
        ),
        namespace="demo_robot",
    )
    client.register(
        "monitor",
        lambda args: monitor_calls.append(dict(args)) or {"status": "running"},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"action": "pick"}},
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"action": "place"}},
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"action": "pick"}},
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"action": "place"}},
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"action": "pick"}},
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"action": "place"}},
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"action": "pick"}},
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"action": "place"}},
                    ],
                    "subtasks": [
                        "Pick up the plate and place it in the dish rack.",
                        "Pick up the bowl and place it in the dish rack.",
                        "Pick up the cup and place it in the dish rack.",
                        "Pick up the spoon and place it in the dish rack.",
                    ],
                    "subtask_index": 0,
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table")

    assert result.parse_ok is False
    assert "execute tool_call directly" in (result.parse_error or "")
    assert result.tool_results == []
    assert execute_calls == []
    assert monitor_calls == []
    assert state.active_execution is None


def test_single_execute_tool_call_is_rejected_before_robot_call():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {
                            "namespace": "demo_robot",
                            "name": "execute",
                            "arguments": {"subtask": "pick cup"},
                        }
                    ],
                    "subtasks": ["pick cup"],
                    "subtask_index": 0,
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table")

    assert result.parse_ok is False
    assert "execute tool_call directly" in (result.parse_error or "")
    assert result.tool_results == []
    assert execute_calls == []
    assert state.active_execution is None


def test_canonical_execute_tool_call_is_rejected_before_robot_call():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "demo_robot___execute",
                            "arguments": {"subtask": "pick cup"},
                        }
                    ],
                    "subtasks": ["pick cup"],
                    "subtask_index": 0,
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table")

    assert result.parse_ok is False
    assert "execute tool_call directly" in (result.parse_error or "")
    assert result.tool_results == []
    assert execute_calls == []
    assert state.active_execution is None


def test_execute_decision_drops_premature_monitor_tool_call():
    client = FakeMCPToolClient()
    execute_calls = []
    monitor_calls = []
    client.register(
        "execute",
        lambda args: (
            execute_calls.append(dict(args))
            or {"executed": True, "status": "running", "subtask": args.get("subtask")}
        ),
        namespace="demo_robot",
    )
    client.register(
        "monitor",
        lambda args: monitor_calls.append(dict(args)) or {"status": "running"},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "tool_calls": [{"namespace": "demo_robot", "name": "monitor", "arguments": {}}],
                    "subtasks": ["pick cup"],
                    "subtask_index": 0,
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table")

    assert result.parse_ok is True
    assert [tool_result.tool_name for tool_result in result.tool_results] == ["execute"]
    assert execute_calls == [{"subtask": "pick cup", "subtask_index": 0}]
    assert monitor_calls == []
    assert state.active_execution is not None
    assert state.active_execution.subtask == "pick cup"


def test_execute_decision_with_control_tool_is_rejected_before_robot_calls():
    client = FakeMCPToolClient()
    execute_calls = []
    stop_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    client.register(
        "stop_task",
        lambda args: stop_calls.append(dict(args)) or {"stopped": True},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "tool_calls": [{"namespace": "demo_robot", "name": "stop_task", "arguments": {}}],
                    "subtasks": ["pick cup"],
                    "subtask_index": 0,
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table")

    assert result.parse_ok is False
    assert "cannot combine decision=\"execute\" with a control tool" in (result.parse_error or "")
    assert result.tool_results == []
    assert execute_calls == []
    assert stop_calls == []
    assert state.active_execution is None


def test_monitor_poll_updates_events_and_vlm_continues_reasoning_until_success():
    client = FakeMCPToolClient()
    monitor_statuses = iter(["success"])
    monitor_calls = []
    planner_inputs = []
    client.register(
        "monitor",
        lambda args: (
            monitor_calls.append(dict(args))
            or {"status": next(monitor_statuses), "subtask": args.get("subtask")}
        ),
        namespace="demo_robot",
    )
    client.register(
        "execute",
        lambda args: {"executed": True, "status": "running", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )

    def planner_fn(planner_input):
        planner_inputs.append(planner_input)
        if len(planner_inputs) == 1:
            return json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "pick cup",
                }
            )
        if len(planner_inputs) == 2:
            return json.dumps({"current_subtask": "pick cup"})
        return json.dumps({"task_complete": True})

    loop = AgenticRobotLoop(CallablePlanner(planner_fn), client, RecordingExecutor())

    result0, state = loop.step("task")
    assert result0.vlm_called is True
    assert state.awaiting_monitor is True
    assert state.monitor_status is MonitorStatus.RUNNING

    result1, state = loop.step("task", state, reason_interval_s=0)
    assert result1.vlm_called is True
    assert result1.parse_ok is True
    assert result1.monitor_status is MonitorStatus.RUNNING
    assert state.awaiting_monitor is True
    assert len(planner_inputs) == 2

    result2, state = loop.poll_monitor("task", state)
    assert result2.vlm_called is False
    assert result2.monitor_status is MonitorStatus.SUCCESS
    assert state.awaiting_monitor is False
    assert len(planner_inputs) == 2
    assert state.pending_events[-1].event_type == "monitor_success"

    result3, state = loop.step("task", state)
    assert result3.vlm_called is True
    assert result3.task_complete is True
    assert len(planner_inputs) == 3
    assert planner_inputs[-1].monitor_status is MonitorStatus.SUCCESS
    assert planner_inputs[-1].events[-1].event_type == "monitor_success"
    assert monitor_calls == [{"subtask": "pick cup"}]


def test_monitor_failure_event_returns_control_to_planner():
    client = FakeMCPToolClient()
    planner_inputs = []
    client.register(
        "monitor",
        lambda args: {"status": "failed", "subtask": args.get("subtask"), "error": "blocked"},
        namespace="demo_robot",
    )
    client.register(
        "execute",
        lambda args: {"executed": True, "status": "running", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )

    def planner_fn(planner_input):
        planner_inputs.append(planner_input)
        if len(planner_inputs) == 1:
            return json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "pick cup",
                }
            )
        return json.dumps({"current_subtask": "retry pick cup"})

    loop = AgenticRobotLoop(CallablePlanner(planner_fn), client, RecordingExecutor())

    _, state = loop.step("task")
    result1, state = loop.poll_monitor("task", state)
    assert result1.vlm_called is False
    assert result1.monitor_status is MonitorStatus.FAILED
    assert result1.monitor_error == "blocked"
    assert state.awaiting_monitor is False

    result2, _ = loop.step("task", state)
    assert result2.vlm_called is True
    assert result2.current_subtask == "retry pick cup"
    assert planner_inputs[-1].monitor_status is MonitorStatus.FAILED
    assert planner_inputs[-1].monitor_error == "blocked"
    assert planner_inputs[-1].events[-1].event_type == "monitor_failed"


def test_running_execution_blocks_duplicate_execute_tool_call():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    client.register(
        "monitor",
        lambda args: {"status": "running", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )

    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "pick cup",
                }
            ),
            json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "pick cup",
                }
            ),
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    _, state = loop.step("task")
    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is False
    assert "active_execution is running" in (result.parse_error or "")
    assert result.tool_results == []
    assert len(execute_calls) == 1
    assert state.active_execution is not None
    assert state.active_execution.status == "running"


def test_running_execution_blocks_task_complete():
    client = FakeMCPToolClient()
    client.register("execute", lambda args: {"executed": True}, namespace="demo_robot")
    client.register(
        "monitor",
        lambda args: {"status": "running", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "pick cup",
                }
            ),
            json.dumps({"task_complete": True}),
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    _, state = loop.step("task")
    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.task_complete is False
    assert result.parse_ok is False
    assert "task_complete while active_execution is running" in (result.parse_error or "")
    assert state.awaiting_monitor is True
    assert state.active_execution is not None
    assert state.active_execution.status == "running"


def test_task_complete_is_rejected_when_plan_has_pending_subtasks():
    planner = _planner(
        [
            json.dumps(
                {
                    "task_complete": True,
                    "current_subtask": "wrong completed subtask",
                    "subtask_index": 0,
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["pick cup", "place cup"],
        subtask_statuses=[SubtaskStatus.SUCCESS, SubtaskStatus.PENDING],
        current_subtask="place cup",
        subtask_index=1,
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, _tool_client(), RecordingExecutor())

    result, state = loop.step("task", state)

    assert result.task_complete is False
    assert result.parse_ok is False
    assert "incomplete subtask" in (result.parse_error or "")
    assert result.events[-1].event_type == "planner_inconsistent"
    assert state.phase is AgenticPhase.ERROR
    assert state.reason_requested is True
    assert state.current_subtask == "place cup"
    assert state.subtask_index == 1


def test_task_complete_is_allowed_before_subtask_success_progress():
    planner = _planner([json.dumps({"task_complete": True})])
    state = AgenticSessionState(
        task="task",
        subtasks=["pick cup"],
        current_subtask="pick cup",
        subtask_index=0,
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, _tool_client(), RecordingExecutor())

    result, state = loop.step("task", state)

    assert result.task_complete is True
    assert result.parse_ok is True
    assert state.phase is AgenticPhase.DONE


def test_wait_decision_during_active_execution_does_not_execute_again():
    planner = _planner([json.dumps({"decision": "wait"})])
    executor = RecordingExecutor()
    state = AgenticSessionState(
        task="task",
        phase=AgenticPhase.RESPONSE,
        current_subtask="pick cup",
        subtask_index=0,
        awaiting_monitor=True,
        active_execution=ActiveExecution(
            subtask="pick cup",
            subtask_index=0,
            execution_id="exec-1",
        ),
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, _tool_client(), executor)

    result, state = loop.step("task", state)

    assert result.parse_ok is True
    assert result.planner_output.decision == "wait"
    assert executor.calls == []
    assert state.active_execution is not None
    assert state.active_execution.status == "running"


def test_repeated_plan_without_execute_is_allowed_after_plan_exists():
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "plan",
                    "tool_calls": [],
                    "subtasks": ["pick cup"],
                    "subtask_index": 0,
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["pick cup"],
        current_subtask="pick cup",
        subtask_index=0,
    )
    executor = RecordingExecutor()
    loop = AgenticRobotLoop(planner, _tool_client(), executor)

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is True
    assert result.parse_error is None
    assert result.events == []
    assert executor.calls == []


def test_revised_plan_current_subtask_must_match_selected_index():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "subtasks": ["pick bowl", "pick spoon"],
                    "subtask_index": 1,
                    "current_subtask": "pick bowl",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["pick cup", "pick bowl", "pick spoon"],
        current_subtask="pick cup",
        subtask_index=0,
        monitor_status=MonitorStatus.SUCCESS,
        active_execution=ActiveExecution(subtask="pick cup", subtask_index=0, status="success"),
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is True
    assert result.current_subtask == "pick bowl"
    assert result.subtask_index == 0
    assert state.subtask_index == 0
    assert execute_calls == [{"subtask": "pick bowl", "subtask_index": 0}]


def test_reconciled_subtask_index_is_passed_to_execute_tool():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "subtasks": ["pick bowl", "pick spoon"],
                    "subtask_index": 1,
                    "current_subtask": "pick bowl",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["pick cup", "pick bowl", "pick spoon"],
        current_subtask="pick cup",
        subtask_index=0,
        monitor_status=MonitorStatus.SUCCESS,
        active_execution=ActiveExecution(subtask="pick cup", subtask_index=0, status="success"),
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, _ = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is True
    assert execute_calls == [{"subtask": "pick bowl", "subtask_index": 0}]


def test_revised_plan_current_subtask_mismatch_is_rejected_when_not_reconcilable():
    client = FakeMCPToolClient()
    client.register("execute", lambda args: {"executed": True}, namespace="demo_robot")
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "replan",
                    "subtasks": ["pick bowl", "pick spoon"],
                    "subtask_index": 1,
                    "current_subtask": "pick plate",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["pick cup", "pick bowl", "pick spoon"],
        current_subtask="pick cup",
        subtask_index=0,
        monitor_status=MonitorStatus.SUCCESS,
        active_execution=ActiveExecution(subtask="pick cup", subtask_index=0, status="success"),
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is False
    assert "current_subtask is not in the subtask plan" in (result.parse_error or "")
    assert result.events[-1].event_type == "planner_inconsistent"
    assert state.current_subtask == "pick cup"


def test_planner_execute_tool_call_is_rejected_even_if_subtask_mismatches():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {
                            "namespace": "demo_robot",
                            "name": "execute",
                            "arguments": {"subtask": "pick spoon"},
                        }
                    ],
                    "subtasks": ["pick bowl", "pick spoon"],
                    "subtask_index": 0,
                    "current_subtask": "pick bowl",
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, _ = loop.step("task")

    assert result.parse_ok is False
    assert "execute tool_call directly" in (result.parse_error or "")
    assert execute_calls == []


def test_monitor_success_blocks_reexecuting_same_subtask():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "subtasks": ["pick bowl", "pick spoon"],
                    "subtask_index": 0,
                    "current_subtask": "pick bowl",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["pick bowl", "pick spoon"],
        current_subtask="pick bowl",
        subtask_index=0,
        monitor_status=MonitorStatus.SUCCESS,
        active_execution=ActiveExecution(subtask="pick bowl", subtask_index=0, status="success"),
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, _ = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is False
    assert "same subtask that just reached monitor_success" in (result.parse_error or "")
    assert result.events[-1].event_type == "planner_repeated_success"
    assert execute_calls == []


def test_monitor_failure_allows_retrying_same_subtask():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    client.register(
        "monitor",
        lambda args: {"status": "running", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "subtask_index": 0,
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["pick bowl", "pick spoon"],
        subtask_statuses=[SubtaskStatus.FAILED, SubtaskStatus.PENDING],
        current_subtask="pick bowl",
        subtask_index=0,
        monitor_status=MonitorStatus.FAILED,
        active_execution=ActiveExecution(subtask="pick bowl", subtask_index=0, status="failed"),
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is True
    assert execute_calls == [{"subtask": "pick bowl", "subtask_index": 0}]
    assert state.active_execution is not None
    assert state.active_execution.status == "running"
    assert state.subtask_statuses[0] is SubtaskStatus.RUNNING


def test_monitor_success_advances_planner_input_to_next_pending_subtask():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    planner_inputs = []

    def planner_fn(planner_input):
        planner_inputs.append(planner_input)
        return json.dumps(
            {
                "decision": "execute",
                "subtask_index": planner_input.subtask_index,
                "current_subtask": planner_input.current_subtask,
            }
        )

    state = AgenticSessionState(
        task="task",
        subtasks=["pick bowl", "pick spoon"],
        subtask_statuses=[SubtaskStatus.SUCCESS, SubtaskStatus.PENDING],
        current_subtask="pick bowl",
        subtask_index=0,
        monitor_status=MonitorStatus.SUCCESS,
        active_execution=ActiveExecution(subtask="pick bowl", subtask_index=0, status="success"),
        pending_events=[],
        reason_requested=True,
    )
    loop = AgenticRobotLoop(CallablePlanner(planner_fn), client, RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is True
    assert planner_inputs[-1].current_subtask == "pick spoon"
    assert planner_inputs[-1].subtask_index == 1
    assert result.current_subtask == "pick spoon"
    assert result.subtask_index == 1
    assert execute_calls == [{"subtask": "pick spoon", "subtask_index": 1}]
    assert state.active_execution is not None
    assert state.active_execution.subtask == "pick spoon"


def test_running_execution_allows_replan_when_active_subtask_stays_selected():
    client = FakeMCPToolClient()
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "observe",
                    "tool_calls": [],
                    "subtasks": ["pick blue spoon", "pick pink cup"],
                    "subtask_index": 0,
                    "current_subtask": "pick blue spoon",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="clean the table",
        subtasks=["pick blue bowl", "pick blue spoon"],
        subtask_statuses=[SubtaskStatus.SUCCESS, SubtaskStatus.RUNNING],
        current_subtask="pick blue spoon",
        subtask_index=1,
        monitor_status=MonitorStatus.RUNNING,
        awaiting_monitor=True,
        active_execution=ActiveExecution(
            subtask="pick blue spoon",
            subtask_index=1,
            execution_id="exec-1",
            monitor_id="mon-1",
            namespace="demo_robot",
        ),
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table", state, reason_interval_s=0)

    assert result.parse_ok is False
    assert "removed completed subtask" in (result.parse_error or "")
    assert result.events[-1].event_type == "planner_invalid_replan"
    assert state.subtasks == ["pick blue bowl", "pick blue spoon"]
    assert state.subtask_statuses == [SubtaskStatus.SUCCESS, SubtaskStatus.RUNNING]
    assert state.current_subtask == "pick blue spoon"
    assert state.subtask_index == 1


def test_running_execution_allows_future_plan_update_if_success_history_kept():
    client = FakeMCPToolClient()
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "observe",
                    "tool_calls": [],
                    "subtasks": ["pick blue bowl", "pick blue spoon", "pick pink cup"],
                    "subtask_index": 1,
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="clean the table",
        subtasks=["pick blue bowl", "pick blue spoon"],
        subtask_statuses=[SubtaskStatus.SUCCESS, SubtaskStatus.RUNNING],
        current_subtask="pick blue spoon",
        subtask_index=1,
        monitor_status=MonitorStatus.RUNNING,
        awaiting_monitor=True,
        active_execution=ActiveExecution(
            subtask="pick blue spoon",
            subtask_index=1,
            execution_id="exec-1",
            monitor_id="mon-1",
            namespace="demo_robot",
        ),
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table", state, reason_interval_s=0)

    assert result.parse_ok is True
    assert state.subtasks == ["pick blue bowl", "pick blue spoon", "pick pink cup"]
    assert state.subtask_statuses == [
        SubtaskStatus.SUCCESS,
        SubtaskStatus.RUNNING,
        SubtaskStatus.PENDING,
    ]
    assert state.current_subtask == "pick blue spoon"
    assert state.subtask_index == 1


def test_running_execution_blocks_replan_that_moves_selection_off_active_subtask():
    client = FakeMCPToolClient()
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "observe",
                    "tool_calls": [],
                    "subtasks": ["pick blue bowl", "pick blue spoon", "pick pink cup"],
                    "subtask_index": 2,
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="clean the table",
        subtasks=["pick blue bowl", "pick blue spoon"],
        subtask_statuses=[SubtaskStatus.SUCCESS, SubtaskStatus.RUNNING],
        current_subtask="pick blue spoon",
        subtask_index=1,
        monitor_status=MonitorStatus.RUNNING,
        awaiting_monitor=True,
        active_execution=ActiveExecution(
            subtask="pick blue spoon",
            subtask_index=1,
            execution_id="exec-1",
            monitor_id="mon-1",
            namespace="demo_robot",
        ),
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table", state, reason_interval_s=0)

    assert result.parse_ok is False
    assert "did not keep the selected subtask on the active execution" in (result.parse_error or "")
    assert result.events[-1].event_type == "planner_blocked_replan"
    assert state.subtasks == ["pick blue bowl", "pick blue spoon"]


def test_replan_cannot_remove_successful_subtasks():
    client = FakeMCPToolClient()
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "replan",
                    "tool_calls": [],
                    "subtasks": ["pick blue spoon", "pick pink cup"],
                    "subtask_index": 0,
                    "current_subtask": "pick blue spoon",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="clean the table",
        subtasks=["pick blue bowl", "pick blue spoon"],
        subtask_statuses=[SubtaskStatus.SUCCESS, SubtaskStatus.FAILED],
        current_subtask="pick blue spoon",
        subtask_index=1,
        monitor_status=MonitorStatus.FAILED,
        active_execution=ActiveExecution(subtask="pick blue spoon", subtask_index=1, status="failed"),
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table", state, reason_interval_s=0)

    assert result.parse_ok is False
    assert "removed completed subtask" in (result.parse_error or "")
    assert result.events[-1].event_type == "planner_invalid_replan"
    assert state.subtasks == ["pick blue bowl", "pick blue spoon"]


def test_replan_after_all_success_must_complete_task_instead():
    client = FakeMCPToolClient()
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "replan",
                    "tool_calls": [],
                    "subtasks": ["pick blue spoon", "pick pink cup"],
                    "subtask_index": 1,
                    "current_subtask": "pick pink cup",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="clean the table",
        subtasks=["pick blue bowl", "pick blue spoon"],
        subtask_statuses=[SubtaskStatus.SUCCESS, SubtaskStatus.SUCCESS],
        current_subtask="pick blue spoon",
        subtask_index=1,
        monitor_status=MonitorStatus.SUCCESS,
        active_execution=ActiveExecution(subtask="pick blue spoon", subtask_index=1, status="success"),
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table", state, reason_interval_s=0)

    assert result.parse_ok is False
    assert "after all existing subtasks were marked success" in (result.parse_error or "")
    assert state.subtasks == ["pick blue bowl", "pick blue spoon"]


def test_current_subtask_must_belong_to_existing_plan_when_no_revised_subtasks():
    client = FakeMCPToolClient()
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "pick pink cup",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="clean the table",
        subtasks=["pick blue bowl", "pick blue spoon"],
        subtask_statuses=[SubtaskStatus.SUCCESS, SubtaskStatus.PENDING],
        current_subtask="pick blue spoon",
        subtask_index=1,
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("clean the table", state, reason_interval_s=0)

    assert result.parse_ok is False
    assert "current_subtask is not in the subtask plan" in (result.parse_error or "")
    assert state.current_subtask == "pick blue spoon"
    assert state.subtask_index == 1


def test_success_subtask_status_blocks_reexecuting_completed_plan_item():
    client = FakeMCPToolClient()
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "subtask_index": 0,
                    "current_subtask": "pick cup",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["pick cup", "pick bowl"],
        subtask_statuses=[SubtaskStatus.SUCCESS, SubtaskStatus.PENDING],
        current_subtask="pick cup",
        subtask_index=0,
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, _ = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is False
    assert "already marked success" in (result.parse_error or "")
    assert result.events[-1].event_type == "planner_repeated_success"
    assert execute_calls == []


def test_tick_without_reason_skips_vlm_and_preserves_state():
    planner_calls = 0

    def planner_fn(_planner_input):
        nonlocal planner_calls
        planner_calls += 1
        return json.dumps({"current_subtask": "inspect scene"})

    state = AgenticSessionState(
        task="task",
        phase=AgenticPhase.READY,
        current_subtask="inspect scene",
        reason_requested=False,
        last_reason_at=time.time(),
    )
    loop = AgenticRobotLoop(CallablePlanner(planner_fn), _tool_client(), RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=60)

    assert result.vlm_called is False
    assert result.planner_output.raw_output == "[system] tick without reason"
    assert state.step_index == 0
    assert planner_calls == 0


def test_execute_status_running_reuses_initial_monitor_feedback():
    client = FakeMCPToolClient()
    monitor_calls = []
    client.register(
        "execute",
        lambda args: {
            "executed": True,
            "status": "running",
            "subtask": args.get("subtask"),
            "execution_id": "exec-1",
            "monitor_id": "mon-1",
        },
        namespace="demo_robot",
    )
    client.register(
        "monitor",
        lambda args: monitor_calls.append(dict(args)) or {"status": "running", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "pick cup",
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task")

    assert result.parse_ok is True
    assert [tool_result.tool_name for tool_result in result.tool_results] == ["execute"]
    assert monitor_calls == []
    assert state.active_execution is not None
    assert state.active_execution.execution_id == "exec-1"
    assert state.active_execution.monitor_id == "mon-1"
    assert state.active_execution.status == "running"


def test_execute_status_success_preserves_terminal_monitor_state():
    client = FakeMCPToolClient()
    client.register(
        "execute",
        lambda args: {
            "executed": True,
            "status": "success",
            "subtask": args.get("subtask"),
            "execution_id": "exec-1",
            "monitor_id": "mon-1",
        },
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "pick cup",
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task")

    assert result.parse_ok is True
    assert result.monitor_status is MonitorStatus.SUCCESS
    assert state.monitor_status is MonitorStatus.SUCCESS
    assert state.awaiting_monitor is False
    assert state.active_execution is not None
    assert state.active_execution.status == "success"
    assert result.events[-1].event_type == "monitor_success"


def test_execute_status_failed_preserves_terminal_monitor_state():
    client = FakeMCPToolClient()
    client.register(
        "execute",
        lambda args: {
            "executed": False,
            "status": "failed",
            "subtask": args.get("subtask"),
            "execution_id": "exec-1",
            "monitor_id": "mon-1",
            "error": "startup failed",
        },
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "pick cup",
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task")

    assert result.parse_ok is True
    assert result.monitor_status is MonitorStatus.FAILED
    assert result.monitor_error == "startup failed"
    assert state.monitor_status is MonitorStatus.FAILED
    assert state.awaiting_monitor is False
    assert state.active_execution is not None
    assert state.active_execution.status == "failed"
    assert state.active_execution.error == "startup failed"
    assert result.events[-1].event_type == "monitor_failed"


def test_monitor_poll_is_skipped_without_active_execution_even_with_current_subtask():
    client = FakeMCPToolClient()
    monitor_calls = []
    client.register(
        "monitor",
        lambda args: monitor_calls.append(dict(args)) or {"status": "running", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )
    state = AgenticSessionState(
        task="task",
        current_subtask="pick cup",
        subtask_index=0,
        awaiting_monitor=True,
        monitor_namespace="demo_robot",
    )
    loop = AgenticRobotLoop(_planner([]), client, RecordingExecutor())

    result, state = loop.poll_monitor("task", state)

    assert result.vlm_called is False
    assert result.tool_results == []
    assert monitor_calls == []
    assert state.awaiting_monitor is False


def test_planner_monitor_call_gets_active_execution_ids():
    client = FakeMCPToolClient()
    monitor_calls = []
    client.register(
        "monitor",
        lambda args: (
            monitor_calls.append(dict(args))
            or {
                "status": "running",
                "subtask": args.get("subtask"),
                "execution_id": args.get("execution_id"),
                "monitor_id": args.get("monitor_id"),
            }
        ),
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"namespace": "demo_robot", "name": "monitor", "arguments": {}}],
                    "current_subtask": "pick cup",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        current_subtask="pick cup",
        subtask_index=0,
        awaiting_monitor=True,
        monitor_namespace="demo_robot",
        active_execution=ActiveExecution(
            subtask="pick cup",
            subtask_index=0,
            execution_id="exec-1",
            monitor_id="mon-1",
            namespace="demo_robot",
        ),
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is True
    assert monitor_calls == [
        {
            "subtask": "pick cup",
            "subtask_index": 0,
            "execution_id": "exec-1",
            "monitor_id": "mon-1",
        }
    ]
    assert state.awaiting_monitor is True


def test_planner_monitor_identity_arguments_cannot_override_active_execution():
    client = FakeMCPToolClient()
    monitor_calls = []
    client.register(
        "monitor",
        lambda args: (
            monitor_calls.append(dict(args))
            or {
                "status": "running",
                "subtask": args.get("subtask"),
                "execution_id": args.get("execution_id"),
                "monitor_id": args.get("monitor_id"),
            }
        ),
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {
                            "namespace": "demo_robot",
                            "name": "monitor",
                            "arguments": {
                                "subtask": "wrong subtask",
                                "subtask_index": 99,
                                "execution_id": "wrong-exec",
                                "monitor_id": "wrong-mon",
                                "custom": "ignored",
                            },
                        }
                    ],
                    "subtask_index": 0,
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        current_subtask="pick cup",
        subtask_index=0,
        awaiting_monitor=True,
        monitor_namespace="demo_robot",
        active_execution=ActiveExecution(
            subtask="pick cup",
            subtask_index=0,
            execution_id="exec-1",
            monitor_id="mon-1",
            namespace="demo_robot",
        ),
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, _ = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is True
    assert monitor_calls == [
        {
            "subtask": "pick cup",
            "subtask_index": 0,
            "execution_id": "exec-1",
            "monitor_id": "mon-1",
            }
        ]


def test_monitor_result_mismatched_ids_is_rejected():
    client = FakeMCPToolClient()
    client.register(
        "monitor",
        lambda args: {
            "status": "success",
            "subtask": args.get("subtask"),
            "execution_id": args.get("execution_id"),
            "monitor_id": "mon-2",
        },
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"namespace": "demo_robot", "name": "monitor", "arguments": {}}],
                    "current_subtask": "pick cup",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        current_subtask="pick cup",
        subtask_index=0,
        awaiting_monitor=True,
        monitor_namespace="demo_robot",
        active_execution=ActiveExecution(
            subtask="pick cup",
            subtask_index=0,
            execution_id="exec-1",
            monitor_id="mon-1",
            namespace="demo_robot",
        ),
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.parse_ok is False
    assert "Monitor monitor_id mismatch" in (result.parse_error or "")
    assert state.active_execution is not None
    assert state.active_execution.status == "running"


def test_stop_task_result_marks_active_execution_failed_locally():
    client = FakeMCPToolClient()
    client.register("stop_task", lambda args: {"stopped": True}, namespace="demo_robot")
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"namespace": "demo_robot", "name": "stop_task", "arguments": {}}],
                    "current_subtask": "pick cup",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["pick cup"],
        subtask_statuses=[SubtaskStatus.RUNNING],
        current_subtask="pick cup",
        subtask_index=0,
        awaiting_monitor=True,
        active_execution=ActiveExecution(
            subtask="pick cup",
            subtask_index=0,
            execution_id="exec-1",
            monitor_id="mon-1",
            namespace="demo_robot",
        ),
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.monitor_status is MonitorStatus.FAILED
    assert state.awaiting_monitor is False
    assert state.active_execution is not None
    assert state.active_execution.status == "failed"
    assert state.subtask_statuses == [SubtaskStatus.FAILED]


def test_stop_task_does_not_mark_completed_active_execution_failed():
    client = FakeMCPToolClient()
    client.register("stop_task", lambda args: {"stopped": True}, namespace="demo_robot")
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"namespace": "demo_robot", "name": "stop_task", "arguments": {}}],
                    "subtask_index": 1,
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["pick bowl", "pick plate", "move basket"],
        subtask_statuses=[
            SubtaskStatus.SUCCESS,
            SubtaskStatus.PENDING,
            SubtaskStatus.PENDING,
        ],
        current_subtask="pick plate",
        subtask_index=1,
        monitor_status=None,
        awaiting_monitor=False,
        active_execution=ActiveExecution(
            subtask="pick bowl",
            subtask_index=0,
            execution_id="exec-1",
            monitor_id="mon-1",
            namespace="demo_robot",
            status=MonitorStatus.SUCCESS.value,
        ),
        reason_requested=True,
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.monitor_status is None
    assert result.monitor_error is None
    assert result.events[-1].event_type == "control_requested"
    assert state.awaiting_monitor is False
    assert state.active_execution is not None
    assert state.active_execution.status == "success"
    assert state.subtask_statuses == [
        SubtaskStatus.SUCCESS,
        SubtaskStatus.PENDING,
        SubtaskStatus.PENDING,
    ]
    assert state.current_subtask == "pick plate"
    assert state.subtask_index == 1


def test_mcp_execute_terminal_monitor_result_becomes_event():
    client = FakeMCPToolClient()
    client.register(
        "execute",
        lambda args: {
            "executed": True,
            "status": "success",
            "subtask": args.get("subtask"),
            "monitor_id": "mon-1",
        },
        namespace="demo_robot",
    )
    planner_inputs = []

    def planner_fn(planner_input):
        planner_inputs.append(planner_input)
        if len(planner_inputs) == 1:
            return json.dumps(
                {
                    "decision": "execute",
                    "subtasks": ["pick cup"],
                    "subtask_index": 0,
                }
            )
        return json.dumps({"task_complete": True})

    loop = AgenticRobotLoop(CallablePlanner(planner_fn), client, RecordingExecutor())

    result0, state = loop.step("task")

    assert result0.events[-1].event_type == "monitor_success"
    assert state.pending_events[-1].event_type == "monitor_success"
    assert state.reason_requested is True
    assert state.awaiting_monitor is False
    assert state.active_execution is not None
    assert state.active_execution.status == "success"

    result1, _ = loop.step("task", state)
    assert result1.task_complete is True
    assert planner_inputs[-1].events[-1].event_type == "monitor_success"


def test_new_execute_without_monitor_feedback_resets_previous_success():
    client = FakeMCPToolClient()
    client.register("execute", lambda args: {"executed": True}, namespace="demo_robot")
    planner = _planner(
        [
            json.dumps(
                {
                    "decision": "execute",
                    "current_subtask": "place cup",
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        current_subtask="pick cup",
        monitor_status=MonitorStatus.SUCCESS,
        awaiting_monitor=False,
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.current_subtask == "place cup"
    assert result.monitor_status is MonitorStatus.RUNNING
    assert state.awaiting_monitor is True
    assert state.monitor_error is None


def test_nonstandard_tool_with_status_is_treated_as_monitor_feedback():
    client = FakeMCPToolClient()
    client.register(
        "check_status",
        lambda args: {"status": "success", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "check_status",
                            "arguments": {"subtask": "open drawer"},
                        }
                    ],
                    "current_subtask": "open drawer",
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task")

    assert result.monitor_status is MonitorStatus.SUCCESS
    assert state.monitor_status is MonitorStatus.SUCCESS


def test_nonstandard_environment_tool_merges_environment_payload():
    client = FakeMCPToolClient()
    client.register(
        "observe_scene",
        lambda args: {"environment": {"objects": ["cup"], "gripper": "empty"}},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"name": "observe_scene", "arguments": {}}],
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    _, state = loop.step("task")

    assert state.environment == {"objects": ["cup"], "gripper": "empty"}


def test_scene_graph_tool_payload_merges_environment():
    client = FakeMCPToolClient()
    client.register(
        "observe_scene",
        lambda args: {"scene_graph": {"objects": {"cup_1": {"class": "cup"}}}},
        namespace="demo_robot",
    )
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"name": "observe_scene", "arguments": {}}],
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    _, state = loop.step("task")

    assert state.environment == {"objects": {"cup_1": {"class": "cup"}}}


def test_planner_metadata_defaults_to_planner_visible_subset():
    planner_inputs = []

    def planner_fn(planner_input):
        planner_inputs.append(planner_input)
        return json.dumps({"current_subtask": "inspect scene"})

    metadata = {
        "run_id": "internal-run",
        "planner_visible_metadata": {"robot_type": "dual_franka"},
    }
    loop = AgenticRobotLoop(CallablePlanner(planner_fn), _tool_client(), RecordingExecutor())

    loop.step("task", metadata=metadata)

    assert planner_inputs[-1].metadata == {"robot_type": "dual_franka"}


def test_nonstandard_action_tool_is_blocked_before_it_can_return_executed_true():
    client = FakeMCPToolClient()
    run_calls = []
    client.register("run_subtask", lambda args: run_calls.append(dict(args)) or {"executed": True}, namespace="demo_robot")
    executor = RecordingExecutor()
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"name": "run_subtask", "arguments": {"subtask": "push button"}}],
                    "current_subtask": "push button",
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, executor)

    result, _ = loop.step("task")

    assert result.parse_ok is False
    assert "action-like tool" in (result.parse_error or "")
    assert result.tool_results == []
    assert run_calls == []
    assert executor.calls == []


def test_physical_action_named_tool_is_blocked_before_call():
    client = FakeMCPToolClient()
    action_calls = []
    client.register("open_drawer", lambda args: action_calls.append(dict(args)) or {"opened": True}, namespace="demo_robot")
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"name": "open_drawer", "arguments": {"drawer": "top"}}],
                    "current_subtask": "open the top drawer",
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, _ = loop.step("task")

    assert result.parse_ok is False
    assert "action-like tool" in (result.parse_error or "")
    assert result.tool_results == []
    assert action_calls == []


def test_plain_extra_tool_requires_no_loop_config_and_does_not_execute():
    client = FakeMCPToolClient()
    client.register("estimate_grasp", lambda args: {"pose": [1, 2, 3]}, namespace="demo_robot")
    executor = RecordingExecutor()
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"name": "estimate_grasp", "arguments": {"object": "cup"}}],
                    "current_subtask": "pick cup",
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, executor)

    result, _ = loop.step("task")

    assert result.tool_results[0].data == {"pose": [1, 2, 3]}
    assert executor.calls == []


def test_namespace_routing_distinguishes_same_tool_name():
    client = FakeMCPToolClient()
    client.register("monitor", lambda args: {"status": "running"}, namespace="robot_a")
    client.register("monitor", lambda args: {"status": "success"}, namespace="robot_b")

    result_a = client.call_tool("monitor", {}, namespace="robot_a")
    result_b = client.call_tool("monitor", {}, namespace="robot_b")

    assert result_a.data["status"] == "running"
    assert result_a.namespace == "robot_a"
    assert result_b.data["status"] == "success"
    assert result_b.namespace == "robot_b"


def test_canonical_tool_name_routes_to_namespace():
    client = FakeMCPToolClient()
    client.register("monitor", lambda args: {"status": "success"}, namespace="demo_robot")
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {
                            "name": "demo_robot___monitor",
                            "arguments": {"subtask": "grasp cup"},
                        }
                    ],
                    "subtask_index": 0,
                }
            )
        ]
    )
    state = AgenticSessionState(
        task="task",
        subtasks=["grasp cup"],
        subtask_statuses=[SubtaskStatus.RUNNING],
        current_subtask="grasp cup",
        subtask_index=0,
        monitor_status=MonitorStatus.RUNNING,
        awaiting_monitor=True,
        monitor_namespace="demo_robot",
        active_execution=ActiveExecution(
            subtask="grasp cup",
            subtask_index=0,
            namespace="demo_robot",
            status=MonitorStatus.RUNNING.value,
        ),
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task", state, reason_interval_s=0)

    assert result.monitor_status is MonitorStatus.SUCCESS
    assert state.monitor_status is MonitorStatus.SUCCESS
    assert result.tool_results[0].tool_name == "monitor"
    assert result.tool_results[0].namespace == "demo_robot"


def test_unqualified_duplicate_tool_name_returns_structured_error():
    client = FakeMCPToolClient()
    client.register("monitor", lambda args: {"status": "running"}, namespace="robot_a")
    client.register("monitor", lambda args: {"status": "success"}, namespace="robot_b")

    result = client.call_tool("monitor", {})

    assert result.ok is False
    assert "robot_a___monitor" in (result.error or "")
    assert "robot_b___monitor" in (result.error or "")


def test_invalid_monitor_status_blocks_and_records_error():
    client = FakeMCPToolClient()
    client.register("monitor", lambda args: {"status": "bogus"}, namespace="demo_robot")
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [{"namespace": "demo_robot", "name": "monitor", "arguments": {}}],
                    "current_subtask": "do thing",
                }
            )
        ]
    )
    executor = RecordingExecutor()
    loop = AgenticRobotLoop(planner, client, executor)

    result, _ = loop.step("task")

    assert result.parse_ok is False
    assert executor.calls == []


def test_run_stops_on_task_complete():
    planner = _planner(
        [
            json.dumps({"decision": "plan", "subtasks": ["step one"], "subtask_index": 0}),
            json.dumps({"task_complete": True}),
        ]
    )
    loop = AgenticRobotLoop(planner, _tool_client(), RecordingExecutor())

    results, state = loop.run("multi step task", max_steps=5)

    assert len(results) == 2
    assert results[-1].task_complete is True
    assert state.step_index == 2


def test_decompose_then_select_by_index_then_revise():
    planner = _planner(
        [
            # step 0: decompose the long-horizon task into a plan, start at index 0
            json.dumps(
                {
                    "decision": "execute",
                    "subtasks": ["approach the radio", "press the power button", "tidy the table"],
                    "subtask_index": 0,
                }
            ),
            # step 1: after monitor success, select index 1 from the existing plan.
            json.dumps({"decision": "execute", "subtask_index": 1}),
            # step 2: after the second monitor success, revise remaining work but keep successes.
            json.dumps(
                {
                    "decision": "execute",
                    "subtasks": [
                        "approach the radio",
                        "press the power button",
                        "verify the radio is on",
                    ],
                    "subtask_index": 2,
                }
            ),
        ]
    )
    client = FakeMCPToolClient()
    monitor_statuses = iter(["running", "success", "running", "success"])
    client.register(
        "monitor",
        lambda args: {"status": next(monitor_statuses), "subtask": args.get("subtask")},
        namespace="demo_robot",
    )
    execute_calls = []
    client.register(
        "execute",
        lambda args: execute_calls.append(dict(args)) or {"executed": True},
        namespace="demo_robot",
    )
    executor = RecordingExecutor()
    loop = AgenticRobotLoop(planner, client, executor)

    result0, state = loop.step("turn on the radio and tidy up")
    assert state.subtasks == ["approach the radio", "press the power button", "tidy the table"]
    assert result0.current_subtask == "approach the radio"
    assert execute_calls[-1] == {"subtask": "approach the radio", "subtask_index": 0}
    assert executor.calls == []

    _, state = loop.poll_monitor("turn on the radio and tidy up", state)
    result1, state = loop.step("turn on the radio and tidy up", state, reason_interval_s=0)
    assert result1.current_subtask == "press the power button"
    assert state.subtasks == ["approach the radio", "press the power button", "tidy the table"]
    assert state.subtask_statuses == [SubtaskStatus.SUCCESS, SubtaskStatus.RUNNING, SubtaskStatus.PENDING]
    assert execute_calls[-1] == {"subtask": "press the power button", "subtask_index": 1}

    _, state = loop.poll_monitor("turn on the radio and tidy up", state)
    result2, state = loop.step("turn on the radio and tidy up", state, reason_interval_s=0)
    assert state.subtasks == [
        "approach the radio",
        "press the power button",
        "verify the radio is on",
    ]
    assert state.subtask_statuses == [
        SubtaskStatus.SUCCESS,
        SubtaskStatus.SUCCESS,
        SubtaskStatus.RUNNING,
    ]
    assert result2.current_subtask == "verify the radio is on"


def test_session_state_round_trips_through_dict():
    state = AgenticSessionState(task="t", current_subtask="s", step_index=3)
    restored = AgenticSessionState.from_dict(state.to_dict())
    assert restored.task == "t"
    assert restored.current_subtask == "s"
    assert restored.step_index == 3
