"""One finalized instruction reaches the loop; grounding targets stay separate."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from dualsystem_agentic.simple_loop import SimpleRobotLoop
from dualsystem_agentic.task_input import TaskInput
from dualsystem_agentic.web_input import WebTargetInput
from robot_runtime.adapters.manual.target_input import ManualTargetInput
from robot_runtime.adapters.manual.robot_driver import ManualRobotDriver
from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider
from robot_runtime.api.app import create_app
from robot_runtime.core.runtime import RobotRuntime
from robot_runtime.core.types import ExecutionRequest, ExecutionState
from test_bridge_adapters import bridge, eventually
from test_simple_loop import RecordingTools


def channel():
    result = ManualTargetInput()
    result.open("ready", "pick {target}", "old cup", allow_full_instruction=True,
                instruction_templates={"box": "move {target} into {{box}}"})
    return result


def test_named_template_is_resolved_on_server_and_submission_cannot_be_replaced():
    source = channel()
    task = source.submit_task("ready", mode="template", template_id="box", target="red cup")["task"]
    assert task["instruction"] == "move red cup into {box}"
    assert task["target_queries"] == ["red cup"]
    assert source.submit_task("ready", mode="template", template_id="box", target="red cup")["task"] == task
    with pytest.raises(ValueError, match="already been submitted"):
        source.submit_task("ready", mode="instruction", instruction="do something else")
    with pytest.raises(ValueError, match="already been submitted"):
        source.submit("ready", "old cup")
    source.close("ready")
    with pytest.raises(ValueError, match="expired"):
        source.submit_task("ready", mode="template", target="cup")


def test_full_instruction_preserves_unicode_braces_and_newlines_without_reusing_target():
    source = channel()
    instruction = "把红杯放到 {box} 旁边。\n然后松开夹爪。"
    task = source.submit_task("ready", mode="instruction", instruction=instruction)["task"]
    assert task["instruction"] == instruction
    assert task["target"] is None and task["target_queries"] is None
    assert source.status()["task"] == task


@pytest.mark.parametrize("extra", [{"bad": "pick {unknown}"}, {"bad": "pick {target!r}"},
                                     {"bad": "pick {target.x}"}, {"bad": "{{target}}"},
                                     {"default": "pick {target}"}, ["pick {target}"]])
def test_invalid_template_definitions_never_open_a_ready_request(extra):
    source = ManualTargetInput()
    with pytest.raises(ValueError):
        source.open("ready", "pick {target}", "", instruction_templates=extra)
    assert source.status() is None


@pytest.mark.parametrize("payload", [
    {"mode": "instruction", "instruction": " "},
    {"mode": "instruction", "instruction": "x" * 2001},
    {"mode": "instruction", "instruction": "pick cup", "target_queries": []},
    {"mode": "instruction", "instruction": "pick cup", "target_queries": [""]},
    {"mode": "instruction", "instruction": "pick cup", "target_queries": ["cup"] * 9},
    {"mode": "template", "template_id": "missing", "target": "cup"},
    {"mode": "unknown"},
])
def test_invalid_task_does_not_consume_the_round(payload):
    source = channel()
    with pytest.raises(ValueError):
        source.submit_task("ready", **payload)
    assert source.status()["task"] is None


@pytest.mark.parametrize("payload,expected,queries", [
    ({"mode": "template", "template_id": "box", "target": "red cup"}, "put red cup in box", ["red cup"]),
    ({"mode": "instruction", "instruction": "把红杯放在盒子旁。", "target_queries": ["red cup"]}, "把红杯放在盒子旁。", ["red cup"]),
])
def test_web_task_input_and_loop_forward_exact_instruction(payload, expected, queries):
    runtime = RobotRuntime(robot_type="x1pro", robot_driver=ManualRobotDriver(), camera_provider=None,
                           monitor_provider=LocalMemoryMonitorProvider())
    source = WebTargetInput("http://runtime", "pick {target}", allow_full_instruction=True,
                            instruction_templates={"box": "put {target} in box"})
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        def request(method, path, payload=None):
            response = client.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()["data"]
        source._request = request
        future = pool.submit(source, "ready")
        pending = eventually(lambda: client.get("/manual/status").json()["data"]["input"])
        response = client.post("/manual/task", json={"request_id": pending["request_id"], **payload})
        assert response.status_code == 200, response.text
        task = future.result(2)
        assert isinstance(task, TaskInput) and task.instruction == expected
        assert client.get("/manual/status").json()["data"]["input"] is None
        tools = RecordingTools(["running", "success"])
        inputs = iter([task, "q"])
        loop = SimpleRobotLoop(tools, read_input=lambda _: next(inputs), write=lambda _: None, sleep=lambda _: None)
        loop.serve_forever()
        execute, monitor = tools.calls[0][1], tools.calls[1][1]
        assert execute["subtask"] == monitor["subtask"] == expected
        assert execute["options"] == {"prompt_mode": "text"}
        assert execute["target_queries"] == queries


def test_optional_queries_are_omitted_from_execute_instead_of_reusing_last_target():
    tools = RecordingTools(["success"])
    loop = SimpleRobotLoop(tools, write=lambda _: None, sleep=lambda _: None)
    loop.last_target = "old cup"
    loop.run_cycle(TaskInput("move the box aside"))
    assert "target_queries" not in tools.calls[0][1]
    assert tools.calls[0][1]["subtask"] == "move the box aside"


def test_text_instruction_cannot_fall_back_to_an_alias_on_an_old_scheduler():
    driver, scheduler, robot = bridge(prompt_map={"custom task": "pick cup"})
    with pytest.raises(ValueError, match="update robot-bridge"):
        driver.execute(ExecutionRequest("custom task", options={"prompt_mode": "text"}),
                       ExecutionState("one", "m", "custom task"))
    assert not robot.calls and all(c["cmd"] == "status" for c in scheduler.calls)
    driver.close()


def test_text_mode_ignores_aliases_and_verifies_the_applied_prompt():
    driver, scheduler, robot = bridge(prompt_mode="text", prompt_map={"custom task": "pick cup"})
    scheduler.state["actions"].append("set_prompt_text")
    original = scheduler.call
    def call(request):
        if request.get("name") == "set_prompt_text":
            scheduler.state["prompt"] = request["args"]["prompt"]
        return original(request)
    scheduler.call = call
    result = driver.execute(ExecutionRequest("custom task"), ExecutionState("one", "m", "custom task"))
    assert result["prompt"] == "custom task" and result["prompt_index"] is None
    assert not any(c.get("name") == "set_prompt" for c in scheduler.calls)
    driver.stop("one")
    scheduler.call = original  # Simulate a server acknowledging but ignoring text.
    with pytest.raises(RuntimeError, match="did not select"):
        driver.execute(ExecutionRequest("another custom task"), ExecutionState("two", "m2", "another custom task"))
    assert scheduler.state["single_step"]
    driver.close()
