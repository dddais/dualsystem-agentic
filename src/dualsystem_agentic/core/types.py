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


class AgenticPhase(str, Enum):
    """High-level controller phases for the agentic loop."""

    INIT = "init"
    READY = "ready"
    REASON = "reason"
    ACT = "act"
    RESPONSE = "response"
    DONE = "done"
    ERROR = "error"


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


def normalize_agentic_phase(value: str | AgenticPhase) -> AgenticPhase:
    """Return a validated agentic phase."""
    if isinstance(value, AgenticPhase):
        return value
    normalized = str(value).strip().lower()
    try:
        return AgenticPhase(normalized)
    except ValueError as exc:
        allowed = ", ".join(phase.value for phase in AgenticPhase)
        raise ValueError(
            f"Unsupported agentic phase: {value!r}. Expected one of: {allowed}"
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
class ActiveExecution:
    """The subtask currently being executed or most recently monitored."""

    subtask: str
    subtask_index: int | None = None
    execution_id: str | None = None
    monitor_id: str | None = None
    status: str = MonitorStatus.RUNNING.value
    error: str | None = None
    namespace: str | None = None
    started_at: float | None = None
    updated_at: float | None = None

    @property
    def running(self) -> bool:
        return self.status == MonitorStatus.RUNNING.value

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]


@dataclass(frozen=True)
class AgenticEvent:
    """Runtime, tool, or monitor event that can trigger another reason step."""

    event_type: str
    data: JsonDict = field(default_factory=dict)
    source: str | None = None
    message: str | None = None
    created_at: float | None = None

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]


@dataclass
class AgenticPlannerInput:
    """Structured context passed to a high-level planner."""

    task: str
    phase: AgenticPhase = AgenticPhase.READY
    step_index: int = 0
    current_subtask: str | None = None
    subtask_index: int | None = None
    subtasks: list[str] = field(default_factory=list)
    monitor_status: MonitorStatus | None = None
    monitor_error: str | None = None
    active_execution: ActiveExecution | None = None
    events: list[AgenticEvent] = field(default_factory=list)
    reason_requested: bool = False
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
    should_execute_explicit: bool = False
    task_complete: bool = False
    parse_ok: bool = True
    parse_error: str | None = None
    decision: str | None = None

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]


@dataclass
class AgenticSessionState:
    """Serializable long-horizon task state."""

    task: str = ""
    phase: AgenticPhase = AgenticPhase.INIT
    subtasks: list[str] = field(default_factory=list)
    current_subtask: str | None = None
    subtask_index: int | None = None
    monitor_status: MonitorStatus | None = None
    monitor_error: str | None = None
    awaiting_monitor: bool = False
    monitor_namespace: str | None = None
    active_execution: ActiveExecution | None = None
    pending_events: list[AgenticEvent] = field(default_factory=list)
    reason_requested: bool = True
    last_reason_at: float | None = None
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
                "phase": self.phase,
                "subtasks": self.subtasks,
                "current_subtask": self.current_subtask,
                "subtask_index": self.subtask_index,
                "monitor_status": self.monitor_status,
                "monitor_error": self.monitor_error,
                "awaiting_monitor": self.awaiting_monitor,
                "monitor_namespace": self.monitor_namespace,
                "active_execution": self.active_execution,
                "pending_events": self.pending_events,
                "reason_requested": self.reason_requested,
                "last_reason_at": self.last_reason_at,
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
        phase = data.get("phase")
        return cls(
            task=str(data.get("task") or ""),
            phase=normalize_agentic_phase(phase) if phase else AgenticPhase.INIT,
            subtasks=[str(item) for item in data.get("subtasks", []) if str(item)],
            current_subtask=_optional_str(data.get("current_subtask")),
            subtask_index=_optional_int(data.get("subtask_index")),
            monitor_status=normalize_monitor_status(monitor_status) if monitor_status else None,
            monitor_error=_optional_str(data.get("monitor_error")),
            awaiting_monitor=bool(data.get("awaiting_monitor", False)),
            monitor_namespace=_optional_str(data.get("monitor_namespace")),
            active_execution=_active_execution_from_dict(data.get("active_execution")),
            pending_events=[
                _event_from_dict(item)
                for item in data.get("pending_events", [])
                if isinstance(item, dict)
            ],
            reason_requested=bool(data.get("reason_requested", True)),
            last_reason_at=_optional_float(data.get("last_reason_at")),
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
    phase: AgenticPhase = AgenticPhase.RESPONSE
    vlm_called: bool = True
    tool_results: list[ToolResult] = field(default_factory=list)
    executor_output: ExecutorOutput | None = None
    current_subtask: str | None = None
    subtask_index: int | None = None
    monitor_status: MonitorStatus | None = None
    monitor_error: str | None = None
    active_execution: ActiveExecution | None = None
    events: list[AgenticEvent] = field(default_factory=list)
    reason_requested: bool = False
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


def _optional_float(value: Any | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _active_execution_from_dict(value: Any | None) -> ActiveExecution | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("active_execution must be a JSON object")
    return ActiveExecution(
        subtask=str(value.get("subtask") or ""),
        subtask_index=_optional_int(value.get("subtask_index")),
        execution_id=_optional_str(value.get("execution_id")),
        monitor_id=_optional_str(value.get("monitor_id")),
        status=str(value.get("status") or MonitorStatus.RUNNING.value),
        error=_optional_str(value.get("error")),
        namespace=_optional_str(value.get("namespace")),
        started_at=_optional_float(value.get("started_at")),
        updated_at=_optional_float(value.get("updated_at")),
    )


def _event_from_dict(value: dict[str, Any]) -> AgenticEvent:
    return AgenticEvent(
        event_type=str(value.get("event_type") or value.get("type") or ""),
        data=_json_dict_or_empty(value.get("data")),
        source=_optional_str(value.get("source")),
        message=_optional_str(value.get("message")),
        created_at=_optional_float(value.get("created_at")),
    )


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
