"""Behavioral tests for the keyboard loop; no VLM, GPU, or real robot."""

import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from dualsystem_agentic.core.types import ToolResult
from dualsystem_agentic.config import SimpleLoopConfig
from dualsystem_agentic.simple_loop import SimplePhase, SimpleRobotLoop, main


class RecordingTools:
    def __init__(self, statuses=("running", "success"), *, failures=None, hidden=()):
        self.statuses = iter(statuses)
        self.failures = failures or {}
        self.hidden = hidden
        self.calls = []
        self.closed = False
        self.execution_id = None
        self.poll_count = 0

    def list_tools(self):
        return [{"name": n, "namespace": "dual_franka"}
                for n in ("execute", "monitor", "stop_task", "reset_task") if n not in self.hidden]

    def call_tool(self, name, arguments=None, **kwargs):
        self.calls.append((name, arguments))
        if name in self.failures:
            failure = self.failures[name]
            if isinstance(failure, BaseException):
                raise failure
            return ToolResult.success(name, failure)
        data = {}
        if name == "execute":
            self.execution_id = arguments["execution_id"]
            self.poll_count = 0
        if name in {"execute", "monitor"}:
            self.poll_count += 1
            data = {"execution_id": self.execution_id, "monitor_id": "m1", "status": next(self.statuses),
                    "poll_count": self.poll_count, "result": {"result_age_s": 0,
                    "modes": {"forward": {"steering": {"applied": True, "degraded": False}}}}}
        elif name == "stop_task":
            data = {"stopped": True}
        elif name == "reset_task":
            data = {"reset": True}
        return ToolResult.success(name, data)

    def close(self):
        self.closed = True


class SimpleLoopTests(unittest.TestCase):
    def make_loop(self, client, **kwargs):
        return SimpleRobotLoop(client, write=lambda _: None, sleep=lambda _: None, **kwargs)

    def test_two_keyboard_cycles_and_quit(self):
        client = RecordingTools(["running", "success", "progress", "failed"])
        inputs = iter(["", "carrot", "", "q"])
        loop = self.make_loop(client, read_input=lambda _: next(inputs))
        loop.serve_forever()
        self.assertEqual([n for n, _ in client.calls],
                         ["execute", "monitor", "stop_task", "reset_task"] * 2)
        self.assertEqual(client.calls[1][1], {
            "execution_id": client.calls[0][1]["execution_id"], "monitor_id": "m1",
            "subtask": "pick the carrot and put it on yellow plate", "subtask_index": 0,
        })
        self.assertEqual(client.calls[0][1]["target_queries"], ["carrot"])
        self.assertEqual(client.calls[4][1]["target_queries"], ["carrot"])
        self.assertNotEqual(client.calls[0][1]["execution_id"], client.calls[4][1]["execution_id"])
        self.assertEqual(client.calls[2][1], {"execution_id": client.calls[0][1]["execution_id"]})
        self.assertEqual(loop.phase, SimplePhase.READY)
        self.assertEqual(loop.identity, {})

    def test_poll_interval_and_all_non_running_statuses_recover(self):
        for terminal in ("success", "failed", "unknown", "", "stopped"):
            with self.subTest(terminal=terminal):
                sleeps = []
                client = RecordingTools(["running", "progress", terminal])
                loop = SimpleRobotLoop(client, sleep=sleeps.append, write=lambda _: None, poll_interval_s=0.2)
                loop.run_cycle("pick carrot")
                self.assertEqual(sleeps, [0.2, 0.2])
                self.assertEqual([n for n, _ in client.calls][-2:], ["stop_task", "reset_task"])

    def test_initial_terminal_does_not_poll(self):
        client = RecordingTools(["success"])
        self.make_loop(client).run_cycle("pick carrot")
        self.assertEqual([n for n, _ in client.calls], ["execute", "stop_task", "reset_task"])

    def test_execute_and_monitor_transport_failures_recover(self):
        for name in ("execute", "monitor"):
            with self.subTest(name=name):
                client = RecordingTools(failures={name: RuntimeError("connection lost")})
                loop = self.make_loop(client)
                loop.run_cycle("pick carrot")
                self.assertEqual([n for n, _ in client.calls][-2:], ["stop_task", "reset_task"])
                self.assertEqual(loop.phase, SimplePhase.READY)

    def test_start_rejected_or_identity_missing_recovers(self):
        for data in ({"executed": False}, {"status": "running"}):
            client = RecordingTools(failures={"execute": data})
            self.make_loop(client).run_cycle("pick carrot")
            self.assertEqual([n for n, _ in client.calls], ["execute", "stop_task", "reset_task"])

    def test_wrong_monitor_identity_recovers(self):
        client = RecordingTools(failures={"monitor": {"status": "running", "monitor_id": "other"}})
        self.make_loop(client).run_cycle("pick carrot")
        self.assertEqual([n for n, _ in client.calls], ["execute", "monitor", "stop_task", "reset_task"])

    def test_stop_failure_prevents_reset_and_next_execution(self):
        client = RecordingTools(["failed"], failures={"stop_task": {"stopped": False}})
        loop = self.make_loop(client)
        with self.assertRaises(RuntimeError):
            loop.run_cycle("pick carrot")
        self.assertEqual(loop.phase, SimplePhase.RECOVERING)
        with self.assertRaises(RuntimeError):
            loop.run_cycle("pick cube")
        self.assertEqual([n for n, _ in client.calls], ["execute", "stop_task"])

    def test_reset_failure_does_not_return_ready(self):
        client = RecordingTools(["failed"], failures={"reset_task": RuntimeError("reset failed")})
        loop = self.make_loop(client)
        with self.assertRaises(RuntimeError):
            loop.run_cycle("pick carrot")
        self.assertEqual(loop.phase, SimplePhase.RECOVERING)

    def test_control_error_payload_is_not_a_successful_recovery(self):
        client = RecordingTools(["failed"], failures={"reset_task": {"error": "arm not ready"}})
        loop = self.make_loop(client)
        with self.assertRaises(RuntimeError):
            loop.run_cycle("pick carrot")
        self.assertEqual(loop.phase, SimplePhase.RECOVERING)

    def test_ctrl_c_during_execution_recovers_then_exits(self):
        client = RecordingTools(failures={"monitor": KeyboardInterrupt()})
        loop = self.make_loop(client, read_input=lambda _: "pick carrot")
        loop.serve_forever()
        self.assertEqual([n for n, _ in client.calls], ["execute", "monitor", "stop_task", "reset_task"])
        self.assertEqual(loop.phase, SimplePhase.READY)

    def test_missing_recovery_tool_rejected_before_execute(self):
        client = RecordingTools(hidden=("reset_task",))
        loop = self.make_loop(client, read_input=lambda _: "pick carrot")
        with self.assertRaises(ValueError):
            loop.serve_forever()
        self.assertEqual(client.calls, [])

    def test_invalid_interval_rejected(self):
        for interval in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                self.make_loop(RecordingTools(), poll_interval_s=interval)

    def test_cli_builds_only_mcp_and_closes_client(self):
        config_path = Path(__file__).resolve().parents[1] / "examples/config.simple_loop.yaml"
        client = RecordingTools()
        with patch("dualsystem_agentic.config.build_mcp_client", return_value=client), \
                patch("dualsystem_agentic.config.build_vlm", side_effect=AssertionError("VLM built")), \
                patch.object(SimpleRobotLoop, "serve_forever"):
            self.assertEqual(main(["--config", str(config_path)]), 0)
        self.assertTrue(client.closed)

    def test_real_mcp_adapter_dispatches_runtime_lifecycle(self):
        try:
            import httpx
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("MCP/httpx unavailable")
        from robot_runtime.core.runtime import RobotRuntime
        from robot_runtime.adapters.dual_franka.monitor_provider import LocalMemoryMonitorProvider
        from robot_runtime.adapters.dual_franka.robot_driver import PlaceholderDualFrankaRobotDriver

        path = Path(__file__).resolve().parents[1] / "mcp_server/dual_franka_mcp_server/server.py"
        spec = importlib.util.spec_from_file_location("simple_loop_adapter_test", path)
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        server.ENABLE_RESET = True
        server.UNKNOWN_STATUS = "failed"
        self.assertEqual(server._derive_monitor_status({"status": "progress"}), "running")

        events = []

        class Driver(PlaceholderDualFrankaRobotDriver):
            def execute(self, request, execution):
                events.append("driver.execute")
                return super().execute(request, execution)

            def stop(self, execution_id=None):
                events.append("driver.stop")
                return super().stop(execution_id)

            def reset(self):
                events.append("driver.reset")
                return super().reset()

        class Monitor(LocalMemoryMonitorProvider):
            def start(self, execution, request):
                events.append("monitor.start")
                return super().start(execution, request)

            def stop(self, monitor_id):
                events.append("monitor.stop")
                return super().stop(monitor_id)

        runtime = RobotRuntime(robot_type="dual_franka", robot_driver=Driver(), camera_provider=None,
                               monitor_provider=Monitor(auto_success_after_polls=2))
        routes = []

        def http_handler(request):
            import json
            payload = json.loads(request.content) if request.content else {}
            route = request.url.path
            routes.append(route)
            if route == "/executions":
                data = {"executed": True, **runtime.create_execution(payload).to_dict()}
            elif route == "/monitors/status":
                data = runtime.monitor_status(payload).to_dict()
            elif route == "/control/stop":
                data = runtime.stop(payload)
            elif route == "/control/reset":
                data = runtime.reset()
            else:
                raise AssertionError(route)
            return httpx.Response(200, json={"success": True, "data": data, "message": "ok"})

        class Adapter(RecordingTools):
            def call_tool(self, name, arguments=None, **kwargs):
                async def dispatch():
                    async with httpx.AsyncClient(base_url="http://robot.test", transport=httpx.MockTransport(http_handler)) as client:
                        return await server._dispatch(client, name, arguments or {})
                return ToolResult.success(name, asyncio.run(dispatch()))

        loop = self.make_loop(Adapter(), settings=SimpleLoopConfig(require_steering=False))
        loop.run_cycle("pick carrot")
        self.assertEqual(routes, ["/executions", "/monitors/status", "/monitors/status", "/control/stop", "/control/reset"])
        self.assertEqual(events, ["monitor.start", "driver.execute", "driver.stop", "monitor.stop", "driver.reset"])
        self.assertEqual(loop.phase, SimplePhase.READY)

    def test_template_validation_and_default_target(self):
        for template in ("pick carrot", "pick {wrong}", "pick {target.name}", "pick {target!r}"):
            with self.assertRaises(ValueError):
                self.make_loop(RecordingTools(), settings=SimpleLoopConfig(instruction_template=template))
        client = RecordingTools(["success"])
        loop = self.make_loop(client, settings=SimpleLoopConfig(
            instruction_template="把 {target} 放到盘子上", default_target="white cube"))
        loop.run_cycle("")
        self.assertEqual(client.calls[0][1]["subtask"], "把 white cube 放到盘子上")
        self.assertEqual(client.calls[0][1]["target_queries"], ["white cube"])

    def test_degraded_steering_and_stale_results_trigger_recovery(self):
        for result in (
            {"modes": {"forward": {"steering": {"applied": False, "degraded": True}}}},
            {"result_age_s": 9999},
        ):
            class BadResultTools(RecordingTools):
                def call_tool(self, name, arguments=None, **kwargs):
                    response = super().call_tool(name, arguments, **kwargs)
                    if name == "execute":
                        return ToolResult.success(name, {**response.data, "result": result})
                    return response
            client = BadResultTools(["running"])
            loop = self.make_loop(client)
            loop.run_cycle("carrot")
            self.assertEqual([name for name, _ in client.calls], ["execute", "stop_task", "reset_task"])

    def test_first_result_stall_and_overall_timeouts(self):
        for count, first, stale, total in ((0, 1, 10, 10), (1, 10, 1, 10), (1, 10, 10, 1)):
            now = [0.0]
            class FrozenTools(RecordingTools):
                def call_tool(self, name, arguments=None, **kwargs):
                    response = super().call_tool(name, arguments, **kwargs)
                    if name in {"execute", "monitor"}:
                        return ToolResult.success(name, {**response.data, "poll_count": count})
                    return response
            client = FrozenTools(["running"] * 10)
            loop = SimpleRobotLoop(client, clock=lambda: now[0],
                sleep=lambda delay: now.__setitem__(0, now[0] + delay), write=lambda _: None,
                settings=SimpleLoopConfig(first_result_timeout_s=first, result_timeout_s=stale,
                                          max_execution_s=total))
            loop.run_cycle("carrot")
            self.assertEqual([name for name, _ in client.calls][-2:], ["stop_task", "reset_task"])
            self.assertLessEqual(now[0], 2)


if __name__ == "__main__":
    unittest.main()
