"""
Mock dual-Franka HTTP Bridge
============================
Pure-stdlib local HTTP bridge for exercising the dual_franka MCP adapter without
real hardware.

Default endpoints match ``server.py``:
    GET  /environment
    GET  /status
    GET  /cameras/concatenated
    POST /task/execute
    POST /task/monitor
    POST /task/stop
    POST /task/reset
    POST /task/emergency_stop
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MockDualFranka] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_SAMPLE_JPEG_BASE64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "////2wBDAf//////////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB"
    "/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAH/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAEFAqf/"
    "xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/Aaf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/Aaf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAY/"
    "Aqf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/IV//2gAMAwEAAgADAAAAEP/EABQRAQAAAAAAAAAAAAAAAAAAABD/2gAIAQMBAT8QH//EABQRAQAAAAAA"
    "AAAAAAAAAAAAABD/2gAIAQIBAT8QH//EABQQAQAAAAAAAAAAAAAAAAAAABD/2gAIAQEAAT8QH//Z"
)


@dataclass
class RobotState:
    running: bool = False
    current_subtask: str = ""
    current_step: int = 0
    total_steps: int = 0
    last_error: str = ""
    left_arm_pose: list[float] = field(default_factory=lambda: [0.0] * 7)
    right_arm_pose: list[float] = field(default_factory=lambda: [0.0] * 7)
    scene_objects: list[str] = field(default_factory=lambda: ["cube", "drawer", "tray"])


state = RobotState()
state_lock = threading.Lock()
auto_complete_steps = 3


def _environment() -> dict:
    return {
        "running": state.running,
        "current_subtask": state.current_subtask,
        "current_step": state.current_step,
        "total_steps": state.total_steps,
        "last_error": state.last_error,
        "left_arm_pose": state.left_arm_pose,
        "right_arm_pose": state.right_arm_pose,
        "scene_objects": state.scene_objects,
    }


def _monitor() -> dict:
    if state.running:
        state.current_step += 1
        if state.current_step >= state.total_steps:
            state.running = False
            status = "success"
        else:
            status = "running"
    elif state.current_subtask and state.current_step >= state.total_steps > 0:
        status = "success"
    else:
        status = "failed"
    return {
        "status": status,
        "running": state.running,
        "subtask": state.current_subtask,
        "current_step": state.current_step,
        "total_steps": state.total_steps,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        return

    def _send(self, success: bool, data=None, message: str = "ok", status: int = 200) -> None:
        body = json.dumps({"success": success, "data": data, "message": message}).encode()
        self.send_response(status if success else max(status, 400))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode() or "{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        if self.path in {"/", "/health"}:
            self._send(True, {"service": "MockDualFrankaBridge", "running": state.running})
            return
        if self.path in {"/environment", "/status"}:
            with state_lock:
                self._send(True, _environment())
            return
        if self.path == "/cameras/concatenated":
            self._send(True, {"concatenated_image": _SAMPLE_JPEG_BASE64, "timestamp": time.time()})
            return
        self._send(False, message=f"unknown path: {self.path}", status=404)

    def do_POST(self) -> None:
        body = self._read_json()
        if self.path == "/task/execute":
            subtask = body.get("subtask") or body.get("prompt") or body.get("instruction")
            if not subtask:
                self._send(False, message="missing subtask")
                return
            with state_lock:
                state.running = True
                state.current_subtask = str(subtask)
                state.current_step = 0
                state.total_steps = auto_complete_steps
                state.last_error = ""
            logger.info("execute: %s", state.current_subtask)
            self._send(True, {"executed": True, "subtask": state.current_subtask})
            return
        if self.path == "/task/monitor":
            with state_lock:
                data = _monitor()
            self._send(True, data)
            return
        if self.path == "/task/stop":
            with state_lock:
                state.running = False
            self._send(True, {"stopped": True})
            return
        if self.path == "/task/reset":
            with state_lock:
                state.running = False
                state.current_subtask = ""
                state.current_step = 0
                state.total_steps = 0
                state.last_error = ""
                state.left_arm_pose = [0.0] * 7
                state.right_arm_pose = [0.0] * 7
            self._send(True, {"reset": True})
            return
        if self.path == "/task/emergency_stop":
            with state_lock:
                state.running = False
                state.last_error = "emergency_stop"
            logger.warning("emergency_stop")
            self._send(True, {"emergency_stop": True})
            return
        self._send(False, message=f"unknown path: {self.path}", status=404)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock dual-Franka HTTP bridge")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8767, help="Listen port")
    parser.add_argument("--auto-complete-steps", type=int, default=3)
    args = parser.parse_args()

    global auto_complete_steps
    auto_complete_steps = args.auto_complete_steps
    logger.info("Starting MockDualFrankaBridge on %s:%d", args.host, args.port)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
