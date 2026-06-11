"""Lightweight interaction layer for online robot-agent sessions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, TextIO

from dualsystem_agentic.core.types import AgenticStepResult


class InteractionLayer(Protocol):
    """User-facing interaction contract for online runtime loops."""

    def read_task(self) -> str | None:
        """Return the next long-horizon task, or ``None`` to exit."""

    def show_startup(self) -> None:
        """Show initial runtime information."""

    def show_task_started(self, task: str, session_id: str) -> None:
        """Show that a task session has started."""

    def show_step(self, result: AgenticStepResult) -> None:
        """Show one step result."""

    def show_task_finished(self, summary: "OnlineTaskSummary") -> None:
        """Show task completion or stop summary."""

    def show_error(self, task: str, error: BaseException) -> None:
        """Show an unrecoverable task error."""

    def show_shutdown(self) -> None:
        """Show runtime shutdown."""


@dataclass(frozen=True)
class OnlineTaskSummary:
    """Human-readable summary for one online task session."""

    task: str
    session_id: str
    steps: int
    task_complete: bool
    stop_reason: str
    current_subtask: str | None = None
    subtask_index: int | None = None
    monitor_status: str | None = None
    monitor_error: str | None = None


class ConsoleInteractionLayer:
    """Minimal stdin/stdout interaction layer for online agent operation."""

    def __init__(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        show_raw_json: bool = False,
        prompt: str = "task> ",
    ) -> None:
        import sys

        self.input_stream = input_stream or sys.stdin
        self.output_stream = output_stream or sys.stdout
        self.show_raw_json = show_raw_json
        self.prompt = prompt
        self._displayed_subtasks: tuple[str, ...] = ()

    def read_task(self) -> str | None:
        while True:
            try:
                if self.input_stream.isatty():
                    self.output_stream.write(self.prompt)
                    self.output_stream.flush()
                line = self.input_stream.readline()
            except KeyboardInterrupt:
                self.output_stream.write("\nInterrupted. Type /quit to exit.\n")
                self.output_stream.flush()
                continue
            if line == "":
                return None
            task = line.strip()
            if not task:
                continue
            if task in {"/quit", "/exit"}:
                return None
            return task

    def show_startup(self) -> None:
        self._write("Online agent ready. Enter a long-horizon task, or /quit to exit.")

    def show_task_started(self, task: str, session_id: str) -> None:
        self._displayed_subtasks = ()
        self._write(f"[{session_id}] task started: {task}")

    def show_step(self, result: AgenticStepResult) -> None:
        plan_update = _format_plan_update(result, self._displayed_subtasks)
        if plan_update is not None:
            text, self._displayed_subtasks = plan_update
            self._write(text)
        if self.show_raw_json:
            self._write(json.dumps(result.to_dict(), ensure_ascii=False))
            return
        tools = ", ".join(tool.tool_name for tool in result.tool_results) or "-"
        status = result.monitor_status.value if result.monitor_status else "-"
        complete = " complete" if result.task_complete else ""
        subtask = result.current_subtask or "-"
        vlm = "called" if result.vlm_called else "skipped"
        self._write(
            f"step {result.step_index}: subtask={subtask!r} "
            f"tools=[{tools}] monitor={status} vlm={vlm}{complete}"
        )

    def show_task_finished(self, summary: OnlineTaskSummary) -> None:
        status = "complete" if summary.task_complete else "stopped"
        monitor = summary.monitor_status or "-"
        self._write(
            f"[{summary.session_id}] task {status}: "
            f"steps={summary.steps} reason={summary.stop_reason} monitor={monitor}"
        )

    def show_error(self, task: str, error: BaseException) -> None:
        self._write(f"task error for {task!r}: {error}")

    def show_shutdown(self) -> None:
        self._write("Online agent stopped.")

    def _write(self, text: str) -> None:
        self.output_stream.write(text + "\n")
        self.output_stream.flush()


class TuiInteractionLayer:
    """Dependency-free curses TUI for online agent operation.

    It falls back to ``ConsoleInteractionLayer`` when stdin/stdout are not TTYs,
    which keeps scripts and tests usable in non-interactive environments.
    """

    def __init__(
        self,
        *,
        show_raw_json: bool = False,
        prompt: str = "task> ",
        max_log_lines: int = 1000,
    ) -> None:
        self.show_raw_json = show_raw_json
        self.prompt = prompt
        self.max_log_lines = max_log_lines
        self._screen = None
        self._curses = None
        self._fallback: ConsoleInteractionLayer | None = None
        self._lines: list[str] = []
        self._displayed_subtasks: tuple[str, ...] = ()

    def read_task(self) -> str | None:
        if not self._ensure_started():
            return self._fallback.read_task() if self._fallback else None
        while True:
            task = self._read_line()
            if task is None:
                return None
            task = task.strip()
            if not task:
                continue
            if task in {"/quit", "/exit"}:
                return None
            return task

    def show_startup(self) -> None:
        if not self._ensure_started():
            if self._fallback:
                self._fallback.show_startup()
            return
        self._append("Online agent ready. Enter a long-horizon task, or /quit to exit.")

    def show_task_started(self, task: str, session_id: str) -> None:
        self._displayed_subtasks = ()
        self._show_or_fallback("show_task_started", task, session_id)

    def show_step(self, result: AgenticStepResult) -> None:
        if self._fallback:
            self._fallback.show_step(result)
            return
        if not self._ensure_started():
            return
        plan_update = _format_plan_update(result, self._displayed_subtasks)
        if plan_update is not None:
            text, self._displayed_subtasks = plan_update
            self._append(text)
        if self.show_raw_json:
            self._append(json.dumps(result.to_dict(), ensure_ascii=False))
            return
        tools = ", ".join(tool.tool_name for tool in result.tool_results) or "-"
        status = result.monitor_status.value if result.monitor_status else "-"
        complete = " complete" if result.task_complete else ""
        parse = f" parse_error={result.parse_error}" if result.parse_error else ""
        subtask = result.current_subtask or "-"
        vlm = "called" if result.vlm_called else "skipped"
        self._append(
            f"step {result.step_index}: subtask={subtask!r} "
            f"tools=[{tools}] monitor={status} vlm={vlm}{complete}{parse}"
        )

    def show_task_finished(self, summary: OnlineTaskSummary) -> None:
        self._show_or_fallback("show_task_finished", summary)

    def show_error(self, task: str, error: BaseException) -> None:
        self._show_or_fallback("show_error", task, error)

    def show_shutdown(self) -> None:
        if self._fallback:
            self._fallback.show_shutdown()
            return
        if self._screen is None or self._curses is None:
            return
        self._append("Online agent stopped.")
        self._close_curses()

    def _show_or_fallback(self, method_name: str, *args: object) -> None:
        if self._fallback:
            getattr(self._fallback, method_name)(*args)
            return
        if not self._ensure_started():
            return
        if method_name == "show_task_started":
            task, session_id = args
            self._append(f"[{session_id}] task started: {task}")
        elif method_name == "show_task_finished":
            (summary,) = args
            status = "complete" if summary.task_complete else "stopped"
            monitor = summary.monitor_status or "-"
            self._append(
                f"[{summary.session_id}] task {status}: "
                f"steps={summary.steps} reason={summary.stop_reason} monitor={monitor}"
            )
        elif method_name == "show_error":
            task, error = args
            self._append(f"task error for {task!r}: {error}")

    def _ensure_started(self) -> bool:
        if self._screen is not None:
            return True
        if self._fallback is not None:
            return False
        try:
            import curses
            import sys

            if not sys.stdin.isatty() or not sys.stdout.isatty():
                self._fallback = ConsoleInteractionLayer(
                    show_raw_json=self.show_raw_json,
                    prompt=self.prompt,
                )
                return False
            self._curses = curses
            self._screen = curses.initscr()
            curses.noecho()
            curses.cbreak()
            self._screen.keypad(True)
            try:
                curses.curs_set(1)
            except curses.error:
                pass
            self._redraw()
            return True
        except Exception:
            self._close_curses()
            self._fallback = ConsoleInteractionLayer(
                show_raw_json=self.show_raw_json,
                prompt=self.prompt,
            )
            return False

    def _append(self, text: str) -> None:
        self._lines.extend(text.splitlines() or [""])
        if len(self._lines) > self.max_log_lines:
            self._lines = self._lines[-self.max_log_lines :]
        self._redraw()

    def _read_line(self) -> str | None:
        if self._screen is None or self._curses is None:
            return None
        chars: list[str] = []
        while True:
            self._draw_prompt("".join(chars))
            key = self._screen.getch()
            if key in (3, 27):
                return None
            if key in (10, 13):
                return "".join(chars)
            if key in (self._curses.KEY_BACKSPACE, 127, 8):
                if chars:
                    chars.pop()
                continue
            if 32 <= key <= 126:
                chars.append(chr(key))

    def _redraw(self) -> None:
        if self._screen is None or self._curses is None:
            return
        height, width = self._screen.getmaxyx()
        self._screen.erase()
        self._safe_addstr(0, 0, "dualsystem-agentic online", width)
        self._safe_addstr(1, 0, "Enter /quit or /exit to stop.", width)
        body_height = max(height - 4, 0)
        visible = self._lines[-body_height:] if body_height else []
        for offset, line in enumerate(visible):
            self._safe_addstr(2 + offset, 0, line, width)
        self._draw_prompt("")
        self._screen.refresh()

    def _draw_prompt(self, value: str) -> None:
        if self._screen is None:
            return
        height, width = self._screen.getmaxyx()
        row = max(height - 1, 0)
        text = self.prompt + value
        if len(text) >= width:
            text = text[-(width - 1) :] if width > 1 else ""
        try:
            self._screen.move(row, 0)
            self._screen.clrtoeol()
            self._screen.addstr(row, 0, text)
            self._screen.refresh()
        except Exception:
            pass

    def _safe_addstr(self, row: int, col: int, text: str, width: int) -> None:
        if self._screen is None or row < 0 or width <= 0:
            return
        clipped = text[: max(width - col - 1, 0)]
        try:
            self._screen.addstr(row, col, clipped)
        except Exception:
            pass

    def _close_curses(self) -> None:
        if self._curses is None:
            self._screen = None
            return
        try:
            if self._screen is not None:
                self._screen.keypad(False)
            self._curses.nocbreak()
            self._curses.echo()
            self._curses.endwin()
        except Exception:
            pass
        self._screen = None
        self._curses = None


def _format_plan_update(
    result: AgenticStepResult,
    displayed_subtasks: tuple[str, ...],
) -> tuple[str, tuple[str, ...]] | None:
    subtasks = tuple(result.planner_output.subtasks)
    if not subtasks or subtasks == displayed_subtasks:
        return None
    label = "subtask_list initialized:" if not displayed_subtasks else "subtask_list updated:"
    current_index = result.subtask_index
    if current_index is None:
        current_index = result.planner_output.subtask_index
    lines = [label]
    for index, subtask in enumerate(subtasks):
        marker = " <- current" if index == current_index else ""
        lines.append(f"  {index}. {subtask}{marker}")
    return "\n".join(lines), subtasks
