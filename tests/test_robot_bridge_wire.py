"""Exercise the unmodified upstream servers and scheduler on localhost.

Optional: PYTHONPATH=/path/to/robot-bridge pytest tests/test_robot_bridge_wire.py
Only the policy backend and hardware controller are fakes; all transports,
scheduler action handling, observation construction and homing dispatch are real.
"""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

pytest.importorskip("robot_bridge")
import numpy as np
from robot_bridge.policy.backends.base import PolicyBackend
from robot_bridge.policy.server import create_policy_server
from robot_bridge.robot.controllers.base import RobotControllerBase
from robot_bridge.robot.server import create_robot_server
from robot_bridge.scheduler.control import ControlServer
from robot_bridge.scheduler.openpi import OpenPiScheduler
from robot_bridge.utils.image import process_images

from robot_runtime.adapters.robot_bridge.camera_provider import RobotBridgeCameraProvider, CAMERA_VIEWS
from robot_runtime.adapters.robot_bridge.clients import BridgeClient
from robot_runtime.adapters.robot_bridge.robot_driver import RobotBridgeRobotDriver
from robot_runtime.core.types import ExecutionRequest, ExecutionState


def wait_until(fn):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.005)
    raise AssertionError("upstream server or operation did not become ready")


class Policy(PolicyBackend):
    def __init__(self):
        self.prompts = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def get_metadata(self):
        return {"mode": "sm2sm", "state_history_size": 1, "state_future_size": 0,
                "state_step": 1, "policy_hz": 20, "slave_state_dim": 14,
                "model_action_dim": 32, "action_dim": 28, "action_horizon": 4,
                "prompts": ["pick carrot", "pick cup"]}

    def infer(self, obs):
        self.entered.set()
        assert self.release.wait(3)
        self.prompts.append(obs["prompt"])
        return {"actions": np.zeros((4, 28), np.float32)}


class Robot(RobotControllerBase):
    def __init__(self):
        super().__init__({"control_hz": 20})
        self.executed = []
        self.homed = 0
        self.cleared = 0
        self.obs_requests = []

    def get_obs(self, slave_state_ts=None, master_state_ts=None, image_ts=None,
                image_format=None, wait_condition=None):
        self.obs_requests.append((image_ts, image_format))
        result = {"obs_lag_ms": 10}
        if slave_state_ts is not None:
            result["slave_state"] = np.zeros((len(slave_state_ts), 14), np.float32)
        if master_state_ts is not None:
            result["master_state"] = np.zeros((len(master_state_ts), 14), np.float32)
        if image_ts is not None:
            images = {view: [np.full((16, 16, 3), i * 80, np.uint8) for _ in image_ts]
                      for i, view in enumerate(CAMERA_VIEWS.values())}
            result["images"] = process_images(images, image_format)
        return result

    def execute(self, actions, blocking=False):
        self.executed.append(actions)

    def clear_actions(self):
        self.cleared += 1
        return 1

    def is_idle(self):
        return True

    def homing(self):
        self.homed += 1


@pytest.fixture
def stack():
    robot, policy = Robot(), Policy()
    robot_server = create_robot_server(robot, host="127.0.0.1", port=0)
    policy_server = create_policy_server(policy, host="127.0.0.1", port=0)
    threads = []
    for server in (robot_server, policy_server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    try:
        wait_until(lambda: robot_server._ws._server is not None and policy_server._server is not None)
        robot_url = f"ws://127.0.0.1:{robot_server._ws._server.socket.getsockname()[1]}"
        policy_url = f"ws://127.0.0.1:{policy_server._server.socket.getsockname()[1]}"
        scheduler = OpenPiScheduler(policy_url, robot_url, latency_step=0, move_steps=2,
                                    control_hz=20, image_format={"encoding": "jpeg"})
        control = ControlServer(scheduler, host="127.0.0.1", port=0)
        control.start()
        control_url = f"ws://127.0.0.1:{control._server.socket.getsockname()[1]}"
        yield robot, policy, scheduler, robot_url, control_url
    finally:
        policy.release.set()
        if "scheduler" in locals():
            scheduler._single_step = False
            scheduler._step_event.set()
            scheduler._policy_client.close()
            scheduler._robot_client.close()
        if "control" in locals():
            control.stop()
        robot_server.shutdown()
        policy_server.shutdown()
        for thread in threads:
            thread.join(3)


def test_stock_scheduler_and_robot_server_execute_stop_home_and_camera(stack):
    robot, policy, scheduler, robot_url, control_url = stack
    driver = RobotBridgeRobotDriver(control_url, robot_url=robot_url,
                                    start_delay_s=0, stop_delay_s=0.01, reset_delay_s=0.1)
    camera = RobotBridgeCameraProvider(robot_url)
    try:
        frame = camera.latest()
        assert frame.images.keys() == {*CAMERA_VIEWS, "concatenated_image"}
        assert frame.timestamp is None  # the actual stock get_obs format
        execution = ExecutionState("one", "mon", "pick cup")
        assert driver.execute(ExecutionRequest("pick cup"), execution)["executed"]
        assert scheduler.run_iteration() == "ok"
        assert policy.prompts == ["pick cup"] and len(robot.executed) == 1
        assert driver.stop("one")["stopped"]
        assert scheduler._single_step and robot.cleared >= 4
        with ThreadPoolExecutor() as pool:
            reset = pool.submit(driver.reset)
            wait_until(lambda: scheduler._homing_requested)
            # The stock run loop handles homing at the next iteration start.
            scheduler.build_obs_request()
            assert reset.result(2)["reset"]
        assert robot.homed == 1 and scheduler._single_step
    finally:
        driver.close()
        camera.close()


def test_pause_during_stock_infer_gates_old_chunk_until_homing_discards_it(stack):
    robot, policy, scheduler, robot_url, control_url = stack
    driver = RobotBridgeRobotDriver(control_url, robot_url=robot_url,
                                    start_delay_s=0, stop_delay_s=0.01, reset_delay_s=0)
    with ThreadPoolExecutor() as pool:
        try:
            driver.execute(ExecutionRequest("pick cup"), ExecutionState("one", "m", "pick cup"))
            policy.release.clear()
            iteration = pool.submit(scheduler.run_iteration)
            assert policy.entered.wait(1)
            driver.stop("one")
            policy.release.set()
            # It waits at single-step; no old action is sent after stop.
            time.sleep(0.03)
            assert not robot.executed
            driver.reset()  # homing flag invalidates and releases that chunk
            assert iteration.result(2) == "skip"
            scheduler.build_obs_request()
            assert robot.homed == 1 and not robot.executed
        finally:
            policy.release.set()
            scheduler._homing_requested = True
            scheduler._step_event.set()
            driver.close()


def test_json_protocol_rejects_unknown_action_without_affecting_scheduler(stack):
    _, _, scheduler, _, control_url = stack
    client = BridgeClient(control_url, json_protocol=True)
    try:
        with pytest.raises(RuntimeError, match="unknown action"):
            client.call({"cmd": "action", "name": "nonexistent"})
        assert client.call({"cmd": "status"})["state"]["single_step"] is True
        assert scheduler._single_step
    finally:
        client.close()
