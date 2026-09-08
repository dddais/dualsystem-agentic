"""Runtime orchestration for robot execution, observation, and monitoring."""

from __future__ import annotations

import threading
import time
import math
from dataclasses import replace
from typing import Protocol

from robot_runtime.core.types import (
    ExecutionRequest,
    ExecutionState,
    JsonDict,
    MonitorState,
    ObservationFrame,
    ObservationImage,
    RobotCapabilities,
    STATUS_FAILED,
    STATUS_RUNNING,
    new_execution_id,
    new_monitor_id,
    normalize_status,
    now_s,
)


class RobotDriver(Protocol):
    def execute(self, request: ExecutionRequest, execution: ExecutionState) -> JsonDict:
        ...

    def stop(self, execution_id: str | None = None) -> JsonDict:
        ...

    def reset(self) -> JsonDict:
        ...

    def emergency_stop(self) -> JsonDict:
        ...

    def status(self) -> JsonDict:
        ...

    def capabilities(self) -> JsonDict:
        ...


class CameraProvider(Protocol):
    def latest(self) -> ObservationFrame:
        ...

    def latest_image(self, camera: str) -> ObservationImage:
        ...

    def health(self) -> JsonDict:
        ...

    def camera_names(self) -> list[str]:
        ...


class MonitorProvider(Protocol):
    def start(self, execution: ExecutionState, request: ExecutionRequest) -> MonitorState:
        ...

    def status(self, monitor: MonitorState) -> MonitorState:
        ...

    def activate(self, monitor: MonitorState) -> MonitorState:
        ...

    def stop(self, monitor_id: str) -> JsonDict:
        ...

    def health(self) -> JsonDict:
        ...


class RobotRuntime:
    """Owns execution and monitor identity for one robot runtime process."""

    def __init__(
        self,
        *,
        robot_type: str,
        robot_driver: RobotDriver,
        camera_provider: CameraProvider,
        monitor_provider: MonitorProvider,
        safety: JsonDict | None = None,
    ) -> None:
        self.robot_type = robot_type
        self.robot_driver = robot_driver
        self.camera_provider = camera_provider
        self.monitor_provider = monitor_provider
        self.safety = dict(safety or {})
        self._executions: dict[str, ExecutionState] = {}
        self._monitors: dict[str, MonitorState] = {}
        self._latest_execution_id: str | None = None
        self._lock = threading.RLock()
        self._driver_lock = threading.RLock()
        self._requests: dict[str, ExecutionRequest] = {}
        self._cancelled: dict[str, threading.Event] = {}
        self._active_execution_id: str | None = None
        self._estop_latched = False
        self._timers: dict[str, threading.Timer] = {}
        self.reference_timeout = float(self.safety.get("monitor_ready_timeout_s", 30.0))
        self.max_execution_s = float(self.safety.get("max_execution_s", 300.0))
        if any(not math.isfinite(v) or v <= 0 for v in (self.reference_timeout, self.max_execution_s)):
            raise ValueError("monitor_ready_timeout_s and max_execution_s must be finite and positive")

    def create_execution(self, payload: JsonDict) -> ExecutionState:
        request = ExecutionRequest.from_payload(payload)
        now = now_s()
        execution = ExecutionState(
            execution_id=request.execution_id or new_execution_id(),
            monitor_id=new_monitor_id(),
            subtask=request.subtask,
            task=request.task,
            subtask_index=request.subtask_index,
            created_at=now,
            updated_at=now,
            metadata=request.metadata,
        )
        with self._lock:
            if self._estop_latched:
                raise ValueError("Reset after emergency stop before executing")
            if execution.execution_id in self._cancelled and execution.execution_id not in self._executions:
                raise ValueError("execution_id was cancelled before startup")
            if execution.execution_id in self._executions:
                if self._requests[execution.execution_id] != request:
                    raise ValueError("execution_id already belongs to a different request")
                return replace(self._executions[execution.execution_id])
            if self._active_execution_id is not None:
                raise ValueError("Stop the active execution before starting another")
            self._executions[execution.execution_id] = execution
            self._requests[execution.execution_id] = request
            cancelled = self._cancelled.setdefault(execution.execution_id, threading.Event())
            self._active_execution_id = execution.execution_id
            self._latest_execution_id = execution.execution_id
            monitor = MonitorState(execution.monitor_id, execution.execution_id, execution.subtask,
                                   subtask_index=execution.subtask_index,
                                   result={"warming_up": True}, message="starting monitor")
            self._monitors[monitor.monitor_id] = monitor
        try:
            deadline = time.monotonic() + self.reference_timeout
            monitor = self.monitor_provider.start(execution, request)
            while True:
                if cancelled.is_set():
                    raise RuntimeError("execution cancelled during monitor startup")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"monitor reference not ready: {monitor.error}")
                if (monitor.execution_id, monitor.monitor_id) != (execution.execution_id, execution.monitor_id):
                    raise RuntimeError("monitor startup returned mismatched IDs")
                if monitor.status != STATUS_RUNNING:
                    raise RuntimeError(monitor.error or "monitor terminated before action start")
                # GRM explicitly reports warming_up. Providers without reference
                # acquisition (e.g. local_memory) can start immediately.
                if monitor.result.get("warming_up") is not True:
                    if monitor.result.get("provider") == "grm" and monitor.result.get("warming_up") is not False:
                        raise RuntimeError("GRM did not report reference readiness")
                    if monitor.result.get("provider") == "grm" and monitor.result.get("inference_enabled") is not False:
                        raise RuntimeError("GRM must support deferred inference before action startup")
                    if monitor.error:
                        raise RuntimeError(monitor.error)
                    break
                cancelled.wait(0.1)
                if not cancelled.is_set():
                    monitor = self.monitor_provider.status(monitor)
            with self._driver_lock:
                if cancelled.is_set():
                    raise RuntimeError("execution cancelled before action start")
                driver_result = self.robot_driver.execute(request, execution)
                _check_driver_result(driver_result, "executed")
                if cancelled.is_set():
                    raise RuntimeError("execution cancelled during driver startup")
                with self._lock:
                    execution.driver_result = dict(driver_result)
                    execution.updated_at = now_s()
                    self._monitors[monitor.monitor_id] = monitor
                    timer = threading.Timer(self.max_execution_s, self._expire_execution, args=(execution.execution_id,))
                    timer.daemon = True
                    self._timers[execution.execution_id] = timer
                    timer.start()
            if monitor.result.get("inference_enabled") is False:
                if cancelled.is_set():
                    raise RuntimeError("execution cancelled before monitor activation")
                monitor = self.monitor_provider.activate(monitor)
                if monitor.result.get("inference_enabled") is not True:
                    raise RuntimeError("monitor did not acknowledge inference activation")
                with self._lock:
                    if cancelled.is_set():
                        raise RuntimeError("execution cancelled during monitor activation")
                    self._monitors[monitor.monitor_id] = monitor
        except Exception as exc:
            # Startup may have reached the driver even if its response was lost.
            cleanup = self.stop({"execution_id": execution.execution_id})
            with self._lock:
                execution.status = STATUS_FAILED
                execution.error = str(exc)
                if cleanup.get("error"):
                    execution.error += f"; cleanup: {cleanup['error']}"
                execution.updated_at = now_s()
                self._monitors[execution.monitor_id] = replace(
                    self._monitors[execution.monitor_id], status=STATUS_FAILED,
                    error=execution.error, message="execution or monitor startup failed")
        return replace(execution)

    def _expire_execution(self, execution_id: str) -> None:
        self.stop({"execution_id": execution_id})
        with self._lock:
            execution = self._executions[execution_id]
            execution.error = "execution time limit exceeded"
            monitor = self._monitors[execution.monitor_id]
            self._monitors[execution.monitor_id] = replace(monitor, error=execution.error)

    def monitor_status(self, payload: JsonDict) -> MonitorState:
        monitor = self._resolve_monitor(payload)
        with self._lock:
            if self._cancelled[monitor.execution_id].is_set():
                return replace(self._monitors[monitor.monitor_id])
        try:
            refreshed = self.monitor_provider.status(monitor)
        except Exception as exc:
            refreshed = MonitorState(
                monitor_id=monitor.monitor_id,
                execution_id=monitor.execution_id,
                subtask=monitor.subtask,
                subtask_index=monitor.subtask_index,
                status=STATUS_FAILED,
                progress=monitor.progress,
                created_at=monitor.created_at,
                updated_at=now_s(),
                error=str(exc),
                message="monitor provider status failed",
                poll_count=monitor.poll_count + 1,
                result={
                    **monitor.result,
                    "provider_error": str(exc),
                },
            )
        refreshed.status = normalize_status(refreshed.status)
        with self._lock:
            if self._cancelled[monitor.execution_id].is_set():
                return replace(self._monitors[monitor.monitor_id])
            self._monitors[refreshed.monitor_id] = refreshed
            execution = self._executions.get(refreshed.execution_id)
            if execution is not None:
                execution.status = refreshed.status
                execution.error = refreshed.error
                execution.updated_at = refreshed.updated_at
        return refreshed

    def latest_observation(self) -> ObservationFrame:
        return self.camera_provider.latest()

    def latest_observation_image(self, camera: str) -> ObservationImage:
        return self.camera_provider.latest_image(camera)

    def environment(self) -> JsonDict:
        return {
            "robot_type": self.robot_type,
            "driver_status": self.robot_driver.status(),
            "camera": self.camera_provider.health(),
            "monitor": self.monitor_provider.health(),
            "latest_execution": self.latest_execution_dict(),
            "safety": dict(self.safety),
        }

    def health(self) -> JsonDict:
        return {
            "status": "running",
            "robot_type": self.robot_type,
            "driver": self.robot_driver.status(),
            "camera": self.camera_provider.health(),
            "monitor": self.monitor_provider.health(),
            "executions": len(self._executions),
            "monitors": len(self._monitors),
        }

    def capabilities(self) -> JsonDict:
        driver_caps = self.robot_driver.capabilities()
        return RobotCapabilities(
            robot_type=self.robot_type,
            cameras=self.camera_provider.camera_names(),
            extra_tools=list(driver_caps.get("extra_tools") or []),
        ).to_dict() | {"driver": driver_caps}

    def stop(self, payload: JsonDict | None = None) -> JsonDict:
        with self._lock:
            execution_id = _optional_text((payload or {}).get("execution_id")) or self._latest_execution_id
            execution = self._executions.get(execution_id)
            # A stop may overtake the client's create request. Remember the ID
            # so that a delayed request cannot launch an action afterwards.
            if execution_id:
                self._cancelled.setdefault(execution_id, threading.Event()).set()
            if execution is None and execution_id:
                return {"stopped": True, "execution_id": execution_id, "not_started": True}
            timer = self._timers.pop(execution_id, None)
            if timer:
                timer.cancel()
        driver_error = None
        try:
            with self._driver_lock:
                # Never let stopping an old ID affect a newer execution.
                with self._lock:
                    stale = self._active_execution_id not in {None, execution_id}
                if stale:
                    driver_result = {"stopped": True, "already_inactive": True}
                else:
                    driver_result = self.robot_driver.stop(execution_id)
                    _check_driver_result(driver_result, "stopped")
        except Exception as exc:
            driver_error = str(exc)
            driver_result = {"stopped": False}
        with self._lock:
            if execution:
                execution.status, execution.error = STATUS_FAILED, driver_error or "monitor stopped"
                execution.updated_at = now_s()
                monitor = self._monitors[execution.monitor_id]
                self._monitors[execution.monitor_id] = replace(
                    monitor, status=STATUS_FAILED, error=driver_error, message="monitor stopped")
            if not driver_error and self._active_execution_id == execution_id:
                self._active_execution_id = None
        # Physical stop has already been attempted, regardless of remote health.
        monitor_error = None
        if execution:
            try:
                result = self.monitor_provider.stop(execution.monitor_id)
                if result.get("stopped") is False:
                    raise RuntimeError(str(result))
            except Exception as exc:
                monitor_error = str(exc)
        return {**driver_result, "stopped": driver_error is None, "execution_id": execution_id,
                "error": driver_error, "monitor_cleanup_error": monitor_error}

    def reset(self) -> JsonDict:
        with self._driver_lock:
            with self._lock:
                if self._active_execution_id is not None:
                    raise RuntimeError("Stop the active execution before reset")
            result = self.robot_driver.reset()
            _check_driver_result(result, "reset")
            with self._lock:
                self._estop_latched = False
            return result

    def emergency_stop(self) -> JsonDict:
        with self._lock:
            self._estop_latched = True
            for cancelled in self._cancelled.values():
                cancelled.set()
            for timer in self._timers.values():
                timer.cancel()
        # Dedicated driver emergency stop must remain callable during execute.
        result = self.robot_driver.emergency_stop()
        _check_driver_result(result, "emergency_stop")
        with self._lock:
            self._active_execution_id = None
            for monitor_id, monitor in self._monitors.items():
                self._monitors[monitor_id] = replace(monitor, status=STATUS_FAILED, error="emergency stop")
            for execution in self._executions.values():
                execution.status, execution.error = STATUS_FAILED, "emergency stop"
        return result

    def close(self) -> None:
        with self._lock:
            active = self._active_execution_id
        if active:
            self.stop({"execution_id": active})

    def latest_execution_dict(self) -> JsonDict | None:
        with self._lock:
            if self._latest_execution_id is None:
                return None
            execution = self._executions.get(self._latest_execution_id)
            return execution.to_dict() if execution else None

    def _resolve_monitor(self, payload: JsonDict) -> MonitorState:
        requested_monitor_id = _optional_text(payload.get("monitor_id"))
        requested_execution_id = _optional_text(
            payload.get("execution_id") or payload.get("task_id") or payload.get("id")
        )
        requested_subtask = _optional_text(payload.get("subtask") or payload.get("current_subtask"))
        requested_index = _optional_int(payload.get("subtask_index"))

        with self._lock:
            if requested_monitor_id:
                monitor = self._monitors.get(requested_monitor_id)
                if monitor is None:
                    raise KeyError(f"unknown monitor_id: {requested_monitor_id}")
                if requested_execution_id and monitor.execution_id != requested_execution_id:
                    raise ValueError(
                        f"monitor_id {requested_monitor_id!r} does not belong to execution_id "
                        f"{requested_execution_id!r}"
                    )
                return monitor

            if requested_execution_id:
                execution = self._executions.get(requested_execution_id)
                if execution is None:
                    raise KeyError(f"unknown execution_id: {requested_execution_id}")
                monitor = self._monitors.get(execution.monitor_id)
                if monitor is None:
                    raise KeyError(f"unknown monitor_id: {execution.monitor_id}")
                return monitor

            candidates = list(self._monitors.values())

        if requested_subtask:
            candidates = [monitor for monitor in candidates if monitor.subtask == requested_subtask]
        if requested_index is not None:
            candidates = [monitor for monitor in candidates if monitor.subtask_index == requested_index]
        if not candidates:
            raise KeyError("no monitor matches request")
        running = [monitor for monitor in candidates if normalize_status(monitor.status) == STATUS_RUNNING]
        if running:
            return max(running, key=lambda monitor: monitor.created_at)
        raise KeyError("no active running monitor matches request")


def _optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _check_driver_result(result: JsonDict, acknowledgement: str) -> None:
    if not isinstance(result, dict) or result.get(acknowledgement) is not True or result.get("error") or any(
        result.get(key) is False for key in ("success", "ok")
    ) or result.get("status") in {"failed", "error"}:
        raise RuntimeError(f"driver {acknowledgement} failed or missing acknowledgement: {result}")
    if acknowledgement == "reset" and (result.get("completed") is False or result.get("status") in {
        "running", "executing", "in_progress", "started",
    }):
        raise RuntimeError("driver reset must acknowledge completion, not just startup")


def _optional_int(value: object | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
