"""Prompt builder for the agentic planner (JSON-in-text protocol)."""

from __future__ import annotations

import json

from dualsystem_agentic.core.tool_names import make_canonical_tool_name
from dualsystem_agentic.core.types import ActiveExecution, AgenticPlannerInput

_RUNTIME_ID_FIELDS = {
    "execution_id",
    "monitor_id",
    "task_id",
    "namespace",
    "call_id",
    "created_at",
    "started_at",
    "updated_at",
}
_EVENT_RUNTIME_ID_FIELDS = _RUNTIME_ID_FIELDS | {"id"}
_TOOL_RESULT_SUMMARY_FIELDS = (
    "status",
    "monitor_status",
    "executed",
    "subtask",
    "subtask_index",
    "error",
    "progress",
    "message",
)

_CORE_INSTRUCTION = """You are the high-level planner of a dual-system robot.
You turn a long-horizon task into subtasks and select the next subtask by
"subtask_index", and start robot execute with decision="execute".

Core protocol:
- Respond with one JSON object only; no prose, markdown, or code fence.
- "subtasks" is the ordered plan. Return it only on the first step or when
  revising; otherwise omit it. Reuse existing plans unless task, scene, safety,
  or failure state requires changes.
- "subtask_index" selects the current item; the controller derives
  current_subtask from subtasks[subtask_index]. Keep successful subtasks as
  completed history when revising.
- Use only tools from "Available tools", by exact canonical name. To start the
  selected physical action, set decision="execute"; the controller fills the
  configured execute tool arguments. Do not write an execute tool_call yourself.
- Execute starts an asynchronous action. Monitor feedback decides whether it is
  running, succeeded, failed, or timed out.
- Use monitor only after an action has started.
- Set task_complete=true only when all required work is finished and no active
  execution is running.

Decision policy:
- execute: start exactly one selected pending physical subtask. Keep
  "tool_calls" empty or omitted; never include execute, monitor, or control
  tools in the same step.
- observe: call a non-execute scene/environment tool only when more scene
  information is needed. wait: active execution is running and no tool is needed.
- replan: pending/failed work must change. complete: all required physical
  effects are achieved and no active execution is running. ask_user: the task is
  unsafe or ambiguous from available context.
- Use at most one non-execute tool call per step unless calls are purely
  observational and independent; omit tool_calls for wait/complete/noop/ask_user
  unless a listed non-execute tool is required.

Executable subtask constraints:
- Every item in "subtasks" must be a complete, concrete physical robot action
  for one object or one tightly coupled object group, including what to
  move/manipulate and where it should end up.
- Do NOT create subtasks for checking status, monitoring, observing, analyzing
  images, planning, deciding, verifying, ensuring, or conditional logic.
- Do NOT split one object into bare skill subtasks such as "grip", "pick",
  "place", or "move".
- Do NOT write conditional or vague subtasks such as "if items are present" or
  "organize the items".
- Prefer scene graph for structured object identities/relations and images for
  current visual grounding. Refer only to visible, scene-graph, or user-named
  objects/locations. If multiple matches exist, use spatial/color/relation
  descriptors.
- Do not invent hidden objects, destinations, robot capabilities, or success
  conditions.

Failure recovery:
- After a failed or timed-out action, do not mark it complete.
- Retry only if the scene still supports the same action; replan if the object
  moved, disappeared, became unreachable, or the target changed.

JSON schema:
{
  "decision": "<one of: plan, execute, observe, wait, replan, cancel, complete, ask_user, noop>",
  "tool_calls": [
    {"name": "<canonical non-execute tool name from the list>", "arguments": {}}
  ],
  "subtasks": ["<full ordered plan; required on the first step and whenever you revise it>"],
  "subtask_index": <0-based index of the current subtask within the plan>,
  "task_complete": false
}
Compatibility note:
Omit "current_subtask" unless you are using a legacy planner; the controller
derives it from "subtask_index"."""


def build_agentic_prompt(planner_input: AgenticPlannerInput) -> str:
    """Render an ``AgenticPlannerInput`` into a planner prompt string."""
    sections: list[str] = [_CORE_INSTRUCTION]

    visual_block = _format_visual_observations(planner_input)
    if visual_block:
        sections.append(visual_block)

    tools_block = _format_available_tools(planner_input)
    if tools_block:
        sections.append(tools_block)

    sections.append(_format_step_guidance(planner_input))
    sections.append(_format_session_memory(planner_input))
    sections.append("Now produce the JSON object for the next step.")
    return "\n\n".join(sections)


def _format_step_guidance(planner_input: AgenticPlannerInput) -> str:
    lines = ["Step guidance:"]

    if planner_input.active_execution is not None and planner_input.active_execution.running:
        lines.extend(
            [
                "- Active execution is running. Do not use decision=\"execute\" and do not call action-like tools.",
                "- Keep subtask_index on the active execution. Wait, observe, monitor, or cancel with an available safety tool.",
            ]
        )
        return "\n".join(lines)

    event_types = {event.event_type for event in planner_input.events}
    if "monitor_success" in event_types or planner_input.monitor_status and planner_input.monitor_status.value == "success":
        lines.extend(
            [
                "- The previous action succeeded. Select the next pending subtask with subtask_index, or complete the task.",
                "- Do not execute a subtask already marked success.",
            ]
        )
    elif {"monitor_failed", "monitor_timeout"} & event_types or (
        planner_input.monitor_status and planner_input.monitor_status.value == "failed"
    ):
        lines.extend(
            [
                "- The previous action failed or timed out. Retry the same subtask, revise pending/failed work, cancel, or ask_user.",
                "- Keep successful subtasks in the plan as completed history.",
            ]
        )
    elif not planner_input.subtasks:
        lines.extend(
            [
                "- No plan exists yet. Return concrete physical subtasks and set subtask_index=0.",
                "- If the first subtask is ready to start now, use decision=\"execute\".",
                "- Do not call monitor before an action has started.",
            ]
        )
    else:
        lines.extend(
            [
                "- Existing plan is available. Select the next pending subtask by subtask_index.",
                "- Revise subtasks only if the scene or task requirements changed; keep successful subtasks.",
            ]
        )

    return "\n".join(lines)


def _format_session_memory(planner_input: AgenticPlannerInput) -> str:
    lines = [
        "Session memory / Runtime state:",
        f"  Task: {planner_input.task}",
    ]

    if planner_input.subtasks:
        lines.append(
            "  Subtask plan (select the current one by index, or revise the list):"
        )
        plan_lines = _format_plan(
            planner_input.subtasks,
            planner_input.subtask_index,
            planner_input.subtask_statuses,
        ).splitlines()
        lines.extend(f"  {line}" for line in plan_lines)
    else:
        lines.append("  Subtask plan: none yet - decompose the task into subtasks first.")

    if planner_input.current_subtask:
        lines.append(f"  Current subtask: {planner_input.current_subtask}")

    if planner_input.monitor_status is not None:
        monitor_line = f"Status of the current subtask: {planner_input.monitor_status.value}"
        if planner_input.monitor_error:
            monitor_line += f" (error: {planner_input.monitor_error})"
        lines.append(f"  {monitor_line}")

    if planner_input.active_execution is not None:
        lines.append("  Active execution:")
        lines.append("  " + _format_json(_planner_visible_active_execution(planner_input.active_execution)))

    if planner_input.events:
        lines.append("  Pending events:")
        lines.extend(f"  {line}" for line in _format_events(planner_input).splitlines())

    if planner_input.environment:
        lines.append("  Scene graph:")
        lines.append("  " + _format_json(planner_input.environment))

    if planner_input.tool_results:
        lines.append("  Results of tools called last step:")
        lines.extend(f"  {line}" for line in _format_tool_results(planner_input).splitlines())

    if planner_input.metadata:
        lines.append("  Planner-visible metadata:")
        lines.append("  " + _format_json(planner_input.metadata))

    return "\n".join(lines)


def _format_visual_observations(planner_input: AgenticPlannerInput) -> str:
    if not planner_input.images:
        return ""
    labels = ", ".join(planner_input.images.keys())
    lines = [
        "Visual observations:",
        f"- Latest images are attached before this text in label order: {labels}.",
        "- Prefer Scene graph for structured object identities and relations when present; use images for current visual grounding.",
    ]
    return "\n".join(lines)


def _planner_visible_active_execution(active_execution: ActiveExecution) -> dict[str, object]:
    visible: dict[str, object] = {
        "subtask": active_execution.subtask,
        "status": active_execution.status,
    }
    if active_execution.subtask_index is not None:
        visible["subtask_index"] = active_execution.subtask_index
    if active_execution.error:
        visible["error"] = active_execution.error
    return visible


def _format_available_tools(planner_input: AgenticPlannerInput) -> str:
    if not planner_input.available_tools:
        return ""
    lines = []
    for tool in planner_input.available_tools:
        name = str(tool.get("name") or "")
        namespace = str(tool.get("namespace") or "")
        display = str(tool.get("canonical_name") or make_canonical_tool_name(name, namespace))
        role = _planner_tool_role(name, tool)
        if role == "execute":
            continue
        signature = _format_planner_tool_signature(role, tool.get("parameters"))
        description = str(tool.get("description") or "").strip()
        head = f"  - {display}{signature}"
        details = _planner_tool_description(role, description)
        lines.append(f"{head}: {details}" if details else head)
    if not lines:
        return ""
    return "Available tools:\n" + "\n".join(lines)


def _planner_tool_role(name: str, tool: dict) -> str | None:
    role = str(tool.get("agentic_role") or tool.get("_agentic_role") or "").strip().lower()
    if role in {"monitor", "execute", "fetch_env", "environment", "env"}:
        return "fetch_env" if role in {"environment", "env"} else role
    normalized = name.lower().replace("-", "_")
    if normalized == "monitor" or normalized.endswith("_monitor"):
        return "monitor"
    if normalized == "execute" or normalized.endswith("_execute"):
        return "execute"
    if normalized in {"fetch_env", "observe_scene"} or normalized.endswith("_fetch_env"):
        return "fetch_env"
    if normalized in {"stop_task", "reset_task", "emergency_stop"}:
        return "control"
    return None


def _format_planner_tool_signature(role: str | None, parameters: object) -> str:
    if role == "monitor":
        return "()"
    return _format_tool_signature(parameters)


def _planner_tool_description(role: str | None, description: str) -> str:
    if role == "monitor":
        return "check active action status."
    if role == "control":
        return _control_tool_description(description)
    if role == "fetch_env":
        return "observe scene."
    return description


def _control_tool_description(description: str) -> str:
    text = description.lower()
    if "emergency" in text:
        return "emergency stop."
    if "reset" in text:
        return "reset task."
    return "stop current task."


def _format_tool_signature(parameters: object) -> str:
    if not isinstance(parameters, dict):
        return "()"
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "()"
    required = parameters.get("required") or []
    parts = []
    for pname, schema in properties.items():
        ptype = schema.get("type", "any") if isinstance(schema, dict) else "any"
        marker = "" if pname in required else "?"
        parts.append(f"{pname}{marker}: {ptype}")
    return "(" + ", ".join(parts) + ")"


def _format_tool_results(planner_input: AgenticPlannerInput) -> str:
    lines = []
    for result in planner_input.tool_results:
        display = make_canonical_tool_name(result.tool_name, result.namespace)
        if result.ok:
            lines.append(f"  - {display}: ok {_format_json(_planner_visible_tool_result_data(result.data))}")
        else:
            lines.append(f"  - {display}: error {result.error}")
    return "\n".join(lines)


def _format_events(planner_input: AgenticPlannerInput) -> str:
    lines = []
    for event in planner_input.events:
        payload = _strip_runtime_fields(event.data, _EVENT_RUNTIME_ID_FIELDS)
        lines.append(f"  - {event.event_type}: {_format_json(payload)}")
    return "\n".join(lines)


def _planner_visible_tool_result_data(data: dict[str, object]) -> object:
    if any(key in data for key in ("scene_graph", "environment", "env")):
        return _strip_runtime_fields(data, _RUNTIME_ID_FIELDS)
    summary = {
        key: data[key]
        for key in _TOOL_RESULT_SUMMARY_FIELDS
        if key in data and data[key] is not None
    }
    if summary:
        return summary
    return _strip_runtime_fields(data, _RUNTIME_ID_FIELDS)


def _strip_runtime_fields(data: dict[str, object], runtime_fields: set[str]) -> dict[str, object]:
    return {str(key): value for key, value in data.items() if str(key) not in runtime_fields}


def _format_plan(
    items: list[str],
    current_index: int | None,
    statuses: list[object] | None = None,
) -> str:
    lines = []
    for index, item in enumerate(items):
        status = _status_value(statuses[index]) if statuses and index < len(statuses) else "pending"
        marker = " <- current" if index == current_index else ""
        lines.append(f"  {index}. [{status}] {item}{marker}")
    return "\n".join(lines)


def _status_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _format_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
