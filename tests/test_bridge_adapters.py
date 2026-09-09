"""Operator handoff, timed controls, and camera contracts without hardware."""

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time

import pytest

from robot_runtime.adapters.manual.robot_driver import ManualRobotDriver
from robot_runtime.adapters.robot_bridge.camera_provider import RobotBridgeCameraProvider, CAMERA_VIEWS
from robot_runtime.adapters.robot_bridge.robot_driver import RobotBridgeRobotDriver
from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider
from robot_runtime.core.runtime import RobotRuntime
from robot_runtime.core.types import ExecutionRequest, ExecutionState


def eventually(fn, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


class DeferredMonitor(LocalMemoryMonitorProvider):
    def __init__(self):
        super().__init__()
        self.activated = False

    def start(self, execution, request):
        return replace(super().start(execution, request), result={
            "provider": "grm", "warming_up": False, "inference_enabled": False})

    def activate(self, monitor):
        self.activated = True
        return replace(monitor, result={"inference_enabled": True})


def test_manual_http_waits_for_operator_before_scoring_and_next_task():
    from fastapi.testclient import TestClient
    from robot_runtime.api.app import create_app

    driver, monitor = ManualRobotDriver(operator_timeout_s=2), DeferredMonitor()
    runtime = RobotRuntime(robot_type="x1pro", robot_driver=driver,
                           camera_provider=None, monitor_provider=monitor)
    client = TestClient(create_app(runtime))
    assert client.get("/manual").status_code == 200
    with ThreadPoolExecutor() as pool:
        try:
            execution = pool.submit(client.post, "/executions", json={"execution_id": "one", "subtask": "pick carrot"})
            pending = eventually(lambda: client.get("/manual/status").json()["data"]["pending"])
            assert pending["action"] == "execute"
            assert not execution.done() and not monitor.activated
            assert client.post("/manual/ack", json={"request_id": "wrong"}).status_code == 409
            client.post("/manual/ack", json={"request_id": pending["request_id"]})
            assert execution.result(2).json()["data"]["executed"]
            assert monitor.activated

            stopped = pool.submit(client.post, "/control/stop", json={"execution_id": "one"})
            stop_pending = eventually(lambda: driver.status()["pending"])
            assert stop_pending["action"] == "stop" and not stopped.done()
            # Repeating an old button request never acknowledges a new action.
            client.post("/manual/ack", json={"request_id": pending["request_id"]})
            assert not stopped.done()
            client.post("/manual/ack", json={"request_id": stop_pending["request_id"]})
            assert stopped.result(2).json()["data"]["stopped"]

            reset = pool.submit(client.post, "/control/reset")
            reset_pending = eventually(lambda: driver.status()["pending"])
            assert reset_pending["action"] == "reset"
            assert client.post("/executions", json={"subtask": "next"}).status_code == 400
            client.post("/manual/ack", json={"request_id": reset_pending["request_id"]})
            assert reset.result(2).json()["data"]["reset"]
        finally:
            driver.close()
            runtime.close()


def test_stop_cancels_manual_start_without_waiting_for_start_ack():
    driver, monitor = ManualRobotDriver(operator_timeout_s=2), DeferredMonitor()
    runtime = RobotRuntime(robot_type="x1pro", robot_driver=driver,
                           camera_provider=None, monitor_provider=monitor)
    with ThreadPoolExecutor() as pool:
        try:
            execution = pool.submit(runtime.create_execution, {"execution_id": "one", "subtask": "pick"})
            start = eventually(lambda: driver.status()["pending"])
            stopped = pool.submit(runtime.stop, {"execution_id": "one"})
            pending = eventually(lambda: (p := driver.status()["pending"]) and p["action"] == "stop" and p)
            with pytest.raises(ValueError):
                driver.acknowledge(start["request_id"])
            driver.acknowledge(pending["request_id"])
            assert stopped.result(2)["stopped"]
            assert execution.result(2).status == "failed"
            assert not monitor.activated
        finally:
            driver.close()
            runtime.close()


def test_manual_timeout_does_not_acknowledge_completion():
    driver = ManualRobotDriver(operator_timeout_s=0.01)
    with pytest.raises(RuntimeError, match="timed out"):
        driver.execute(ExecutionRequest("pick"), ExecutionState("one", "mon", "pick"))
    assert driver.status()["pending"] is None
    driver.close()


class Scheduler:
    def __init__(self, *, takeover=False):
        self.calls = []
        self.state = {"actions": ["set_prompt", "toggle_single_step", "homing"],
                      "single_step": True, "prompts": ["pick carrot", "pick cup"],
                      "prompt": "pick carrot"}
        if takeover:
            self.state.update(mode="idle", pending_mode=None)
            self.state["actions"].append("set_mode")

    def call(self, request):
        self.calls.append(request)
        if request["cmd"] == "status":
            return {"status": "ok", "state": dict(self.state)}
        name, args = request["name"], request["args"]
        if name == "set_prompt":
            self.state["prompt"] = self.state["prompts"][args["index"]]
        elif name == "toggle_single_step":
            self.state["single_step"] = not self.state["single_step"]
        elif name == "set_mode":
            self.state["mode"] = args["mode"]
        elif name == "homing":
            self.state["single_step"] = True
        return {"status": "ok"}

    def close(self):
        pass


class Robot:
    def __init__(self):
        self.calls = []

    def call(self, request):
        self.calls.append(request)
        return {"status": "ok", "dropped": 3}

    def close(self):
        pass


def bridge(scheduler=None, robot=None, **kwargs):
    scheduler, robot = scheduler or Scheduler(), robot or Robot()
    config = dict(stop_delay_s=0, reset_delay_s=0, start_delay_s=0)
    config.update(kwargs)
    return RobotBridgeRobotDriver(scheduler_client=scheduler, robot_client=robot,
        emergency_scheduler_client=scheduler, emergency_robot_client=robot, **config), scheduler, robot


@pytest.mark.parametrize("takeover", [False, True])
def test_bridge_fixed_prompt_start_stop_home_and_repeat(takeover):
    driver, scheduler, robot = bridge(Scheduler(takeover=takeover), prompt_map={"cup instruction": 1})
    for index, task in enumerate(["cup instruction", "pick carrot"]):
        execution_id = str(index)
        result = driver.execute(ExecutionRequest(task), ExecutionState(execution_id, "m", task))
        assert result["prompt"] == ("pick cup" if index == 0 else "pick carrot")
        assert result["executed"]
        assert driver.stop(execution_id)["stopped"]
        assert driver.reset()["completion_basis"] == "command_and_delay"
    assert all(call["cmd"] == "clear_actions" for call in robot.calls)
    assert [c.get("name") for c in scheduler.calls].count("homing") == 2
    assert scheduler.state["single_step"] is True
    if takeover:
        assert scheduler.state["mode"] == "idle"


def test_unknown_prompt_cannot_start_or_toggle_scheduler():
    driver, scheduler, robot = bridge()
    with pytest.raises(ValueError, match="not in scheduler prompts"):
        driver.execute(ExecutionRequest("unsupported"), ExecutionState("e", "m", "unsupported"))
    assert not robot.calls
    assert all(c["cmd"] == "status" for c in scheduler.calls)


def test_stop_still_clears_robot_when_scheduler_control_fails():
    class BrokenScheduler(Scheduler):
        def call(self, request):
            raise TimeoutError("scheduler unreachable")
    driver, _, robot = bridge(BrokenScheduler())
    with pytest.raises(RuntimeError, match="scheduler unreachable"):
        driver.stop("one")
    assert robot.calls == [{"cmd": "clear_actions"}] * 2


def test_bridge_settling_delays_and_stop_interrupts_reset():
    driver, scheduler, _ = bridge(reset_delay_s=10)
    runtime = RobotRuntime(robot_type="x1pro", robot_driver=driver,
                           camera_provider=None, monitor_provider=LocalMemoryMonitorProvider())
    first = runtime.create_execution({"execution_id": "one", "subtask": "pick carrot"})
    assert first.status == "running"
    runtime.stop({"execution_id": "one"})
    with ThreadPoolExecutor() as pool:
        reset = pool.submit(runtime.reset)
        eventually(lambda: any(c.get("name") == "homing" for c in scheduler.calls))
        assert not reset.done()
        with pytest.raises(ValueError, match="reset"):
            runtime.create_execution({"subtask": "pick cup"})
        assert runtime.stop({"execution_id": "one"})["stopped"]
        with pytest.raises(RuntimeError, match="cancelled"):
            reset.result(1)
    runtime.close()


def test_old_stop_does_not_cancel_new_bridge_start():
    driver, _, robot = bridge()
    runtime = RobotRuntime(robot_type="x1pro", robot_driver=driver,
                           camera_provider=None, monitor_provider=LocalMemoryMonitorProvider())
    runtime.create_execution({"execution_id": "old", "subtask": "pick carrot"})
    runtime.stop({"execution_id": "old"})
    runtime.create_execution({"execution_id": "new", "subtask": "pick cup"})
    before = len(robot.calls)
    runtime.stop({"execution_id": "old"})
    assert len(robot.calls) == before
    assert driver.status()["active_execution_id"] == "new"
    runtime.close()


def jpeg(value):
    import cv2
    import numpy as np
    return cv2.imencode(".jpg", np.full((16, 16, 3), value, np.uint8))[1].tobytes()


class Camera(Robot):
    def __init__(self):
        super().__init__()
        self.images = {view: [jpeg(i * 70)] for i, view in enumerate(CAMERA_VIEWS.values())}
        self.lag = 15

    def call(self, request):
        self.calls.append(request)
        return {"status": "ok", "obs": {"images": self.images, "obs_lag_ms": self.lag}}


def test_camera_pins_http_views_and_preserves_duplicate_identity():
    from fastapi.testclient import TestClient
    from robot_runtime.api.app import create_app

    source = Camera()
    provider = RobotBridgeCameraProvider(client=source, cache_s=0)
    driver, _, _ = bridge()
    runtime = RobotRuntime(robot_type="x1pro", robot_driver=driver,
                           camera_provider=provider, monitor_provider=LocalMemoryMonitorProvider())
    client = TestClient(create_app(runtime))
    first = client.get("/observations/latest/metadata").json()["data"]
    original = dict(source.images)
    source.images = {view: [jpeg(255)] for view in CAMERA_VIEWS.values()}
    second = client.get("/observations/latest/metadata").json()["data"]
    assert first["frame_id"] != second["frame_id"]
    for camera, view in CAMERA_VIEWS.items():
        response = client.get(first["binary_endpoints"][camera])
        assert response.content == original[view][0]
        assert "x-timestamp" not in response.headers
        assert response.headers["content-type"] == "image/jpeg"
    repeat = provider.latest()
    assert repeat.frame_id == second["frame_id"] and repeat.timestamp is None
    assert base64.b64decode(repeat.images["cam_high"]) == source.images["face_view"][0]
    assert not repeat.metadata["synchronization_verified"]
    assert all(c == {"cmd": "get_obs", "image_ts": [0.0],
                     "image_format": {"encoding": "jpeg", "quality": 90}} for c in source.calls)
    runtime.close()


@pytest.mark.parametrize("failure", ["missing_view", "bad_jpeg", "stale", "nan", "disconnect"])
def test_camera_faults_do_not_serve_stale_latest(failure):
    source = Camera()
    provider = RobotBridgeCameraProvider(client=source, cache_s=0)
    provider.latest()
    if failure == "missing_view":
        del source.images["left_wrist_view"]
    elif failure == "bad_jpeg":
        source.images["face_view"] = [b"not jpeg"]
    elif failure == "stale":
        source.lag = 4000
    elif failure == "nan":
        source.lag = float("nan")
    else:
        def broken(_):
            raise OSError("disconnected")
        source.call = broken
    with pytest.raises(FileNotFoundError):
        provider.latest()
    assert not provider.health()["ready"]


def test_snapshot_eviction_is_explicit():
    source = Camera()
    provider = RobotBridgeCameraProvider(client=source, cache_s=0, max_snapshots=1)
    first = provider.latest()
    source.images["face_view"] = [jpeg(230)]
    provider.latest()
    with pytest.raises(FileNotFoundError, match="expired"):
        provider.snapshot_image(first.frame_id, "cam_high")


def test_manual_and_bridge_configs_build_without_connecting():
    from pathlib import Path
    from robot_runtime.api.app import build_runtime_from_config, load_runtime_config
    root = Path(__file__).resolve().parents[1] / "robot_runtime/robot_runtime/configs"
    for name in ("manual", "robot_bridge"):
        runtime = build_runtime_from_config(load_runtime_config(root / f"{name}.runtime.yaml"))
        assert runtime.robot_driver.capabilities()["driver"] == name
        assert runtime.camera_provider.health()["provider"] == "robot_bridge"
        runtime.close()
