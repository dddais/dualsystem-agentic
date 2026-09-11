"""A saved instruction is reused; each round still requires an explicit Start."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from dualsystem_agentic.config import SimpleLoopConfig
from dualsystem_agentic.simple_loop import SimpleRobotLoop
from dualsystem_agentic.task_input import TaskInput
from dualsystem_agentic.web_input import WebTargetInput
from robot_runtime.adapters.manual.target_input import ManualTargetInput
from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider
from robot_runtime.api.app import create_app
from test_bridge_adapters import eventually
from test_manual_bridge import make_stack, pending_action, DeferredMonitor
from test_manual_bridge_lifecycle import RuntimeTools, control


def setup(monitor=None):
    runtime, driver, scheduler, robot = make_stack(monitor=monitor)
    driver.auto_stop = True
    scheduler.state["actions"].append("set_prompt_text")
    original = scheduler.call
    def call(request):
        if request.get("name") == "set_prompt_text":
            scheduler.state["prompt"] = request["args"]["prompt"]
        return original(request)
    scheduler.call = call
    return runtime, driver, scheduler, robot


def state(client):
    return client.get("/manual/status").json()["data"]


def save(client, instruction="pick cup", **kwargs):
    editor = state(client)["instruction_editor"]
    response = client.post("/manual/instruction", json={"revision": editor["revision"],
        "mode": "instruction", "instruction": instruction, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def ready(client, request_id="round"):
    response = client.post("/manual/input/open", json={"request_id": request_id,
        "instruction_template": "pick {target}", "allow_full_instruction": True})
    assert response.status_code == 200, response.text


def start(client, *, request_id=None, revision=None, code=200):
    current = state(client)
    response = client.post("/manual/bridge/action", json={"name": "set_mode", "args": {
        "mode": "autonomous", "input_request_id": request_id or current["input"]["request_id"],
        "instruction_revision": revision or current["instruction_editor"]["revision"]}})
    assert response.status_code == code, response.text
    return response.json()


def execute_payload(task, execution_id="run"):
    return {"execution_id": execution_id, "subtask": task["instruction"],
            "target_queries": task["target_queries"],
            "options": {"prompt_mode": "text", "manual_start_token": task["start_token"]}}


def test_record_preserves_instruction_metadata_but_uses_original_scheduler_api():
    runtime, driver, scheduler, _ = setup()
    scheduler.state["actions"] += ["toggle_recording", "set_person"]
    original = scheduler.call
    def call(request):
        response = original(request)
        if request.get("name") == "toggle_recording":
            response["recording_info"] = {"id": "video", "state": "starting"}
        return response
    scheduler.call = call
    with TestClient(create_app(runtime)) as client:
        ready(client)
        save(client, "抓取本轮 cup", target_queries=["cup"])
        def record():
            response = client.post("/manual/bridge/action", json={"name": "toggle_recording",
                "args": {"person": "张三", "instruction": "untrusted client override"}})
            assert response.status_code == 200, response.text
            actions = [c for c in scheduler.calls if c.get("name") in {"toggle_recording", "set_person"}]
            assert actions[-2]["name"] == "set_person" and actions[-2]["args"] == {"person": "张三"}
            assert actions[-1]["name"] == "toggle_recording" and actions[-1]["args"] == {}
            return response.json()["data"]["recording_info"]["instruction"]
        assert record() == "抓取本轮 cup"
        start(client)
        task = client.get("/manual/input/round").json()["data"]["task"]
        client.delete("/manual/input/round")
        runtime.create_execution(execute_payload(task))
        save(client, "下一轮抓取 carrot", target_queries=["carrot"])
        assert record() == "抓取本轮 cup"
        assert scheduler.state["prompt"] == "抓取本轮 cup"


def test_saved_template_survives_leases_and_requests_are_immutable():
    now = [0.0]
    channel = ManualTargetInput(clock=lambda: now[0])
    channel.open("one", "pick {target}", "", instruction_templates={"plate": "put {target} on plate"}, allow_full_instruction=True)
    editor = channel.editor()
    saved = channel.save(revision=editor["revision"], templates_revision=editor["templates_revision"],
                         mode="template", template_id="plate", target="red cup")
    assert saved["task"]["instruction"] == "put red cup on plate"
    assert saved["task"]["target_queries"] == ["red cup"]
    assert channel.poll("one")["task"] is None  # Save never consumes a ready lease.
    first = channel.start_saved("one", saved["revision"])["task"]
    assert channel.start_saved("one", saved["revision"])["already_requested"]
    channel.save(revision=saved["revision"], mode="instruction", instruction="把 carrot 放入盒子。", target_queries=["carrot"])
    assert channel.poll("one")["task"] == first
    channel.close("one")
    channel.open("two", "pick {target}", "", allow_full_instruction=True)
    assert channel.poll("two")["task"] is None
    assert channel.editor()["task"]["instruction"] == "把 carrot 放入盒子。"
    second = channel.start_saved("two", channel.editor()["revision"])["task"]
    assert second["start_token"] != first["start_token"]
    now[0] = 16
    assert channel.status() is None
    assert channel.editor()["task"]["instruction"] == second["instruction"]
    with pytest.raises(ValueError, match="expired"):
        channel.start_saved("two", channel.editor()["revision"])


def test_stale_tabs_invalid_drafts_and_changed_templates_do_not_overwrite_saved_text():
    channel = ManualTargetInput()
    editor = channel.editor()
    saved = channel.save(revision=editor["revision"], mode="instruction", instruction="pick cup")
    for payload in ({"instruction": "different", "revision": editor["revision"]},
                    {"instruction": "", "revision": saved["revision"]},
                    {"instruction": "x" * 2001, "revision": saved["revision"]}):
        with pytest.raises(ValueError):
            channel.save(mode="instruction", **payload)
    channel.open("new", "move {target}", "", allow_full_instruction=True)
    with pytest.raises(ValueError, match="templates changed"):
        channel.save(revision=saved["revision"], mode="template", target="carrot", templates_revision=editor["templates_revision"])
    assert channel.editor()["task"]["instruction"] == "pick cup"


def test_one_start_waits_for_reference_and_freezes_instruction_for_both_models():
    monitor = DeferredMonitor()
    runtime, driver, scheduler, robot = setup(monitor)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        ready(client)
        saved = save(client, "把 cup 放到 {box} 旁。\n然后松开夹爪。", target_queries=["cup"])
        assert not runtime._executions and not scheduler.calls and not robot.calls
        start(client)
        task = client.get("/manual/input/round").json()["data"]["task"]
        start(client)  # Repeat before the loop consumes its request.
        client.delete("/manual/input/round")
        save(client, "pick carrot", target_queries=["carrot"])
        execution = pool.submit(runtime.create_execution, execute_payload(task))
        eventually(lambda: runtime._monitors)
        assert not monitor.activated.is_set() and not scheduler.calls
        assert runtime._requests["run"].subtask == saved["task"]["instruction"]
        assert runtime._requests["run"].target_queries == ["cup"]
        monitor.reference_ready.set()
        result = execution.result(3)
        assert result.driver_result["executed"] and monitor.activated.is_set()
        assert scheduler.state["prompt"] == saved["task"]["instruction"]
        assert driver.status()["pending"] is None  # No second Start click.
        before = len(scheduler.calls)
        assert runtime.create_execution(execute_payload(task)).execution_id == "run"
        assert len(scheduler.calls) == before  # Exact execution retry is idempotent.
        runtime.stop()
        recovery = pool.submit(runtime.recover, {"execution_id": "run"})
        pending_action(client, "recover")
        control(client, "homing")
        assert recovery.result(3)["recovered"]
        assert not state(client)["recovery_required"]
        before = len(scheduler.calls)
        with pytest.raises(ValueError, match="manual Start expired or cancelled"):
            runtime.create_execution(execute_payload(task, "replay"))
        assert len(scheduler.calls) == before
        assert "replay" not in runtime._executions


def test_three_rounds_reuse_saved_instruction_and_apply_edits_at_next_start():
    runtime, driver, scheduler, _ = setup(LocalMemoryMonitorProvider(auto_success_after_polls=1))
    source = WebTargetInput("http://runtime", "pick {target}", allow_full_instruction=True)
    loop = SimpleRobotLoop(RuntimeTools(runtime), settings=SimpleLoopConfig(require_steering=False, recover_tool="recover_task"),
                           poll_interval_s=.01, write=lambda _: None)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        def request(method, path, payload=None):
            response = client.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()["data"]
        source._request = request
        save(client, "pick cup", target_queries=["cup"])
        ids = []
        for index, expected in enumerate(["pick cup", "pick cup", "pick carrot"]):
            def cycle():
                loop.run_cycle(source("ready"))
            job = pool.submit(cycle)
            eventually(lambda: state(client)["input"])
            assert not job.done() and driver.status()["pending"] is None
            assert len(runtime._executions) == index
            start(client)
            pending_action(client, "recover")
            ids.append(state(client)["execution"]["execution_id"])
            assert scheduler.state["prompt"] == expected
            if index == 1:
                save(client, "pick carrot", target_queries=["carrot"])
            control(client, "homing")
            job.result(3)
            assert state(client)["instruction_editor"]["task"]["instruction"] == ("pick carrot" if index else "pick cup")
        assert len(set(ids)) == len(runtime._monitors) == 3
        assert [r.subtask for r in runtime._requests.values()] == ["pick cup", "pick cup", "pick carrot"]


@pytest.mark.parametrize("failure", ["wrong_instruction", "wrong_queries", "wrong_token", "estop", "expired"])
def test_start_authorization_rejects_tampering_expiry_and_estop(failure):
    runtime, _, scheduler, _ = setup()
    with TestClient(create_app(runtime)) as client:
        ready(client)
        save(client, target_queries=["cup"])
        start(client)
        task = client.get("/manual/input/round").json()["data"]["task"]
        payload = execute_payload(task)
        if failure == "wrong_instruction":
            payload["subtask"] = "pick carrot"
        elif failure == "wrong_queries":
            payload["target_queries"] = ["carrot"]
        elif failure == "wrong_token":
            payload["options"]["manual_start_token"] = "f" * 32
        elif failure == "expired":
            runtime._web_start["expires"] = 0
        else:
            runtime.emergency_stop()
        before = len(scheduler.calls)
        with pytest.raises(ValueError):
            runtime.create_execution(payload)
        assert not runtime._executions and len(scheduler.calls) == before


def test_start_requires_live_lease_and_current_saved_revision():
    runtime, _, scheduler, _ = setup()
    with TestClient(create_app(runtime)) as client:
        ready(client)
        saved = save(client)
        save(client, "pick carrot")
        start(client, revision=saved["revision"], code=409)
        start(client, request_id="old-page", code=409)
        assert not runtime._executions and not scheduler.calls
