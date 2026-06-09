"""
Mock x2robot Bridge Server
==========================
Simulates the x2robot bridge HTTP API on localhost:8766 so the x2robot MCP server
can be exercised end-to-end without a real robot. Pure standard library: no ROS,
no OpenCV, no extra web framework.

A running task advances ``current_step`` every ``step_interval`` seconds and
auto-completes after ``--auto-complete-steps`` steps, letting ``monitor`` report
running -> success.

Usage:
    python mock_x2robot_bridge.py [--host 0.0.0.0] [--port 8766] [--auto-complete-steps 4]
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
    format="%(asctime)s [MockX2Robot] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class RobotState:
    running_mode: int = 0
    prompt: str = ""
    inference_ip: str = ""
    inference_port: int = 0
    step_interval: float = 1.0
    current_step: int = 0
    total_steps: int = 0
    left_arm_pos: list[float] = field(default_factory=lambda: [0.0] * 7)
    right_arm_pos: list[float] = field(default_factory=lambda: [0.0] * 7)


robot = RobotState()
state_lock = threading.Lock()
auto_complete_steps = 4
_ticker_stop: threading.Event | None = None


def _ticker_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        time.sleep(max(robot.step_interval, 0.05))
        with state_lock:
            if robot.running_mode != 1:
                break
            robot.current_step += 1
            logger.info("step %d / %d", robot.current_step, robot.total_steps)
            if robot.total_steps > 0 and robot.current_step >= robot.total_steps:
                robot.running_mode = 0
                logger.info("task auto-completed after %d steps", robot.current_step)
                break


def _start_ticker() -> None:
    global _ticker_stop
    _stop_ticker()
    _ticker_stop = threading.Event()
    threading.Thread(target=_ticker_loop, args=(_ticker_stop,), daemon=True).start()


def _stop_ticker() -> None:
    global _ticker_stop
    if _ticker_stop is not None:
        _ticker_stop.set()
        _ticker_stop = None


def _status_data() -> dict:
    mode_text = {0: "idle", 1: "autonomous", 2: "teleop"}.get(robot.running_mode, "unknown")
    return {
        "state": mode_text,
        "running_mode": robot.running_mode,
        "prompt": robot.prompt,
        "current_step": robot.current_step,
        "total_steps": robot.total_steps,
        "inference_server": f"{robot.inference_ip}:{robot.inference_port}" if robot.inference_ip else "",
        "left_arm_pos": robot.left_arm_pos,
        "right_arm_pos": robot.right_arm_pos,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quieter access log
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
        if self.path == "/status":
            with state_lock:
                self._send(True, _status_data())
        elif self.path == "/health":
            self._send(True, {"ros_available": False, "running_mode": robot.running_mode})
        elif self.path == "/":
            self._send(True, {"service": "MockX2RobotBridge", "running_mode": robot.running_mode})
        else:
            self._send(False, message=f"unknown path: {self.path}", status=404)

    def do_POST(self) -> None:
        body = self._read_json()
        if self.path == "/task/set_params":
            params = body.get("evaluate_params", {})
            if not params:
                self._send(False, message="missing evaluate_params")
                return
            with state_lock:
                robot.prompt = params.get("prompt", robot.prompt)
                policy = params.get("policy", {})
                robot.inference_ip = policy.get("host", robot.inference_ip)
                robot.inference_port = policy.get("port", robot.inference_port)
                robot.step_interval = params.get("step_interval", robot.step_interval)
                robot.current_step = 0
                robot.total_steps = auto_complete_steps
            logger.info("set_params: prompt=%r policy=%s:%s", robot.prompt, robot.inference_ip, robot.inference_port)
            self._send(True, _status_data())
        elif self.path == "/task/start":
            with state_lock:
                robot.running_mode = 1
                robot.current_step = 0
            _start_ticker()
            logger.info("task_start: running_mode=1")
            self._send(True, "running_mode set to 1 (autonomous)")
        elif self.path == "/task/stop":
            _stop_ticker()
            with state_lock:
                robot.running_mode = 0
            logger.info("task_stop: running_mode=0")
            self._send(True, "running_mode set to 0 (idle)")
        elif self.path == "/task/reset":
            _stop_ticker()
            with state_lock:
                robot.running_mode = 0
                robot.current_step = 0
                robot.total_steps = 0
                robot.left_arm_pos = [0.0] * 7
                robot.right_arm_pos = [0.0] * 7
            logger.info("task_reset: arms reset to home")
            self._send(True, "running_mode set to 0 and arms reset to home pose")
        elif self.path == "/task/emergency_stop":
            _stop_ticker()
            with state_lock:
                robot.running_mode = 0
            logger.warning("EMERGENCY STOP triggered")
            self._send(True, "emergency stop executed")
        else:
            self._send(False, message=f"unknown path: {self.path}", status=404)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock x2robot Bridge Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8766, help="Listen port")
    parser.add_argument(
        "--auto-complete-steps", type=int, default=4,
        help="Steps after which a running task auto-completes",
    )
    args = parser.parse_args()

    global auto_complete_steps
    auto_complete_steps = args.auto_complete_steps

    logger.info("Starting MockX2RobotBridge on %s:%d (auto_complete_steps=%d)", args.host, args.port, auto_complete_steps)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
