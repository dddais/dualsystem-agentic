"""Back is a recovery choice, with execution acknowledgement and cancellation."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from robot_runtime.api.app import create_app
from test_bridge_adapters import eventually
from test_manual_bridge import make_stack, pending_action
from test_manual_bridge_lifecycle import control


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


def test_back_waits_for_robot_completion_deduplicates_click_and_returns_ready():
    runtime, driver, scheduler, _ = make_stack(takeover=True)
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
        result = recovery.result(3)
        assert result["recovered"] and not result["homed"] and "reset" not in result
        assert result["recovery_method"] == "back"
        assert result["completion_basis"] == "sdk_dispatch_and_delay"
        assert result["gripper_policy"] == "replay_history"
        assert result["pre_event_steps"] == 10 and result["step_hz"] == 20
        assert not runtime.manual_snapshot()["recovery_required"]
        assert len([c for c in scheduler.calls if c.get("name") == "back"]) == 1
        assert not any(c.get("name") == "homing" for c in scheduler.calls)


@pytest.mark.parametrize("failure", ["missing_history", "timeout", "estop", "mismatched_id"])
def test_failed_back_cannot_release_recovery_or_estop(failure):
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
        elif failure == "estop":
            runtime.emergency_stop()
            finish.set()  # A late result must not unlock the Runtime.
        with pytest.raises(RuntimeError):
            recovery.result(3)
        assert runtime.manual_snapshot()["recovery_required"]
        assert driver.status()["last_operation"]["error"]
        assert robot.calls[-1]["cmd"] == "clear_actions"
        with pytest.raises(ValueError):
            runtime.create_execution({"subtask": "next"})
        if failure == "estop":
            assert runtime.manual_snapshot()["estop_latched"]
            back(client, code=409)


def test_early_back_click_before_loop_recovery_needs_no_second_click():
    runtime, driver, scheduler, _ = make_stack(takeover=True)
    finish, _ = enable_back(scheduler)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start_stop(runtime, client, pool)
        back(client, execution_id="old", code=409)
        back(client)
        eventually(lambda: scheduler.state["back"]["phase"] == "running")
        finish.set()
        eventually(lambda: not runtime.manual_snapshot()["recovery_required"])
        assert runtime.recover()["recovery_method"] == "back"


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
