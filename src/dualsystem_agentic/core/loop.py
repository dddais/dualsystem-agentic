"""Agentic robot loop orchestration."""

from __future__ import annotations

import copy
import logging
import time

from dualsystem_agentic.core.parser import parse_agentic_planner_output
from dualsystem_agentic.core.types import (
    ActiveExecution,
    AgenticEvent,
    AgenticPlannerInput,
    AgenticPlannerOutput,
    AgenticPhase,
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
            state.phase = AgenticPhase.READY if state.active_execution is None else AgenticPhase.RESPONSE
            result = AgenticStepResult(
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
                monitor_status=state.monitor_status,
                monitor_error=state.monitor_error,
                active_execution=copy.deepcopy(state.active_execution),
                events=events,
                reason_requested=False,
            )
            return result, state

        state.phase = AgenticPhase.REASON
        state.last_reason_at = now
        state.reason_requested = False
        state.pending_events = []
        raw_output = self.planner.generate(planner_input)
        _merge_planner_visual_scene(
            planner_input,
            self.planner,
            state.environment,
        )
        planner_output = parse_agentic_planner_output(raw_output)

        if planner_output.task_complete:
            parse_ok = planner_output.parse_ok
            parse_error = planner_output.parse_error
            if _active_execution_running(state.active_execution):
                parse_ok = False
                parse_error = "planner cannot mark task_complete while active_execution is running"
                state.phase = AgenticPhase.ERROR
                state.reason_requested = True
            else:
                state.phase = AgenticPhase.DONE
            if planner_output.current_subtask:
                state.current_subtask = planner_output.current_subtask
            if planner_output.subtask_index is not None:
                state.subtask_index = planner_output.subtask_index
            state.last_tool_results = []
            state.awaiting_monitor = _active_execution_running(state.active_execution)
            result = AgenticStepResult(
                task=state.task,
                step_index=state.step_index,
                planner_input=planner_input,
                planner_output=planner_output,
                phase=state.phase,
                current_subtask=state.current_subtask,
                subtask_index=state.subtask_index,
                monitor_status=state.monitor_status,
                monitor_error=state.monitor_error,
                active_execution=copy.deepcopy(state.active_execution),
                events=events,
                reason_requested=state.reason_requested,
                task_complete=parse_ok,
                parse_ok=parse_ok,
                parse_error=parse_error,
            )
            state.step_index += 1
            return result, state

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
        tool_results: list[ToolResult] = []
        produced_events: list[AgenticEvent] = []
        blocked_execute = _blocked_execute_call(
            planner_output.tool_calls,
            state.active_execution,
            self.execute_tool_name,
        )
        if blocked_execute is not None:
            parse_ok = False
            parse_error = blocked_execute
            state.phase = AgenticPhase.ERROR
            state.reason_requested = True
        elif parse_ok:
            state.phase = AgenticPhase.ACT if planner_output.tool_calls else AgenticPhase.RESPONSE
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

        saw_monitor_feedback = False
        tool_calls_by_id = _tool_calls_by_id(planner_output.tool_calls)
        for tool_result in tool_results:
            if self._is_monitor_result(tool_result) and not self._is_execute_result(tool_result):
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
                    _queue_event_if_reasonable(
                        state,
                        _monitor_event_from_status(
                            monitor_status,
                            tool_result=tool_result,
                            subtask=current_subtask or state.current_subtask,
                            subtask_index=effective_index,
                        ),
                        events=produced_events,
                    )
                except (TypeError, ValueError) as exc:
                    parse_ok = False
                    parse_error = str(exc)
                    state.phase = AgenticPhase.ERROR
                    monitor_error = str(exc)
                    _queue_event_if_reasonable(
                        state,
                        _event("monitor_failed", {"error": str(exc)}, source="monitor"),
                        events=produced_events,
                    )

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
        requested_mcp_execute = any(
            _is_execute_call(tool_call, self.execute_tool_name)
            for tool_call in planner_output.tool_calls
        ) or any(self._is_execute_result(tool_result) for tool_result in tool_results)
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
        if parse_ok and current_subtask and not planner_output.task_complete and not requested_mcp_execute:
            if _active_execution_running(state.active_execution):
                if planner_output.should_execute_explicit and planner_output.should_execute:
                    parse_ok = False
                    parse_error = (
                        "planner explicitly requested downstream executor while active_execution is running; "
                        "set should_execute=false, wait, observe, or cancel before executing again"
                    )
                    state.phase = AgenticPhase.ERROR
                    state.reason_requested = True
            elif not planner_output.should_execute:
                pass
            else:
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
                if executor_output.ok:
                    start_monitor_result = self._start_active_execution(
                        state=state,
                        subtask=current_subtask,
                        subtask_index=state.subtask_index,
                        namespace=None,
                        execution_id=_optional_str(executor_output.data.get("execution_id") or executor_output.data.get("id")),
                        started_at=now,
                        metadata=metadata or {},
                        monitor_result=_first_monitor_result(
                            tool_results,
                            self.monitor_tool_name,
                            self.execute_tool_name,
                        ),
                        emit_monitor_event=not saw_monitor_feedback,
                        events=produced_events,
                    )
                    if start_monitor_result is not None:
                        tool_results.append(start_monitor_result)
                else:
                    parse_ok = False
                    parse_error = executor_output.error or "executor failed"
                    state.phase = AgenticPhase.ERROR
                    _queue_event_if_reasonable(
                        state,
                        _event(
                            "execute_failed",
                            {"subtask": current_subtask, "error": parse_error},
                            source="executor",
                        ),
                        events=produced_events,
                    )
                    state.reason_requested = True

        if requested_mcp_execute and parse_ok and _active_execution_running(state.active_execution):
            parse_ok = False
            parse_error = (
                "execute result was produced while active_execution is running; "
                "planner must wait, observe, or cancel before executing again"
            )
            state.phase = AgenticPhase.ERROR
            state.reason_requested = True
        elif requested_mcp_execute and parse_ok and current_subtask and not planner_output.task_complete:
            state.monitor_namespace = _monitor_namespace(tool_results, state.monitor_namespace)
            execute_result = _first_execute_result(tool_results, self.execute_tool_name)
            start_monitor_result = self._start_active_execution(
                state=state,
                subtask=current_subtask,
                subtask_index=state.subtask_index,
                namespace=state.monitor_namespace,
                execution_id=_execution_id(execute_result),
                started_at=now,
                metadata=metadata or {},
                monitor_result=_first_monitor_result(
                    tool_results,
                    self.monitor_tool_name,
                    self.execute_tool_name,
                ),
                emit_monitor_event=not saw_monitor_feedback,
                events=produced_events,
            )
            if start_monitor_result is not None:
                tool_results.append(start_monitor_result)
            if saw_monitor_feedback and state.monitor_status in {MonitorStatus.SUCCESS, MonitorStatus.FAILED}:
                _finish_active_execution(state, state.monitor_status, state.monitor_error)
            elif state.monitor_status is None or state.monitor_status is not MonitorStatus.RUNNING:
                state.monitor_status = MonitorStatus.RUNNING
                state.monitor_error = None
        elif state.monitor_status in {MonitorStatus.SUCCESS, MonitorStatus.FAILED}:
            state.awaiting_monitor = False
            _finish_active_execution(state, state.monitor_status, state.monitor_error)

        if not parse_ok:
            state.phase = AgenticPhase.ERROR
        if parse_ok and state.phase not in {AgenticPhase.ERROR, AgenticPhase.DONE}:
            state.phase = AgenticPhase.RESPONSE
        if state.pending_events:
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
        if state.active_execution is None and not state.current_subtask:
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
                monitor_status=state.monitor_status,
                monitor_error=state.monitor_error,
                active_execution=copy.deepcopy(state.active_execution),
                events=list(state.pending_events),
                reason_requested=state.reason_requested,
            )
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

    def _start_active_execution(
        self,
        *,
        state: AgenticSessionState,
        subtask: str,
        subtask_index: int | None,
        namespace: str | None,
        execution_id: str | None,
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
                metadata=metadata,
                events=events,
            )
            monitor_result = started_monitor_result
        monitor_ok = bool(monitor_result and monitor_result.ok)
        monitor_id = _optional_str(monitor_result.data.get("monitor_id")) if monitor_ok else None
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
        metadata: JsonDict,
        events: list[AgenticEvent] | None = None,
    ) -> ToolResult | None:
        arguments: JsonDict = {"subtask": subtask}
        if subtask_index is not None:
            arguments["subtask_index"] = subtask_index
        if execution_id is not None:
            arguments["execution_id"] = execution_id
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


def _first_execute_result(tool_results: list[ToolResult], execute_tool_name: str) -> ToolResult | None:
    for tool_result in tool_results:
        if not tool_result.ok:
            continue
        if tool_result.tool_name == execute_tool_name:
            return tool_result
        if _agentic_role(tool_result) in {"execute", "action"}:
            return tool_result
        if tool_result.data.get("executed") is True:
            return tool_result
    return None


def _first_failed_execute_result(tool_results: list[ToolResult], execute_tool_name: str) -> ToolResult | None:
    for tool_result in tool_results:
        if tool_result.ok:
            continue
        if tool_result.tool_name == execute_tool_name:
            return tool_result
        if _agentic_role(tool_result) in {"execute", "action"}:
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
            continue
        if tool_result.tool_name == monitor_tool_name:
            return tool_result
        if _agentic_role(tool_result) == "monitor":
            return tool_result
        if _is_valid_monitor_status(tool_result.data.get("status")):
            return tool_result
    return None


def _is_execute_result_value(tool_result: ToolResult, execute_tool_name: str) -> bool:
    if tool_result.tool_name == execute_tool_name:
        return True
    if _agentic_role(tool_result) in {"execute", "action"}:
        return True
    return tool_result.data.get("executed") is True


def _execution_id(tool_result: ToolResult | None) -> str | None:
    if tool_result is None:
        return None
    return _optional_str(
        tool_result.data.get("execution_id")
        or tool_result.data.get("id")
        or tool_result.data.get("task_id")
        or tool_result.call_id
    )


def _monitor_status_from_result(tool_result: ToolResult | None) -> MonitorStatus | None:
    if tool_result is None or not tool_result.ok:
        return None
    value = tool_result.data.get("status")
    if value is None:
        return None
    try:
        return normalize_monitor_status(str(value))
    except ValueError:
        return None


def _blocked_execute_call(
    tool_calls: list[ToolCall],
    active_execution: ActiveExecution | None,
    execute_tool_name: str,
) -> str | None:
    if not _active_execution_running(active_execution):
        return None
    for tool_call in tool_calls:
        if _is_execute_call(tool_call, execute_tool_name):
            return (
                "planner attempted execute while active_execution is running "
                f"for {active_execution.subtask!r}; wait, observe, or cancel before executing again"
            )
    return None


def _is_execute_call(tool_call: ToolCall, execute_tool_name: str) -> bool:
    if tool_call.name == execute_tool_name:
        return True
    role = _optional_str(tool_call.arguments.get("agentic_role") or tool_call.arguments.get("_agentic_role"))
    return role is not None and role.lower() in {"execute", "action"}


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


def _merge_planner_visual_scene(
    planner_input: AgenticPlannerInput,
    planner: VLMPlanner,
    state_environment: JsonDict,
) -> None:
    scene = getattr(planner, "last_visual_scene", None)
    key = getattr(planner, "environment_key", "visual_scene")
    if not isinstance(scene, dict) or not key:
        return
    planner_input.environment[key] = copy.deepcopy(scene)
    state_environment[key] = copy.deepcopy(scene)
