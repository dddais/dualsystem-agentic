"""Runtime orchestration for robot execution, observation, and monitoring."""

from __future__ import annotations

import threading
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

    def create_execution(self, payload: JsonDict) -> ExecutionState:
        request = ExecutionRequest.from_payload(payload)
        now = now_s()
        execution = ExecutionState(
            execution_id=new_execution_id(),
            monitor_id=new_monitor_id(),
            subtask=request.subtask,
            task=request.task,
            subtask_index=request.subtask_index,
            created_at=now,
            updated_at=now,
            metadata=request.metadata,
        )
        with self._lock:
            self._executions[execution.execution_id] = execution
            self._latest_execution_id = execution.execution_id

        try:
            driver_result = self.robot_driver.execute(request, execution)
            monitor = self.monitor_provider.start(execution, request)
        except Exception as exc:
            execution.status = STATUS_FAILED
            execution.error = str(exc)
            execution.updated_at = now_s()
            monitor = MonitorState(
                monitor_id=execution.monitor_id,
                execution_id=execution.execution_id,
                subtask=execution.subtask,
                subtask_index=execution.subtask_index,
                status=STATUS_FAILED,
                error=str(exc),
                message="execution or monitor startup failed",
            )
            driver_result = {}
        else:
            execution.driver_result = dict(driver_result or {})
            execution.monitor_id = monitor.monitor_id
            execution.status = normalize_status(monitor.status)
            execution.updated_at = now_s()

        with self._lock:
            self._executions[execution.execution_id] = execution
            self._monitors[monitor.monitor_id] = monitor
        return execution

    def monitor_status(self, payload: JsonDict) -> MonitorState:
        monitor = self._resolve_monitor(payload)
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
        refreshed.updated_at = now_s()
        with self._lock:
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
        execution_id = _optional_text((payload or {}).get("execution_id")) or self._latest_execution_id
        if execution_id:
            with self._lock:
                execution = self._executions.get(execution_id)
                monitor_id = execution.monitor_id if execution else None
            if monitor_id:
                self.monitor_provider.stop(monitor_id)
        return self.robot_driver.stop(execution_id)

    def reset(self) -> JsonDict:
        return self.robot_driver.reset()

    def emergency_stop(self) -> JsonDict:
        return self.robot_driver.emergency_stop()

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
        return max(candidates, key=lambda monitor: monitor.created_at)


def _optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
