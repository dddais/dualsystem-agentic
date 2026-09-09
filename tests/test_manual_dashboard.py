"""Web targets are an input source for the existing loop, never a task queue."""

from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from fastapi.testclient import TestClient

from dualsystem_agentic.web_input import WebTargetInput
from robot_runtime.adapters.manual.target_input import ManualTargetInput
from robot_runtime.adapters.manual.robot_driver import ManualRobotDriver
from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider
from robot_runtime.api.app import create_app
from robot_runtime.core.runtime import RobotRuntime


def test_ready_input_lease_is_renewed_only_by_the_loop():
    now = [0.0]
    channel = ManualTargetInput(clock=lambda: now[0])
    channel.open("first", "pick {target}", "carrot")
    now[0] = 14
    assert channel.status()["target"] is None
    now[0] = 16
    assert channel.status() is None
    with pytest.raises(ValueError, match="expired"):
        channel.submit("first", "cup")
    channel.open("second", "pick {target}", "carrot")
    now[0] = 30
    channel.poll("second")
    now[0] = 42
    assert channel.submit("second", "")["target"] == "carrot"


def test_ready_input_has_no_queue_and_stale_requests_cannot_affect_next_round():
    channel = ManualTargetInput()
    channel.open("first", "pick {target}", "")
    with pytest.raises(ValueError, match="another loop"):
        channel.open("second", "pick {target}", "")
    channel.submit("first", "carrot")
    assert channel.submit("first", "carrot")["accepted"]
    with pytest.raises(ValueError, match="already been submitted"):
        channel.submit("first", "cup")
    channel.close("first")
    channel.open("second", "pick {target}", "carrot")
    channel.close("first")
    with pytest.raises(ValueError):
        channel.submit("first", "cup")
    assert channel.poll("second")["target"] is None


@pytest.fixture
def manual_api():
    monitor = LocalMemoryMonitorProvider(auto_success_after_polls=1)
    runtime = RobotRuntime(robot_type="x1pro", robot_driver=ManualRobotDriver(operator_timeout_s=2),
                           camera_provider=None, monitor_provider=monitor)
    with TestClient(create_app(runtime)) as client:
        yield client, runtime, monitor


def test_dashboard_reads_cached_scores_without_advancing_monitor(manual_api):
    client, runtime, monitor = manual_api
    from robot_runtime.core.types import ExecutionRequest, ExecutionState
    execution = ExecutionState("e", "m", "pick carrot")
    state = monitor.start(execution, ExecutionRequest("pick carrot"))
    runtime._executions["e"] = execution
    runtime._latest_execution_id = "e"
    runtime._monitors["m"] = state
    for _ in range(5):
        data = client.get("/manual/status").json()["data"]
        assert data["monitor"]["poll_count"] == 0
        assert data["monitor"]["status"] == "running"
    assert monitor._states["m"].poll_count == 0
    assert client.get("/manual/app.js").headers["content-type"].startswith("text/javascript")


def test_web_input_returns_ui_target_and_closes_prompt(manual_api):
    client, _, _ = manual_api
    source = WebTargetInput("http://runtime", "pick {target}")

    def request(method, path, payload=None):
        response = client.request(method, path, json=payload)
        response.raise_for_status()
        return response.json()["data"]

    source._request = request
    with ThreadPoolExecutor() as pool:
        future = pool.submit(source, "ready")
        deadline = time.monotonic() + 2
        while not (pending := client.get("/manual/status").json()["data"]["input"]):
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert client.post("/manual/target", json={"request_id": "stale", "target": "cup"}).status_code == 409
        assert client.post("/manual/target", json={"request_id": pending["request_id"], "target": "carrot"}).status_code == 200
        assert future.result(2) == "carrot"
    assert client.get("/manual/status").json()["data"]["input"] is None
    assert client.post("/manual/target", json={"request_id": pending["request_id"], "target": "cup"}).status_code == 409


@pytest.mark.parametrize("flag", ["_active_execution_id", "_resetting", "_estop_latched"])
def test_input_rejected_during_execution_recovery_or_estop(manual_api, flag):
    client, runtime, _ = manual_api
    payload = {"request_id": "ready", "instruction_template": "pick {target}"}
    assert client.post("/manual/input/open", json=payload).status_code == 200
    setattr(runtime, flag, "busy" if flag == "_active_execution_id" else True)
    try:
        assert client.post("/manual/target", json={"request_id": "ready", "target": "carrot"}).status_code == 409
        assert client.get("/manual/input/ready").status_code == 409
    finally:
        setattr(runtime, flag, None if flag == "_active_execution_id" else False)


def test_frame_proxy_allows_only_registered_style_ids_and_known_cameras(manual_api, monkeypatch):
    from robot_runtime.adapters.dual_franka.monitor_provider import RemoteHTTPMonitorProvider
    client, runtime, _ = manual_api
    provider = RemoteHTTPMonitorProvider(url="http://unused")
    runtime.monitor_provider = provider
    def unexpected(*args, **kwargs):
        raise AssertionError("invalid frame requests must never reach the network")
    monkeypatch.setattr("urllib.request.urlopen", unexpected)
    assert client.get("/manual/monitor/frames/not-an-id/cam_high.png").status_code == 400
    assert client.get(f"/manual/monitor/frames/{'a'*32}/unknown.png").status_code == 400
