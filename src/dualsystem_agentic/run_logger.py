"""Run logging for online agent sessions."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from dualsystem_agentic.core.prompts import build_agentic_prompt
from dualsystem_agentic.core.types import (
    AgenticPlannerInput,
    AgenticStepResult,
    ImageInput,
    JsonDict,
    ensure_jsonable,
)


class RunLogger(Protocol):
    """Logging contract used by ``OnlineAgentRuntime``."""

    def start_run(self) -> None:
        """Prepare run-level logging resources."""

    def start_session(self, task: str, session_id: str) -> None:
        """Record the start of one user task session."""

    def log_step(self, session_id: str, result: AgenticStepResult) -> None:
        """Record one agentic loop step."""

    def finish_session(
        self,
        session_id: str,
        *,
        stop_reason: str,
        task_complete: bool,
        steps: int,
    ) -> None:
        """Record the end of one user task session."""

    def log_error(self, session_id: str, task: str, error: BaseException) -> None:
        """Record an unrecoverable task error."""

    def close(self) -> None:
        """Flush and close logging resources."""


class NullRunLogger:
    """No-op run logger."""

    def start_run(self) -> None:
        pass

    def start_session(self, task: str, session_id: str) -> None:
        pass

    def log_step(self, session_id: str, result: AgenticStepResult) -> None:
        pass

    def finish_session(
        self,
        session_id: str,
        *,
        stop_reason: str,
        task_complete: bool,
        steps: int,
    ) -> None:
        pass

    def log_error(self, session_id: str, task: str, error: BaseException) -> None:
        pass

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class SavedImageRef:
    """Reference to an image saved outside JSONL events."""

    label: str
    path: str
    mime_type: str | None
    sha256: str
    size_bytes: int

    def to_dict(self) -> JsonDict:
        return ensure_jsonable(self)  # type: ignore[return-value]


class JsonlRunLogger:
    """Write online runtime events to JSONL plus a readable companion log."""

    def __init__(
        self,
        root_dir: str | Path = "runs",
        *,
        save_images: bool = True,
        run_id: str | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser()
        self.run_id = run_id or time.strftime("run_%Y%m%d_%H%M%S")
        self.run_dir = self.root_dir / self.run_id
        self.save_images = save_images
        self._file = None
        self._human_file = None
        self._prompt_file = None

    def start_run(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._file = (self.run_dir / "events.jsonl").open("a", encoding="utf-8")
        self._human_file = (self.run_dir / "events.log").open("a", encoding="utf-8")
        self._prompt_file = (self.run_dir / "prompt.log").open("a", encoding="utf-8")
        self._write({"event": "run_started", "run_id": self.run_id, "time": time.time()})

    def start_session(self, task: str, session_id: str) -> None:
        self._ensure_started()
        session_dir = self.run_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        self._write(
            {
                "event": "session_started",
                "run_id": self.run_id,
                "session_id": session_id,
                "task": task,
                "time": time.time(),
            }
        )

    def log_step(self, session_id: str, result: AgenticStepResult) -> None:
        self._ensure_started()
        planner_input = _planner_input_without_image_payloads(
            result.planner_input,
            self._save_images(session_id, result.step_index, result.planner_input.images),
        )
        planner_prompt = build_agentic_prompt(result.planner_input) if result.vlm_called else None
        self._write(
            {
                "event": "step",
                "run_id": self.run_id,
                "session_id": session_id,
                "time": time.time(),
                "step_index": result.step_index,
                "vlm_called": result.vlm_called,
                "planner_input": planner_input,
                "planner_prompt": planner_prompt,
                "vlm_raw_output": result.planner_output.raw_output,
                "planner_output": result.planner_output.to_dict(),
                "tool_results": [tool_result.to_dict() for tool_result in result.tool_results],
                "executor_output": result.executor_output.to_dict()
                if result.executor_output
                else None,
                "current_subtask": result.current_subtask,
                "subtask_index": result.subtask_index,
                "monitor_status": result.monitor_status.value if result.monitor_status else None,
                "monitor_error": result.monitor_error,
                "task_complete": result.task_complete,
                "parse_ok": result.parse_ok,
                "parse_error": result.parse_error,
            }
        )

    def finish_session(
        self,
        session_id: str,
        *,
        stop_reason: str,
        task_complete: bool,
        steps: int,
    ) -> None:
        self._ensure_started()
        self._write(
            {
                "event": "session_finished",
                "run_id": self.run_id,
                "session_id": session_id,
                "time": time.time(),
                "stop_reason": stop_reason,
                "task_complete": task_complete,
                "steps": steps,
            }
        )

    def log_error(self, session_id: str, task: str, error: BaseException) -> None:
        self._ensure_started()
        self._write(
            {
                "event": "session_error",
                "run_id": self.run_id,
                "session_id": session_id,
                "time": time.time(),
                "task": task,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )

    def close(self) -> None:
        if self._file is None:
            return
        self._write({"event": "run_finished", "run_id": self.run_id, "time": time.time()})
        self._file.close()
        self._file = None
        if self._human_file is not None:
            self._human_file.close()
            self._human_file = None
        if self._prompt_file is not None:
            self._prompt_file.close()
            self._prompt_file = None

    def _save_images(
        self,
        session_id: str,
        step_index: int,
        images: dict[str, ImageInput],
    ) -> dict[str, SavedImageRef]:
        if not self.save_images or not images:
            return {}
        step_dir = self.run_dir / session_id / f"step_{step_index:04d}" / "images"
        step_dir.mkdir(parents=True, exist_ok=True)
        saved: dict[str, SavedImageRef] = {}
        for label, image in images.items():
            payload = _image_bytes(image)
            if payload is None:
                continue
            data, mime_type = payload
            digest = hashlib.sha256(data).hexdigest()
            suffix = _suffix_for_mime(mime_type)
            path = step_dir / f"{_safe_name(label)}_{digest[:12]}{suffix}"
            if not path.exists():
                path.write_bytes(data)
            saved[label] = SavedImageRef(
                label=label,
                path=str(path.relative_to(self.run_dir)),
                mime_type=mime_type,
                sha256=digest,
                size_bytes=len(data),
            )
        return saved

    def _ensure_started(self) -> None:
        if self._file is None:
            self.start_run()

    def _write(self, event: dict) -> None:
        if self._file is None:
            return
        jsonable_event = cast(JsonDict, ensure_jsonable(event))
        self._file.write(json.dumps(jsonable_event, ensure_ascii=False) + "\n")
        self._file.flush()
        if self._human_file is not None:
            self._human_file.write(_format_human_event(jsonable_event))
            self._human_file.flush()
        if self._prompt_file is not None:
            prompt_text = _format_prompt_event(jsonable_event)
            if prompt_text:
                self._prompt_file.write(prompt_text)
                self._prompt_file.flush()


def _planner_input_without_image_payloads(
    planner_input: AgenticPlannerInput,
    saved_images: dict[str, SavedImageRef],
) -> JsonDict:
    data = planner_input.to_dict()
    images: dict[str, JsonDict] = {}
    for label, image in planner_input.images.items():
        saved = saved_images.get(label)
        if saved is not None:
            images[label] = saved.to_dict()
        else:
            images[label] = {
                "label": label,
                "type": image.type,
                "mime_type": image.mime_type,
                "path": image.path,
                "data_omitted": True,
            }
    data["images"] = images
    return data


def _format_human_event(event: JsonDict) -> str:
    event_name = str(event.get("event") or "event")
    if event_name == "run_started":
        return _format_run_started(event)
    if event_name == "session_started":
        return _format_session_started(event)
    if event_name == "step":
        return _format_step(event)
    if event_name == "session_finished":
        return _format_session_finished(event)
    if event_name == "session_error":
        return _format_session_error(event)
    if event_name == "run_finished":
        return _format_run_finished(event)
    return f"[{_human_time(event.get('time'))}] {event_name}\n{_compact_json(event)}\n\n"


def _format_prompt_event(event: JsonDict) -> str:
    if event.get("event") != "step":
        return ""
    prompt = event.get("planner_prompt")
    if not isinstance(prompt, str):
        return ""
    planner_input = _as_dict(event.get("planner_input"))
    lines = [
        "=" * 80,
        (
            f"[{_human_time(event.get('time'))}] PROMPT "
            f"session={event.get('session_id')} "
            f"step={event.get('step_index')}"
        ),
        f"  task: {_one_line(planner_input.get('task'))}",
        f"  current: {_current_subtask(event)}",
        f"  prompt_chars: {len(prompt)}",
        "-" * 80,
        prompt.rstrip(),
        "=" * 80,
        "",
    ]
    return "\n".join(lines) + "\n"


def _format_run_started(event: JsonDict) -> str:
    return (
        f"[{_human_time(event.get('time'))}] RUN STARTED\n"
        f"  run_id: {event.get('run_id')}\n\n"
    )


def _format_run_finished(event: JsonDict) -> str:
    return (
        f"[{_human_time(event.get('time'))}] RUN FINISHED\n"
        f"  run_id: {event.get('run_id')}\n\n"
    )


def _format_session_started(event: JsonDict) -> str:
    return (
        f"[{_human_time(event.get('time'))}] SESSION STARTED\n"
        f"  session_id: {event.get('session_id')}\n"
        f"  task: {_one_line(event.get('task'))}\n\n"
    )


def _format_session_finished(event: JsonDict) -> str:
    return (
        f"[{_human_time(event.get('time'))}] SESSION FINISHED\n"
        f"  session_id: {event.get('session_id')}\n"
        f"  stop_reason: {event.get('stop_reason')}\n"
        f"  task_complete: {event.get('task_complete')}\n"
        f"  steps: {event.get('steps')}\n\n"
    )


def _format_session_error(event: JsonDict) -> str:
    return (
        f"[{_human_time(event.get('time'))}] SESSION ERROR\n"
        f"  session_id: {event.get('session_id')}\n"
        f"  task: {_one_line(event.get('task'))}\n"
        f"  error: {event.get('error_type')}: {_one_line(event.get('error'))}\n\n"
    )


def _format_step(event: JsonDict) -> str:
    planner_input = _as_dict(event.get("planner_input"))
    planner_output = _as_dict(event.get("planner_output"))
    lines = [
        (
            f"[{_human_time(event.get('time'))}] STEP "
            f"session={event.get('session_id')} "
            f"step={event.get('step_index')} "
            f"vlm={_vlm_state(event)} "
            f"parse={_parse_state(event)} "
            f"complete={event.get('task_complete')}"
        ),
        f"  task: {_one_line(planner_input.get('task'))}",
        f"  current: {_current_subtask(event)}",
    ]

    parse_error = event.get("parse_error")
    if parse_error:
        lines.append(f"  parse_error: {_one_line(parse_error)}")
    monitor_status = event.get("monitor_status")
    monitor_error = event.get("monitor_error")
    if monitor_status or monitor_error:
        lines.append(f"  monitor: status={monitor_status} error={_one_line(monitor_error)}")

    _append_subtasks(lines, planner_input, planner_output, event.get("subtask_index"))
    _append_images(lines, planner_input)
    _append_environment(lines, planner_input)

    planner_prompt = event.get("planner_prompt")
    if isinstance(planner_prompt, str):
        lines.append(
            f"  planner_prompt: {len(planner_prompt)} chars "
            "(full text in prompt.log and events.jsonl)"
        )
    elif event.get("vlm_called") is False:
        lines.append("  planner_prompt: <not sent; system monitor poll>")

    _append_tool_calls(lines, planner_output)
    _append_tool_results(lines, event.get("tool_results"))
    _append_executor(lines, event.get("executor_output"))
    _append_vlm_raw_output(lines, event.get("vlm_raw_output"))

    return "\n".join(lines) + "\n\n"


def _append_subtasks(
    lines: list[str],
    planner_input: dict[str, Any],
    planner_output: dict[str, Any],
    event_subtask_index: object,
) -> None:
    subtasks = planner_input.get("subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        subtasks = planner_output.get("subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        lines.append("  plan: <none>")
        return
    current_index = event_subtask_index
    if current_index is None:
        current_index = planner_input.get("subtask_index")
    if current_index is None:
        current_index = planner_output.get("subtask_index")
    lines.append("  plan:")
    for index, subtask in enumerate(subtasks):
        marker = " <- current" if index == current_index else ""
        lines.append(f"    {index}. {_one_line(subtask)}{marker}")


def _append_images(lines: list[str], planner_input: dict[str, Any]) -> None:
    images = _as_dict(planner_input.get("images"))
    if not images:
        return
    lines.append("  images:")
    for label, image_value in images.items():
        image = _as_dict(image_value)
        path = image.get("path") or "<no path>"
        size = _format_size(image.get("size_bytes"))
        mime_type = image.get("mime_type") or "unknown"
        lines.append(f"    - {label}: {path} ({mime_type}, {size})")


def _append_environment(lines: list[str], planner_input: dict[str, Any]) -> None:
    environment = planner_input.get("environment")
    if environment:
        lines.append(f"  environment: {_compact_json(environment, max_chars=1000)}")


def _append_tool_calls(lines: list[str], planner_output: dict[str, Any]) -> None:
    tool_calls = planner_output.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        lines.append("  tool_calls: <none>")
        return
    lines.append("  tool_calls:")
    for call_value in tool_calls:
        call = _as_dict(call_value)
        name = _canonical_tool_name(call)
        arguments = _compact_json(call.get("arguments") or {}, max_chars=800)
        lines.append(f"    - {name} {arguments}")


def _append_tool_results(lines: list[str], tool_results_value: object) -> None:
    if not isinstance(tool_results_value, list) or not tool_results_value:
        lines.append("  tool_results: <none>")
        return
    lines.append("  tool_results:")
    for result_value in tool_results_value:
        result = _as_dict(result_value)
        name = _canonical_result_name(result)
        status = result.get("status")
        if result.get("error"):
            lines.append(f"    - {name}: {status} error={_one_line(result.get('error'))}")
            continue
        data = _compact_json(result.get("data") or {}, max_chars=1000)
        lines.append(f"    - {name}: {status} {data}")


def _append_executor(lines: list[str], executor_output_value: object) -> None:
    if executor_output_value is None:
        lines.append("  executor_output: <none>")
        return
    executor_output = _as_dict(executor_output_value)
    status = executor_output.get("status")
    if executor_output.get("error"):
        lines.append(f"  executor_output: {status} error={_one_line(executor_output.get('error'))}")
        return
    lines.append(f"  executor_output: {status} {_compact_json(executor_output.get('data') or {})}")


def _append_vlm_raw_output(lines: list[str], raw_output: object) -> None:
    if not isinstance(raw_output, str):
        return
    if not raw_output:
        lines.append("  vlm_raw_output: <empty>")
        return
    lines.append("  vlm_raw_output:")
    lines.extend(f"    {line}" for line in _truncate(raw_output.strip(), 1600).splitlines())


def _current_subtask(event: JsonDict) -> str:
    subtask = event.get("current_subtask")
    index = event.get("subtask_index")
    if subtask is None:
        return "<none>"
    if index is None:
        return _one_line(subtask)
    return f"[{index}] {_one_line(subtask)}"


def _parse_state(event: JsonDict) -> str:
    return "ok" if event.get("parse_ok") else "failed"


def _vlm_state(event: JsonDict) -> str:
    return "called" if event.get("vlm_called", True) else "skipped"


def _canonical_tool_name(call: dict[str, Any]) -> str:
    namespace = call.get("namespace")
    name = call.get("name")
    if namespace:
        return f"{namespace}___{name}"
    return str(name)


def _canonical_result_name(result: dict[str, Any]) -> str:
    namespace = result.get("namespace")
    name = result.get("tool_name")
    if namespace:
        return f"{namespace}___{name}"
    return str(name)


def _human_time(value: object) -> str:
    if isinstance(value, (int, float)):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))
    return "unknown time"


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _compact_json(value: object, *, max_chars: int = 1200) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return _truncate(text, max_chars)


def _one_line(value: object, *, max_chars: int = 240) -> str:
    if value is None:
        return "<none>"
    return _truncate(str(value).replace("\n", "\\n"), max_chars)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def _format_size(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "unknown size"
    size = float(value)
    for unit in ("B", "KiB", "MiB"):
        if size < 1024 or unit == "MiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} MiB"


def _image_bytes(image: ImageInput) -> tuple[bytes, str | None] | None:
    if image.type == "base64":
        return base64.b64decode(image.data), image.mime_type or "image/jpeg"
    if image.type == "path":
        path = Path(image.data).expanduser()
        if not path.exists():
            return None
        mime_type = image.mime_type or mimetypes.guess_type(str(path))[0] or "image/jpeg"
        return path.read_bytes(), mime_type
    return None


def _suffix_for_mime(mime_type: str | None) -> str:
    if not mime_type:
        return ".img"
    suffix = mimetypes.guess_extension(mime_type)
    if suffix == ".jpe":
        return ".jpg"
    return suffix or ".img"


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return cleaned or "image"
