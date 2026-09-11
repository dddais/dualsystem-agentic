"""VLA controls and loop recovery share one lifecycle, using simulated hardware."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi.testclient import TestClient

from dualsystem_agentic.config import SimpleLoopConfig
from dualsystem_agentic.core.types import ToolResult
from dualsystem_agentic.simple_loop import SimpleRobotLoop, SimplePhase
from robot_runtime.api.app import create_app, build_runtime_from_config
from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider
from test_bridge_adapters import eventually
from test_manual_bridge import make_stack, pending_action


def control(client, value, *, request_id=None, execution_id=None, code=200):
    if request_id is None and execution_id is None:
        state = client.get("/manual/status").json()["data"]
        request_id = (state.get("pending") or {}).get("request_id")
        execution_id = state["active_execution_id"] or (state["execution"] or {}).get("execution_id")
    args = {"execution_id": execution_id}
    if request_id:
        args["request_id"] = request_id
    if value != "homing":
        args["mode"] = value
    response = client.post("/manual/bridge/action", json={"name": "homing" if value == "homing" else "set_mode", "args": args})
    assert response.status_code == code, response.text
    return response.json()


class RuntimeTools:
    def __init__(self, runtime):
        self.runtime, self.recoveries = runtime, []

    def call_tool(self, name, args, **kwargs):
        if name == "execute":
            data = self.runtime.create_execution(args).to_dict()
        elif name == "monitor":
            data = self.runtime.monitor_status(args).to_dict()
        elif name == "stop_task":
            data = self.runtime.stop(args)
        elif name == "recover_task":
            data = self.runtime.recover(args)
            self.recoveries.append(data)
        else:
            raise AssertionError(name)
        return ToolResult.success(name, data)


@pytest.mark.parametrize("auto_stop", [False, True])
@pytest.mark.parametrize("recovery", ["homing", "teleop"])
def test_two_loop_cycles_use_vla_controls_and_fresh_monitors(auto_stop, recovery):
    runtime, driver, scheduler, _ = make_stack(takeover=True,
        monitor=LocalMemoryMonitorProvider(auto_success_after_polls=1))
    driver.auto_stop = auto_stop
    scheduler.state["modes"] = ["idle", "teleop", "autonomous"]
    tools = RuntimeTools(runtime)
    loop = SimpleRobotLoop(tools, settings=SimpleLoopConfig(recover_tool="recover_task", require_steering=False),
                           poll_interval_s=.01, write=lambda _: None)
    ids = []
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        for target in ("carrot", "cup"):
            # Existing fixed-prompt fixtures use these exact tasks.
            loop.settings.instruction_template = "pick {target}"
            cycle = pool.submit(loop.run_cycle, target)
            start = pending_action(client, "execute")
            ids.append(start["execution_id"])
            control(client, "teleop", code=409)
            control(client, "homing", code=409)
            control(client, "autonomous")
            if not auto_stop:
                eventually(lambda: runtime.manual_snapshot()["monitor"]["poll_count"] >= 4)
                assert driver.status()["pending"] is None
                assert loop.phase is SimplePhase.EXECUTING
                assert scheduler.state["mode"] == "autonomous"
                assert not cycle.done()
                control(client, "idle")
            recovery_request = pending_action(client, "recover")
            assert driver.status()["last_operation"]["result"]["operator_triggered"] is (not auto_stop)
            assert scheduler.state["mode"] == "idle" and not cycle.done()
            assert client.get("/manual/status").json()["data"]["recovery_required"]
            with pytest.raises(ValueError):
                runtime.create_execution({"subtask": "too early"})
            control(client, recovery)
            # Lost HTTP reply must never replay homing or re-enter teleop.
            control(client, recovery, request_id=recovery_request["request_id"])
            if recovery == "teleop":
                eventually(lambda: driver.status()["pending"] and driver.status()["pending"]["phase"] == "adjusting")
                assert scheduler.state["mode"] == "teleop" and not cycle.done()
                old_polls = [m.poll_count for m in runtime._monitors.values()]
                for _ in range(3):
                    client.get("/manual/status")
                assert old_polls == [m.poll_count for m in runtime._monitors.values()]
                control(client, "autonomous", code=409)
                assert client.post("/manual/input/open", json={"request_id": "early", "instruction_template": "pick {target}"}).status_code == 409
                control(client, "idle")
            cycle.result(3)
            assert loop.phase is SimplePhase.READY and scheduler.state["mode"] == "idle"
            assert not runtime.manual_snapshot()["recovery_required"]
        assert len(set(ids)) == len(runtime._monitors) == 2
        assert [c.get("name") for c in scheduler.calls].count("homing") == (2 if recovery == "homing" else 0)
        assert all(r["recovered"] and r["homed"] == (recovery == "homing") for r in tools.recoveries)
        if recovery == "teleop":
            assert all("reset" not in r and r["recovery_method"] == "teleop_adjustment" for r in tools.recoveries)


def test_idle_during_execution_stops_runtime_without_waiting_for_monitor_terminal():
    runtime, driver, scheduler, robot = make_stack(takeover=True)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start = pool.submit(runtime.create_execution, {"execution_id": "one", "subtask": "pick cup"})
        pending_action(client, "execute")
        control(client, "autonomous")
        start.result(3)
        control(client, "idle", execution_id="old", code=409)
        control(client, "teleop", execution_id="one", code=409)
        control(client, "idle", execution_id="one")
        eventually(lambda: runtime.manual_snapshot()["active_execution_id"] is None)
        assert scheduler.state["mode"] == "idle"
        assert runtime.monitor_status({"execution_id": "one"}).status == "failed"
        assert runtime.monitor_provider._states[runtime._executions["one"].monitor_id].status == "failed"
        assert len(robot.calls) == 4  # startup park + stop park, no extra manual stop click
        recovery = pool.submit(runtime.recover, {"execution_id": "one"})
        pending_action(client, "recover")
        control(client, "homing")
        recovery.result(3)
        before = len(scheduler.calls)
        assert runtime.recover({"execution_id": "one"})["recovered"]
        assert len(scheduler.calls) == before


@pytest.mark.parametrize("interrupt", ["timeout", "estop"])
def test_adjustment_cancellation_parks_and_blocks_next_cycle(interrupt):
    runtime, driver, scheduler, _ = make_stack(takeover=True)
    scheduler.state["modes"] = ["idle", "teleop", "autonomous"]
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start = pool.submit(runtime.create_execution, {"subtask": "pick cup"})
        pending_action(client, "execute")
        control(client, "autonomous")
        start.result(3)
        stop = pool.submit(runtime.stop)
        pending_action(client, "stop")
        control(client, "idle")
        stop.result(3)
        recovery = pool.submit(runtime.recover)
        pending_action(client, "recover")
        if interrupt == "timeout":
            driver.operator_timeout_s = .2
        control(client, "teleop")
        eventually(lambda: driver.status()["pending"] and driver.status()["pending"]["phase"] == "adjusting")
        if interrupt == "estop":
            assert runtime.emergency_stop()["emergency_stop"]
        with pytest.raises(RuntimeError):
            recovery.result(3)
        assert scheduler.state["mode"] == "idle" and runtime.manual_snapshot()["recovery_required"]
        with pytest.raises(ValueError):
            runtime.create_execution({"subtask": "pick carrot"})
        driver.operator_timeout_s = 2
        retry = pool.submit(runtime.recover)
        pending_action(client, "recover")
        if interrupt == "estop":
            control(client, "teleop", code=409)
        control(client, "homing")
        assert retry.result(3)["homed"]


def test_auto_stop_is_validated_and_optional_in_runtime_config():
    for value in (False, True):
        runtime = build_runtime_from_config({"robot": {"driver": "manual_bridge", "auto_stop": value},
                                             "recording": {"enabled": False}})
        assert runtime.robot_driver.auto_stop is value
        runtime.close()
    with pytest.raises(ValueError, match="auto_stop"):
        build_runtime_from_config({"robot": {"driver": "manual_bridge", "auto_stop": "false"}})


@pytest.mark.parametrize("during_start", [False, True])
def test_idle_cancels_waiting_or_inflight_start(during_start):
    runtime, driver, scheduler, _ = make_stack(takeover=True, start_delay_s=.3)
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        start = pool.submit(runtime.create_execution, {"execution_id": "one", "subtask": "pick cup"})
        pending = pending_action(client, "execute")
        # Malformed mode must not fall through to the legacy default Start action.
        assert client.post("/manual/bridge/action", json={"name": "set_mode", "args": {"request_id": pending["request_id"]}}).status_code == 409
        if during_start:
            control(client, "autonomous")
            eventually(lambda: driver.status()["pending"]["phase"] == "running")
        control(client, "idle")
        assert start.result(3).status == "failed"
        eventually(lambda: runtime.manual_snapshot()["active_execution_id"] is None)
        assert scheduler.state["mode"] == "idle"
        assert runtime.manual_snapshot()["recovery_required"]


def test_homing_after_estop_without_loop_waiter_needs_only_one_click():
    runtime, driver, scheduler, _ = make_stack(takeover=True)
    with TestClient(create_app(runtime)) as client:
        runtime.emergency_stop()
        control(client, "homing")
        eventually(lambda: not runtime.manual_snapshot()["estop_latched"])
        assert driver.status()["pending"] is None
        assert [c.get("name") for c in scheduler.calls].count("homing") == 1


def test_estop_after_homing_ack_cannot_be_cleared_by_late_reset_result():
    runtime, driver, _, _ = make_stack()
    original = driver.reset
    def late_estop():
        result = original()
        runtime.emergency_stop()
        return result
    driver.reset = late_estop
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        reset = pool.submit(runtime.reset)
        pending_action(client, "reset")
        control(client, "homing")
        with pytest.raises(RuntimeError, match="interrupted"):
            reset.result(3)
        assert runtime.manual_snapshot()["estop_latched"]


def test_home_click_and_loop_recovery_race_share_one_homing(monkeypatch):
    runtime, driver, scheduler, _ = make_stack(takeover=True)
    real_start = threading.Thread.start
    delayed = []
    def delay_dashboard(thread):
        if thread.name == "manual-homing":
            delayed.append(thread)
        else:
            real_start(thread)
    with TestClient(create_app(runtime)) as client:
        runtime.emergency_stop()
        monkeypatch.setattr(threading.Thread, "start", delay_dashboard)
        control(client, "homing")
        # The loop reaches recovery before the dashboard's background worker.
        assert runtime.recover()["homed"]
        assert len(delayed) == 1
        real_start(delayed[0])
        delayed[0].join(2)
        assert not delayed[0].is_alive() and driver.status()["pending"] is None
        assert [c.get("name") for c in scheduler.calls].count("homing") == 1
