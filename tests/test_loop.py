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
)


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
                "current_subtask": "Pick up the pink cup and place it in the dish rack.",
                "should_execute": False,
            }
        )


def test_planner_visual_scene_is_recorded_in_step_input_and_state():
    planner = VisualScenePlanner()
    loop = AgenticRobotLoop(planner, _tool_client(), RecordingExecutor())

    result, state = loop.step("organize the desk")

    assert result.planner_input.environment["visual_scene"]["objects"][0]["name"] == "pink cup"
    assert state.environment["visual_scene"]["target_locations"] == ["dish rack"]


def test_normal_subtask_execution_with_fetch_env_and_monitor():
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {"namespace": "demo_robot", "name": "fetch_env", "arguments": {}},
                        {
                            "namespace": "demo_robot",
                            "name": "monitor",
                            "arguments": {"subtask": "turn on the radio"},
                        },
                    ],
                    "current_subtask": "turn on the radio",
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
    assert len(executor.calls) == 1
    assert executor.calls[0].subtask == "turn on the radio"


def test_monitor_feedback_flows_into_next_planner_input():
    client = FakeMCPToolClient()
    client.register(
        "monitor",
        lambda args: {"status": "success", "subtask": args.get("subtask")},
        namespace="demo_robot",
    )
    captured = {}

    def fn(planner_input) -> str:
        captured["monitor_status"] = planner_input.monitor_status
        return json.dumps(
            {
                "tool_calls": [
                    {"namespace": "demo_robot", "name": "monitor", "arguments": {"subtask": "grasp cup"}}
                ],
                "current_subtask": "grasp cup",
            }
        )

    planner = CallablePlanner(fn)
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    _, state = loop.step("task")
    assert state.monitor_status is MonitorStatus.SUCCESS

    loop.step("task", state, reason_interval_s=0)
    assert captured["monitor_status"] is MonitorStatus.SUCCESS


def test_task_complete_short_circuits_tools_and_executor():
    planner = _planner([json.dumps({"task_complete": True})])
    executor = RecordingExecutor()
    loop = AgenticRobotLoop(planner, _tool_client(), executor)

    result, _ = loop.step("done task")

    assert result.task_complete is True
    assert result.tool_results == []
    assert executor.calls == []


def test_mcp_execute_skips_downstream_executor():
    planner = _planner(
        [
            json.dumps(
                {
                    "tool_calls": [
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"subtask": "push button"}}
                    ],
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


def test_monitor_poll_updates_events_and_vlm_continues_reasoning_until_success():
    client = FakeMCPToolClient()
    monitor_statuses = iter(["running", "success"])
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
                    "tool_calls": [
                        {
                            "namespace": "demo_robot",
                            "name": "execute",
                            "arguments": {"subtask": "pick cup"},
                        }
                    ],
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
    assert monitor_calls == [{"subtask": "pick cup"}, {"subtask": "pick cup"}]


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
                    "tool_calls": [
                        {
                            "namespace": "demo_robot",
                            "name": "execute",
                            "arguments": {"subtask": "pick cup"},
                        }
                    ],
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
                    "tool_calls": [
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"subtask": "pick cup"}}
                    ],
                    "current_subtask": "pick cup",
                }
            ),
            json.dumps(
                {
                    "tool_calls": [
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"subtask": "pick cup"}}
                    ],
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
                    "tool_calls": [
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"subtask": "pick cup"}}
                    ],
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


def test_tick_without_reason_skips_vlm_and_preserves_state():
    planner_calls = 0

    def planner_fn(_planner_input):
        nonlocal planner_calls
        planner_calls += 1
        return json.dumps({"current_subtask": "inspect scene", "should_execute": False})

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


def test_execute_status_running_still_starts_monitor_once():
    client = FakeMCPToolClient()
    monitor_calls = []
    client.register(
        "execute",
        lambda args: {"executed": True, "status": "running", "subtask": args.get("subtask")},
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
                    "tool_calls": [
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"subtask": "pick cup"}}
                    ],
                    "current_subtask": "pick cup",
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task")

    assert result.parse_ok is True
    assert [tool_result.tool_name for tool_result in result.tool_results] == ["execute", "monitor"]
    assert monitor_calls == [{"subtask": "pick cup"}]
    assert state.active_execution is not None
    assert state.active_execution.status == "running"


def test_downstream_executor_auto_monitor_terminal_result_becomes_event():
    client = FakeMCPToolClient()
    client.register(
        "monitor",
        lambda args: {"status": "success", "subtask": args.get("subtask"), "monitor_id": "mon-1"},
        namespace="demo_robot",
    )
    planner_inputs = []

    def planner_fn(planner_input):
        planner_inputs.append(planner_input)
        if len(planner_inputs) == 1:
            return json.dumps({"current_subtask": "pick cup"})
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
                    "tool_calls": [
                        {"namespace": "demo_robot", "name": "execute", "arguments": {"subtask": "place cup"}}
                    ],
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
                    "should_execute": False,
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
                    "should_execute": False,
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
                    "should_execute": False,
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
        return json.dumps({"current_subtask": "inspect scene", "should_execute": False})

    metadata = {
        "run_id": "internal-run",
        "planner_visible_metadata": {"robot_type": "dual_franka"},
    }
    loop = AgenticRobotLoop(CallablePlanner(planner_fn), _tool_client(), RecordingExecutor())

    loop.step("task", metadata=metadata)

    assert planner_inputs[-1].metadata == {"robot_type": "dual_franka"}


def test_nonstandard_execute_tool_with_executed_true_skips_downstream_executor():
    client = FakeMCPToolClient()
    client.register("run_subtask", lambda args: {"executed": True}, namespace="demo_robot")
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

    assert any(tr.tool_name == "run_subtask" for tr in result.tool_results)
    assert executor.calls == []


def test_plain_extra_tool_requires_no_loop_config_and_does_not_block_executor():
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
    assert executor.calls[-1].subtask == "pick cup"


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
                    "current_subtask": "grasp cup",
                    "should_execute": False,
                }
            )
        ]
    )
    loop = AgenticRobotLoop(planner, client, RecordingExecutor())

    result, state = loop.step("task")

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
            json.dumps({"current_subtask": "step one", "should_execute": False}),
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
                    "subtasks": ["approach the radio", "press the power button", "tidy the table"],
                    "subtask_index": 0,
                }
            ),
            # step 1: after monitor success, select index 1 from the existing plan.
            json.dumps({"subtask_index": 1}),
            # step 2: after the second monitor success, revise and select a new step.
            json.dumps({"subtasks": ["press the power button", "verify the radio is on"], "subtask_index": 1}),
        ]
    )
    client = FakeMCPToolClient()
    monitor_statuses = iter(["success", "success"])
    client.register(
        "monitor",
        lambda args: {"status": next(monitor_statuses), "subtask": args.get("subtask")},
        namespace="demo_robot",
    )
    executor = RecordingExecutor()
    loop = AgenticRobotLoop(planner, client, executor)

    result0, state = loop.step("turn on the radio and tidy up")
    assert state.subtasks == ["approach the radio", "press the power button", "tidy the table"]
    assert result0.current_subtask == "approach the radio"
    assert executor.calls[-1].subtask == "approach the radio"

    _, state = loop.poll_monitor("turn on the radio and tidy up", state)
    result1, state = loop.step("turn on the radio and tidy up", state, reason_interval_s=0)
    assert result1.current_subtask == "press the power button"
    assert executor.calls[-1].subtask == "press the power button"

    _, state = loop.poll_monitor("turn on the radio and tidy up", state)
    result2, state = loop.step("turn on the radio and tidy up", state, reason_interval_s=0)
    assert state.subtasks == ["press the power button", "verify the radio is on"]
    assert result2.current_subtask == "verify the radio is on"


def test_session_state_round_trips_through_dict():
    state = AgenticSessionState(task="t", current_subtask="s", step_index=3)
    restored = AgenticSessionState.from_dict(state.to_dict())
    assert restored.task == "t"
    assert restored.current_subtask == "s"
    assert restored.step_index == 3
