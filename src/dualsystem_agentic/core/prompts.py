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

Executable subtask constraints:
- Every item in "subtasks" must be a concrete physical robot action that can be
  sent directly to the execute tool or downstream executor.
- Use the attached images to name visible objects and target locations directly
  before writing subtasks.
- Do NOT create subtasks for checking status, monitoring, observing, analyzing
  images, planning, deciding, verifying, ensuring, or conditional logic.
- Do NOT write conditional subtasks such as "if items are present..." or vague
  subtasks such as "organize the items".
- Good subtasks:
  - "Pick up the pink cup and place it in the dish rack."
  - "Pick up the blue bowl and place it in the dish rack."
- Bad subtasks:
  - "Check the status of the current task."
  - "Analyze the image to identify all items."
  - "If items are present, move them to the dish rack."
  - "Perform a final check to ensure all items are organized."

Tool use:
- Call ONLY tools from the "Available tools" list, by the exact canonical name
  shown, and pass arguments that match the listed signature.
- Any available tool may be called; newly exposed robot tools do not need a
  special config entry. Interpret their listed description and schema.
- Tools that report subtask status should return {"status": "running|success|failed"}.
  Tools that return scene state may return {"scene_graph": {...}},
  {"environment": {...}}, or {"env": {...}}. Tools that perform an action may
  return {"executed": true}; in that case the downstream executor is skipped for
  that step.
- Treat execute as STARTING an asynchronous robot action, not as proof that the
  action finished. A monitor event/status tells you whether the action is still
  running, succeeded, failed, or timed out.
- If Active execution is running, DO NOT start any new execute action. You may
  observe, update the plan, wait, cancel/stop with an available safety tool, or
  react to monitor events. Set "should_execute": false while waiting/observing.
- A monitor_success event means the active action reached a terminal success; now
  advance to the next subtask or complete the task. A monitor_failed or
  monitor_timeout event means retry, replan, cancel, ask for help, or abort.
- Set "task_complete": true only when the whole task is finished AND there is no
  running Active execution.

Respond with ONLY a JSON object, no extra prose:
{
  "decision": "plan|execute|observe|wait|replan|cancel|complete|noop",
  "tool_calls": [
    {"name": "<canonical tool name from the list>", "arguments": {}}
  ],
  "subtasks": ["<full ordered plan; required on the first step and whenever you revise it>"],
  "subtask_index": <0-based index of the current subtask within the plan>,
  "current_subtask": "<optional: the subtask text; defaults to subtasks[subtask_index]>",
  "should_execute": false,
  "task_complete": false
}
Use "should_execute": true only when you want the downstream executor to start
the current_subtask and you did not call an execute tool. Use "should_execute":
false for plan, observe, wait, cancel, complete, and noop decisions."""


def build_agentic_prompt(planner_input: AgenticPlannerInput) -> str:
    """Render an ``AgenticPlannerInput`` into a planner prompt string."""
    sections: list[str] = [_SYSTEM_INSTRUCTION]

    visual_block = _format_visual_observations(planner_input)
    if visual_block:
        sections.append(visual_block)

    tools_block = _format_available_tools(planner_input)
    if tools_block:
        sections.append(tools_block)

    sections.append(_format_session_memory(planner_input))
    sections.append("Now produce the JSON object for the next step.")
    return "\n\n".join(sections)


def _format_session_memory(planner_input: AgenticPlannerInput) -> str:
    lines = [
        "Session memory / Runtime state:",
        f"  Task: {planner_input.task}",
        f"  Controller phase: {planner_input.phase.value}",
        f"  Step index: {planner_input.step_index}",
        f"  Reason requested: {str(planner_input.reason_requested).lower()}",
    ]

    if planner_input.subtasks:
        lines.append(
            "  Subtask plan (select the current one by index, or revise the list):"
        )
        plan_lines = _format_plan(
            planner_input.subtasks,
            planner_input.subtask_index,
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
        lines.append("  " + _format_json(planner_input.active_execution.to_dict()))

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
        f"- Images are attached before this text in this label order: {labels}.",
        "- Treat the attached images as the latest visual observation for this reason step.",
        "- Use Scene graph only when it is present; it is structured memory and may not be refreshed on every VLM call.",
    ]
    return "\n".join(lines)


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


def _format_events(planner_input: AgenticPlannerInput) -> str:
    lines = []
    for event in planner_input.events:
        payload = event.to_dict()
        lines.append(f"  - {event.event_type}: {_format_json(payload)}")
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
