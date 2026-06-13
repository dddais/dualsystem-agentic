"""Online runtime for repeated long-horizon robot-agent tasks."""

from __future__ import annotations

import time

from dualsystem_agentic.core.loop import AgenticRobotLoop
from dualsystem_agentic.core.types import (
    AgenticEvent,
    AgenticSessionState,
    AgenticStepResult,
    JsonDict,
    MonitorStatus,
)
from dualsystem_agentic.interaction import InteractionLayer, OnlineTaskSummary
from dualsystem_agentic.run_logger import NullRunLogger, RunLogger


class OnlineAgentRuntime:
    """Run one initialized ``AgenticRobotLoop`` across many user tasks."""

    def __init__(
        self,
        loop: AgenticRobotLoop,
        *,
        interaction: InteractionLayer,
        logger: RunLogger | None = None,
        max_steps: int = 20,
        reason_interval_s: float = 1.0,
        monitor_poll_interval_s: float = 1.0,
        max_monitor_polls: int = 300,
        metadata: JsonDict | None = None,
    ) -> None:
        self.loop = loop
        self.interaction = interaction
        self.logger = logger or NullRunLogger()
        self.max_steps = max_steps
        self.reason_interval_s = max(0.0, reason_interval_s)
        self.monitor_poll_interval_s = max(0.0, monitor_poll_interval_s)
        self.max_monitor_polls = max_monitor_polls
        self.metadata = metadata or {}
        self._session_count = 0

    def serve_forever(self) -> list[OnlineTaskSummary]:
        """Wait for tasks until the interaction layer asks to exit."""
        summaries: list[OnlineTaskSummary] = []
        self.logger.start_run()
        self.interaction.show_startup()
        try:
            while True:
                task = self.interaction.read_task()
                if task is None:
                    break
                summaries.append(self.run_task(task))
        finally:
            self.interaction.show_shutdown()
            self.logger.close()
        return summaries

    def run_task(self, task: str) -> OnlineTaskSummary:
        """Run one long-horizon task with fresh session state."""
        session_id = self._next_session_id()
        state = AgenticSessionState(task=task)
        results: list[AgenticStepResult] = []
        stop_reason = "max_steps"
        vlm_steps = 0
        monitor_polls = 0
        monitor_key: tuple[str | None, str | None, str | None, int | None] | None = None
        last_monitor_poll_at = 0.0
        last_reason_tick_at = 0.0
        self.logger.start_session(task, session_id)
        self.interaction.show_task_started(task, session_id)
        try:
            while (
                vlm_steps < self.max_steps
                or _monitor_active(state)
                or bool(state.pending_events)
                or state.reason_requested
            ):
                now = time.time()
                did_poll_monitor = False
                if not _monitor_active(state):
                    monitor_polls = 0
                    monitor_key = None
                if _monitor_active(state) and _due(now, last_monitor_poll_at, self.monitor_poll_interval_s):
                    next_monitor_key = _active_execution_key(state)
                    if next_monitor_key != monitor_key:
                        monitor_polls = 0
                        monitor_key = next_monitor_key
                    result, state = self.loop.poll_monitor(task, state, metadata=self.metadata)
                    did_poll_monitor = True
                    last_monitor_poll_at = now
                    monitor_polls += 1
                    if (
                        _monitor_active(state)
                        and self.max_monitor_polls > 0
                        and monitor_polls >= self.max_monitor_polls
                    ):
                        timeout_event = _monitor_timeout_event(state, monitor_polls)
                        state.pending_events.append(timeout_event)
                        state.awaiting_monitor = False
                        state.reason_requested = True
                        result.events.append(timeout_event)
                        result.monitor_status = state.monitor_status
                        result.monitor_error = state.monitor_error
                        result.reason_requested = True
                        result.active_execution = state.active_execution
                    results.append(result)
                    self.logger.log_step(session_id, result)
                    self.interaction.show_step(result)
                    if result.events and result.events[-1].event_type == "monitor_timeout":
                        if vlm_steps >= self.max_steps:
                            stop_reason = "max_monitor_polls"
                            break
                force_reason = bool(state.pending_events) or state.reason_requested
                if not force_reason and _due(now, last_reason_tick_at, self.reason_interval_s):
                    force_reason = True
                if force_reason and vlm_steps >= self.max_steps:
                    if _monitor_active(state) and not _has_terminal_reason_event(state.pending_events):
                        sleep_s = _next_sleep_s(
                            now,
                            last_monitor_poll_at,
                            self.monitor_poll_interval_s,
                            0.0,
                            0.0,
                        )
                        if sleep_s > 0 and not did_poll_monitor:
                            time.sleep(sleep_s)
                        continue
                    stop_reason = "max_steps"
                    break
                if not force_reason:
                    sleep_s = _next_sleep_s(
                        now,
                        last_monitor_poll_at if _monitor_active(state) else 0.0,
                        self.monitor_poll_interval_s if _monitor_active(state) else 0.0,
                        last_reason_tick_at,
                        self.reason_interval_s,
                    )
                    if sleep_s > 0 and not did_poll_monitor:
                        time.sleep(sleep_s)
                    continue
                result, state = self.loop.step(
                    task,
                    state,
                    metadata=self.metadata,
                    force_reason=force_reason,
                )
                last_reason_tick_at = now
                results.append(result)
                self.logger.log_step(session_id, result)
                self.interaction.show_step(result)
                if result.vlm_called:
                    vlm_steps += 1
                if result.task_complete:
                    stop_reason = "task_complete"
                    break
            summary = _summary_from(
                task=task,
                session_id=session_id,
                results=results,
                state=state,
                stop_reason=stop_reason,
            )
            self.logger.finish_session(
                session_id,
                stop_reason=summary.stop_reason,
                task_complete=summary.task_complete,
                steps=summary.steps,
            )
            self.interaction.show_task_finished(summary)
            return summary
        except KeyboardInterrupt:
            summary = _summary_from(
                task=task,
                session_id=session_id,
                results=results,
                state=state,
                stop_reason="interrupted",
            )
            self.logger.finish_session(
                session_id,
                stop_reason=summary.stop_reason,
                task_complete=summary.task_complete,
                steps=summary.steps,
            )
            self.interaction.show_task_finished(summary)
            return summary
        except Exception as exc:
            self.logger.log_error(session_id, task, exc)
            self.logger.finish_session(
                session_id,
                stop_reason="error",
                task_complete=False,
                steps=len(results),
            )
            self.interaction.show_error(task, exc)
            return _summary_from(
                task=task,
                session_id=session_id,
                results=results,
                state=state,
                stop_reason="error",
            )

    def _next_session_id(self) -> str:
        self._session_count += 1
        return f"session_{self._session_count:04d}_{int(time.time() * 1000)}"


def _due(now: float, last_at: float, interval_s: float) -> bool:
    if interval_s <= 0:
        return True
    return last_at <= 0 or now - last_at >= interval_s


def _monitor_active(state: AgenticSessionState) -> bool:
    if state.awaiting_monitor:
        return True
    active_execution = state.active_execution
    return active_execution is not None and active_execution.status == MonitorStatus.RUNNING.value


def _active_execution_key(
    state: AgenticSessionState,
) -> tuple[str | None, str | None, str | None, int | None] | None:
    active_execution = state.active_execution
    if active_execution is None:
        return None
    return (
        active_execution.subtask,
        active_execution.execution_id,
        active_execution.monitor_id,
        active_execution.subtask_index,
    )


def _next_sleep_s(
    now: float,
    last_monitor_poll_at: float,
    monitor_poll_interval_s: float,
    last_reason_tick_at: float,
    reason_interval_s: float,
) -> float:
    waits = []
    if monitor_poll_interval_s > 0 and last_monitor_poll_at > 0:
        waits.append(max(0.0, monitor_poll_interval_s - (now - last_monitor_poll_at)))
    if reason_interval_s > 0 and last_reason_tick_at > 0:
        waits.append(max(0.0, reason_interval_s - (now - last_reason_tick_at)))
    if not waits:
        return 0.0
    return min(waits)


def _monitor_timeout_event(state: AgenticSessionState, poll_count: int) -> AgenticEvent:
    active_execution = state.active_execution
    now = time.time()
    data = {
        "poll_count": poll_count,
        "subtask": active_execution.subtask if active_execution is not None else state.current_subtask,
        "subtask_index": (
            active_execution.subtask_index if active_execution is not None else state.subtask_index
        ),
    }
    if active_execution is not None:
        active_execution.status = MonitorStatus.FAILED.value
        active_execution.error = "monitor poll limit exceeded"
        active_execution.updated_at = now
        data["execution_id"] = active_execution.execution_id
        data["monitor_id"] = active_execution.monitor_id
    state.monitor_status = MonitorStatus.FAILED
    state.monitor_error = "monitor poll limit exceeded"
    return AgenticEvent(
        event_type="monitor_timeout",
        data=data,
        source="runtime",
        message="monitor poll limit exceeded",
        created_at=now,
    )


def _has_terminal_reason_event(events: list[AgenticEvent]) -> bool:
    terminal_events = {
        "monitor_success",
        "monitor_failed",
        "monitor_timeout",
        "execute_failed",
        "tool_failed",
    }
    return any(event.event_type in terminal_events for event in events)


def _summary_from(
    *,
    task: str,
    session_id: str,
    results: list[AgenticStepResult],
    state: AgenticSessionState,
    stop_reason: str,
) -> OnlineTaskSummary:
    last = results[-1] if results else None
    task_complete = bool(last and last.task_complete)
    monitor_status = None
    if state.monitor_status is not None:
        monitor_status = state.monitor_status.value
    return OnlineTaskSummary(
        task=task,
        session_id=session_id,
        steps=len(results),
        task_complete=task_complete,
        stop_reason=stop_reason,
        current_subtask=state.current_subtask,
        subtask_index=state.subtask_index,
        monitor_status=monitor_status,
        monitor_error=state.monitor_error,
    )
