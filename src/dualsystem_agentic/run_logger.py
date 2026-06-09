"""Run logging for online agent sessions."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
    """Write online runtime events to one JSONL file per process run."""

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

    def start_run(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._file = (self.run_dir / "events.jsonl").open("a", encoding="utf-8")
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
        self._write(
            {
                "event": "step",
                "run_id": self.run_id,
                "session_id": session_id,
                "time": time.time(),
                "step_index": result.step_index,
                "planner_input": planner_input,
                "planner_prompt": build_agentic_prompt(result.planner_input),
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
        self._file.write(json.dumps(ensure_jsonable(event), ensure_ascii=False) + "\n")
        self._file.flush()


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

