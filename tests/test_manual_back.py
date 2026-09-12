"""Back is a recovery choice, with execution acknowledgement and cancellation."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from robot_runtime.api.app import create_app
from dualsystem_agentic.config import SimpleLoopConfig
from dualsystem_agentic.simple_loop import SimplePhase, SimpleRobotLoop
from test_bridge_adapters import eventually
from test_manual_bridge import make_stack, pending_action
from test_manual_bridge_lifecycle import RuntimeTools, control


def enable_back(scheduler):
    original = scheduler.call
    scheduler.state["actions"] += ["back", "cancel_back"]
    scheduler.state["back"] = {"phase": "idle", "operation_id": None}
    finish = threading.Event()
    outcome = {"phase": "completed", "back": True, "gripper_policy": "replay_history",
               "pre_event_steps": 10, "step_hz": 20}

    def call(request):
        if request.get("name") == "back":
            scheduler.calls.append(request)
            scheduler.state["back"] = {"phase": "running", "operation_id": request["args"]["operation_id"]}
            return {"status": "ok", "back": dict(scheduler.state["back"])}
        if request.get("name") == "cancel_back":
            scheduler.calls.append(request)
            scheduler.state["back"] = {**scheduler.state["back"], "phase": "cancelled", "error": "back cancelled"}
            return {"status": "ok"}
        if request.get("cmd") == "status" and finish.is_set() and scheduler.state["back"]["phase"] == "running":
            scheduler.state["back"].update(outcome)
        return original(request)

    scheduler.call = call
    return finish, outcome


def back(client, request_id=None, execution_id=None, code=200):
    state = client.get("/manual/status").json()["data"]
    args = {"execution_id": execution_id or (state["execution"] or {}).get("execution_id")}
    request_id = request_id or (state.get("pending") or {}).get("request_id")
    if request_id:
        args["request_id"] = request_id
    response = client.post("/manual/bridge/action", json={"name": "back", "args": args})
    assert response.status_code == code, response.text
    return response.json()


def start_stop(runtime, client, pool):
    start = pool.submit(runtime.create_execution, {"execution_id": "one", "subtask": "pick cup"})
    pending_action(client, "execute")
    back(client, code=409)
    control(client, "autonomous")
    start.result(3)
    back(client, code=409)
    control(client, "idle")
    eventually(lambda: runtime.manual_snapshot()["active_execution_id"] is None)


def waiting_after_back(driver):
    def waiting():
        pending = driver.status()["pending"]
        return pending and pending["phase"] == "waiting" and pending["last_back"]
    eventually(waiting)
    return driver.status()["pending"]


@pytest.mark.parametrize("following", ["homing", "teleop"])
def test_back_waits_for_completion_then_remains_stopped_for_homing_or_teleop(following):
    runtime, driver, scheduler, _ = make_stack(takeover=True)
    scheduler.state["modes"] = ["idle", "teleop", "autonomous"]
    finish, _ = enable_back(scheduler)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start_stop(runtime, client, pool)
        recovery = pool.submit(runtime.recover)
        pending = pending_action(client, "recover")
        assert "back" in client.get("/manual/status").json()["data"]["vla_controls"]
        back(client)
        eventually(lambda: scheduler.state["back"]["phase"] == "running")
        back(client, request_id=pending["request_id"])
        assert not recovery.done()
        assert driver.status()["pending"]["choice"] == "back"
        assert runtime.manual_snapshot()["recovery_required"]
        with pytest.raises(ValueError):
            runtime.create_execution({"subtask": "too early"})
        finish.set()
        next_request = waiting_after_back(driver)
        result = next_request["last_back"]
        assert result["back"] and not result["recovered"] and result["recovery_required"]
        assert result["gripper_policy"] == "replay_history" and result["pre_event_steps"] == 10
        assert next_request["request_id"] != pending["request_id"]
        assert not recovery.done() and runtime.manual_snapshot()["recovery_required"]
        assert scheduler.state["mode"] == "idle"
        assert set(runtime.dashboard_lifecycle_controls()) == {"homing", "back", "teleop"}
        with pytest.raises(ValueError):
            runtime.create_execution({"subtask": "still too early"})
        back(client, request_id=pending["request_id"])  # Retry the old click after completion.
        control(client, "homing", request_id=pending["request_id"], code=409)
        assert len([c for c in scheduler.calls if c.get("name") == "back"]) == 1
        assert not any(c.get("name") == "homing" for c in scheduler.calls)
        control(client, following)
        if following == "teleop":
            eventually(lambda: driver.status()["pending"]["phase"] == "adjusting")
            assert not recovery.done() and scheduler.state["mode"] == "teleop"
            control(client, "idle")
        recovered = recovery.result(3)
        assert recovered["recovery_method"] == ("homing" if following == "homing" else "teleop_adjustment")
        assert recovered["back_results"] == [result]
        assert not runtime.manual_snapshot()["recovery_required"]


@pytest.mark.parametrize("failure", ["missing_history", "timeout", "mismatched_id"])
def test_failed_back_keeps_loop_waiting_and_allows_homing(failure):
    runtime, driver, scheduler, robot = make_stack(takeover=True, back_timeout_s=.15 if failure == "timeout" else 2)
    finish, outcome = enable_back(scheduler)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start_stop(runtime, client, pool)
        recovery = pool.submit(runtime.recover)
        pending_action(client, "recover")
        back(client)
        eventually(lambda: scheduler.state["back"]["phase"] == "running")
        if failure == "missing_history":
            outcome.update(phase="failed", error="no retained gripper position")
            finish.set()
        elif failure == "mismatched_id":
            outcome["operation_id"] = "old-operation"
            finish.set()
        pending = waiting_after_back(driver)
        assert pending["last_back"]["phase"] == "failed"
        assert not recovery.done()
        assert runtime.manual_snapshot()["recovery_required"]
        assert driver.status()["last_operation"]["error"]
        assert robot.calls[-1]["cmd"] == "clear_actions"
        with pytest.raises(ValueError):
            runtime.create_execution({"subtask": "next"})
        control(client, "homing")
        assert recovery.result(3)["homed"]


def test_early_back_click_before_loop_recovery_needs_no_second_click():
    runtime, driver, scheduler, _ = make_stack(takeover=True)
    finish, _ = enable_back(scheduler)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start_stop(runtime, client, pool)
        back(client, execution_id="old", code=409)
        back(client)
        eventually(lambda: scheduler.state["back"]["phase"] == "running")
        finish.set()
        waiting_after_back(driver)
        assert runtime.manual_snapshot()["recovery_required"]
        # The loop joins the same recovery while the early dashboard worker owns it.
        recovery = pool.submit(runtime.recover)
        control(client, "homing")
        assert recovery.result(3)["recovery_method"] == "homing"
        eventually(lambda: not runtime.manual_snapshot()["recovery_required"])
        assert [c.get("name") for c in scheduler.calls].count("back") == 1
        assert [c.get("name") for c in scheduler.calls].count("homing") == 1


@pytest.mark.parametrize("after_completion", [False, True])
def test_estop_during_back_or_the_new_waiting_gate_cannot_release_recovery(after_completion):
    runtime, driver, scheduler, _ = make_stack(takeover=True)
    finish, _ = enable_back(scheduler)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start_stop(runtime, client, pool)
        recovery = pool.submit(runtime.recover)
        pending_action(client, "recover")
        back(client)
        eventually(lambda: scheduler.state["back"]["phase"] == "running")
        if after_completion:
            finish.set()
            waiting_after_back(driver)
        runtime.emergency_stop()
        finish.set()  # Late completion cannot re-open the normal recovery gate.
        with pytest.raises(RuntimeError):
            recovery.result(3)
        assert runtime.manual_snapshot()["estop_latched"]
        assert runtime.manual_snapshot()["recovery_required"]
        back(client, code=409)
        control(client, "homing")
        eventually(lambda: not runtime.manual_snapshot()["estop_latched"])


@pytest.mark.parametrize("following", ["homing", "teleop"])
def test_actual_loop_stays_recovering_through_repeated_back(following):
    runtime, driver, scheduler, _ = make_stack(takeover=True)
    scheduler.state["modes"] = ["idle", "teleop", "autonomous"]
    finish, _ = enable_back(scheduler)
    tools = RuntimeTools(runtime)
    loop = SimpleRobotLoop(tools, settings=SimpleLoopConfig(recover_tool="recover_task", require_steering=False,
        instruction_template="pick {target}"), poll_interval_s=.01, write=lambda _: None)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        cycle = pool.submit(loop.run_cycle, "cup")
        pending_action(client, "execute")
        control(client, "autonomous")
        eventually(lambda: runtime.latest_execution_dict()["driver_result"].get("executed"))
        control(client, "idle")
        pending_action(client, "recover")
        for _ in range(2):
            finish.clear()
            back(client)
            eventually(lambda: scheduler.state["back"]["phase"] == "running")
            finish.set()
            waiting_after_back(driver)
            assert loop.phase is SimplePhase.RECOVERING
            assert not cycle.done() and not tools.recoveries
            assert scheduler.state["mode"] == "idle"
        control(client, following)
        if following == "teleop":
            eventually(lambda: driver.status()["pending"]["phase"] == "adjusting")
            assert loop.phase is SimplePhase.RECOVERING
            control(client, "idle")
        cycle.result(3)
        assert loop.phase is SimplePhase.READY
        assert len(tools.recoveries[0]["back_results"]) == 2


def test_unsupported_scheduler_and_reset_only_gate_reject_back():
    runtime, driver, scheduler, _ = make_stack()
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start_stop(runtime, client, pool)
        recovery = pool.submit(runtime.recover)
        pending_action(client, "recover")
        back(client, code=409)
        control(client, "homing")
        recovery.result(3)
        enable_back(scheduler)
        driver.scheduler_status()
        reset = pool.submit(runtime.reset)
        pending_action(client, "reset")
        back(client, code=409)
        control(client, "homing")
        reset.result(3)
