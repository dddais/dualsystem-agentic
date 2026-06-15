"""JSON-safe data models shared by the robot runtime service."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

JsonDict = dict[str, Any]

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
VALID_STATUSES = {STATUS_RUNNING, STATUS_SUCCESS, STATUS_FAILED}


def now_s() -> float:
    return time.time()


def new_execution_id() -> str:
    return f"exec-{uuid.uuid4().hex}"


def new_monitor_id() -> str:
    return f"mon-{uuid.uuid4().hex}"


def normalize_status(value: object | None, *, default: str = STATUS_RUNNING) -> str:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"running", "executing", "busy", "in_progress", "active", "started"}:
        return STATUS_RUNNING
    if text in {"success", "succeeded", "done", "completed", "complete", "finished"}:
        return STATUS_SUCCESS
    if text in {"fail", "failed", "failure", "error", "aborted", "cancelled", "canceled", "stopped"}:
        return STATUS_FAILED
    return default


@dataclass
class ExecutionRequest:
    subtask: str
    task: str | None = None
    subtask_index: int | None = None
    metadata: JsonDict = field(default_factory=dict)
    options: JsonDict = field(default_factory=dict)
    raw_request: JsonDict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: JsonDict) -> "ExecutionRequest":
        subtask = _text(payload.get("subtask") or payload.get("prompt") or payload.get("instruction"))
        if not subtask:
            raise ValueError("missing subtask")
        return cls(
            subtask=subtask,
            task=_text(payload.get("task")),
            subtask_index=_optional_int(payload.get("subtask_index")),
            metadata=_dict_or_empty(payload.get("metadata")),
            options=_dict_or_empty(payload.get("options")),
            raw_request=dict(payload),
        )


@dataclass
class ExecutionState:
    execution_id: str
    monitor_id: str
    subtask: str
    status: str = STATUS_RUNNING
    task: str | None = None
    subtask_index: int | None = None
    created_at: float = field(default_factory=now_s)
    updated_at: float = field(default_factory=now_s)
    error: str | None = None
    metadata: JsonDict = field(default_factory=dict)
    driver_result: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "execution_id": self.execution_id,
            "monitor_id": self.monitor_id,
            "subtask": self.subtask,
            "task": self.task,
            "subtask_index": self.subtask_index,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "metadata": self.metadata,
            "driver_result": self.driver_result,
        }


@dataclass
class MonitorState:
    monitor_id: str
    execution_id: str
    subtask: str
    status: str = STATUS_RUNNING
    subtask_index: int | None = None
    progress: float = 0.0
    created_at: float = field(default_factory=now_s)
    updated_at: float = field(default_factory=now_s)
    error: str | None = None
    message: str | None = None
    poll_count: int = 0
    result: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "monitor_id": self.monitor_id,
            "execution_id": self.execution_id,
            "subtask": self.subtask,
            "subtask_index": self.subtask_index,
            "status": self.status,
            "progress": self.progress,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "message": self.message,
            "poll_count": self.poll_count,
            "result": self.result,
        }


@dataclass
class ObservationFrame:
    images: dict[str, str]
    timestamp: float
    frame_id: str
    missing: list[str] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)
    mime_types: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        payload: JsonDict = {
            **self.images,
            "timestamp": self.timestamp,
            "frame_id": self.frame_id,
            "missing_cameras": list(self.missing),
            "metadata": dict(self.metadata),
            "mime_types": dict(self.mime_types),
        }
        if "concatenated_image" not in payload and "cam_high" in self.images:
            payload["concatenated_image"] = self.images["cam_high"]
        return payload

    def metadata_dict(self) -> JsonDict:
        return {
            "timestamp": self.timestamp,
            "frame_id": self.frame_id,
            "cameras": [
                key for key in self.images if key != "concatenated_image"
            ],
            "missing_cameras": list(self.missing),
            "metadata": dict(self.metadata),
            "mime_types": dict(self.mime_types),
        }


@dataclass
class ObservationImage:
    camera: str
    data: bytes
    timestamp: float
    frame_id: str
    mime_type: str = "image/jpeg"
    metadata: JsonDict = field(default_factory=dict)


@dataclass
class RobotCapabilities:
    robot_type: str
    cameras: list[str]
    standard_tools: list[str] = field(
        default_factory=lambda: [
            "execute",
            "monitor",
            "stop_task",
            "reset_task",
            "emergency_stop",
            "fetch_env",
        ]
    )
    extra_tools: list[JsonDict] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "robot_type": self.robot_type,
            "cameras": list(self.cameras),
            "standard_tools": list(self.standard_tools),
            "extra_tools": list(self.extra_tools),
        }


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object | None) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _dict_or_empty(value: object | None) -> JsonDict:
    return dict(value) if isinstance(value, dict) else {}
