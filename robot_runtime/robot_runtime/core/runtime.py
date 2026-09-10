"""Runtime orchestration for robot execution, observation, and monitoring."""

from __future__ import annotations

import threading
import logging
import time
import math
from dataclasses import replace
from copy import deepcopy
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
        recording: JsonDict | None = None,
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
        self._resetting = False
        self._timers: dict[str, threading.Timer] = {}
        self.reference_timeout = float(self.safety.get("monitor_ready_timeout_s", 30.0))
        self.max_execution_s = float(self.safety.get("max_execution_s", 300.0))
        if any(not math.isfinite(v) or v <= 0 for v in (self.reference_timeout, self.max_execution_s)):
            raise ValueError("monitor_ready_timeout_s and max_execution_s must be finite and positive")
        self.recorder = None
        self._recovery_required = False
        self._recoveries = {}
        self._dashboard_stops = set()
        self._dashboard_threads = []
        self._emergency_generation = 0
        if recording is not None and recording.get("enabled", True):
            if not hasattr(robot_driver, "scheduler_status"):
                raise ValueError("progress recording requires the manual_bridge driver")
            from robot_runtime.recording import ProgressRecorder
            self.recorder = ProgressRecorder(self, **{k: v for k, v in recording.items() if k != "enabled"})

    def start(self):
        if self.recorder is not None:
            self.recorder.start()

    def recording_status(self):
        return self.recorder.status() if self.recorder is not None else {"enabled": False}

    def recording_monitors(self, since):
        with self._lock:
            return deepcopy([{**m.to_dict(), "target_queries": self._requests[m.execution_id].target_queries}
                for m in self._monitors.values()
                if m.updated_at >= since or self._executions[m.execution_id].updated_at >= since
                or m.execution_id == self._active_execution_id])

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
            if self._resetting:
                raise ValueError("Wait for reset before starting another execution")
            if self._recovery_required:
                raise ValueError("Complete homing or teleoperation adjustment before the next execution")
            self._executions[execution.execution_id] = execution
            self._requests[execution.execution_id] = request
            cancelled = self._cancelled.setdefault(execution.execution_id, threading.Event())
            self._active_execution_id = execution.execution_id
            self._recovery_required = hasattr(self.robot_driver, "recover")
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

    def latest_observation_image(self, camera: str, frame_id: str | None = None) -> ObservationImage:
        if frame_id is not None:
            snapshot_image = getattr(self.camera_provider, "snapshot_image", None)
            if snapshot_image is None:
                raise KeyError("camera provider does not retain snapshots")
            return snapshot_image(frame_id, camera)
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
            # Unblock operator/startup/reset waits before acquiring the driver
            # lock. A delayed stop for an older task must not cancel a new one.
            if self._active_execution_id in {None, execution_id}:
                cancel = getattr(self.robot_driver, "cancel_pending", None)
                if cancel is not None:
                    cancel(execution_id)
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
                self._resetting = True
                emergency_generation = self._emergency_generation
            try:
                result = self.robot_driver.reset()
                _check_driver_result(result, "reset")
                with self._lock:
                    if emergency_generation != self._emergency_generation:
                        raise RuntimeError("software stop interrupted recovery")
                    self._estop_latched = False
                    self._recovery_required = False
                    self._recoveries[self._latest_execution_id] = {**result, "recovered": True,
                        "recovery_method": "homing", "homed": True}
                return result
            finally:
                with self._lock:
                    self._resetting = False

    def recover(self, payload: JsonDict | None = None) -> JsonDict:
        """Complete the recovery branch without misreporting adjustment as homing."""
        if not hasattr(self.robot_driver, "recover"):
            return {**self.reset(), "recovered": True, "recovery_method": "homing", "homed": True}
        with self._driver_lock:
            with self._lock:
                execution_id = (payload or {}).get("execution_id") or self._latest_execution_id
                if execution_id != self._latest_execution_id or self._active_execution_id is not None:
                    raise ValueError("Stop the current execution before recovery")
                if execution_id in self._recoveries and not self._estop_latched:
                    return deepcopy(self._recoveries[execution_id])
                self._resetting = True
                allow_adjustment = not self._estop_latched
                emergency_generation = self._emergency_generation
            try:
                result = self.robot_driver.recover(allow_adjustment=allow_adjustment)
                _check_driver_result(result, "recovered")
                with self._lock:
                    if emergency_generation != self._emergency_generation:
                        raise RuntimeError("software stop interrupted recovery")
                    self._recovery_required = False
                    self._estop_latched = False
                    self._recoveries[execution_id] = deepcopy(result)
                return result
            finally:
                with self._lock:
                    self._resetting = False

    def emergency_stop(self) -> JsonDict:
        with self._lock:
            self._estop_latched = True
            self._emergency_generation += 1
            self._recovery_required = hasattr(self.robot_driver, "recover")
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
        try:
            if active:
                self.stop({"execution_id": active})
        finally:
            if self.recorder is not None:
                self.recorder.close()
            for provider in (self.robot_driver, self.camera_provider, self.monitor_provider):
                close = getattr(provider, "close", None)
                if close is not None:
                    close()

    def latest_execution_dict(self) -> JsonDict | None:
        with self._lock:
            if self._latest_execution_id is None:
                return None
            execution = self._executions.get(self._latest_execution_id)
            return execution.to_dict() if execution else None

    def manual_snapshot(self) -> JsonDict:
        """Read cached state only; opening a dashboard must not poll the provider."""
        with self._lock:
            execution = self._executions.get(self._latest_execution_id)
            monitor = self._monitors.get(execution.monitor_id) if execution else None
            return deepcopy({
                "execution": execution.to_dict() if execution else None,
                "monitor": monitor.to_dict() if monitor else None,
                "active_execution_id": self._active_execution_id,
                "resetting": self._resetting,
                "estop_latched": self._estop_latched,
                "recovery_required": self._recovery_required,
                "vla_controls": self.dashboard_lifecycle_controls() if hasattr(self.robot_driver, "recover") else [],
            })

    def dashboard_lifecycle_controls(self) -> list[str]:
        with self._lock:
            state = self.robot_driver.status()
            if state.get("pending"):
                if self._estop_latched:
                    return [c for c in state.get("pending_controls", []) if c == "homing"]
                if state["pending"]["action"] == "execute":
                    return [*state.get("pending_controls", []), "idle"]
                return state.get("pending_controls", [])
            if self._resetting:
                return []
            if self._estop_latched:
                return ["homing"]
            if self._active_execution_id:
                return ["idle"]
            if self._recovery_required:
                return ["homing"]
            return []

    def dashboard_lifecycle_action(self, control: str, args: JsonDict) -> JsonDict:
        # This path must remain available while the worker holds _driver_lock
        # waiting for an operator choice. Never proxy mode changes directly.
        if not isinstance(control, str) or control not in {"idle", "teleop", "autonomous", "homing"}:
            raise ValueError("unsupported VLA control")
        if "request_id" in args and not isinstance(args["request_id"], str):
            raise ValueError("request_id must be a string")
        with self._lock:
            pending = self.robot_driver.status().get("pending")
            cancel_start = pending and pending["action"] == "execute" and control == "idle" and args.get("request_id") == pending["request_id"]
            if args.get("request_id") and not cancel_start:
                # Driver deduplicates (request, control), including lost replies
                # retried after completion. Replays cannot affect a newer gate.
                if self._estop_latched and control != "homing":
                    raise ValueError("software stop is latched")
                return self.robot_driver.request_action(args["request_id"], control)
            if control not in self.dashboard_lifecycle_controls():
                raise ValueError("VLA control is unavailable in the current loop phase")
            if pending and not cancel_start:
                if args.get("request_id") != pending["request_id"]:
                    raise ValueError("pending request_id is required; refresh the controls")
                return self.robot_driver.request_action(pending["request_id"], control)
            execution_id = args.get("execution_id")
            if execution_id != self._active_execution_id and not (control == "homing" and (self._estop_latched or self._recovery_required)
                                                                 and execution_id == self._latest_execution_id):
                raise ValueError("execution_id is stale; refresh the controls")
            if control == "idle":
                if execution_id in self._dashboard_stops:
                    return {"accepted": True, "already_requested": True}
                self._dashboard_stops.add(execution_id)
                self.robot_driver.request_stop(execution_id)
                operation = lambda: self.stop({"execution_id": execution_id})
            else:
                # A Home click can arrive before the loop's recovery RPC, or
                # after that RPC failed. It must not require a second click.
                self.robot_driver.request_home()
                operation = lambda: self.recover({"execution_id": execution_id})
                self._resetting = True
            emergency_generation = self._emergency_generation

            def run():
                try:
                    with self._lock:
                        if control == "homing" and emergency_generation != self._emergency_generation:
                            self._resetting = False
                            return
                    operation()
                except Exception:
                    logging.getLogger(__name__).exception("Dashboard lifecycle control failed")
            worker = threading.Thread(target=run, daemon=True, name=f"manual-{control}")
            self._dashboard_threads = [t for t in self._dashboard_threads if t.is_alive()]
            self._dashboard_threads.append(worker)
            worker.start()
            return {"accepted": True, "execution_id": execution_id}

    def dashboard_allowed_actions(self) -> list[str]:
        """Ancillary scheduler controls must respect the execution lifecycle."""
        with self._lock:
            driver = self.robot_driver
            if self._estop_latched or self._resetting or driver.status().get("pending"):
                return []
            execution = self._executions.get(self._active_execution_id)
            if self._active_execution_id:
                if (execution is None or not execution.driver_result.get("executed")
                        or self._cancelled[execution.execution_id].is_set()):
                    return []
                # Keep the prompt seen by VLA consistent with the GRM task.
                return sorted((driver.SETTINGS - {"set_prompt"}) | driver.MOTION)
            return sorted(driver.SETTINGS)

    def dashboard_action(self, name: str, args: JsonDict) -> JsonDict:
        if name in {"set_mode", "homing"}:
            return self.dashboard_lifecycle_action("homing" if name == "homing" else args.get("mode"), args)
        # A manual handoff holds _driver_lock; reject promptly rather than
        # queueing a stale button behind several minutes of operator waiting.
        if not self._driver_lock.acquire(blocking=False):
            raise ValueError("finish the pending start/stop/reset operation first")
        try:
            with self._lock:
                if name not in self.dashboard_allowed_actions():
                    raise ValueError("scheduler action is unavailable in the current runtime phase")
                cancelled = self._cancelled.get(self._active_execution_id)
            # Keep the runtime state lock free for the independent emergency
            # stop path while scheduler I/O is pending. The execution token
            # prevents a delayed status reply from issuing a resume afterwards.
            result = self.robot_driver.scheduler_action(name, args, cancelled=cancelled)
            if name == "toggle_recording" and self.recorder is not None:
                self.recorder.notify(result)
            return result
        finally:
            self._driver_lock.release()

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
    if acknowledgement in {"reset", "recovered"} and (result.get("completed") is False or result.get("status") in {
        "running", "executing", "in_progress", "started",
    }):
        raise RuntimeError("driver reset must acknowledge completion, not just startup")


def _optional_int(value: object | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
