"""Click-to-control lifecycle, including HTTP retries and cancellation races."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading

import pytest
from fastapi.testclient import TestClient

from robot_runtime.adapters.manual_bridge.robot_driver import ManualBridgeRobotDriver
from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider
from robot_runtime.api.app import create_app
from robot_runtime.core.runtime import RobotRuntime
from robot_runtime.core.types import ExecutionRequest, ExecutionState
from test_bridge_adapters import Scheduler, bridge, eventually


class DeferredMonitor(LocalMemoryMonitorProvider):
    def __init__(self):
        super().__init__()
        self.reference_ready = threading.Event()
        self.activated = threading.Event()

    def start(self, execution, request):
        state = super().start(execution, request)
        return replace(state, result={"warming_up": True, "inference_enabled": False})

    def status(self, monitor):
        return replace(monitor, result={"warming_up": not self.reference_ready.is_set(),
                                        "inference_enabled": False})

    def activate(self, monitor):
        self.activated.set()
        return replace(monitor, result={"warming_up": False, "inference_enabled": True})


def make_stack(*, takeover=False, monitor=None, **config):
    backend, scheduler, robot = bridge(Scheduler(takeover=takeover), **config)
    driver = ManualBridgeRobotDriver(operator_timeout_s=2, bridge_driver=backend, control_client=scheduler)
    runtime = RobotRuntime(robot_type="x1pro", robot_driver=driver, camera_provider=None,
                           monitor_provider=monitor or LocalMemoryMonitorProvider())
    return runtime, driver, scheduler, robot


def pending_action(client, action):
    def read():
        value = client.get("/manual/status").json()["data"]["pending"]
        return value if value and value["action"] == action else None
    return eventually(read)


def click(client, pending):
    response = client.post("/manual/action", json={"request_id": pending["request_id"]})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["accepted"]


@pytest.mark.parametrize("takeover", [False, True])
def test_http_clicks_drive_full_cycle_and_gate_monitor_activation(takeover):
    monitor = DeferredMonitor()
    runtime, driver, scheduler, robot = make_stack(takeover=takeover, monitor=monitor)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        assert client.get("/manual").status_code == 200
        assert client.get("/manual/bridge/status").status_code == 200
        start = pool.submit(client.post, "/executions", json={"execution_id": "one", "subtask": "pick cup"})
        eventually(lambda: runtime.manual_snapshot()["active_execution_id"])
        assert driver.status()["pending"] is None
        assert not robot.calls and not monitor.activated.is_set()
        assert client.post("/manual/bridge/action", json={"name": "step"}).status_code == 409
        monitor.reference_ready.set()
        pending = pending_action(client, "execute")
        assert not start.done() and not robot.calls
        assert client.post("/manual/ack", json={"request_id": pending["request_id"]}).status_code == 409
        assert client.post("/manual/action", json={"request_id": "stale"}).status_code == 409
        assert client.post("/manual/bridge/action", json={"name": "homing"}).status_code == 409
        click(client, pending)
        result = start.result(3).json()["data"]
        assert result["executed"] and result["driver_result"]["prompt"] == "pick cup"
        assert result["driver_result"]["completion_basis"] == "command_and_delay"
        assert monitor.activated.is_set()
        before = len(robot.calls)
        click(client, pending)  # A lost HTTP response must not start VLA twice.
        assert len(robot.calls) == before
        assert client.post("/manual/bridge/action", json={"name": "set_prompt", "args": {"index": 0}}).status_code == 409

        stop = pool.submit(client.post, "/control/stop", json={"execution_id": "one"})
        stop_pending = pending_action(client, "stop")
        click(client, pending)  # An old button cannot confirm the new stop.
        assert not stop.done() and len(robot.calls) == before
        click(client, stop_pending)
        assert stop.result(3).json()["data"]["stopped"]
        reset = pool.submit(client.post, "/control/reset")
        reset_pending = pending_action(client, "reset")
        assert not any(c.get("name") == "homing" for c in scheduler.calls)
        click(client, reset_pending)
        assert reset.result(3).json()["data"]["reset"]
        assert [c.get("name") for c in scheduler.calls].count("homing") == 1
        assert client.get("/manual/status").json()["data"]["active_execution_id"] is None
        assert client.post("/manual/input/open", json={"request_id": "next", "instruction_template": "pick {target}"}).status_code == 200


def test_command_delay_is_visible_and_clicks_return_without_waiting():
    runtime, driver, scheduler, _ = make_stack(start_delay_s=.2, reset_delay_s=.2)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start = pool.submit(runtime.create_execution, {"execution_id": "one", "subtask": "pick cup"})
        request = pending_action(client, "execute")
        click(client, request)
        eventually(lambda: driver.status()["pending"]["phase"] == "running")
        assert not start.done()
        click(client, request)
        assert start.result(3).driver_result["wait_s"] == .2
        stop = pool.submit(runtime.stop)
        click(client, pending_action(client, "stop"))
        assert stop.result(3)["stopped"]
        reset = pool.submit(runtime.reset)
        click(client, pending_action(client, "reset"))
        eventually(lambda: any(c.get("name") == "homing" for c in scheduler.calls))
        assert not reset.done()
        assert client.post("/manual/input/open", json={"request_id": "too-early", "instruction_template": "pick {target}"}).status_code == 409
        assert reset.result(3)["wait_s"] == .2


def test_failed_command_is_not_reported_complete_or_replayed():
    runtime, driver, scheduler, _ = make_stack()
    original = scheduler.call
    def fail_start(request):
        if request.get("name") == "set_prompt":
            raise OSError("lost command response")
        return original(request)
    scheduler.call = fail_start
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start = pool.submit(runtime.create_execution, {"execution_id": "one", "subtask": "pick cup"})
        selected = pending_action(client, "execute")
        click(client, selected)
        cleanup = pending_action(client, "stop")
        last = driver.status()["last_operation"]
        assert last["phase"] == "failed" and "lost command response" in last["error"]
        click(client, selected)
        assert not start.done() and driver.status()["pending"]["request_id"] == cleanup["request_id"]
        click(client, cleanup)
        assert start.result(3).status == "failed"


@pytest.mark.parametrize("during_command", [False, True])
def test_software_estop_cancels_start_without_waiting_for_operator(during_command):
    runtime, driver, scheduler, robot = make_stack(start_delay_s=10)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start = pool.submit(runtime.create_execution, {"execution_id": "one", "subtask": "pick cup"})
        selected = pending_action(client, "execute")
        if during_command:
            click(client, selected)
            eventually(lambda: scheduler.state["single_step"] is False)
        assert client.post("/control/emergency_stop").json()["data"]["emergency_stop"]
        assert start.result(3).status == "failed"
        assert runtime.manual_snapshot()["estop_latched"] and scheduler.state["single_step"]
        assert len(robot.calls) >= 2
        with pytest.raises(ValueError, match="emergency stop"):
            runtime.create_execution({"subtask": "pick carrot"})
        reset = pool.submit(runtime.reset)
        click(client, pending_action(client, "reset"))
        assert reset.result(3)["reset"]
        assert not runtime.manual_snapshot()["estop_latched"]


@pytest.mark.parametrize("action", ["execute", "reset"])
def test_cancel_between_click_and_bridge_begin_cannot_send_motion(action):
    runtime, driver, scheduler, robot = make_stack()
    entered, release = threading.Event(), threading.Event()
    original = getattr(driver.bridge, action)
    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    setattr(driver.bridge, action, delayed)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(driver.execute, ExecutionRequest("pick cup"), ExecutionState("one", "m", "pick cup")) if action == "execute" else pool.submit(driver.reset)
        selected = eventually(lambda: driver.status()["pending"])
        driver.request_action(selected["request_id"])
        assert entered.wait(1)
        driver.cancel_pending()
        release.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            future.result(2)
        assert not robot.calls and not scheduler.calls
    driver.close()


def test_auxiliary_controls_are_local_to_enabled_driver_and_lifecycle():
    runtime, driver, scheduler, _ = make_stack()
    scheduler.state["actions"].append("toggle_recording")
    with TestClient(create_app(runtime)) as client:
        assert client.post("/manual/bridge/action", json={"name": "toggle_recording"}).status_code == 200
        assert client.post("/manual/bridge/action", json={"name": "set_prompt", "args": {"index": 1}}).status_code == 200
        assert scheduler.state["prompt"] == "pick cup"
        assert client.post("/manual/bridge/action", json={"name": "set_prompt", "args": {"index": True}}).status_code == 409
        assert client.post("/manual/bridge/action", json={"name": "step"}).status_code == 409
        assert client.get("/manual/bridge/log?target=unknown").status_code == 400
        assert client.get("/manual/bridge/log?lines=1001").status_code == 400
        driver._control.call = lambda request: (_ for _ in ()).throw(OSError("disconnected"))
        assert client.get("/manual/bridge/status").status_code == 503
        assert client.get("/manual/status").status_code == 200


def test_plain_manual_cannot_send_bridge_commands():
    from robot_runtime.adapters.manual.robot_driver import ManualRobotDriver
    runtime = RobotRuntime(robot_type="x1pro", robot_driver=ManualRobotDriver(),
                           camera_provider=None, monitor_provider=LocalMemoryMonitorProvider())
    with TestClient(create_app(runtime)) as client:
        assert client.post("/manual/action", json={"request_id": "any"}).status_code == 404
        assert client.get("/manual/bridge/status").status_code == 404
        assert client.post("/manual/bridge/action", json={"name": "step"}).status_code == 404


def test_digit_mode_switch_respects_handoffs_and_cannot_bypass_prompt_lock():
    runtime, driver, scheduler, _ = make_stack()
    scheduler.state["actions"] += ["toggle_digit_mode", "press_digit"]
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        assert client.post("/manual/bridge/action", json={"name":"toggle_digit_mode"}).status_code == 200
        # Raw digit dispatch could resolve to a prompt after a concurrent mode
        # switch. The UI must use the separately guarded set_phase/set_prompt.
        assert client.post("/manual/bridge/action", json={"name":"press_digit", "args":{"digit":0}}).status_code == 409
        start = pool.submit(runtime.create_execution, {"subtask":"pick cup"})
        pending = pending_action(client, "execute")
        assert client.post("/manual/bridge/action", json={"name":"toggle_digit_mode"}).status_code == 409
        click(client, pending)
        assert start.result(3).driver_result["executed"]
        assert client.post("/manual/bridge/action", json={"name":"toggle_digit_mode"}).status_code == 200
        for name, args in [("set_prompt", {"index":0}), ("press_digit", {"digit":0})]:
            assert client.post("/manual/bridge/action", json={"name":name, "args":args}).status_code == 409
        assert scheduler.state["prompt"] == "pick cup"
        assert not any(call.get("name") == "press_digit" for call in scheduler.calls)


def test_delayed_auxiliary_status_does_not_block_estop_or_resume_after_it():
    runtime, driver, scheduler, _ = make_stack()
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start = pool.submit(runtime.create_execution, {"execution_id": "one", "subtask": "pick cup"})
        click(client, pending_action(client, "execute"))
        assert start.result(3).driver_result["executed"]
        entered, release = threading.Event(), threading.Event()
        class DelayedControl:
            def call(self, request):
                assert request["cmd"] == "status"
                entered.set()
                assert release.wait(2)
                return scheduler.call(request)
            def close(self):
                pass
        driver._control = DelayedControl()
        action = pool.submit(runtime.dashboard_action, "toggle_single_step", {})
        assert entered.wait(1)
        try:
            estop = pool.submit(runtime.emergency_stop)
            assert estop.result(1)["emergency_stop"]
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="cancelled"):
            action.result(2)
        assert scheduler.state["single_step"]
