"""Agentic robot loop orchestration."""

from __future__ import annotations

import copy
import logging

from dualsystem_agentic.core.parser import parse_agentic_planner_output
from dualsystem_agentic.core.types import (
    AgenticPlannerInput,
    AgenticPlannerOutput,
    AgenticSessionState,
    AgenticStepResult,
    ExecutorInput,
    ExecutorOutput,
    ImageInput,
    JsonDict,
    MonitorStatus,
    ToolCall,
    ToolResult,
    normalize_monitor_status,
)
from dualsystem_agentic.executor.base import ExecutorClient
from dualsystem_agentic.io.dataloader import DataLoader
from dualsystem_agentic.mcp.base import MCPToolClient
from dualsystem_agentic.vlm.base import VLMPlanner

logger = logging.getLogger(__name__)

MONITOR_TOOL_NAME = "monitor"
EXECUTE_TOOL_NAME = "execute"
FETCH_ENV_TOOL_NAME = "fetch_env"


class AgenticRobotLoop:
    """Coordinate planner, MCP tools, monitor feedback, and executor handoff."""

    def __init__(
        self,
        planner: VLMPlanner,
        tool_client: MCPToolClient,
        executor: ExecutorClient,
        *,
        monitor_tool_name: str = MONITOR_TOOL_NAME,
        execute_tool_name: str = EXECUTE_TOOL_NAME,
        fetch_env_tool_name: str = FETCH_ENV_TOOL_NAME,
        dataloader: DataLoader | None = None,
    ) -> None:
        self.planner = planner
        self.tool_client = tool_client
        self.executor = executor
        self.monitor_tool_name = monitor_tool_name
        self.execute_tool_name = execute_tool_name
        self.fetch_env_tool_name = fetch_env_tool_name
        self.dataloader = dataloader

    def step(
        self,
        task: str,
        session_state: AgenticSessionState | JsonDict | None = None,
        *,
        images: dict[str, ImageInput] | None = None,
        metadata: JsonDict | None = None,
    ) -> tuple[AgenticStepResult, AgenticSessionState]:
        state = _state_from(session_state)
        if task:
            state.task = task

        # --- Image acquisition ---
        # Priority: DataLoader > explicit images > previously captured images.
        captured = self._capture_images()
        merged_images = {**(state._last_captured_images or {}), **(images or {})}
        if captured:
            merged_images.update(captured)
            state._last_captured_images = captured

        planner_input = AgenticPlannerInput(
            task=state.task,
            step_index=state.step_index,
            current_subtask=state.current_subtask,
            subtask_index=state.subtask_index,
            subtasks=list(state.subtasks),
            monitor_status=state.monitor_status,
            monitor_error=state.monitor_error,
            tool_results=list(state.last_tool_results),
            environment=dict(state.environment),
            available_tools=self.tool_client.list_tools(),
            images=dict(merged_images),
            metadata=metadata or {},
        )

        if state.awaiting_monitor and state.current_subtask:
            return self._poll_monitor(planner_input, state)

        raw_output = self.planner.generate(planner_input)
        planner_output = parse_agentic_planner_output(raw_output)

        if planner_output.task_complete:
            if planner_output.current_subtask:
                state.current_subtask = planner_output.current_subtask
            if planner_output.subtask_index is not None:
                state.subtask_index = planner_output.subtask_index
            state.last_tool_results = []
            state.awaiting_monitor = False
            result = AgenticStepResult(
                task=state.task,
                step_index=state.step_index,
                planner_input=planner_input,
                planner_output=planner_output,
                current_subtask=state.current_subtask,
                subtask_index=state.subtask_index,
                monitor_status=state.monitor_status,
                monitor_error=state.monitor_error,
                task_complete=True,
                parse_ok=planner_output.parse_ok,
                parse_error=planner_output.parse_error,
            )
            state.step_index += 1
            return result, state

        tool_results = [
            self.tool_client.call_tool(
                tool_call.name,
                tool_call.arguments,
                namespace=tool_call.namespace,
                call_id=tool_call.call_id,
            )
            for tool_call in planner_output.tool_calls
        ]

        # If a tool returned environment updates, capture a fresh image so the
        # next step shows the latest scene to the VLM.
        if any(self._is_environment_result(tool_result) for tool_result in tool_results):
            fresh = self._capture_images()
            if fresh:
                merged_images.update(fresh)
                state._last_captured_images = fresh

        # Resolve the plan for this step: the planner may decompose (new subtasks),
        # revise the list, or just select an entry by index. Current subtask falls
        # back to subtasks[subtask_index] so the planner can select without restating.
        effective_subtasks = list(planner_output.subtasks) if planner_output.subtasks else list(state.subtasks)
        effective_index = (
            planner_output.subtask_index
            if planner_output.subtask_index is not None
            else state.subtask_index
        )
        current_subtask = planner_output.current_subtask
        if not current_subtask and effective_index is not None and 0 <= effective_index < len(effective_subtasks):
            current_subtask = effective_subtasks[effective_index]

        parse_ok = planner_output.parse_ok
        parse_error = planner_output.parse_error
        monitor_status = state.monitor_status
        monitor_error = state.monitor_error
        saw_monitor_feedback = False
        tool_calls_by_id = _tool_calls_by_id(planner_output.tool_calls)
        for tool_result in tool_results:
            if self._is_monitor_result(tool_result):
                saw_monitor_feedback = True
                try:
                    tool_call = tool_calls_by_id.get(tool_result.call_id or "") or _first_tool_call(
                        planner_output.tool_calls,
                        tool_result.tool_name,
                    )
                    _validate_monitor_identity(
                        tool_call=tool_call,
                        result=tool_result,
                        current_subtask=current_subtask or state.current_subtask,
                        subtask_index=effective_index,
                    )
                    monitor_status = normalize_monitor_status(str(tool_result.data.get("status") or ""))
                    monitor_error = _optional_str(tool_result.data.get("error"))
                except (TypeError, ValueError) as exc:
                    parse_ok = False
                    parse_error = str(exc)
                    monitor_error = str(exc)

        state.last_tool_results = tool_results
        state.environment = self._merge_environment(state.environment, tool_results)

        state.subtasks = effective_subtasks
        if effective_index is not None:
            state.subtask_index = effective_index
        if current_subtask:
            state.current_subtask = current_subtask

        state.monitor_status = monitor_status
        state.monitor_error = monitor_error

        executor_output: ExecutorOutput | None = None
        requested_mcp_execute = any(self._is_execute_result(tool_result) for tool_result in tool_results)
        if (
            parse_ok
            and current_subtask
            and planner_output.should_execute
            and not planner_output.task_complete
            and not requested_mcp_execute
        ):
            executor_output = self.executor.execute(
                ExecutorInput(
                    task=state.task,
                    subtask=current_subtask,
                    metadata={
                        **(metadata or {}),
                        "step_index": state.step_index,
                        "subtask_index": state.subtask_index,
                        "monitor_status": state.monitor_status.value if state.monitor_status else None,
                    },
                )
            )
            if executor_output is None:
                executor_output = ExecutorOutput.success()
            if not executor_output.ok and state.monitor_status is MonitorStatus.SUCCESS:
                state.monitor_status = MonitorStatus.FAILED
                state.monitor_error = executor_output.error or "executor failed"

        if requested_mcp_execute and parse_ok and current_subtask and not planner_output.task_complete:
            state.monitor_namespace = _monitor_namespace(tool_results, state.monitor_namespace)
            if not saw_monitor_feedback:
                state.monitor_status = MonitorStatus.RUNNING
                state.monitor_error = None
                state.awaiting_monitor = True
            else:
                state.awaiting_monitor = (
                    state.monitor_status is not MonitorStatus.SUCCESS
                    and state.monitor_status is not MonitorStatus.FAILED
                )
                if state.awaiting_monitor and state.monitor_status is None:
                    state.monitor_status = MonitorStatus.RUNNING
        elif state.monitor_status in {MonitorStatus.SUCCESS, MonitorStatus.FAILED}:
            state.awaiting_monitor = False

        result = AgenticStepResult(
            task=state.task,
            step_index=state.step_index,
            planner_input=planner_input,
            planner_output=planner_output,
            tool_results=tool_results,
            executor_output=executor_output,
            current_subtask=state.current_subtask,
            subtask_index=state.subtask_index,
            monitor_status=state.monitor_status,
            monitor_error=state.monitor_error,
            task_complete=planner_output.task_complete,
            parse_ok=parse_ok,
            parse_error=parse_error,
        )

        state.step_index += 1
        return result, state

    def _poll_monitor(
        self,
        planner_input: AgenticPlannerInput,
        state: AgenticSessionState,
    ) -> tuple[AgenticStepResult, AgenticSessionState]:
        tool_call = ToolCall(
            name=self.monitor_tool_name,
            arguments=_monitor_arguments(state),
            namespace=state.monitor_namespace,
        )
        tool_result = self.tool_client.call_tool(
            tool_call.name,
            tool_call.arguments,
            namespace=tool_call.namespace,
            call_id=tool_call.call_id,
        )
        tool_results = [tool_result]
        planner_output = AgenticPlannerOutput(
            raw_output="[system] monitor poll without VLM",
            tool_calls=[tool_call],
            current_subtask=state.current_subtask,
            subtask_index=state.subtask_index,
            subtasks=[],
            should_execute=False,
            task_complete=False,
        )

        parse_ok = True
        parse_error = None
        monitor_status = state.monitor_status
        monitor_error = state.monitor_error
        if self._is_monitor_result(tool_result):
            try:
                _validate_monitor_identity(
                    tool_call=tool_call,
                    result=tool_result,
                    current_subtask=state.current_subtask,
                    subtask_index=state.subtask_index,
                )
                monitor_status = normalize_monitor_status(str(tool_result.data.get("status") or ""))
                monitor_error = _optional_str(tool_result.data.get("error"))
            except (TypeError, ValueError) as exc:
                parse_ok = False
                parse_error = str(exc)
                monitor_status = MonitorStatus.FAILED
                monitor_error = str(exc)
        else:
            parse_ok = False
            parse_error = tool_result.error or "monitor poll did not return a valid monitor status"
            monitor_status = MonitorStatus.FAILED
            monitor_error = parse_error

        state.last_tool_results = tool_results
        state.environment = self._merge_environment(state.environment, tool_results)
        state.monitor_status = monitor_status
        state.monitor_error = monitor_error
        state.monitor_namespace = tool_result.namespace or state.monitor_namespace
        state.awaiting_monitor = parse_ok and monitor_status is MonitorStatus.RUNNING

        result = AgenticStepResult(
            task=state.task,
            step_index=state.step_index,
            planner_input=planner_input,
            planner_output=planner_output,
            vlm_called=False,
            tool_results=tool_results,
            current_subtask=state.current_subtask,
            subtask_index=state.subtask_index,
            monitor_status=state.monitor_status,
            monitor_error=state.monitor_error,
            task_complete=False,
            parse_ok=parse_ok,
            parse_error=parse_error,
        )

        state.step_index += 1
        return result, state

    def run(
        self,
        task: str,
        *,
        max_steps: int,
        session_state: AgenticSessionState | JsonDict | None = None,
        images: dict[str, ImageInput] | None = None,
        metadata: JsonDict | None = None,
    ) -> tuple[list[AgenticStepResult], AgenticSessionState]:
        state = _state_from(session_state)
        results: list[AgenticStepResult] = []
        for _ in range(max_steps):
            result, state = self.step(task, state, images=images, metadata=metadata)
            results.append(result)
            if result.task_complete:
                break
        return results, state

    def _merge_environment(self, environment: JsonDict, tool_results: list[ToolResult]) -> JsonDict:
        merged = dict(environment)
        for tool_result in tool_results:
            if not self._is_environment_result(tool_result):
                continue
            merged.update(_environment_payload(tool_result, self.fetch_env_tool_name))
        return merged

    def _is_monitor_result(self, tool_result: ToolResult) -> bool:
        if not tool_result.ok:
            return False
        if tool_result.tool_name == self.monitor_tool_name:
            return True
        if _agentic_role(tool_result) == "monitor":
            return True
        return _is_valid_monitor_status(tool_result.data.get("status"))

    def _is_environment_result(self, tool_result: ToolResult) -> bool:
        if not tool_result.ok:
            return False
        if tool_result.tool_name == self.fetch_env_tool_name:
            return True
        if _agentic_role(tool_result) in {"environment", "env", "fetch_env"}:
            return True
        return isinstance(tool_result.data.get("environment"), dict) or isinstance(tool_result.data.get("env"), dict)

    def _is_execute_result(self, tool_result: ToolResult) -> bool:
        if not tool_result.ok:
            return False
        if tool_result.tool_name == self.execute_tool_name:
            return True
        if _agentic_role(tool_result) in {"execute", "action"}:
            return True
        return tool_result.data.get("executed") is True

    def _capture_images(self) -> dict[str, ImageInput] | None:
        if self.dataloader is None:
            return None
        try:
            frame = self.dataloader.capture()
        except Exception as exc:
            logger.warning("DataLoader capture failed: %s", exc)
            return None
        if frame is None:
            return None
        return frame.images or None


def _state_from(value: AgenticSessionState | JsonDict | None) -> AgenticSessionState:
    if isinstance(value, AgenticSessionState):
        return copy.deepcopy(value)
    return AgenticSessionState.from_dict(value)


def _validate_monitor_identity(
    *,
    tool_call: ToolCall | None,
    result: ToolResult,
    current_subtask: str | None,
    subtask_index: int | None,
) -> None:
    for monitor_subtask in _identity_values(("subtask", "current_subtask"), tool_call, result):
        normalized_subtask = _optional_str(monitor_subtask)
        if normalized_subtask and current_subtask and normalized_subtask != current_subtask:
            raise ValueError(
                f"Monitor subtask mismatch: expected {current_subtask!r}, got {normalized_subtask!r}"
            )
    for monitor_index in _identity_values(("subtask_index",), tool_call, result):
        if monitor_index is not None:
            normalized_index = int(monitor_index)
            if subtask_index is not None and normalized_index != subtask_index:
                raise ValueError(
                    f"Monitor subtask_index mismatch: expected {subtask_index}, got {normalized_index}"
                )


def _identity_values(keys: tuple[str, ...], tool_call: ToolCall | None, result: ToolResult) -> list[object]:
    values: list[object] = []
    for key in keys:
        if tool_call and tool_call.arguments.get(key) is not None:
            values.append(tool_call.arguments[key])
        if result.data.get(key) is not None:
            values.append(result.data[key])
    return values


def _tool_calls_by_id(tool_calls: list[ToolCall]) -> dict[str, ToolCall]:
    return {tool_call.call_id: tool_call for tool_call in tool_calls if tool_call.call_id}


def _first_tool_call(tool_calls: list[ToolCall], name: str) -> ToolCall | None:
    for tool_call in tool_calls:
        if tool_call.name == name:
            return tool_call
    return None


def _monitor_arguments(state: AgenticSessionState) -> JsonDict:
    arguments: JsonDict = {}
    if state.current_subtask is not None:
        arguments["subtask"] = state.current_subtask
    if state.subtask_index is not None:
        arguments["subtask_index"] = state.subtask_index
    return arguments


def _monitor_namespace(tool_results: list[ToolResult], fallback: str | None) -> str | None:
    for tool_result in tool_results:
        if tool_result.namespace:
            return tool_result.namespace
    return fallback


def _optional_str(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _agentic_role(tool_result: ToolResult) -> str | None:
    role = _optional_str(tool_result.data.get("agentic_role") or tool_result.data.get("_agentic_role"))
    return role.lower() if role else None


def _is_valid_monitor_status(value: object | None) -> bool:
    if value is None:
        return False
    try:
        normalize_monitor_status(str(value))
    except ValueError:
        return False
    return True


def _environment_payload(tool_result: ToolResult, fetch_env_tool_name: str) -> JsonDict:
    if isinstance(tool_result.data.get("environment"), dict):
        return tool_result.data["environment"]  # type: ignore[return-value]
    if isinstance(tool_result.data.get("env"), dict):
        return tool_result.data["env"]  # type: ignore[return-value]
    if tool_result.tool_name == fetch_env_tool_name or _agentic_role(tool_result) in {"environment", "env", "fetch_env"}:
        return tool_result.data
    return {}
