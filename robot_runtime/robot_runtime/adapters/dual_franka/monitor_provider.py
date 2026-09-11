"""Monitor provider adapters for the robot runtime."""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
import urllib.parse
from dataclasses import replace

from robot_runtime.core.types import (
    ExecutionRequest,
    ExecutionState,
    JsonDict,
    MonitorState,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    normalize_status,
)


class LocalMemoryMonitorProvider:
    """In-memory monitor placeholder used before GRM worker integration."""

    def __init__(
        self,
        *,
        default_status: str = STATUS_RUNNING,
        auto_success_after_polls: int = 0,
    ) -> None:
        self.default_status = normalize_status(default_status)
        self.auto_success_after_polls = max(0, int(auto_success_after_polls))
        self._states: dict[str, MonitorState] = {}

    def start(self, execution: ExecutionState, request: ExecutionRequest) -> MonitorState:
        state = MonitorState(
            monitor_id=execution.monitor_id,
            execution_id=execution.execution_id,
            subtask=execution.subtask,
            subtask_index=execution.subtask_index,
            status=STATUS_RUNNING if request.options.get("continuous_monitoring") else self.default_status,
            message="local memory monitor placeholder",
            result={"provider": "local_memory", "continuous_monitoring": request.options.get("continuous_monitoring", False), "status": self.default_status},
        )
        self._states[state.monitor_id] = state
        return state

    def status(self, monitor: MonitorState) -> MonitorState:
        state = self._states.get(monitor.monitor_id, monitor)
        poll_count = state.poll_count + 1
        status = state.result.get("status", state.status) if state.result.get("continuous_monitoring") else state.status
        progress = state.progress
        if self.auto_success_after_polls and poll_count >= self.auto_success_after_polls:
            status = STATUS_SUCCESS
            progress = 1.0
        refreshed = replace(
            state,
            poll_count=poll_count,
            status=STATUS_RUNNING if state.result.get("continuous_monitoring") else status,
            progress=progress,
            updated_at=time.time(),
            result={**state.result, "status": status},
        )
        self._states[refreshed.monitor_id] = refreshed
        return refreshed

    def activate(self, monitor: MonitorState) -> MonitorState:
        return monitor

    def stop(self, monitor_id: str) -> JsonDict:
        state = self._states.get(monitor_id)
        if state is not None:
            self._states[monitor_id] = replace(state, status="failed", message="monitor stopped")
        return {"stopped": True, "monitor_id": monitor_id}

    def health(self) -> JsonDict:
        return {
            "provider": "local_memory",
            "monitors": len(self._states),
            "auto_success_after_polls": self.auto_success_after_polls,
        }


class RemoteHTTPMonitorProvider:
    """Delegates monitor lifecycle to a remote HTTP service."""

    def __init__(
        self,
        *,
        url: str,
        timeout: float = 30.0,
        start_path: str = "/monitors/start",
        status_path: str = "/monitors/status",
        stop_path: str = "/monitors/stop",
        activate_path: str = "/monitors/activate",
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.start_path = start_path
        self.status_path = status_path
        self.stop_path = stop_path
        self.activate_path = activate_path

    def start(self, execution: ExecutionState, request: ExecutionRequest) -> MonitorState:
        data = self._request(
            "POST",
            self.start_path,
            {
                "execution_id": execution.execution_id,
                "monitor_id": execution.monitor_id,
                "subtask": execution.subtask,
                "subtask_index": execution.subtask_index,
                "task": execution.task,
                "metadata": execution.metadata,
                "defer_inference": True,
                **({"continuous_monitoring": True} if request.options.get("continuous_monitoring") else {}),
                **({"target_queries": request.target_queries} if request.target_queries is not None else {}),
            },
        )
        monitor = _monitor_from_payload(data, fallback=execution)
        if request.options.get("continuous_monitoring") and monitor.result.get("continuous_monitoring") is not True:
            raise RuntimeError("GRM Monitor does not support continuous_monitoring; update and restart Monitor for auto_stop: false")
        return monitor

    def status(self, monitor: MonitorState) -> MonitorState:
        data = self._request(
            "POST",
            self.status_path,
            {
                "execution_id": monitor.execution_id,
                "monitor_id": monitor.monitor_id,
                "subtask": monitor.subtask,
                "subtask_index": monitor.subtask_index,
            },
        )
        return _monitor_from_payload(data, fallback=monitor)

    def activate(self, monitor: MonitorState) -> MonitorState:
        data = self._request("POST", self.activate_path, {
            "execution_id": monitor.execution_id, "monitor_id": monitor.monitor_id,
        })
        return _monitor_from_payload(data, fallback=monitor)

    def stop(self, monitor_id: str) -> JsonDict:
        return self._request("POST", self.stop_path, {"monitor_id": monitor_id})

    def health(self) -> JsonDict:
        return {"provider": "remote_http", "url": self.url}

    def progress_records(self, monitor_id: str, execution_id: str, cursor: int = 0) -> JsonDict:
        query = urllib.parse.urlencode({"execution_id": execution_id, "cursor": cursor, "limit": 100})
        path = f"/monitors/{urllib.parse.quote(monitor_id, safe='')}/records?{query}"
        with urllib.request.urlopen(self.url + path, timeout=min(self.timeout, 2.0)) as response:
            content = response.read(16 * 1024 * 1024 + 1)
        if len(content) > 16 * 1024 * 1024:
            raise ValueError("progress journal page exceeds 16 MiB")
        return _unwrap(json.loads(content))

    def frame_image(self, frame_set_id: str, camera: str) -> bytes:
        if not re.fullmatch(r"[a-f0-9]{32}", frame_set_id) or camera not in {
            "cam_high", "cam_left_wrist", "cam_right_wrist"
        }:
            raise ValueError("invalid monitor frame or camera")
        path = f"/monitors/frames/{frame_set_id}/{camera}.png"
        try:
            with urllib.request.urlopen(self.url + path, timeout=min(self.timeout, 5.0)) as response:
                data = response.read(16 * 1024 * 1024 + 1)
            if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 16 * 1024 * 1024:
                raise ValueError("monitor frame is not a valid bounded PNG response")
            return data
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"monitor frame unavailable: {exc}") from exc

    def _request(self, method: str, path: str, payload: JsonDict | None = None) -> JsonDict:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.url + _safe_path(path),
            data=body,
            method=method.upper(),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc)
            message = f"remote monitor {method.upper()} {self.url}{_safe_path(path)} returned HTTP {exc.code}"
            if detail:
                message = f"{message}: {detail}"
            raise RuntimeError(message) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"remote monitor request failed: {exc}") from exc
        return _unwrap(data)


def _monitor_from_payload(data: JsonDict, *, fallback: ExecutionState | MonitorState) -> MonitorState:
    for key in ("monitor_id", "execution_id"):
        if data.get(key) != getattr(fallback, key):
            raise ValueError(f"remote monitor returned a different or missing {key}")
    status = normalize_status(data.get("status"), default="invalid")
    if status == "invalid":
        raise ValueError("remote monitor returned an invalid or missing status")
    progress = float(data.get("progress") or 0.0)
    if not math.isfinite(progress) or not 0 <= progress <= 1:
        raise ValueError("remote monitor progress must be finite and within [0, 1]")
    return MonitorState(
        monitor_id=str(data.get("monitor_id") or fallback.monitor_id),
        execution_id=str(data.get("execution_id") or fallback.execution_id),
        subtask=str(data.get("subtask") or fallback.subtask),
        subtask_index=data.get("subtask_index", fallback.subtask_index),
        status=status,
        progress=progress,
        created_at=float(data.get("created_at") or getattr(fallback, "created_at", time.time())),
        updated_at=float(data.get("updated_at") or time.time()),
        error=data.get("error"),
        message=data.get("message"),
        poll_count=int(data.get("poll_count") or getattr(fallback, "poll_count", 0)),
        result=dict(data.get("result") or data),
    )


def _unwrap(data: object) -> JsonDict:
    if not isinstance(data, dict):
        return {"data": data}
    if data.get("success") is False or data.get("ok") is False:
        raise RuntimeError(str(data.get("message") or data.get("error") or "monitor request failed"))
    payload = data.get("data")
    if isinstance(payload, dict):
        return payload
    return data


def _safe_path(path: str) -> str:
    if not path.startswith("/") or "://" in path or ".." in path.split("/"):
        raise ValueError(f"unsafe remote monitor path: {path!r}")
    return path


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except ValueError:
        return body[:500]
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if message:
            return str(message)
    return body[:500]
