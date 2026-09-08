"""Startup, cancellation and remote-monitor regression tests; no hardware."""

import asyncio
import threading
import time
import unittest
from dataclasses import replace

from robot_runtime.core.runtime import RobotRuntime
from robot_runtime.core.types import ExecutionRequest, ExecutionState, MonitorState
from robot_runtime.adapters.dual_franka.robot_driver import PlaceholderDualFrankaRobotDriver
from robot_runtime.adapters.dual_franka.monitor_provider import (
    LocalMemoryMonitorProvider, RemoteHTTPMonitorProvider, _monitor_from_payload,
)


class RecordingDriver(PlaceholderDualFrankaRobotDriver):
    def __init__(self, events):
        super().__init__()
        self.events = events
        self.stopped = threading.Event()

    def execute(self, request, execution):
        self.events.append("execute")
        return super().execute(request, execution)

    def stop(self, execution_id=None):
        self.events.append("stop")
        self.stopped.set()
        return super().stop(execution_id)


class RuntimeContractTests(unittest.TestCase):
    def make_runtime(self, monitor=None, driver=None, **safety):
        runtime = RobotRuntime(robot_type="dual_franka", robot_driver=driver or RecordingDriver([]),
                               camera_provider=None, monitor_provider=monitor or LocalMemoryMonitorProvider(),
                               safety=safety)
        self.addCleanup(runtime.close)
        return runtime

    def test_monitor_outage_never_prevents_driver_stop(self):
        events = []
        class Monitor(LocalMemoryMonitorProvider):
            def stop(self, monitor_id):
                events.append("monitor_stop")
                raise RuntimeError("network down")
        runtime = self.make_runtime(Monitor(), RecordingDriver(events))
        execution = runtime.create_execution({"subtask": "pick carrot"})
        result = runtime.stop({"execution_id": execution.execution_id})
        self.assertEqual(events, ["execute", "stop", "monitor_stop"])
        self.assertTrue(result["stopped"])
        self.assertEqual(result["monitor_cleanup_error"], "network down")
        self.assertTrue(runtime.reset()["reset"])

    def test_reference_is_ready_before_driver_and_queries_are_forwarded(self):
        events = []
        class Monitor(RemoteHTTPMonitorProvider):
            def _request(self, method, path, payload=None):
                if path == "/monitors/start":
                    events.append("monitor_start")
                    self.start_payload = payload
                    return {**payload, "status": "running", "result": {"provider": "grm", "warming_up": True, "inference_enabled": False}}
                if path == "/monitors/status":
                    events.append("reference_ready")
                    return {**payload, "status": "running", "result": {"provider": "grm", "warming_up": False, "inference_enabled": False}}
                if path == "/monitors/activate":
                    events.append("activate")
                    return {**payload, "status": "running", "result": {"inference_enabled": True}}
                return {"stopped": True}
        monitor = Monitor(url="http://unused")
        runtime = self.make_runtime(monitor, RecordingDriver(events))
        execution = runtime.create_execution({"subtask": "把 carrot 放到盘子上", "target_queries": ["carrot"]})
        self.assertEqual(events, ["monitor_start", "reference_ready", "execute", "activate"])
        self.assertEqual(monitor.start_payload["target_queries"], ["carrot"])
        self.assertEqual(execution.driver_result["target_queries"], ["carrot"])

    def test_reference_timeout_never_starts_driver(self):
        events = []
        class Monitor(LocalMemoryMonitorProvider):
            def start(self, execution, request):
                return replace(super().start(execution, request), result={"warming_up": True})
            def status(self, monitor):
                return monitor
        runtime = self.make_runtime(Monitor(), RecordingDriver(events), monitor_ready_timeout_s=0.01)
        execution = runtime.create_execution({"subtask": "pick carrot"})
        self.assertEqual(execution.status, "failed")
        self.assertIn("reference not ready", execution.error)
        self.assertEqual(events, ["stop"])

    def test_driver_rejection_is_not_success(self):
        events = []
        class Driver(RecordingDriver):
            def execute(self, request, execution):
                return {"executed": False, "error": "rejected"}
        runtime = self.make_runtime(driver=Driver(events))
        execution = runtime.create_execution({"subtask": "pick carrot"})
        self.assertEqual(execution.status, "failed")
        self.assertIn("rejected", execution.error)
        self.assertEqual(events, ["stop"])

    def test_stop_during_startup_prevents_late_driver_launch(self):
        entered, release = threading.Event(), threading.Event()
        events = []
        class Monitor(LocalMemoryMonitorProvider):
            def start(self, execution, request):
                entered.set()
                release.wait(2)
                return super().start(execution, request)
        runtime = self.make_runtime(Monitor(), RecordingDriver(events))
        results = []
        thread = threading.Thread(target=lambda: results.append(runtime.create_execution({
            "execution_id": "client-1", "subtask": "pick carrot"})))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(runtime.stop({"execution_id": "client-1"})["stopped"])
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("execute", events)
        self.assertEqual(results[0].status, "failed")

    def test_stop_before_create_and_idempotent_create(self):
        events = []
        runtime = self.make_runtime(driver=RecordingDriver(events))
        runtime.stop({"execution_id": "late"})
        with self.assertRaisesRegex(ValueError, "cancelled"):
            runtime.create_execution({"execution_id": "late", "subtask": "pick carrot"})
        payload = {"execution_id": "e1", "subtask": "pick carrot", "target_queries": ["carrot"]}
        first = runtime.create_execution(payload)
        second = runtime.create_execution(payload)
        self.assertEqual(first.monitor_id, second.monitor_id)
        self.assertEqual(events, ["execute"])
        with self.assertRaises(ValueError):
            runtime.create_execution({**payload, "target_queries": ["cube"]})
        with self.assertRaises(ValueError):
            runtime.create_execution({"execution_id": "e2", "subtask": "pick cube"})

    def test_stop_old_id_does_not_stop_new_execution(self):
        events = []
        runtime = self.make_runtime(driver=RecordingDriver(events))
        first = runtime.create_execution({"subtask": "pick carrot"})
        runtime.stop({"execution_id": first.execution_id})
        runtime.create_execution({"subtask": "pick cube"})
        events.clear()
        runtime.stop({"execution_id": first.execution_id})
        self.assertEqual(events, [])

    def test_watchdog_stops_even_without_client_polling(self):
        driver = RecordingDriver([])
        runtime = self.make_runtime(driver=driver, max_execution_s=0.03)
        runtime.create_execution({"subtask": "pick carrot"})
        self.assertTrue(driver.stopped.wait(1))

    def test_remote_status_does_not_refresh_inference_timestamp(self):
        class Monitor(LocalMemoryMonitorProvider):
            def status(self, monitor):
                return replace(monitor, updated_at=100.0, poll_count=4,
                               result={"inference_updated_at": 100.0, "result_age_s": 999})
        runtime = self.make_runtime(Monitor())
        execution = runtime.create_execution({"subtask": "pick carrot"})
        result = runtime.monitor_status({"execution_id": execution.execution_id})
        self.assertEqual(result.updated_at, 100.0)
        self.assertEqual(result.poll_count, 4)

    def test_remote_response_identity_and_status_validation(self):
        fallback = ExecutionState("e", "m", "pick carrot")
        good = {"execution_id": "e", "monitor_id": "m", "status": "running"}
        for data in ({**good, "monitor_id": "other"}, {**good, "status": "unknown"},
                     {**good, "progress": float("nan")}, {"status": "running"}):
            with self.assertRaises(ValueError):
                _monitor_from_payload(data, fallback=fallback)
        for queries in ([], [""], ["carrot"] * 9):
            with self.assertRaises(ValueError):
                ExecutionRequest.from_payload({"subtask": "pick", "target_queries": queries})

    def test_async_reset_acknowledgement_is_rejected(self):
        class Driver(RecordingDriver):
            def reset(self):
                return {"reset": True, "status": "running"}
        runtime = self.make_runtime(driver=Driver([]))
        with self.assertRaisesRegex(RuntimeError, "completion"):
            runtime.reset()

    def test_slow_monitor_does_not_block_emergency_stop_http(self):
        try:
            import httpx
            from robot_runtime.api.app import create_app
        except ImportError:
            self.skipTest("FastAPI/httpx unavailable")
        entered, release = threading.Event(), threading.Event()
        class Monitor(LocalMemoryMonitorProvider):
            def status(self, monitor):
                entered.set()
                release.wait(1)
                return monitor
        runtime = self.make_runtime(Monitor())
        execution = runtime.create_execution({"subtask": "pick carrot"})
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(runtime)), base_url="http://test") as client:
                poll = asyncio.create_task(client.post("/monitors/status", json={"execution_id": execution.execution_id}))
                try:
                    await asyncio.to_thread(entered.wait, 1)
                    stopped = await asyncio.wait_for(client.post("/control/emergency_stop"), timeout=0.5)
                    self.assertTrue(stopped.json()["data"]["emergency_stop"])
                    self.assertFalse(poll.done())
                finally:
                    release.set()
                    response = await poll
                self.assertEqual(response.json()["data"]["status"], "failed")
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
