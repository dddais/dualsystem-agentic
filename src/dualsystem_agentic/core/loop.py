"""Agentic robot loop orchestration."""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass

from dualsystem_agentic.core.parser import parse_agentic_planner_output
from dualsystem_agentic.core.types import (
    ActiveExecution,
    AgenticEvent,
    AgenticPlannerInput,
    AgenticPlannerOutput,
    AgenticPhase,
    AgenticSessionState,
    AgenticStepResult,
    ExecutorOutput,
    ImageInput,
    JsonDict,
    MonitorStatus,
    SubtaskStatus,
    ToolCall,
    ToolResult,
    normalize_monitor_status,
)
from dualsystem_agentic.core.tool_names import split_qualified_tool_name
from dualsystem_agentic.executor.base import ExecutorClient
from dualsystem_agentic.io.dataloader import DataLoader
from dualsystem_agentic.mcp.base import MCPToolClient
from dualsystem_agentic.vlm.base import VLMPlanner

logger = logging.getLogger(__name__)

MONITOR_TOOL_NAME = "monitor"
EXECUTE_TOOL_NAME = "execute"
FETCH_ENV_TOOL_NAME = "fetch_env"
CONTROL_TOOL_NAMES = {"stop_task", "reset_task", "emergency_stop"}


@dataclass(frozen=True)
class _PlannerBlock:
    message: str
    event_type: str = "planner_inconsistent"


@dataclass(frozen=True)
class _PlanSelection:
    current_subtask: str | None
    subtask_index: int | None
    error: str | None = None


@dataclass(frozen=True)
class _StepPlan:
    subtasks: list[str]
    subtask_statuses: list[SubtaskStatus]
    current_subtask: str | None
    subtask_index: int | None


@dataclass(frozen=True)
class _MonitorUpdate:
    monitor_status: MonitorStatus | None
    monitor_error: str | None
    error: str | None = None


@dataclass(frozen=True)
class _ExecutionStep:
    parse_ok: bool
    parse_error: str | None
    executor_output: ExecutorOutput | None = None


@dataclass(frozen=True)
class _ControlUpdate:
    monitor_status: MonitorStatus | None
    monitor_error: str | None
    affected_subtask: str | None = None
    affected_subtask_index: int | None = None


@dataclass(frozen=True)
class _AvailableToolNames:
    canonical: set[str]
    short_counts: dict[str, int]


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
        include_metadata_in_prompt: bool = False,
    ) -> None:
        self.planner = planner
        self.tool_client = tool_client
        self.executor = executor
        self.monitor_tool_name = monitor_tool_name
        self.execute_tool_name = execute_tool_name
        self.fetch_env_tool_name = fetch_env_tool_name
        self.dataloader = dataloader
        self.include_metadata_in_prompt = include_metadata_in_prompt

    def step(
        self,
        task: str,
        session_state: AgenticSessionState | JsonDict | None = None,
        *,
        images: dict[str, ImageInput] | None = None,
        metadata: JsonDict | None = None,
        force_reason: bool = False,
        reason_interval_s: float | None = None,
    ) -> tuple[AgenticStepResult, AgenticSessionState]:
        state = _state_from(session_state)
        if task:
            state.task = task
        if state.phase is AgenticPhase.INIT:
            state.phase = AgenticPhase.READY
        _advance_current_after_completed_success(state)

        events = list(state.pending_events)
        should_reason = force_reason or state.reason_requested or bool(events)
        now = time.time()
        if reason_interval_s is not None and reason_interval_s >= 0:
            should_reason = should_reason or state.last_reason_at is None or (
                now - state.last_reason_at >= reason_interval_s
            )

        # --- Image acquisition ---
        # Priority: DataLoader > explicit images > previously captured images.
        captured = self._capture_images()
        merged_images = {**(state._last_captured_images or {}), **(images or {})}
        if captured:
            merged_images.update(captured)
            state._last_captured_images = captured

        planner_input = AgenticPlannerInput(
            task=state.task,
            phase=AgenticPhase.REASON if should_reason else state.phase,
            step_index=state.step_index,
            current_subtask=state.current_subtask,
            subtask_index=state.subtask_index,
            subtasks=list(state.subtasks),
            subtask_statuses=list(state.subtask_statuses),
            monitor_status=state.monitor_status,
            monitor_error=state.monitor_error,
            active_execution=copy.deepcopy(state.active_execution),
            events=copy.deepcopy(events),
            reason_requested=should_reason,
            tool_results=list(state.last_tool_results),
            environment=dict(state.environment),
            available_tools=self.tool_client.list_tools(),
            images=dict(merged_images),
            metadata=_planner_metadata(metadata or {}, self.include_metadata_in_prompt),
        )

        if not should_reason:
            return _tick_without_reason_result(state, planner_input, events), state

        state.phase = AgenticPhase.REASON
        state.last_reason_at = now
        state.reason_requested = False
        state.pending_events = []
        raw_output = self.planner.generate(planner_input)
        planner_input = _planner_input_after_generate(
            planner_input,
            self.planner,
            state.environment,
        )
        planner_output = parse_agentic_planner_output(raw_output)

        if planner_output.task_complete:
            result = _task_complete_result(
                state=state,
                planner_input=planner_input,
                planner_output=planner_output,
                prior_events=events,
            )
            state.step_index += 1
            return result, state

        # Resolve the plan for this step: the planner may decompose (new subtasks),
        # revise the list, or just select an entry by index. Current subtask falls
        # back to subtasks[subtask_index] so the planner can select without restating.
        step_plan = _resolve_step_plan(state, planner_output)
        effective_subtasks = step_plan.subtasks
        effective_subtask_statuses = step_plan.subtask_statuses
        effective_index = step_plan.subtask_index
        current_subtask = step_plan.current_subtask

        parse_ok = planner_output.parse_ok
        parse_error = planner_output.parse_error
        monitor_status = state.monitor_status
        monitor_error = state.monitor_error
        tool_results: list[ToolResult] = []
        produced_events: list[AgenticEvent] = []
        blocked: _PlannerBlock | None = None
        if parse_ok:
            plan_selection = _validate_and_reconcile_plan_selection(
                effective_subtasks,
                effective_index,
                current_subtask,
            )
            if plan_selection.error:
                parse_ok = False
                parse_error = plan_selection.error
                state.phase = AgenticPhase.ERROR
                _queue_event_if_reasonable(
                    state,
                    _event(
                        "planner_inconsistent",
                        {"error": parse_error, "subtask": current_subtask},
                        source="planner",
                    ),
                    events=produced_events,
                )
            else:
                effective_index = plan_selection.subtask_index
                current_subtask = plan_selection.current_subtask
                blocked = _prepare_planner_tool_calls(
                    state=state,
                    planner_output=planner_output,
                    available_tools=planner_input.available_tools,
                    current_subtask=current_subtask,
                    subtask_index=effective_index,
                    execute_tool_name=self.execute_tool_name,
                    monitor_tool_name=self.monitor_tool_name,
                )
        if parse_ok:
            planner_output.tool_calls = _hydrate_monitor_tool_calls(
                planner_output.tool_calls,
                state,
                self.monitor_tool_name,
            )
        if parse_ok and blocked is None:
            blocked = _planner_action_block(
                state=state,
                planner_output=planner_output,
                subtasks=effective_subtasks,
                subtask_statuses=effective_subtask_statuses,
                current_subtask=current_subtask,
                subtask_index=effective_index,
                monitor_tool_name=self.monitor_tool_name,
                execute_tool_name=self.execute_tool_name,
                fetch_env_tool_name=self.fetch_env_tool_name,
            )
        if blocked is not None:
            parse_ok = False
            parse_error = blocked.message
            state.phase = AgenticPhase.ERROR
            _queue_event_if_reasonable(
                state,
                _event(
                    blocked.event_type,
                    {"error": blocked.message, "subtask": current_subtask},
                    source="planner",
                ),
                events=produced_events,
            )
        elif parse_ok:
            state.phase = AgenticPhase.ACT if planner_output.tool_calls else AgenticPhase.RESPONSE
            tool_results = self._call_tools(planner_output.tool_calls)

        # If a tool returned environment updates, capture a fresh image so the
        # next step shows the latest scene to the VLM.
        if any(self._is_environment_result(tool_result) for tool_result in tool_results):
            self._refresh_images_after_environment_result(state, merged_images)

        saw_monitor_feedback = False
        tool_calls_by_id = _tool_calls_by_id(planner_output.tool_calls)
        for tool_result in tool_results:
            if self._is_monitor_result(tool_result) and not self._is_execute_result(tool_result):
                saw_monitor_feedback = True
                monitor_update = _apply_monitor_result(
                    state=state,
                    tool_result=tool_result,
                    tool_call=tool_calls_by_id.get(tool_result.call_id or "") or _first_tool_call(
                        planner_output.tool_calls,
                        tool_result.tool_name,
                    ),
                    current_subtask=current_subtask or state.current_subtask,
                    subtask_index=effective_index,
                    subtasks=effective_subtasks,
                    statuses=effective_subtask_statuses,
                    events=produced_events,
                )
                if monitor_update.error:
                    parse_ok = False
                    parse_error = monitor_update.error
                    state.phase = AgenticPhase.ERROR
                    monitor_error = monitor_update.error
                else:
                    monitor_status = monitor_update.monitor_status
                    monitor_error = monitor_update.monitor_error

        control_status = _apply_control_results(state, tool_results, events=produced_events)
        if control_status is not None and control_status.monitor_status is not None:
            monitor_status = control_status.monitor_status
            monitor_error = control_status.monitor_error
            if control_status.affected_subtask is not None:
                _mark_subtask_status(
                    state,
                    control_status.affected_subtask,
                    control_status.affected_subtask_index,
                    control_status.monitor_status,
                    subtasks=effective_subtasks,
                    statuses=effective_subtask_statuses,
                )

        state.last_tool_results = tool_results
        state.environment = self._merge_environment(state.environment, tool_results)

        if parse_ok:
            state.subtasks = effective_subtasks
            state.subtask_statuses = effective_subtask_statuses
            if effective_index is not None:
                state.subtask_index = effective_index
            if current_subtask:
                state.current_subtask = current_subtask

        state.monitor_status = monitor_status
        state.monitor_error = monitor_error

        executor_output: ExecutorOutput | None = None
        requested_mcp_execute = any(
            _is_execute_call(tool_call, self.execute_tool_name)
            for tool_call in planner_output.tool_calls
        )
        failed_execute_result = _first_failed_execute_result(tool_results, self.execute_tool_name)
        if failed_execute_result is not None:
            parse_ok = False
            parse_error = failed_execute_result.error or "execute tool failed"
            state.phase = AgenticPhase.ERROR
            _queue_event_if_reasonable(
                state,
                _event(
                    "execute_failed",
                    {"subtask": current_subtask, "error": parse_error},
                    source=failed_execute_result.namespace or failed_execute_result.tool_name,
                ),
                events=produced_events,
            )
            state.reason_requested = True

        if parse_ok:
            execute_step = self._finalize_mcp_execute_if_needed(
                state=state,
                planner_output=planner_output,
                current_subtask=current_subtask,
                requested_mcp_execute=requested_mcp_execute,
                tool_results=tool_results,
                started_at=now,
                metadata=metadata or {},
                saw_monitor_feedback=saw_monitor_feedback,
                events=produced_events,
            )
            parse_ok = execute_step.parse_ok
            parse_error = execute_step.parse_error

        if not parse_ok:
            state.phase = AgenticPhase.ERROR
        if parse_ok and state.phase not in {AgenticPhase.ERROR, AgenticPhase.DONE}:
            state.phase = AgenticPhase.RESPONSE
        if state.pending_events:
            state.reason_requested = True
        if parse_ok and _passive_plan_needs_followup_reason(
            state,
            planner_output,
        ):
            state.reason_requested = True

        result = AgenticStepResult(
            task=state.task,
            step_index=state.step_index,
            planner_input=planner_input,
            planner_output=planner_output,
            phase=state.phase,
            tool_results=tool_results,
            executor_output=executor_output,
            current_subtask=state.current_subtask,
            subtask_index=state.subtask_index,
            subtask_statuses=list(state.subtask_statuses),
            monitor_status=state.monitor_status,
            monitor_error=state.monitor_error,
            active_execution=copy.deepcopy(state.active_execution),
            events=events + _dedupe_events(produced_events + list(state.pending_events)),
            reason_requested=state.reason_requested,
            task_complete=planner_output.task_complete and parse_ok,
            parse_ok=parse_ok,
            parse_error=parse_error,
        )

        state.step_index += 1
        return result, state

    def poll_monitor(
        self,
        task: str,
        session_state: AgenticSessionState | JsonDict,
        *,
        images: dict[str, ImageInput] | None = None,
        metadata: JsonDict | None = None,
    ) -> tuple[AgenticStepResult, AgenticSessionState]:
        """Poll a legacy synchronous monitor and turn its status into events."""
        state = _state_from(session_state)
        if task:
            state.task = task
        if state.active_execution is None:
            planner_input = self._planner_input(
                state,
                images=images,
                metadata=metadata,
                events=list(state.pending_events),
                reason_requested=False,
            )
            result = AgenticStepResult(
                task=state.task,
                step_index=state.step_index,
                planner_input=planner_input,
                planner_output=AgenticPlannerOutput(
                    raw_output="[system] monitor poll skipped; no active execution",
                    should_execute=False,
                ),
                phase=state.phase,
                vlm_called=False,
                current_subtask=state.current_subtask,
                subtask_index=state.subtask_index,
                subtask_statuses=list(state.subtask_statuses),
                monitor_status=state.monitor_status,
                monitor_error=state.monitor_error,
                active_execution=None,
                events=list(state.pending_events),
                reason_requested=state.reason_requested,
            )
            state.awaiting_monitor = False
            return result, state

        planner_input = self._planner_input(
            state,
            images=images,
            metadata=metadata,
            events=list(state.pending_events),
            reason_requested=False,
        )
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
        monitored_subtask = _active_subtask(state)
        monitored_subtask_index = _active_subtask_index(state)
        planner_output = AgenticPlannerOutput(
            raw_output="[system] monitor poll without VLM",
            tool_calls=[tool_call],
            current_subtask=monitored_subtask,
            subtask_index=monitored_subtask_index,
            subtasks=[],
            should_execute=False,
            task_complete=False,
        )

        parse_ok = True
        parse_error = None
        monitor_status = state.monitor_status
        monitor_error = state.monitor_error
        produced_events: list[AgenticEvent] = []
        if self._is_monitor_result(tool_result):
            try:
                _validate_monitor_identity(
                    tool_call=tool_call,
                    result=tool_result,
                    current_subtask=monitored_subtask,
                    subtask_index=monitored_subtask_index,
                )
                monitor_status = normalize_monitor_status(str(tool_result.data.get("status") or ""))
                monitor_error = _optional_str(tool_result.data.get("error"))
                produced_events.append(
                    _monitor_event_from_status(
                        monitor_status,
                        tool_result=tool_result,
                        subtask=monitored_subtask,
                        subtask_index=monitored_subtask_index,
                    )
                )
                _mark_subtask_status(state, monitored_subtask, monitored_subtask_index, monitor_status)
            except (TypeError, ValueError) as exc:
                parse_ok = False
                parse_error = str(exc)
                monitor_status = MonitorStatus.FAILED
                monitor_error = str(exc)
                produced_events.append(
                    _event("monitor_failed", {"error": str(exc)}, source="monitor")
                )
        else:
            parse_ok = False
            parse_error = tool_result.error or "monitor poll did not return a valid monitor status"
            monitor_status = MonitorStatus.FAILED
            monitor_error = parse_error
            produced_events.append(
                _event("monitor_failed", {"error": parse_error}, source="monitor")
            )

        state.last_tool_results = tool_results
        state.environment = self._merge_environment(state.environment, tool_results)
        state.monitor_status = monitor_status
        state.monitor_error = monitor_error
        state.monitor_namespace = tool_result.namespace or state.monitor_namespace
        state.awaiting_monitor = parse_ok and monitor_status is MonitorStatus.RUNNING
        if state.active_execution is not None:
            state.active_execution.status = monitor_status.value
            state.active_execution.error = monitor_error
            state.active_execution.updated_at = time.time()
            state.active_execution.monitor_id = (
                _optional_str(tool_result.data.get("monitor_id"))
                or _optional_str(tool_result.data.get("id"))
                or state.active_execution.monitor_id
            )
        if monitor_status in {MonitorStatus.SUCCESS, MonitorStatus.FAILED}:
            _finish_active_execution(state, monitor_status, monitor_error)
        queued_events = [
            _queue_event_if_reasonable(state, event)
            for event in produced_events
            if _event_requests_reason(event)
        ]
        if queued_events:
            state.reason_requested = True
        state.phase = AgenticPhase.RESPONSE if parse_ok else AgenticPhase.ERROR

        result = AgenticStepResult(
            task=state.task,
            step_index=state.step_index,
            planner_input=planner_input,
            planner_output=planner_output,
            phase=state.phase,
            vlm_called=False,
            tool_results=tool_results,
            current_subtask=monitored_subtask,
            subtask_index=monitored_subtask_index,
            subtask_statuses=list(state.subtask_statuses),
            monitor_status=state.monitor_status,
            monitor_error=state.monitor_error,
            active_execution=copy.deepcopy(state.active_execution),
            events=produced_events,
            reason_requested=state.reason_requested,
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
        vlm_steps = 0
        while vlm_steps < max_steps:
            result, state = self.step(
                task,
                state,
                images=images,
                metadata=metadata,
                force_reason=True,
            )
            results.append(result)
            if result.vlm_called:
                vlm_steps += 1
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
        return (
            isinstance(tool_result.data.get("scene_graph"), dict)
            or isinstance(tool_result.data.get("environment"), dict)
            or isinstance(tool_result.data.get("env"), dict)
        )

    def _is_execute_result(self, tool_result: ToolResult) -> bool:
        if not tool_result.ok:
            return False
        return _is_configured_execute_result(tool_result, self.execute_tool_name)

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

    def _call_tools(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        return [
            self.tool_client.call_tool(
                tool_call.name,
                tool_call.arguments,
                namespace=tool_call.namespace,
                call_id=tool_call.call_id,
            )
            for tool_call in tool_calls
        ]

    def _refresh_images_after_environment_result(
        self,
        state: AgenticSessionState,
        merged_images: dict[str, ImageInput],
    ) -> None:
        fresh = self._capture_images()
        if not fresh:
            return
        merged_images.update(fresh)
        state._last_captured_images = fresh

    def _planner_input(
        self,
        state: AgenticSessionState,
        *,
        images: dict[str, ImageInput] | None = None,
        metadata: JsonDict | None = None,
        events: list[AgenticEvent] | None = None,
        reason_requested: bool = False,
    ) -> AgenticPlannerInput:
        captured = self._capture_images()
        merged_images = {**(state._last_captured_images or {}), **(images or {})}
        if captured:
            merged_images.update(captured)
            state._last_captured_images = captured
        return AgenticPlannerInput(
            task=state.task,
            phase=state.phase,
            step_index=state.step_index,
            current_subtask=state.current_subtask,
            subtask_index=state.subtask_index,
            subtasks=list(state.subtasks),
            subtask_statuses=list(state.subtask_statuses),
            monitor_status=state.monitor_status,
            monitor_error=state.monitor_error,
            active_execution=copy.deepcopy(state.active_execution),
            events=copy.deepcopy(events or []),
            reason_requested=reason_requested,
            tool_results=list(state.last_tool_results),
            environment=dict(state.environment),
            available_tools=self.tool_client.list_tools(),
            images=dict(merged_images),
            metadata=_planner_metadata(metadata or {}, self.include_metadata_in_prompt),
        )

    def _finalize_mcp_execute_if_needed(
        self,
        *,
        state: AgenticSessionState,
        planner_output: AgenticPlannerOutput,
        current_subtask: str | None,
        requested_mcp_execute: bool,
        tool_results: list[ToolResult],
        started_at: float,
        metadata: JsonDict,
        saw_monitor_feedback: bool,
        events: list[AgenticEvent],
    ) -> _ExecutionStep:
        if requested_mcp_execute and _active_execution_running(state.active_execution):
            parse_error = (
                "execute result was produced while active_execution is running; "
                "planner must wait, observe, or cancel before executing again"
            )
            state.phase = AgenticPhase.ERROR
            state.reason_requested = True
            return _ExecutionStep(parse_ok=False, parse_error=parse_error)
        if requested_mcp_execute and current_subtask and not planner_output.task_complete:
            state.monitor_namespace = _monitor_namespace(tool_results, state.monitor_namespace)
            execute_result = _first_execute_result(tool_results, self.execute_tool_name)
            start_monitor_result = self._start_active_execution(
                state=state,
                subtask=current_subtask,
                subtask_index=state.subtask_index,
                namespace=state.monitor_namespace,
                execution_id=_execution_id(execute_result),
                monitor_id=_monitor_id(execute_result),
                started_at=started_at,
                metadata=metadata,
                monitor_result=_first_monitor_result(
                    tool_results,
                    self.monitor_tool_name,
                    self.execute_tool_name,
                ),
                emit_monitor_event=not saw_monitor_feedback,
                events=events,
            )
            if start_monitor_result is not None:
                tool_results.append(start_monitor_result)
            _finalize_requested_execute_state(
                state,
                saw_monitor_feedback=saw_monitor_feedback,
            )
        elif _active_execution_running(state.active_execution) and state.monitor_status in {
            MonitorStatus.SUCCESS,
            MonitorStatus.FAILED,
        }:
            state.awaiting_monitor = False
            _finish_active_execution(state, state.monitor_status, state.monitor_error)
        return _ExecutionStep(parse_ok=True, parse_error=None)

    def _start_active_execution(
        self,
        *,
        state: AgenticSessionState,
        subtask: str,
        subtask_index: int | None,
        namespace: str | None,
        execution_id: str | None,
        monitor_id: str | None,
        started_at: float,
        metadata: JsonDict,
        monitor_result: ToolResult | None = None,
        emit_monitor_event: bool = True,
        events: list[AgenticEvent] | None = None,
    ) -> ToolResult | None:
        started_monitor_result: ToolResult | None = None
        if monitor_result is None:
            started_monitor_result = self._start_monitor(
                state=state,
                subtask=subtask,
                subtask_index=subtask_index,
                namespace=namespace,
                execution_id=execution_id,
                monitor_id=monitor_id,
                metadata=metadata,
                events=events,
            )
            monitor_result = started_monitor_result
        monitor_ok = bool(monitor_result and monitor_result.ok)
        monitor_id = (
            (_optional_str(monitor_result.data.get("monitor_id")) if monitor_ok else None)
            or monitor_id
        )
        state.active_execution = ActiveExecution(
            subtask=subtask,
            subtask_index=subtask_index,
            execution_id=execution_id,
            monitor_id=monitor_id,
            status=MonitorStatus.RUNNING.value,
            namespace=namespace or (monitor_result.namespace if monitor_ok else None),
            started_at=started_at,
            updated_at=started_at,
        )
        state.monitor_status = MonitorStatus.RUNNING
        state.monitor_error = None
        state.awaiting_monitor = True
        state.monitor_namespace = state.active_execution.namespace or state.monitor_namespace
        _mark_subtask_status(state, subtask, subtask_index, MonitorStatus.RUNNING)
        if monitor_ok and monitor_result is not None:
            status = _monitor_status_from_result(monitor_result)
            if status is not None:
                state.monitor_status = status
                state.monitor_error = _optional_str(monitor_result.data.get("error"))
                state.active_execution.status = status.value
                state.active_execution.error = state.monitor_error
                if status in {MonitorStatus.SUCCESS, MonitorStatus.FAILED}:
                    _finish_active_execution(state, status, state.monitor_error)
                if emit_monitor_event:
                    _queue_event_if_reasonable(
                        state,
                        _monitor_event_from_status(
                            status,
                            tool_result=monitor_result,
                            subtask=subtask,
                            subtask_index=subtask_index,
                        ),
                        events=events,
                    )
            elif emit_monitor_event:
                _queue_event_if_reasonable(
                    state,
                    _event(
                        "monitor_running",
                        {
                            "subtask": subtask,
                            "subtask_index": subtask_index,
                            "execution_id": execution_id,
                            "monitor_id": monitor_id,
                        },
                        source="monitor",
                    ),
                    events=events,
                )
        return started_monitor_result

    def _start_monitor(
        self,
        *,
        state: AgenticSessionState,
        subtask: str,
        subtask_index: int | None,
        namespace: str | None,
        execution_id: str | None,
        monitor_id: str | None,
        metadata: JsonDict,
        events: list[AgenticEvent] | None = None,
    ) -> ToolResult | None:
        arguments: JsonDict = {"subtask": subtask}
        if subtask_index is not None:
            arguments["subtask_index"] = subtask_index
        if execution_id is not None:
            arguments["execution_id"] = execution_id
        if monitor_id is not None:
            arguments["monitor_id"] = monitor_id
        if metadata:
            arguments["metadata"] = metadata
        result = self.tool_client.call_tool(
            self.monitor_tool_name,
            arguments,
            namespace=namespace or state.monitor_namespace,
        )
        if result.ok:
            state.monitor_namespace = result.namespace or namespace or state.monitor_namespace
            return result
        _queue_event_if_reasonable(
            state,
            _event(
                "monitor_failed",
                {
                    "subtask": subtask,
                    "subtask_index": subtask_index,
                    "execution_id": execution_id,
                    "error": result.error,
                },
                source="monitor",
            ),
            events=events,
        )
        state.reason_requested = True
        return result


def _state_from(value: AgenticSessionState | JsonDict | None) -> AgenticSessionState:
    if isinstance(value, AgenticSessionState):
        return copy.deepcopy(value)
    return AgenticSessionState.from_dict(value)


def _tick_without_reason_result(
    state: AgenticSessionState,
    planner_input: AgenticPlannerInput,
    events: list[AgenticEvent],
) -> AgenticStepResult:
    state.phase = AgenticPhase.READY if state.active_execution is None else AgenticPhase.RESPONSE
    return AgenticStepResult(
        task=state.task,
        step_index=state.step_index,
        planner_input=planner_input,
        planner_output=AgenticPlannerOutput(
            raw_output="[system] tick without reason",
            should_execute=False,
        ),
        phase=state.phase,
        vlm_called=False,
        current_subtask=state.current_subtask,
        subtask_index=state.subtask_index,
        subtask_statuses=list(state.subtask_statuses),
        monitor_status=state.monitor_status,
        monitor_error=state.monitor_error,
        active_execution=copy.deepcopy(state.active_execution),
        events=events,
        reason_requested=False,
    )


def _task_complete_result(
    *,
    state: AgenticSessionState,
    planner_input: AgenticPlannerInput,
    planner_output: AgenticPlannerOutput,
    prior_events: list[AgenticEvent],
) -> AgenticStepResult:
    parse_ok = planner_output.parse_ok
    parse_error = planner_output.parse_error
    produced_events: list[AgenticEvent] = []
    if planner_output.decision not in {None, "complete"}:
        parse_ok = False
        parse_error = (
            "planner cannot combine task_complete=true with "
            f"decision={planner_output.decision!r}"
        )
        state.phase = AgenticPhase.ERROR
        _queue_event_if_reasonable(
            state,
            _event(
                "planner_inconsistent",
                {"error": parse_error, "subtask": state.current_subtask},
                source="planner",
            ),
            events=produced_events,
        )
    elif planner_output.tool_calls:
        parse_ok = False
        parse_error = "planner cannot combine task_complete=true with tool_calls"
        state.phase = AgenticPhase.ERROR
        _queue_event_if_reasonable(
            state,
            _event(
                "planner_inconsistent",
                {"error": parse_error, "subtask": state.current_subtask},
                source="planner",
            ),
            events=produced_events,
        )
    elif _active_execution_running(state.active_execution):
        parse_ok = False
        parse_error = "planner cannot mark task_complete while active_execution is running"
        state.phase = AgenticPhase.ERROR
        state.reason_requested = True
    elif _has_partial_subtask_progress_with_remaining_work(state):
        parse_ok = False
        parse_error = (
            "planner cannot mark task_complete while the subtask plan has "
            "incomplete subtask(s); finish, replan, or explicitly cancel remaining work"
        )
        state.phase = AgenticPhase.ERROR
        _queue_event_if_reasonable(
            state,
            _event(
                "planner_inconsistent",
                {"error": parse_error, "subtask": state.current_subtask},
                source="planner",
            ),
            events=produced_events,
        )
    else:
        state.phase = AgenticPhase.DONE
    if parse_ok and planner_output.current_subtask:
        state.current_subtask = planner_output.current_subtask
    if parse_ok and planner_output.subtask_index is not None:
        state.subtask_index = planner_output.subtask_index
    state.last_tool_results = []
    state.awaiting_monitor = _active_execution_running(state.active_execution)
    return AgenticStepResult(
        task=state.task,
        step_index=state.step_index,
        planner_input=planner_input,
        planner_output=planner_output,
        phase=state.phase,
        current_subtask=state.current_subtask,
        subtask_index=state.subtask_index,
        subtask_statuses=list(state.subtask_statuses),
        monitor_status=state.monitor_status,
        monitor_error=state.monitor_error,
        active_execution=copy.deepcopy(state.active_execution),
        events=prior_events + _dedupe_events(produced_events + list(state.pending_events)),
        reason_requested=state.reason_requested,
        task_complete=parse_ok,
        parse_ok=parse_ok,
        parse_error=parse_error,
    )


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
    for key in ("execution_id", "monitor_id"):
        expected_identity: str | None = None
        for identity_value in _identity_values((key,), tool_call, result):
            normalized_identity = _optional_str(identity_value)
            if not normalized_identity:
                continue
            if expected_identity is None:
                expected_identity = normalized_identity
            elif normalized_identity != expected_identity:
                raise ValueError(
                    f"Monitor {key} mismatch: expected {expected_identity!r}, got {normalized_identity!r}"
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


def _first_execute_result(tool_results: list[ToolResult], execute_tool_name: str) -> ToolResult | None:
    for tool_result in tool_results:
        if not tool_result.ok:
            continue
        if _is_configured_execute_result(tool_result, execute_tool_name):
            return tool_result
    return None


def _first_failed_execute_result(tool_results: list[ToolResult], execute_tool_name: str) -> ToolResult | None:
    for tool_result in tool_results:
        if tool_result.ok:
            continue
        if _is_configured_execute_result(tool_result, execute_tool_name):
            return tool_result
    return None


def _first_monitor_result(
    tool_results: list[ToolResult],
    monitor_tool_name: str,
    execute_tool_name: str,
) -> ToolResult | None:
    for tool_result in tool_results:
        if not tool_result.ok:
            continue
        if _is_execute_result_value(tool_result, execute_tool_name):
            if _is_valid_monitor_status(
                tool_result.data.get("monitor_status") or tool_result.data.get("status")
            ):
                return tool_result
            continue
        if tool_result.tool_name == monitor_tool_name:
            return tool_result
        if _agentic_role(tool_result) == "monitor":
            return tool_result
        if _is_valid_monitor_status(tool_result.data.get("status")):
            return tool_result
    return None


def _is_execute_result_value(tool_result: ToolResult, execute_tool_name: str) -> bool:
    return _is_configured_execute_result(tool_result, execute_tool_name)


def _is_configured_execute_result(tool_result: ToolResult, execute_tool_name: str) -> bool:
    namespace, name = split_qualified_tool_name(execute_tool_name)
    if tool_result.tool_name != name:
        return False
    return namespace is None or tool_result.namespace == namespace


def _execution_id(tool_result: ToolResult | None) -> str | None:
    if tool_result is None:
        return None
    return _optional_str(
        tool_result.data.get("execution_id")
        or tool_result.data.get("id")
        or tool_result.data.get("task_id")
        or tool_result.call_id
    )


def _monitor_id(tool_result: ToolResult | None) -> str | None:
    if tool_result is None:
        return None
    monitor = tool_result.data.get("monitor")
    nested_monitor_id = monitor.get("monitor_id") if isinstance(monitor, dict) else None
    return _optional_str(
        tool_result.data.get("monitor_id")
        or nested_monitor_id
    )


def _monitor_status_from_result(tool_result: ToolResult | None) -> MonitorStatus | None:
    if tool_result is None or not tool_result.ok:
        return None
    value = tool_result.data.get("monitor_status") or tool_result.data.get("status")
    if value is None:
        return None
    try:
        return normalize_monitor_status(str(value))
    except ValueError:
        return None


def _blocked_action_tool_call_while_active(
    *,
    tool_calls: list[ToolCall],
    active_execution: ActiveExecution | None,
    monitor_tool_name: str,
    execute_tool_name: str,
    fetch_env_tool_name: str,
) -> _PlannerBlock | None:
    if not _active_execution_running(active_execution):
        return None
    for tool_call in tool_calls:
        if _is_action_tool_call(
            tool_call,
            monitor_tool_name=monitor_tool_name,
            execute_tool_name=execute_tool_name,
            fetch_env_tool_name=fetch_env_tool_name,
        ):
            return _PlannerBlock(
                (
                    "planner attempted an action tool while active_execution is running "
                    f"for {active_execution.subtask!r}; wait, observe, or cancel before executing again"
                ),
                event_type="planner_blocked_execute",
            )
    return None


def _blocked_non_execute_action_tool_call(
    tool_calls: list[ToolCall],
    *,
    monitor_tool_name: str,
    execute_tool_name: str,
    fetch_env_tool_name: str,
) -> _PlannerBlock | None:
    for tool_call in tool_calls:
        if _is_execute_call(tool_call, execute_tool_name):
            continue
        if _is_action_tool_call(
            tool_call,
            monitor_tool_name=monitor_tool_name,
            execute_tool_name=execute_tool_name,
            fetch_env_tool_name=fetch_env_tool_name,
        ):
            return _PlannerBlock(
                (
                    f"planner attempted action-like tool {tool_call.name!r}; "
                    f"use decision=\"execute\" so the controller can call {execute_tool_name!r}"
                ),
                event_type="planner_blocked_execute",
            )
    return None


def _blocked_monitor_without_active_execution(
    tool_calls: list[ToolCall],
    *,
    active_execution: ActiveExecution | None,
    monitor_tool_name: str,
) -> _PlannerBlock | None:
    if active_execution is not None and _active_execution_running(active_execution):
        return None
    for tool_call in tool_calls:
        if _is_monitor_call(tool_call, monitor_tool_name):
            return _PlannerBlock(
                (
                    "planner requested monitor but no active execution is running; "
                    "use decision=\"execute\" to start the selected subtask"
                ),
                event_type="planner_invalid_monitor",
            )
    return None


def _blocked_planner_execute_tool_call(
    tool_calls: list[ToolCall],
    *,
    execute_tool_name: str,
) -> _PlannerBlock | None:
    if not any(_is_execute_call(tool_call, execute_tool_name) for tool_call in tool_calls):
        return None
    return _PlannerBlock(
        (
            "planner wrote an execute tool_call directly; use decision=\"execute\" "
            "and subtask_index so the controller starts exactly one selected action"
        ),
        event_type="planner_blocked_execute",
    )


def _blocked_execute_with_control_tool(
    planner_output: AgenticPlannerOutput,
) -> _PlannerBlock | None:
    if planner_output.decision != "execute":
        return None
    for tool_call in planner_output.tool_calls:
        if tool_call.name in CONTROL_TOOL_NAMES:
            return _PlannerBlock(
                (
                    "planner cannot combine decision=\"execute\" with a control tool; "
                    "choose either start the selected action or stop/cancel the current task"
                ),
                event_type="planner_inconsistent",
            )
    return None


def _blocked_execute_decision_while_active(
    *,
    state: AgenticSessionState,
    planner_output: AgenticPlannerOutput,
) -> _PlannerBlock | None:
    if planner_output.decision != "execute":
        return None
    if not _active_execution_running(state.active_execution):
        return None
    return _PlannerBlock(
        (
            "planner selected decision=\"execute\" while active_execution is running "
            f"for {state.active_execution.subtask!r}; wait, observe, monitor, or cancel before executing again"
        ),
        event_type="planner_blocked_execute",
    )


def _prepare_planner_tool_calls(
    *,
    state: AgenticSessionState,
    planner_output: AgenticPlannerOutput,
    available_tools: list[JsonDict],
    current_subtask: str | None,
    subtask_index: int | None,
    execute_tool_name: str,
    monitor_tool_name: str,
) -> _PlannerBlock | None:
    block = (
        _blocked_planner_execute_tool_call(
            planner_output.tool_calls,
            execute_tool_name=execute_tool_name,
        )
        or _blocked_unavailable_tool_call(
            planner_output.tool_calls,
            available_tools=available_tools,
        )
        or _blocked_execute_with_control_tool(planner_output)
        or _blocked_execute_decision_while_active(
            state=state,
            planner_output=planner_output,
        )
        or _planner_structure_block(
            state=state,
            planner_output=planner_output,
            current_subtask=current_subtask,
        )
    )
    if block is not None:
        return block

    block = _ensure_execute_tool_call_if_requested(
        planner_output=planner_output,
        available_tools=available_tools,
        current_subtask=current_subtask,
        subtask_index=subtask_index,
        execute_tool_name=execute_tool_name,
    )
    if block is not None:
        return block

    planner_output.tool_calls = _normalize_monitor_tool_calls(
        planner_output.tool_calls,
        current_subtask=current_subtask,
        subtask_index=subtask_index,
        monitor_tool_name=monitor_tool_name,
    )
    planner_output.tool_calls = _drop_premature_monitor_calls_when_executing(
        planner_output.tool_calls,
        active_execution=state.active_execution,
        execute_tool_name=execute_tool_name,
        monitor_tool_name=monitor_tool_name,
    )
    return None


def _blocked_unavailable_tool_call(
    tool_calls: list[ToolCall],
    *,
    available_tools: list[JsonDict],
) -> _PlannerBlock | None:
    if not available_tools:
        return None
    available = _available_tool_names(available_tools)
    for tool_call in tool_calls:
        if _tool_call_is_available(tool_call, available):
            continue
        requested = _display_tool_call_name(tool_call)
        allowed = ", ".join(sorted(available.canonical))
        return _PlannerBlock(
            (
                f"planner requested unavailable tool {requested!r}; "
                f"use only Available tools: {allowed}"
            ),
            event_type="planner_invalid_tool",
        )
    return None


def _drop_premature_monitor_calls_when_executing(
    tool_calls: list[ToolCall],
    *,
    active_execution: ActiveExecution | None,
    execute_tool_name: str,
    monitor_tool_name: str,
) -> list[ToolCall]:
    if _active_execution_running(active_execution):
        return tool_calls
    if not any(_is_execute_call(tool_call, execute_tool_name) for tool_call in tool_calls):
        return tool_calls
    return [
        tool_call
        for tool_call in tool_calls
        if not _is_monitor_call(tool_call, monitor_tool_name)
    ]


def _ensure_execute_tool_call_if_requested(
    *,
    planner_output: AgenticPlannerOutput,
    available_tools: list[JsonDict],
    current_subtask: str | None,
    subtask_index: int | None,
    execute_tool_name: str,
) -> _PlannerBlock | None:
    if planner_output.decision != "execute":
        return None
    if any(_is_execute_call(tool_call, execute_tool_name) for tool_call in planner_output.tool_calls):
        return None
    if not current_subtask:
        return _PlannerBlock(
            "planner selected decision=execute but no current subtask could be resolved",
            event_type="planner_inconsistent",
        )
    tool_call = _execute_tool_call_from_available_tools(
        available_tools,
        execute_tool_name=execute_tool_name,
        arguments=_execute_arguments(current_subtask, subtask_index),
    )
    if tool_call is None:
        return _PlannerBlock(
            f"planner selected decision=execute but no available {execute_tool_name!r} tool was found",
            event_type="planner_inconsistent",
        )
    planner_output.tool_calls.append(tool_call)
    return None


def _execute_tool_call_from_available_tools(
    available_tools: list[JsonDict],
    *,
    execute_tool_name: str,
    arguments: JsonDict,
) -> ToolCall | None:
    configured_namespace, configured_name = split_qualified_tool_name(execute_tool_name)
    matches: list[ToolCall] = []
    for tool in available_tools:
        name = _optional_str(tool.get("name"))
        namespace = _optional_str(tool.get("namespace"))
        canonical_name = _optional_str(tool.get("canonical_name"))
        canonical_namespace, canonical_tool_name = split_qualified_tool_name(canonical_name or "")
        if configured_namespace is not None:
            if canonical_name == execute_tool_name:
                return ToolCall(name=canonical_tool_name, arguments=arguments, namespace=canonical_namespace)
            if namespace == configured_namespace and name == configured_name:
                return ToolCall(name=name, arguments=arguments, namespace=namespace)
            continue
        if name == configured_name or canonical_tool_name == configured_name:
            matches.append(
                ToolCall(
                    name=name or canonical_tool_name,
                    arguments=arguments,
                    namespace=namespace or canonical_namespace,
                )
            )
    if len(matches) == 1:
        return matches[0]
    if not available_tools:
        return ToolCall(name=configured_name, arguments=arguments, namespace=configured_namespace)
    return None


def _available_tool_names(available_tools: list[JsonDict]) -> _AvailableToolNames:
    canonical: set[str] = set()
    short_counts: dict[str, int] = {}
    for tool in available_tools:
        name = _optional_str(tool.get("name"))
        namespace = _optional_str(tool.get("namespace"))
        canonical_name = _optional_str(tool.get("canonical_name"))
        if name:
            short_counts[name] = short_counts.get(name, 0) + 1
            canonical.add(name if namespace is None else f"{namespace}___{name}")
        if canonical_name:
            canonical.add(canonical_name)
            _, canonical_short_name = split_qualified_tool_name(canonical_name)
            if canonical_short_name and canonical_short_name != name:
                short_counts[canonical_short_name] = short_counts.get(canonical_short_name, 0) + 1
    return _AvailableToolNames(canonical=canonical, short_counts=short_counts)


def _tool_call_is_available(tool_call: ToolCall, available: _AvailableToolNames) -> bool:
    if _tool_call_key(tool_call) in available.canonical:
        return True
    return tool_call.namespace is None and available.short_counts.get(tool_call.name) == 1


def _tool_call_key(tool_call: ToolCall) -> str:
    if tool_call.namespace:
        return f"{tool_call.namespace}___{tool_call.name}"
    return tool_call.name


def _display_tool_call_name(tool_call: ToolCall) -> str:
    return _tool_call_key(tool_call)


def _is_execute_call(tool_call: ToolCall, execute_tool_name: str) -> bool:
    namespace, name = split_qualified_tool_name(execute_tool_name)
    if tool_call.name == name and (namespace is None or tool_call.namespace == namespace):
        return True
    return False


def _is_monitor_call(tool_call: ToolCall, monitor_tool_name: str) -> bool:
    namespace, name = split_qualified_tool_name(monitor_tool_name)
    return tool_call.name == name and (namespace is None or tool_call.namespace == namespace)


def _is_action_tool_call(
    tool_call: ToolCall,
    *,
    monitor_tool_name: str,
    execute_tool_name: str,
    fetch_env_tool_name: str,
) -> bool:
    if _is_execute_call(tool_call, execute_tool_name):
        return True
    role = _optional_str(tool_call.arguments.get("agentic_role") or tool_call.arguments.get("_agentic_role"))
    if role is not None and role.lower() in {"execute", "action"}:
        return True
    if tool_call.name in {monitor_tool_name, fetch_env_tool_name, *CONTROL_TOOL_NAMES}:
        return False
    return _looks_like_action_tool_name(tool_call.name)


def _looks_like_action_tool_name(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    _, unqualified = split_qualified_tool_name(normalized)
    passive_prefixes = (
        "estimate_",
        "detect_",
        "classify_",
        "observe_",
        "fetch_",
        "get_",
        "check_",
        "monitor_",
        "plan_",
        "compute_",
    )
    if unqualified.startswith(passive_prefixes):
        return False
    action_tokens = (
        "execute",
        "run_subtask",
        "run_action",
        "action",
        "move",
        "pick",
        "place",
        "grasp",
        "push",
        "press",
        "open",
        "close",
    )
    return any(token in unqualified for token in action_tokens)


def _normalize_monitor_tool_calls(
    tool_calls: list[ToolCall],
    *,
    current_subtask: str | None,
    subtask_index: int | None,
    monitor_tool_name: str,
) -> list[ToolCall]:
    normalized: list[ToolCall] = []
    for tool_call in tool_calls:
        if not _is_monitor_call(tool_call, monitor_tool_name):
            normalized.append(tool_call)
            continue
        arguments: JsonDict = {}
        if current_subtask:
            arguments["subtask"] = current_subtask
        if subtask_index is not None:
            arguments["subtask_index"] = subtask_index
        normalized.append(
            ToolCall(
                name=tool_call.name,
                arguments=arguments,
                namespace=tool_call.namespace,
                call_id=tool_call.call_id,
            )
        )
    return normalized


def _execute_arguments(subtask: str, subtask_index: int | None) -> JsonDict:
    arguments: JsonDict = {"subtask": subtask}
    if subtask_index is not None:
        arguments["subtask_index"] = subtask_index
    return arguments


def _resolve_step_plan(
    state: AgenticSessionState,
    planner_output: AgenticPlannerOutput,
) -> _StepPlan:
    subtasks = list(planner_output.subtasks) if planner_output.subtasks else list(state.subtasks)
    subtask_statuses = _reconcile_subtask_statuses(
        state.subtasks,
        state.subtask_statuses,
        subtasks,
    )
    subtask_index = (
        planner_output.subtask_index
        if planner_output.subtask_index is not None
        else state.subtask_index
    )
    current_subtask = planner_output.current_subtask
    if not current_subtask and subtask_index is not None and 0 <= subtask_index < len(subtasks):
        current_subtask = subtasks[subtask_index]
    if not current_subtask and subtask_index is None:
        subtask_index = _first_unfinished_subtask_index(subtask_statuses)
        if subtask_index is not None and subtask_index < len(subtasks):
            current_subtask = subtasks[subtask_index]
    return _StepPlan(
        subtasks=subtasks,
        subtask_statuses=subtask_statuses,
        current_subtask=current_subtask,
        subtask_index=subtask_index,
    )


def _validate_and_reconcile_plan_selection(
    subtasks: list[str],
    subtask_index: int | None,
    current_subtask: str | None,
) -> _PlanSelection:
    if not subtasks:
        return _PlanSelection(current_subtask=current_subtask, subtask_index=subtask_index)
    if subtask_index is not None:
        if subtask_index < 0 or subtask_index >= len(subtasks):
            return _PlanSelection(
                current_subtask=current_subtask,
                subtask_index=subtask_index,
                error=(
                    f"planner selected subtask_index {subtask_index}, but the plan has "
                    f"{len(subtasks)} item(s)"
                ),
            )
        expected = subtasks[subtask_index]
        if current_subtask and current_subtask != expected:
            matches = [index for index, subtask in enumerate(subtasks) if subtask == current_subtask]
            if len(matches) == 1:
                return _PlanSelection(current_subtask=current_subtask, subtask_index=matches[0])
            if not matches:
                return _PlanSelection(
                    current_subtask=current_subtask,
                    subtask_index=subtask_index,
                    error=(
                        "planner current_subtask is not in the subtask plan; "
                        "select an existing subtask by subtask_index or return a revised subtasks list"
                    ),
                )
            return _PlanSelection(
                current_subtask=current_subtask,
                subtask_index=subtask_index,
                error=(
                    "planner current_subtask does not match subtasks[subtask_index]: "
                    f"index {subtask_index} is {expected!r}, got {current_subtask!r}"
                ),
            )
        return _PlanSelection(current_subtask=expected, subtask_index=subtask_index)
    if current_subtask:
        matches = [index for index, subtask in enumerate(subtasks) if subtask == current_subtask]
        if len(matches) == 1:
            return _PlanSelection(current_subtask=current_subtask, subtask_index=matches[0])
        return _PlanSelection(
            current_subtask=current_subtask,
            subtask_index=subtask_index,
            error=(
                "planner current_subtask is not in the subtask plan; "
                "select an existing subtask by subtask_index or return a revised subtasks list"
            ),
        )
    return _PlanSelection(current_subtask=current_subtask, subtask_index=subtask_index)


def _planner_structure_block(
    *,
    state: AgenticSessionState,
    planner_output: AgenticPlannerOutput,
    current_subtask: str | None,
) -> _PlannerBlock | None:
    return (
        _blocked_replan_while_active(
            state=state,
            planner_output=planner_output,
        )
        or _blocked_replan_after_all_success(
            state=state,
            planner_output=planner_output,
        )
        or _blocked_remove_successful_subtasks(
            state=state,
            planner_output=planner_output,
        )
        or _blocked_active_current_subtask_mismatch(
            state=state,
            current_subtask=current_subtask,
        )
    )


def _planner_action_block(
    *,
    state: AgenticSessionState,
    planner_output: AgenticPlannerOutput,
    subtasks: list[str],
    subtask_statuses: list[SubtaskStatus],
    current_subtask: str | None,
    subtask_index: int | None,
    monitor_tool_name: str,
    execute_tool_name: str,
    fetch_env_tool_name: str,
) -> _PlannerBlock | None:
    return (
        _blocked_action_tool_call_while_active(
            tool_calls=planner_output.tool_calls,
            active_execution=state.active_execution,
            monitor_tool_name=monitor_tool_name,
            execute_tool_name=execute_tool_name,
            fetch_env_tool_name=fetch_env_tool_name,
        )
        or _blocked_monitor_without_active_execution(
            planner_output.tool_calls,
            active_execution=state.active_execution,
            monitor_tool_name=monitor_tool_name,
        )
        or _blocked_non_execute_action_tool_call(
            planner_output.tool_calls,
            monitor_tool_name=monitor_tool_name,
            execute_tool_name=execute_tool_name,
            fetch_env_tool_name=fetch_env_tool_name,
        )
        or _blocked_reexecute_after_success(
            state=state,
            planner_output=planner_output,
            current_subtask=current_subtask,
            execute_tool_name=execute_tool_name,
        )
        or _blocked_execute_completed_subtask(
            tool_calls=planner_output.tool_calls,
            subtasks=subtasks,
            subtask_statuses=subtask_statuses,
            current_subtask=current_subtask,
            subtask_index=subtask_index,
            execute_tool_name=execute_tool_name,
        )
    )


def _blocked_replan_while_active(
    *,
    state: AgenticSessionState,
    planner_output: AgenticPlannerOutput,
) -> _PlannerBlock | None:
    if not _active_execution_running(state.active_execution):
        return None
    if not planner_output.subtasks or planner_output.subtasks == state.subtasks:
        return None
    active = state.active_execution
    active_matches = [
        index for index, subtask in enumerate(planner_output.subtasks)
        if subtask == active.subtask
    ]
    if len(active_matches) == 1 and planner_output.subtask_index == active_matches[0]:
        return None
    return _PlannerBlock(
        (
            "planner revised subtasks while active_execution is running but did not keep "
            f"the selected subtask on the active execution {active.subtask!r}; keep "
            "subtask_index on the active execution until a terminal monitor event"
        ),
        event_type="planner_blocked_replan",
    )


def _blocked_remove_successful_subtasks(
    *,
    state: AgenticSessionState,
    planner_output: AgenticPlannerOutput,
) -> _PlannerBlock | None:
    if not planner_output.subtasks or planner_output.subtasks == state.subtasks:
        return None
    removed_successes = [
        subtask
        for index, subtask in enumerate(state.subtasks)
        if index < len(state.subtask_statuses)
        and state.subtask_statuses[index] is SubtaskStatus.SUCCESS
        and subtask not in planner_output.subtasks
    ]
    if not removed_successes:
        return None
    removed = ", ".join(repr(subtask) for subtask in removed_successes)
    return _PlannerBlock(
        (
            "planner revised subtasks but removed completed subtask(s): "
            f"{removed}; keep success items in the plan as completed history and only "
            "add/remove pending or failed work"
        ),
        event_type="planner_invalid_replan",
    )


def _blocked_replan_after_all_success(
    *,
    state: AgenticSessionState,
    planner_output: AgenticPlannerOutput,
) -> _PlannerBlock | None:
    if not _all_subtasks_success(state):
        return None
    if not planner_output.subtasks or planner_output.subtasks == state.subtasks:
        return None
    return _PlannerBlock(
        (
            "planner revised subtasks after all existing subtasks were marked success; "
            "set task_complete=true unless the user starts a new task"
        ),
        event_type="planner_invalid_replan",
    )


def _blocked_active_current_subtask_mismatch(
    *,
    state: AgenticSessionState,
    current_subtask: str | None,
) -> _PlannerBlock | None:
    active_execution = state.active_execution
    if not _active_execution_running(active_execution) or not current_subtask:
        return None
    if current_subtask == active_execution.subtask:
        return None
    return _PlannerBlock(
        (
            "planner selected a different current_subtask while active_execution is running: "
            f"active={active_execution.subtask!r}, selected={current_subtask!r}; keep the "
            "current_subtask on the active execution until monitor terminal status"
        ),
        event_type="planner_inconsistent",
    )


def _blocked_reexecute_after_success(
    *,
    state: AgenticSessionState,
    planner_output: AgenticPlannerOutput,
    current_subtask: str | None,
    execute_tool_name: str,
) -> _PlannerBlock | None:
    if state.monitor_status is not MonitorStatus.SUCCESS or not current_subtask:
        return None
    if state.active_execution is None or state.active_execution.subtask != current_subtask:
        return None
    if any(_is_execute_call(tool_call, execute_tool_name) for tool_call in planner_output.tool_calls):
        return _PlannerBlock(
            (
                "planner attempted to execute the same subtask that just reached monitor_success; "
                "advance subtask_index, revise the plan, or complete the task instead"
            ),
            event_type="planner_repeated_success",
        )
    return None


def _blocked_execute_completed_subtask(
    *,
    tool_calls: list[ToolCall],
    subtasks: list[str],
    subtask_statuses: list[SubtaskStatus],
    current_subtask: str | None,
    subtask_index: int | None,
    execute_tool_name: str,
) -> _PlannerBlock | None:
    if not any(_is_execute_call(tool_call, execute_tool_name) for tool_call in tool_calls):
        return None
    status = _selected_subtask_status(
        subtasks,
        subtask_statuses,
        current_subtask,
        subtask_index,
    )
    if status is not SubtaskStatus.SUCCESS:
        return None
    return _PlannerBlock(
        (
            "planner attempted to execute a subtask already marked success; "
            "select the next pending subtask, revise the plan, or complete the task instead"
        ),
        event_type="planner_repeated_success",
    )


def _hydrate_monitor_tool_calls(
    tool_calls: list[ToolCall],
    state: AgenticSessionState,
    monitor_tool_name: str,
) -> list[ToolCall]:
    if state.active_execution is None:
        return tool_calls
    hydrated: list[ToolCall] = []
    for tool_call in tool_calls:
        if not _is_monitor_call(tool_call, monitor_tool_name):
            hydrated.append(tool_call)
            continue
        arguments = dict(_monitor_arguments(state))
        if not arguments.get("execution_id") and state.active_execution.execution_id:
            arguments["execution_id"] = state.active_execution.execution_id
        if not arguments.get("monitor_id") and state.active_execution.monitor_id:
            arguments["monitor_id"] = state.active_execution.monitor_id
        hydrated.append(
            ToolCall(
                name=tool_call.name,
                arguments=arguments,
                namespace=tool_call.namespace or state.monitor_namespace,
                call_id=tool_call.call_id,
            )
        )
    return hydrated


def _apply_control_results(
    state: AgenticSessionState,
    tool_results: list[ToolResult],
    *,
    events: list[AgenticEvent] | None = None,
) -> _ControlUpdate | None:
    control_result = next(
        (
            tool_result
            for tool_result in tool_results
            if tool_result.ok and tool_result.tool_name in CONTROL_TOOL_NAMES
        ),
        None,
    )
    if control_result is None:
        return None
    monitor_error = f"{control_result.tool_name} requested"
    if _active_execution_running(state.active_execution):
        affected_subtask = state.active_execution.subtask
        affected_subtask_index = state.active_execution.subtask_index
        state.monitor_status = MonitorStatus.FAILED
        state.monitor_error = monitor_error
        _finish_active_execution(state, MonitorStatus.FAILED, state.monitor_error)
        event_type = "monitor_failed"
        event_data: JsonDict = {
            "status": MonitorStatus.FAILED.value,
            "tool_name": control_result.tool_name,
            "error": state.monitor_error,
            "subtask": affected_subtask,
        }
        if affected_subtask_index is not None:
            event_data["subtask_index"] = affected_subtask_index
        update = _ControlUpdate(
            monitor_status=MonitorStatus.FAILED,
            monitor_error=state.monitor_error,
            affected_subtask=affected_subtask,
            affected_subtask_index=affected_subtask_index,
        )
    else:
        state.awaiting_monitor = False
        event_type = "control_requested"
        event_data = {
            "tool_name": control_result.tool_name,
            "message": monitor_error,
        }
        update = _ControlUpdate(monitor_status=None, monitor_error=None)
    _queue_event_if_reasonable(
        state,
        _event(
            event_type,
            event_data,
            source=control_result.namespace or control_result.tool_name,
        ),
        events=events,
    )
    return update


def _apply_monitor_result(
    *,
    state: AgenticSessionState,
    tool_result: ToolResult,
    tool_call: ToolCall | None,
    current_subtask: str | None,
    subtask_index: int | None,
    subtasks: list[str],
    statuses: list[SubtaskStatus],
    events: list[AgenticEvent] | None = None,
) -> _MonitorUpdate:
    try:
        _validate_monitor_identity(
            tool_call=tool_call,
            result=tool_result,
            current_subtask=current_subtask,
            subtask_index=subtask_index,
        )
        monitor_status = normalize_monitor_status(str(tool_result.data.get("status") or ""))
        monitor_error = _optional_str(tool_result.data.get("error"))
        _queue_event_if_reasonable(
            state,
            _monitor_event_from_status(
                monitor_status,
                tool_result=tool_result,
                subtask=current_subtask,
                subtask_index=subtask_index,
            ),
            events=events,
        )
        _mark_subtask_status(
            state,
            current_subtask,
            subtask_index,
            monitor_status,
            subtasks=subtasks,
            statuses=statuses,
        )
        return _MonitorUpdate(
            monitor_status=monitor_status,
            monitor_error=monitor_error,
        )
    except (TypeError, ValueError) as exc:
        error = str(exc)
        _queue_event_if_reasonable(
            state,
            _event("monitor_failed", {"error": error}, source="monitor"),
            events=events,
        )
        return _MonitorUpdate(
            monitor_status=MonitorStatus.FAILED,
            monitor_error=error,
            error=error,
        )


def _finalize_requested_execute_state(
    state: AgenticSessionState,
    *,
    saw_monitor_feedback: bool,
) -> None:
    if state.monitor_status in {MonitorStatus.SUCCESS, MonitorStatus.FAILED}:
        _finish_active_execution(state, state.monitor_status, state.monitor_error)
        return
    if saw_monitor_feedback:
        return
    if _active_execution_running(state.active_execution):
        state.monitor_status = MonitorStatus.RUNNING
        state.monitor_error = None


def _reconcile_subtask_statuses(
    previous_subtasks: list[str],
    previous_statuses: list[SubtaskStatus],
    subtasks: list[str],
) -> list[SubtaskStatus]:
    if not subtasks:
        return []
    reconciled: list[SubtaskStatus] = []
    used_previous_indexes: set[int] = set()
    for index, subtask in enumerate(subtasks):
        status = SubtaskStatus.PENDING
        if index < len(previous_subtasks) and previous_subtasks[index] == subtask:
            if index < len(previous_statuses):
                status = previous_statuses[index]
            used_previous_indexes.add(index)
        else:
            for previous_index, previous_subtask in enumerate(previous_subtasks):
                if previous_index in used_previous_indexes or previous_subtask != subtask:
                    continue
                if previous_index < len(previous_statuses):
                    status = previous_statuses[previous_index]
                used_previous_indexes.add(previous_index)
                break
        reconciled.append(status)
    return reconciled


def _mark_subtask_status(
    state: AgenticSessionState,
    subtask: str | None,
    subtask_index: int | None,
    monitor_status: MonitorStatus,
    *,
    subtasks: list[str] | None = None,
    statuses: list[SubtaskStatus] | None = None,
) -> None:
    target_subtasks = subtasks if subtasks is not None else state.subtasks
    target_statuses = statuses if statuses is not None else state.subtask_statuses
    if not target_subtasks:
        return
    while len(target_statuses) < len(target_subtasks):
        target_statuses.append(SubtaskStatus.PENDING)
    if len(target_statuses) > len(target_subtasks):
        del target_statuses[len(target_subtasks) :]
    index = subtask_index if subtask_index is not None else None
    if index is None or index < 0 or index >= len(target_subtasks):
        if subtask is None:
            return
        matches = [candidate for candidate, value in enumerate(target_subtasks) if value == subtask]
        if len(matches) != 1:
            return
        index = matches[0]
    elif subtask is not None and target_subtasks[index] != subtask:
        matches = [candidate for candidate, value in enumerate(target_subtasks) if value == subtask]
        if len(matches) != 1:
            return
        index = matches[0]
    target_statuses[index] = SubtaskStatus(monitor_status.value)


def _selected_subtask_status(
    subtasks: list[str],
    subtask_statuses: list[SubtaskStatus],
    current_subtask: str | None,
    subtask_index: int | None,
) -> SubtaskStatus | None:
    index = _selected_subtask_index(subtasks, current_subtask, subtask_index)
    if index is None:
        return None
    return subtask_statuses[index] if index < len(subtask_statuses) else SubtaskStatus.PENDING


def _passive_plan_needs_followup_reason(
    state: AgenticSessionState,
    planner_output: AgenticPlannerOutput,
) -> bool:
    if planner_output.task_complete or planner_output.tool_calls:
        return False
    if _active_execution_running(state.active_execution):
        return False
    if not state.current_subtask:
        return False
    if planner_output.decision in {"observe", "wait", "cancel", "ask_user", "noop"}:
        return False
    status = _selected_subtask_status(
        state.subtasks,
        state.subtask_statuses,
        state.current_subtask,
        state.subtask_index,
    )
    return status is not SubtaskStatus.SUCCESS


def _advance_current_after_completed_success(state: AgenticSessionState) -> None:
    if _active_execution_running(state.active_execution):
        return
    index = _selected_subtask_index(state.subtasks, state.current_subtask, state.subtask_index)
    if index is None or index >= len(state.subtask_statuses):
        return
    if state.subtask_statuses[index] is not SubtaskStatus.SUCCESS:
        return

    next_index = _next_pending_subtask_index(state.subtask_statuses, start_after=index)
    if next_index is None:
        state.current_subtask = None
        state.subtask_index = None
    else:
        state.current_subtask = state.subtasks[next_index]
        state.subtask_index = next_index
    if state.monitor_status is MonitorStatus.SUCCESS:
        state.monitor_status = None
        state.monitor_error = None


def _selected_subtask_index(
    subtasks: list[str],
    current_subtask: str | None,
    subtask_index: int | None,
) -> int | None:
    index = subtask_index
    if index is not None and 0 <= index < len(subtasks):
        if current_subtask is None or subtasks[index] == current_subtask:
            return index
    if current_subtask is None:
        return None
    matches = [candidate for candidate, subtask in enumerate(subtasks) if subtask == current_subtask]
    if len(matches) != 1:
        return None
    return matches[0]


def _next_pending_subtask_index(
    subtask_statuses: list[SubtaskStatus],
    *,
    start_after: int,
) -> int | None:
    for index in range(start_after + 1, len(subtask_statuses)):
        if subtask_statuses[index] is SubtaskStatus.PENDING:
            return index
    for index, status in enumerate(subtask_statuses):
        if status is SubtaskStatus.PENDING:
            return index
    return None


def _first_unfinished_subtask_index(subtask_statuses: list[SubtaskStatus]) -> int | None:
    for index, status in enumerate(subtask_statuses):
        if status is SubtaskStatus.PENDING:
            return index
    for index, status in enumerate(subtask_statuses):
        if status is not SubtaskStatus.SUCCESS:
            return index
    return None


def _all_subtasks_success(state: AgenticSessionState) -> bool:
    if not state.subtasks or len(state.subtask_statuses) < len(state.subtasks):
        return False
    return all(
        status is SubtaskStatus.SUCCESS
        for status in state.subtask_statuses[: len(state.subtasks)]
    )


def _has_partial_subtask_progress_with_remaining_work(state: AgenticSessionState) -> bool:
    if not state.subtasks:
        return False
    statuses = list(state.subtask_statuses[: len(state.subtasks)])
    if not any(status is SubtaskStatus.SUCCESS for status in statuses):
        return False
    return len(statuses) < len(state.subtasks) or any(
        status is not SubtaskStatus.SUCCESS for status in statuses
    )


def _active_execution_running(active_execution: ActiveExecution | None) -> bool:
    return active_execution is not None and active_execution.status == MonitorStatus.RUNNING.value


def _active_subtask(state: AgenticSessionState) -> str | None:
    return state.active_execution.subtask if state.active_execution is not None else state.current_subtask


def _active_subtask_index(state: AgenticSessionState) -> int | None:
    return state.active_execution.subtask_index if state.active_execution is not None else state.subtask_index


def _finish_active_execution(
    state: AgenticSessionState,
    monitor_status: MonitorStatus,
    monitor_error: str | None,
) -> None:
    if state.active_execution is None:
        return
    state.active_execution.status = monitor_status.value
    state.active_execution.error = monitor_error
    state.active_execution.updated_at = time.time()
    _mark_subtask_status(
        state,
        state.active_execution.subtask,
        state.active_execution.subtask_index,
        monitor_status,
    )
    state.awaiting_monitor = False


def _monitor_event_from_status(
    monitor_status: MonitorStatus,
    *,
    tool_result: ToolResult,
    subtask: str | None,
    subtask_index: int | None,
) -> AgenticEvent:
    if monitor_status is MonitorStatus.SUCCESS:
        event_type = "monitor_success"
    elif monitor_status is MonitorStatus.FAILED:
        event_type = "monitor_failed"
    else:
        event_type = "monitor_running"
    data: JsonDict = {
        "status": monitor_status.value,
        "tool_name": tool_result.tool_name,
    }
    if subtask is not None:
        data["subtask"] = subtask
    if subtask_index is not None:
        data["subtask_index"] = subtask_index
    for key in ("execution_id", "monitor_id", "id", "error"):
        value = tool_result.data.get(key)
        if value is not None:
            data[key] = value
    return _event(event_type, data, source="monitor", message=tool_result.error)


def _event(
    event_type: str,
    data: JsonDict | None = None,
    *,
    source: str | None = None,
    message: str | None = None,
) -> AgenticEvent:
    return AgenticEvent(
        event_type=event_type,
        data=data or {},
        source=source,
        message=message,
        created_at=time.time(),
    )


def _queue_event_if_reasonable(
    state: AgenticSessionState,
    event: AgenticEvent,
    *,
    events: list[AgenticEvent] | None = None,
) -> AgenticEvent:
    if events is not None:
        events.append(event)
    if _event_requests_reason(event):
        state.pending_events.append(event)
        state.reason_requested = True
    return event


def _event_requests_reason(event: AgenticEvent) -> bool:
    return event.event_type != "monitor_running"


def _dedupe_events(events: list[AgenticEvent]) -> list[AgenticEvent]:
    deduped: list[AgenticEvent] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...], str | None, str | None]] = set()
    for event in events:
        key = (
            event.event_type,
            tuple(sorted((str(k), str(v)) for k, v in event.data.items())),
            event.source,
            event.message,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)
    return deduped


def _monitor_arguments(state: AgenticSessionState) -> JsonDict:
    arguments: JsonDict = {}
    active_execution = state.active_execution
    subtask = active_execution.subtask if active_execution else state.current_subtask
    subtask_index = active_execution.subtask_index if active_execution else state.subtask_index
    execution_id = active_execution.execution_id if active_execution else None
    monitor_id = active_execution.monitor_id if active_execution else None
    if subtask is not None:
        arguments["subtask"] = subtask
    if subtask_index is not None:
        arguments["subtask_index"] = subtask_index
    if execution_id is not None:
        arguments["execution_id"] = execution_id
    if monitor_id is not None:
        arguments["monitor_id"] = monitor_id
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
    if isinstance(tool_result.data.get("scene_graph"), dict):
        return tool_result.data["scene_graph"]  # type: ignore[return-value]
    if isinstance(tool_result.data.get("environment"), dict):
        return tool_result.data["environment"]  # type: ignore[return-value]
    if isinstance(tool_result.data.get("env"), dict):
        return tool_result.data["env"]  # type: ignore[return-value]
    if tool_result.tool_name == fetch_env_tool_name or _agentic_role(tool_result) in {"environment", "env", "fetch_env"}:
        return tool_result.data
    return {}


def _planner_metadata(metadata: JsonDict, include_all: bool) -> JsonDict:
    if include_all:
        return dict(metadata)
    visible = metadata.get("planner_visible_metadata")
    if isinstance(visible, dict):
        return visible  # type: ignore[return-value]
    return {}


def _planner_input_after_generate(
    planner_input: AgenticPlannerInput,
    planner: VLMPlanner,
    state_environment: JsonDict,
) -> AgenticPlannerInput:
    actual_input = getattr(planner, "last_planner_input", None)
    if isinstance(actual_input, AgenticPlannerInput):
        planner_input = actual_input
    scene = getattr(planner, "last_visual_scene", None)
    key = getattr(planner, "environment_key", "visual_scene")
    if not isinstance(scene, dict) or not key:
        return planner_input
    state_environment[key] = copy.deepcopy(scene)
    return planner_input
