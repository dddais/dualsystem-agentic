"""Dual-Franka robot driver adapters."""

from __future__ import annotations

import time

from robot_runtime.core.types import ExecutionRequest, ExecutionState, JsonDict


class PlaceholderDualFrankaRobotDriver:
    """Safe placeholder driver: records requests without moving hardware."""

    def __init__(self) -> None:
        self.last_execute_request: JsonDict = {}
        self.last_control_request: JsonDict = {}

    def execute(self, request: ExecutionRequest, execution: ExecutionState) -> JsonDict:
        self.last_execute_request = {
            "execution_id": execution.execution_id,
            "monitor_id": execution.monitor_id,
            "subtask": request.subtask,
            "task": request.task,
            "subtask_index": request.subtask_index,
            "metadata": request.metadata,
            "options": request.options,
            "target_queries": request.target_queries,
            "timestamp": time.time(),
        }
        return {"executed": True, "placeholder": True, **self.last_execute_request}

    def stop(self, execution_id: str | None = None) -> JsonDict:
        self.last_control_request = {
            "control": "stop",
            "execution_id": execution_id,
            "timestamp": time.time(),
            "placeholder": True,
        }
        return {"stopped": True, **self.last_control_request}

    def reset(self) -> JsonDict:
        self.last_control_request = {
            "control": "reset",
            "timestamp": time.time(),
            "placeholder": True,
        }
        return {"reset": True, **self.last_control_request}

    def emergency_stop(self) -> JsonDict:
        self.last_control_request = {
            "control": "emergency_stop",
            "timestamp": time.time(),
            "placeholder": True,
        }
        return {"emergency_stop": True, **self.last_control_request}

    def status(self) -> JsonDict:
        return {
            "provider": "placeholder",
            "safe_placeholder": True,
            "last_execute_request": self.last_execute_request,
            "last_control_request": self.last_control_request,
        }

    def capabilities(self) -> JsonDict:
        return {
            "driver": "placeholder",
            "arms": ["left", "right"],
            "safe_placeholder": True,
            "extra_tools": [],
        }
