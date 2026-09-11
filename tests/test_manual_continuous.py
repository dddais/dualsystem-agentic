"""auto_stop false leaves execution running until an explicit UI Idle."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from dualsystem_agentic.config import SimpleLoopConfig
from dualsystem_agentic.simple_loop import SimpleRobotLoop, SimplePhase
from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider, RemoteHTTPMonitorProvider
from robot_runtime.api.app import create_app
from robot_runtime.core.types import ExecutionRequest, ExecutionState
from test_bridge_adapters import eventually
from test_manual_bridge import make_stack, pending_action
from test_manual_bridge_lifecycle import RuntimeTools, control


@pytest.mark.parametrize("verdict", ["success", "failed"])
def test_manual_mode_survives_verdict_and_duration_until_idle(verdict):
    runtime, driver, scheduler, _ = make_stack(takeover=True, monitor=LocalMemoryMonitorProvider(
        default_status=verdict, auto_success_after_polls=1 if verdict == "success" else 0))
    runtime.max_execution_s = .02
    loop = SimpleRobotLoop(RuntimeTools(runtime), poll_interval_s=.01, write=lambda _: None,
        settings=SimpleLoopConfig(instruction_template="pick {target}", max_execution_s=.02,
                                  require_steering=False, recover_tool="recover_task"))
    with TestClient(create_app(runtime)) as client, ThreadPoolExecutor() as pool:
        job = pool.submit(loop.run_cycle, "cup")
        pending_action(client, "execute")
        control(client, "autonomous")
        try:
            eventually(lambda: runtime.manual_snapshot()["monitor"]["poll_count"] >= 10)
            snapshot = runtime.manual_snapshot()
            assert snapshot["monitor"]["status"] == snapshot["execution"]["status"] == "running"
            assert snapshot["monitor"]["result"]["status"] == verdict
            assert snapshot["monitor"]["result"]["continuous_monitoring"] is True
            assert driver.status()["pending"] is None and not runtime._timers
            assert not job.done() and loop.phase is SimplePhase.EXECUTING
            assert scheduler.state["mode"] == "autonomous"
        finally:
            control(client, "idle")
            pending_action(client, "recover")
            control(client, "homing")
            job.result(3)
        assert loop.phase is SimplePhase.READY
        assert not runtime.manual_snapshot()["active_execution_id"]


def test_remote_provider_requires_ack_and_forwards_policy():
    provider = RemoteHTTPMonitorProvider(url="http://unused")
    execution = ExecutionState("e", "m", "pick cup")
    request = ExecutionRequest("pick cup", options={"continuous_monitoring": True})
    calls = []
    response = {"monitor_id": "m", "execution_id": "e", "subtask": "pick cup", "status": "running",
                "result": {"provider": "grm"}}
    def remote(method, path, payload):
        calls.append(payload)
        return response
    provider._request = remote
    with pytest.raises(RuntimeError, match="update and restart Monitor"):
        provider.start(execution, request)
    assert calls[-1]["continuous_monitoring"] is True
    response["result"]["continuous_monitoring"] = True
    assert provider.start(execution, request).result["continuous_monitoring"]
    provider.start(execution, ExecutionRequest("pick cup"))
    assert "continuous_monitoring" not in calls[-1]
