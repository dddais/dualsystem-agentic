"""Core JSON-safe data structures for the agentic robot loop."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonDict = dict[str, JsonValue]


class MonitorStatus(str, Enum):
    """Allowed monitor states for a subtask lifecycle."""

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


def normalize_monitor_status(value: str | MonitorStatus) -> MonitorStatus:
    """Return a validated monitor status."""
    if isinstance(value, MonitorStatus):
        return value
    normalized = str(value).strip().lower()
    try:
        return MonitorStatus(normalized)
    except ValueError as exc:
        allowed = ", ".join(status.value for status in MonitorStatus)
        raise ValueError(
            f"Unsupported monitor status: {value!r}. Expected one of: {allowed}"
        ) from exc


def to_jsonable(value: Any) -> JsonValue:
    """Convert supported values to JSON-safe Python objects."""
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "tolist"):
        return to_jsonable(value.tolist())
    if hasattr(value, "item"):
        return to_jsonable(value.item())
    raise TypeError(f"Value of type {type(value).__name__} is not JSON-safe")


def ensure_jsonable(value: Any) -> JsonValue:
    """Validate and return a JSON-safe version of a value."""
    converted = to_jsonable(value)
    _assert_jsonable(converted)
    return converted


@dataclass
class ImageInput:
    """Serializable image payload passed to a VLM planner."""

    type: str
    data: str
    mime_type: str | None = None
    path: str | None = None

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: JsonDict) -> "ImageInput":
        return cls(
            type=str(data["type"]),
            data=str(data["data"]),
            mime_type=data.get("mime_type"),  # type: ignore[arg-type]
            path=data.get("path"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class ToolCall:
    """One requested tool invocation, routed to an MCP server by ``namespace``."""

    name: str
    arguments: JsonDict = field(default_factory=dict)
    namespace: str | None = None
    call_id: str | None = None

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]


@dataclass(frozen=True)
class ToolResult:
    """Structured result from a tool invocation."""

    tool_name: str
    status: str
    data: JsonDict = field(default_factory=dict)
    namespace: str | None = None
    error: str | None = None
    raw_response: JsonDict | None = None
    call_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.error is None

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]

    @classmethod
    def success(
        cls,
        tool_name: str,
        data: Any | None = None,
        *,
        namespace: str | None = None,
        raw_response: Any | None = None,
        call_id: str | None = None,
    ) -> "ToolResult":
        return cls(
            tool_name=tool_name,
            status="ok",
            data=_json_dict_or_empty(data),
            namespace=namespace,
            raw_response=_optional_json_dict(raw_response),
            call_id=call_id,
        )

    @classmethod
    def failure(
        cls,
        tool_name: str,
        error: str,
        *,
        namespace: str | None = None,
        data: Any | None = None,
        raw_response: Any | None = None,
        call_id: str | None = None,
    ) -> "ToolResult":
        return cls(
            tool_name=tool_name,
            status="error",
            data=_json_dict_or_empty(data),
            namespace=namespace,
            error=error,
            raw_response=_optional_json_dict(raw_response),
            call_id=call_id,
        )


@dataclass
class AgenticPlannerInput:
    """Structured context passed to a high-level planner."""

    task: str
    step_index: int = 0
    current_subtask: str | None = None
    subtask_index: int | None = None
    subtasks: list[str] = field(default_factory=list)
    monitor_status: MonitorStatus | None = None
    monitor_error: str | None = None
    tool_results: list[ToolResult] = field(default_factory=list)
    environment: JsonDict = field(default_factory=dict)
    available_tools: list[JsonDict] = field(default_factory=list)
    images: dict[str, ImageInput] = field(default_factory=dict)
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]


@dataclass
class AgenticPlannerOutput:
    """Parsed planner decision for one agentic step."""

    raw_output: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    current_subtask: str | None = None
    subtask_index: int | None = None
    subtasks: list[str] = field(default_factory=list)
    should_execute: bool = True
    task_complete: bool = False
    parse_ok: bool = True
    parse_error: str | None = None

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]


@dataclass
class AgenticSessionState:
    """Serializable long-horizon task state."""

    task: str = ""
    subtasks: list[str] = field(default_factory=list)
    current_subtask: str | None = None
    subtask_index: int | None = None
    monitor_status: MonitorStatus | None = None
    monitor_error: str | None = None
    last_tool_results: list[ToolResult] = field(default_factory=list)
    environment: JsonDict = field(default_factory=dict)
    step_index: int = 0
    # Transient — images from the dataloader, carried between steps but not
    # serialized. Populated by AgenticRobotLoop.step().
    _last_captured_images: dict[str, ImageInput] | None = field(default=None, repr=False)

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(
            {
                "task": self.task,
                "subtasks": self.subtasks,
                "current_subtask": self.current_subtask,
                "subtask_index": self.subtask_index,
                "monitor_status": self.monitor_status,
                "monitor_error": self.monitor_error,
                "last_tool_results": self.last_tool_results,
                "environment": self.environment,
                "step_index": self.step_index,
            }
        )  # type: ignore[return-value]

    @classmethod
    def from_dict(cls, data: JsonDict | None) -> "AgenticSessionState":
        if not data:
            return cls()
        monitor_status = data.get("monitor_status")
        return cls(
            task=str(data.get("task") or ""),
            subtasks=[str(item) for item in data.get("subtasks", []) if str(item)],
            current_subtask=_optional_str(data.get("current_subtask")),
            subtask_index=_optional_int(data.get("subtask_index")),
            monitor_status=normalize_monitor_status(monitor_status) if monitor_status else None,
            monitor_error=_optional_str(data.get("monitor_error")),
            last_tool_results=[
                _tool_result_from_dict(item)
                for item in data.get("last_tool_results", [])
                if isinstance(item, dict)
            ],
            environment=_json_dict_or_empty(data.get("environment")),
            step_index=int(data.get("step_index", 0)),
        )


@dataclass
class ExecutorInput:
    """Input passed to the downstream VLA or executor adapter."""

    task: str
    subtask: str
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]


@dataclass
class ExecutorOutput:
    """Structured downstream executor result."""

    status: str = "ok"
    data: JsonDict = field(default_factory=dict)
    error: str | None = None
    raw_response: JsonDict | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.error is None

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]

    @classmethod
    def success(cls, data: Any | None = None, *, raw_response: Any | None = None) -> "ExecutorOutput":
        return cls(status="ok", data=_json_dict_or_empty(data), raw_response=_optional_json_dict(raw_response))

    @classmethod
    def failure(cls, error: str, *, data: Any | None = None, raw_response: Any | None = None) -> "ExecutorOutput":
        return cls(
            status="error",
            data=_json_dict_or_empty(data),
            error=error,
            raw_response=_optional_json_dict(raw_response),
        )


@dataclass
class AgenticStepResult:
    """Structured result for one agentic step."""

    task: str
    step_index: int
    planner_input: AgenticPlannerInput
    planner_output: AgenticPlannerOutput
    tool_results: list[ToolResult] = field(default_factory=list)
    executor_output: ExecutorOutput | None = None
    current_subtask: str | None = None
    subtask_index: int | None = None
    monitor_status: MonitorStatus | None = None
    monitor_error: str | None = None
    task_complete: bool = False
    parse_ok: bool = True
    parse_error: str | None = None

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]


def _json_dict_or_empty(value: Any | None) -> JsonDict:
    if value is None:
        return {}
    converted = ensure_jsonable(value)
    if not isinstance(converted, dict):
        raise TypeError("Payload must be a JSON object")
    return converted


def _optional_json_dict(value: Any | None) -> JsonDict | None:
    if value is None:
        return None
    return _json_dict_or_empty(value)


def _optional_str(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: Any | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _tool_result_from_dict(data: dict[str, Any]) -> ToolResult:
    return ToolResult(
        tool_name=str(data.get("tool_name") or ""),
        status=str(data.get("status") or "error"),
        data=_json_dict_or_empty(data.get("data")),
        namespace=_optional_str(data.get("namespace")),
        error=_optional_str(data.get("error")),
        raw_response=_optional_json_dict(data.get("raw_response")),
        call_id=_optional_str(data.get("call_id")),
    )


def _assert_jsonable(value: JsonValue) -> None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        for item in value:
            _assert_jsonable(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            _assert_jsonable(item)
        return
    raise TypeError(f"Value of type {type(value).__name__} is not JSON-safe")
