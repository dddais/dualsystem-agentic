"""Prompt builder for the agentic planner (JSON-in-text protocol)."""

from __future__ import annotations

import json

from dualsystem_agentic.core.tool_names import make_canonical_tool_name
from dualsystem_agentic.core.types import AgenticPlannerInput

_SYSTEM_INSTRUCTION = """You are the high-level planner of a dual-system robot.
You turn a long-horizon task into subtasks and drive a low-level executor through
them.

Planning protocol:
- If there is NO subtask plan yet, FIRST decompose the whole task into an ordered
  list of subtasks (the "subtasks" field), then set "subtask_index" to 0 to start
  from the first subtask.
- Once a plan exists, SELECT the current subtask from the plan by its
  "subtask_index". You MAY REVISE the plan (return an updated full "subtasks" list)
  when needed, e.g. a subtask failed, the scene differs from expectation, or extra
  steps are required. Omit "subtasks" if the plan is unchanged.

Tool use:
- Call ONLY tools from the "Available tools" list, by the exact canonical name
  shown, and pass arguments that match the listed signature.
- Any available tool may be called; newly exposed robot tools do not need a
  special config entry. Interpret their listed description and schema.
- Tools that report subtask status should return {"status": "running|success|failed"}.
  Tools that return scene state may return {"environment": {...}}. Tools that
  perform an action may return {"executed": true}; in that case the downstream
  executor is skipped for that step.
- Use the latest tool results and status to decide whether to advance to the next
  subtask, retry the current one, or revise the plan.

Respond with ONLY a JSON object, no extra prose:
{
  "tool_calls": [
    {"name": "<canonical tool name from the list>", "arguments": {}}
  ],
  "subtasks": ["<full ordered plan; required on the first step and whenever you revise it>"],
  "subtask_index": <0-based index of the current subtask within the plan>,
  "current_subtask": "<optional: the subtask text; defaults to subtasks[subtask_index]>",
  "task_complete": false
}
Set "task_complete" to true only when the whole task is finished."""


def build_agentic_prompt(planner_input: AgenticPlannerInput) -> str:
    """Render an ``AgenticPlannerInput`` into a planner prompt string."""
    sections: list[str] = [_SYSTEM_INSTRUCTION, f"Task: {planner_input.task}"]

    tools_block = _format_available_tools(planner_input)
    if tools_block:
        sections.append(tools_block)

    if planner_input.subtasks:
        sections.append(
            "Subtask plan (select the current one by index, or revise the list):\n"
            + _format_plan(planner_input.subtasks, planner_input.subtask_index)
        )
    else:
        sections.append("Subtask plan: none yet — decompose the task into subtasks first.")

    if planner_input.monitor_status is not None:
        monitor_line = f"Status of the current subtask: {planner_input.monitor_status.value}"
        if planner_input.monitor_error:
            monitor_line += f" (error: {planner_input.monitor_error})"
        sections.append(monitor_line)

    if planner_input.environment:
        sections.append("Environment state:\n" + _format_json(planner_input.environment))

    if planner_input.tool_results:
        sections.append(
            "Results of tools called last step:\n" + _format_tool_results(planner_input)
        )

    sections.append("Now produce the JSON object for the next step.")
    return "\n\n".join(sections)


def _format_available_tools(planner_input: AgenticPlannerInput) -> str:
    if not planner_input.available_tools:
        return ""
    lines = []
    for tool in planner_input.available_tools:
        name = str(tool.get("name") or "")
        namespace = str(tool.get("namespace") or "")
        display = str(tool.get("canonical_name") or make_canonical_tool_name(name, namespace))
        signature = _format_tool_signature(tool.get("parameters"))
        description = str(tool.get("description") or "").strip()
        service_description = str(tool.get("service_description") or "").strip()
        head = f"  - {display}{signature}"
        details = description
        if service_description:
            details = f"{details} [{service_description}]" if details else service_description
        lines.append(f"{head}: {details}" if details else head)
    return "Available tools:\n" + "\n".join(lines)


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
            lines.append(f"  - {display}: ok {_format_json(result.data)}")
        else:
            lines.append(f"  - {display}: error {result.error}")
    return "\n".join(lines)


def _format_plan(items: list[str], current_index: int | None) -> str:
    lines = []
    for index, item in enumerate(items):
        marker = " <- current" if index == current_index else ""
        lines.append(f"  {index}. {item}{marker}")
    return "\n".join(lines)


def _format_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
