"""Online runtime for repeated long-horizon robot-agent tasks."""

from __future__ import annotations

import time

from dualsystem_agentic.core.loop import AgenticRobotLoop
from dualsystem_agentic.core.types import AgenticSessionState, AgenticStepResult, JsonDict
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
        metadata: JsonDict | None = None,
    ) -> None:
        self.loop = loop
        self.interaction = interaction
        self.logger = logger or NullRunLogger()
        self.max_steps = max_steps
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
        self.logger.start_session(task, session_id)
        self.interaction.show_task_started(task, session_id)
        try:
            for _ in range(self.max_steps):
                result, state = self.loop.step(task, state, metadata=self.metadata)
                results.append(result)
                self.logger.log_step(session_id, result)
                self.interaction.show_step(result)
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

