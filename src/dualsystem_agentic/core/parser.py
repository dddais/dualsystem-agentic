"""Parser for structured (JSON-in-text) agentic planner responses."""

from __future__ import annotations

import json
import re
from typing import Any

from dualsystem_agentic.core.tool_names import split_qualified_tool_name
from dualsystem_agentic.core.types import (
    AgenticPlannerOutput,
    JsonDict,
    ToolCall,
    ensure_jsonable,
)


def parse_agentic_planner_output(text: str) -> AgenticPlannerOutput:
    """Parse a planner response into structured tool calls and subtask data."""
    raw_output = text or ""
    cleaned = _strip_code_fences(raw_output)
    data = _load_embedded_json(cleaned)
    if not isinstance(data, dict):
        return AgenticPlannerOutput(
            raw_output=raw_output,
            should_execute=False,
            parse_ok=False,
            parse_error="failed to parse planner JSON object",
        )

    try:
        tool_calls = _parse_tool_calls(data.get("tool_calls") or data.get("tools") or [])
        current_subtask = _optional_text(data.get("current_subtask") or data.get("subtask"))
        subtasks = _parse_subtasks(data.get("subtasks") or data.get("plan") or [])
        subtask_index = _optional_int(
            data.get("subtask_index") if data.get("subtask_index") is not None else data.get("subtask_id")
        )
        should_execute = bool(data.get("should_execute", True))
        task_complete = bool(data.get("task_complete", data.get("complete", False)))
    except (TypeError, ValueError) as exc:
        return AgenticPlannerOutput(
            raw_output=raw_output,
            should_execute=False,
            parse_ok=False,
            parse_error=str(exc),
        )

    if not current_subtask and subtask_index is not None and 0 <= subtask_index < len(subtasks):
        current_subtask = subtasks[subtask_index]

    if task_complete:
        return AgenticPlannerOutput(
            raw_output=raw_output,
            tool_calls=tool_calls,
            current_subtask=current_subtask,
            subtask_index=subtask_index,
            subtasks=subtasks,
            should_execute=False,
            task_complete=True,
        )

    if not current_subtask and not tool_calls and subtask_index is None and not subtasks:
        return AgenticPlannerOutput(
            raw_output=raw_output,
            subtasks=subtasks,
            subtask_index=subtask_index,
            should_execute=False,
            task_complete=task_complete,
            parse_ok=False,
            parse_error="planner output must include a subtask plan, a subtask index, a current subtask, or a tool call",
        )

    return AgenticPlannerOutput(
        raw_output=raw_output,
        tool_calls=tool_calls,
        current_subtask=current_subtask,
        subtask_index=subtask_index,
        subtasks=subtasks,
        should_execute=should_execute,
        task_complete=task_complete,
    )


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _load_embedded_json(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\{.*\})", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _parse_tool_calls(value: Any) -> list[ToolCall]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("tool_calls must be a list")
    tool_calls: list[ToolCall] = []
    for item in value:
        if not isinstance(item, dict):
            raise TypeError("each tool call must be an object")
        name = _optional_text(item.get("name") or item.get("tool_name") or item.get("tool"))
        if not name:
            raise ValueError("tool call is missing a name")
        namespace = _optional_text(item.get("namespace") or item.get("server") or item.get("service"))
        qualified_namespace, tool_name = split_qualified_tool_name(name)
        if qualified_namespace is not None:
            if namespace is not None and namespace != qualified_namespace:
                raise ValueError(
                    f"tool call namespace mismatch: name={name!r}, namespace={namespace!r}"
                )
            namespace = qualified_namespace
            name = tool_name
        arguments = _json_dict(item.get("arguments") or item.get("args") or {})
        call_id = _optional_text(item.get("call_id") or item.get("id"))
        tool_calls.append(
            ToolCall(name=name, arguments=arguments, namespace=namespace, call_id=call_id)
        )
    return tool_calls


def _parse_subtasks(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError("subtasks must be a list")
    subtasks: list[str] = []
    for item in value:
        text = _optional_text(item.get("subtask") if isinstance(item, dict) else item)
        if text:
            subtasks.append(text)
    return subtasks


def _json_dict(value: Any) -> JsonDict:
    converted = ensure_jsonable(value)
    if not isinstance(converted, dict):
        raise TypeError("tool call arguments must be a JSON object")
    return converted


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)
