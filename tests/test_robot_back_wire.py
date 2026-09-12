"""Real JSON and msgpack transport, Scheduler invalidation and recovery status."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

pytest.importorskip("robot_bridge")

from robot_runtime.adapters.robot_bridge.clients import BridgeClient
from robot_runtime.adapters.robot_bridge.robot_driver import RobotBridgeRobotDriver
from robot_runtime.core.types import ExecutionRequest, ExecutionState
from test_robot_bridge_wire import stack, wait_until


def robot_back(monkeypatch, robot, scheduler):
    finished = threading.Event()
    robot.back_calls = []
    robot.back_requests = []
    robot.back_state = {"phase": "idle", "operation_id": None}
    original_handler = type(robot).handle_back

    def handle_back(self, request):
        self.back_requests.append(dict(request))
        return original_handler(self, request)

    def begin(self, operation_id):
        self.back_calls.append(operation_id)
        self.back_state = {"phase": "running", "operation_id": operation_id}
        return self.back_state

    def status(self):
        if finished.is_set() and self.back_state["phase"] == "running":
            self.back_state = {**self.back_state, "phase": "completed", "back": True,
                               "completion_basis": "sdk_dispatch", "gripper_policy": "replay_history",
                               "pre_event_steps": 10, "step_hz": self.back_requests[-1]["policy_hz"]}
        return self.back_state

    monkeypatch.setattr(type(robot), "back", begin)
    monkeypatch.setattr(type(robot), "handle_back", handle_back)
    monkeypatch.setattr(type(robot), "back_status", status)
    scheduler._robot_capabilities = scheduler._fetch_robot_capabilities()
    return finished


def test_back_discards_inflight_prediction_then_waits_for_robot_ack(stack, monkeypatch):
    robot, policy, scheduler, robot_url, control_url = stack
    finished = robot_back(monkeypatch, robot, scheduler)
    driver = RobotBridgeRobotDriver(control_url, robot_url=robot_url, start_delay_s=0, stop_delay_s=0)
    with ThreadPoolExecutor() as pool:
        try:
            driver.execute(ExecutionRequest("pick cup"), ExecutionState("one", "m", "pick cup"))
            policy.release.clear()
            iteration = pool.submit(scheduler.run_iteration)
            assert policy.entered.wait(1)
            driver.stop("one")
            recovery = pool.submit(driver.back)
            wait_until(scheduler._back_pending)
            policy.release.set()
            assert iteration.result(2) == "skip"
            assert not robot.executed and not robot.back_calls
            operation = pool.submit(scheduler.build_obs_request)
            wait_until(lambda: robot.back_calls)
            assert not recovery.done()
            finished.set()
            result = recovery.result(3)
            operation.result(3)
            assert result["recovery_method"] == "back" and result["homed"] is False
            assert result["gripper_policy"] == "replay_history"
            assert result["pre_event_steps"] == 10 and result["step_hz"] == 20
            assert robot.back_requests[0]["policy_hz"] == 20
            assert scheduler._single_step and scheduler._auto_starting and robot.homed == 0
            assert not robot.executed and len(robot.back_calls) == 1
        finally:
            policy.release.set()
            scheduler._cancel_back({})
            scheduler._step_event.set()
            driver.close()


@pytest.mark.parametrize("started", [False, True])
def test_emergency_cancel_prevents_queued_or_running_back_completion(stack, monkeypatch, started):
    robot, _, scheduler, robot_url, control_url = stack
    finished = robot_back(monkeypatch, robot, scheduler)
    driver = RobotBridgeRobotDriver(control_url, robot_url=robot_url, stop_delay_s=0)
    with ThreadPoolExecutor() as pool:
        try:
            recovery = pool.submit(driver.back)
            wait_until(scheduler._back_pending)
            if started:
                operation = pool.submit(scheduler.build_obs_request)
                wait_until(lambda: robot.back_calls)
            driver.emergency_stop()
            finished.set()
            with pytest.raises(RuntimeError, match="cancel"):
                recovery.result(3)
            if started:
                operation.result(3)
            else:
                scheduler.build_obs_request()
                assert not robot.back_calls
            assert scheduler.get_status()["back"]["phase"] == "cancelled"
            assert scheduler._single_step and robot.cleared >= 4
        finally:
            scheduler._cancel_back({})
            driver.close()


def test_duplicate_back_command_has_one_robot_dispatch(stack, monkeypatch):
    robot, _, scheduler, _, control_url = stack
    finished = robot_back(monkeypatch, robot, scheduler)
    scheduler._policy_hz = 30  # The wire command must use the resolved frequency.
    finished.set()
    client = BridgeClient(control_url, json_protocol=True)
    command = {"cmd": "action", "name": "back", "args": {"operation_id": "same"}}
    try:
        client.call(command)
        client.call(command)
        scheduler.build_obs_request()
        assert client.call(command)["back"]["phase"] == "completed"
        assert robot.back_calls == ["same"]
        assert robot.back_requests[0]["policy_hz"] == 30
        client.call({**command, "args": {"operation_id": "next"}})
        assert client.call(command)["back"]["phase"] == "completed"
        assert scheduler.get_status()["back"]["operation_id"] == "next"
        scheduler._cancel_back({})
    finally:
        client.close()


def test_worker_failure_diagnostics_survive_scheduler_transport(stack, monkeypatch):
    robot, _, scheduler, _, control_url = stack
    robot_back(monkeypatch, robot, scheduler)
    client = BridgeClient(control_url, json_protocol=True)
    diagnostics = {"samples": 250, "gripper_change_threshold": .1,
                   "grippers": {"left": {"min": .2, "max": .24, "range": .04}}}
    try:
        client.call({"cmd": "action", "name": "back", "args": {"operation_id": "small-grip"}})
        with ThreadPoolExecutor() as pool:
            operation = pool.submit(scheduler.build_obs_request)
            wait_until(lambda: robot.back_calls)
            robot.back_state.update(phase="failed", error="no retained gripper interaction; threshold=0.1",
                                    error_code="no_gripper_interaction", diagnostics=diagnostics)
            operation.result(3)
        state = client.call({"cmd": "status"})["state"]["back"]
        assert state["phase"] == "failed" and state["error_code"] == "no_gripper_interaction"
        assert state["diagnostics"] == diagnostics
        assert "threshold=0.1" in state["error"]
    finally:
        client.close()


def test_request_and_cancel_during_observation_still_discards_that_prediction(stack, monkeypatch):
    robot, _, scheduler, _, control_url = stack
    robot_back(monkeypatch, robot, scheduler)
    original = scheduler.build_policy_obs
    client = BridgeClient(control_url, json_protocol=True)

    def observe(obs):
        client.call({"cmd": "action", "name": "back", "args": {"operation_id": "cancel-before-infer"}})
        client.call({"cmd": "action", "name": "cancel_back"})
        # This snapshots the NEW epoch even though recovery never ran.
        return original(obs)

    monkeypatch.setattr(scheduler, "build_policy_obs", observe)
    with ThreadPoolExecutor() as pool:
        try:
            iteration = pool.submit(scheduler.run_iteration)
            assert iteration.result(2) == "skip"
            assert not robot.executed and not robot.back_calls
            assert scheduler._single_step
        finally:
            scheduler._step_event.set()
            client.close()
