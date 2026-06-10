#!/usr/bin/env python3
"""
dual_franka Bridge Server
=========================
HTTP bridge that runs on the dual-Franka machine and exposes local robot-side
state/files to the remote MCP adapter.

Current real-machine integration points:
  - Images are read from local files under /tmp/img/.
  - Execution is a placeholder interface that logs the requested subtask.
  - Monitoring writes the target subtask to /tmp/subtask.txt and reads
    /tmp/monitor_result.txt with file locks to reduce read/write conflicts.
  - Extra robot-specific endpoints are placeholders for future integration.

Dependencies:
    pip install fastapi uvicorn

Usage:
    python mcp_server/dual_franka_mcp_server/dual_franka_bridge.py --port 8767
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

try:
    import fcntl

    HAS_FCNTL = True
except ImportError:  # pragma: no cover - non-Unix fallback
    fcntl = None
    HAS_FCNTL = False


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [dual-franka-bridge] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_IMAGE_DIR = Path("/tmp/img")
DEFAULT_SUBTASK_FILE = Path("/tmp/subtask.txt")
DEFAULT_MONITOR_RESULT_FILE = Path("/tmp/monitor_result.txt")

CAMERA_FILES = {
    "cam_high": "base_0_rgb.jpg",
    "cam_left_wrist": "left_wrist_0_rgb.jpg",
    "cam_right_wrist": "right_wrist_0_rgb.jpg",
}

app = FastAPI(title="dual_franka Bridge Server")

image_dir = DEFAULT_IMAGE_DIR
subtask_file = DEFAULT_SUBTASK_FILE
monitor_result_file = DEFAULT_MONITOR_RESULT_FILE

_last_execute_request: dict[str, Any] = {}
_last_monitor_request: dict[str, Any] = {}
_last_extra_request: dict[str, Any] = {}


def _ok(data=None, message: str = "ok") -> JSONResponse:
    return JSONResponse({"success": True, "data": data, "message": message})


def _fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"success": False, "data": None, "message": message}, status_code=status)


@app.get("/cameras/latest")
async def cameras_latest():
    """Return the three expected camera images as base64 JPEG strings."""
    images, missing = _read_camera_images()
    if missing:
        return _fail(f"missing camera image(s): {', '.join(missing)}", status=503)
    return _ok({**images, "timestamp": time.time()})


@app.get("/cameras/concatenated")
async def cameras_concatenated():
    """Return a single payload used by HTTPDataLoader.

    The current bridge does not concatenate pixels; it exposes cam_high under
    ``concatenated_image`` so existing configs can keep using one image key, and
    also includes wrist views as extra top-level images for VLMs that consume
    multiple images.
    """
    images, missing = _read_camera_images()
    if missing:
        return _fail(f"missing camera image(s): {', '.join(missing)}", status=503)
    return _ok(
        {
            "concatenated_image": images["cam_high"],
            **images,
            "timestamp": time.time(),
        }
    )


@app.get("/environment")
async def environment():
    images, missing = _read_camera_images(allow_missing=True)
    return _ok(
        {
            "image_dir": str(image_dir),
            "available_cameras": sorted(images.keys()),
            "missing_cameras": missing,
            "subtask_file": str(subtask_file),
            "monitor_result_file": str(monitor_result_file),
            "last_execute_request": _last_execute_request,
            "last_monitor_request": _last_monitor_request,
            "last_extra_request": _last_extra_request,
        }
    )


@app.post("/task/execute")
async def task_execute(body: dict):
    """Placeholder execution hook.

    It intentionally does not move hardware yet. The request is logged and stored
    so the surrounding agentic loop can be tested safely on the real machine.
    """
    global _last_execute_request
    subtask = body.get("subtask") or body.get("prompt") or body.get("instruction")
    if not subtask:
        return _fail("missing subtask")
    _last_execute_request = {
        "subtask": str(subtask),
        "request": body,
        "timestamp": time.time(),
    }
    logger.info("[EXECUTE PLACEHOLDER] subtask=%r body=%s", subtask, json.dumps(body, ensure_ascii=False))
    return _ok({"executed": True, "subtask": str(subtask), "placeholder": True})


@app.post("/task/monitor")
async def task_monitor(body: dict):
    """Write monitor target subtask and read monitor result."""
    global _last_monitor_request
    subtask = body.get("subtask") or body.get("current_subtask") or ""
    subtask_index = body.get("subtask_index")
    task_id = body.get("task_id")
    if not subtask:
        return _fail("missing subtask")

    _write_subtask_file(str(subtask))
    monitor_payload = _read_monitor_result()
    status = _derive_monitor_status(monitor_payload)

    _last_monitor_request = {
        "subtask": str(subtask),
        "subtask_index": subtask_index,
        "task_id": task_id,
        "timestamp": time.time(),
        "monitor_result": monitor_payload,
    }
    return _ok(
        {
            "status": status,
            "subtask": str(subtask),
            "subtask_index": subtask_index,
            "task_id": task_id,
            "monitor_result": monitor_payload,
        }
    )


@app.post("/task/stop")
async def task_stop():
    logger.info("[STOP PLACEHOLDER] stop requested")
    return _ok({"stopped": True, "placeholder": True})


@app.post("/task/reset")
async def task_reset():
    logger.info("[RESET PLACEHOLDER] reset requested")
    return _ok({"reset": True, "placeholder": True})


@app.post("/task/emergency_stop")
async def task_emergency_stop():
    logger.warning("[EMERGENCY STOP PLACEHOLDER] emergency stop requested")
    return _ok({"emergency_stop": True, "placeholder": True})


@app.post("/extra/{name}")
async def extra_module(name: str, body: dict | None = None):
    """Placeholder for future dual-Franka-specific HTTP modules."""
    global _last_extra_request
    _last_extra_request = {
        "name": name,
        "body": body or {},
        "timestamp": time.time(),
    }
    logger.info("[EXTRA PLACEHOLDER] name=%s body=%s", name, json.dumps(body or {}, ensure_ascii=False))
    return _ok({"name": name, "placeholder": True})


@app.get("/health")
async def health():
    images, missing = _read_camera_images(allow_missing=True)
    return _ok(
        {
            "image_dir": str(image_dir),
            "available_cameras": sorted(images.keys()),
            "missing_cameras": missing,
            "subtask_file": str(subtask_file),
            "monitor_result_file": str(monitor_result_file),
        }
    )


@app.get("/")
async def root():
    return JSONResponse(
        {
            "service": "dual_franka_bridge_server",
            "status": "running",
            "endpoints": [
                "GET  /cameras/latest",
                "GET  /cameras/concatenated",
                "GET  /environment",
                "POST /task/execute",
                "POST /task/monitor",
                "POST /task/stop",
                "POST /task/reset",
                "POST /task/emergency_stop",
                "POST /extra/{name}",
                "GET  /health",
            ],
        }
    )


def _read_camera_images(*, allow_missing: bool = False) -> tuple[dict[str, str], list[str]]:
    images: dict[str, str] = {}
    missing: list[str] = []
    for camera_name, filename in CAMERA_FILES.items():
        path = image_dir / filename
        if not path.exists():
            missing.append(f"{camera_name}:{path}")
            continue
        try:
            images[camera_name] = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            logger.warning("failed to read image %s: %s", path, exc)
            missing.append(f"{camera_name}:{path}")
    if allow_missing:
        return images, missing
    return images, missing


def _write_subtask_file(subtask: str) -> None:
    subtask_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = subtask_file.with_suffix(subtask_file.suffix + ".lock")
    with _exclusive_lock(lock_path):
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{subtask_file.name}.",
            dir=str(subtask_file.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(subtask)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, subtask_file)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _read_monitor_result() -> dict[str, Any]:
    if not monitor_result_file.exists():
        return {"status": "running", "message": "monitor result file not found"}
    try:
        with monitor_result_file.open("r", encoding="utf-8") as handle:
            if HAS_FCNTL:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                text = handle.read().strip()
            finally:
                if HAS_FCNTL:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning("failed to read monitor result %s: %s", monitor_result_file, exc)
        return {"status": "running", "error": str(exc)}

    if not text:
        return {"status": "running", "message": "monitor result file is empty"}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"status": _normalize_status(text) or "running", "raw_text": text}
    if isinstance(parsed, dict):
        return parsed
    return {"status": _normalize_status(str(parsed)) or "running", "result": parsed}


def _derive_monitor_status(payload: dict[str, Any]) -> str:
    for key in ("status", "task_status", "execution_status", "state"):
        value = payload.get(key)
        if value is None:
            continue
        normalized = _normalize_status(str(value))
        if normalized:
            return normalized
    if payload.get("success") is True or payload.get("completed") is True:
        return "success"
    if payload.get("running") is True or payload.get("is_running") is True:
        return "running"
    if payload.get("failed") is True or payload.get("error"):
        return "failed"
    return "running"


def _normalize_status(value: str) -> str | None:
    text = value.strip().lower()
    if text in {"running", "executing", "busy", "in_progress", "active", "started"}:
        return "running"
    if text in {"success", "succeeded", "done", "completed", "complete", "finished"}:
        return "success"
    if text in {"failed", "failure", "error", "aborted", "cancelled", "canceled", "stopped"}:
        return "failed"
    return None


@contextmanager
def _exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if HAS_FCNTL:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if HAS_FCNTL:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main() -> None:
    global image_dir, subtask_file, monitor_result_file

    parser = argparse.ArgumentParser(description="dual_franka Bridge Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8767, help="Listen port")
    parser.add_argument("--image-dir", default=str(DEFAULT_IMAGE_DIR), help="Directory containing camera jpg files")
    parser.add_argument("--subtask-file", default=str(DEFAULT_SUBTASK_FILE), help="Monitor target subtask output file")
    parser.add_argument(
        "--monitor-result-file",
        default=str(DEFAULT_MONITOR_RESULT_FILE),
        help="Monitor result input file",
    )
    args = parser.parse_args()

    image_dir = Path(args.image_dir).expanduser()
    subtask_file = Path(args.subtask_file).expanduser()
    monitor_result_file = Path(args.monitor_result_file).expanduser()

    logger.info("Image dir: %s", image_dir)
    logger.info("Subtask file: %s", subtask_file)
    logger.info("Monitor result file: %s", monitor_result_file)
    logger.info("Starting dual_franka Bridge Server on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
