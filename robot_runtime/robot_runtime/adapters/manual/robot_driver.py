"""Wait for a human action without reading stdin in the HTTP server."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import threading
import time
from uuid import uuid4

from robot_runtime.core.types import ExecutionRequest, ExecutionState
from robot_runtime.adapters.robot_bridge.clients import duration

LOG = logging.getLogger(__name__)


@dataclass
class _OperatorRequest:
    action: str
    execution_id: str | None
    instruction: str
    request_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: float = field(default_factory=time.time)
    done: threading.Event = field(default_factory=threading.Event)
    error: str | None = None

    def public(self):
        return {"request_id": self.request_id, "action": self.action,
                "execution_id": self.execution_id, "instruction": self.instruction,
                "created_at": self.created_at}


class ManualRobotDriver:
    def __init__(self, *, operator_timeout_s: float = 300.0):
        self.operator_timeout_s = duration(operator_timeout_s, "operator_timeout_s")
        self._lock = threading.RLock()
        self._pending: _OperatorRequest | None = None
        self._active_id: str | None = None
        self._instruction = ""
        self._last_stopped: str | None = None
        self._acknowledged: set[str] = set()
        self._cancelled_ids: set[str] = set()
        self._closed = False

    def _wait(self, action: str, execution_id: str | None, instruction: str):
        with self._lock:
            if self._closed:
                raise RuntimeError("manual driver is closed")
            if action == "execute" and execution_id in self._cancelled_ids:
                raise RuntimeError("manual execution cancelled before startup")
            pending = self._pending
            if pending is not None:
                if (pending.action, pending.execution_id) != (action, execution_id):
                    raise RuntimeError(f"operator is still handling {pending.action}")
            else:
                pending = _OperatorRequest(action, execution_id, instruction)
                self._pending = pending
                LOG.warning("Manual %s: %s — open Robot Runtime /manual", action, instruction)
        # Concurrent cleanup calls for the same action share one operator ack.
        acknowledged = pending.done.wait(self.operator_timeout_s)
        with self._lock:
            if not acknowledged and not pending.done.is_set():
                pending.error = f"operator {action} timed out after {self.operator_timeout_s}s"
                pending.done.set()
            if self._pending is pending:
                self._pending = None
            if pending.error:
                raise RuntimeError(pending.error)
        return {"manual": True, "completion_basis": "operator", "request_id": pending.request_id}

    def acknowledge(self, request_id: str) -> dict:
        with self._lock:
            if request_id in self._acknowledged:
                return {"acknowledged": True, "already_acknowledged": True}
            if self._pending is None or self._pending.request_id != request_id:
                raise ValueError("manual request expired or does not match the pending action")
            self._acknowledged.add(request_id)
            self._pending.done.set()
            return {"acknowledged": True, "request_id": request_id}

    def cancel_pending(self, execution_id=None):
        with self._lock:
            if execution_id is not None:
                self._cancelled_ids.add(execution_id)
            pending = self._pending
            if pending and pending.action in {"execute", "reset"} and (
                execution_id is None or pending.execution_id == execution_id
            ):
                pending.error = f"manual {pending.action} cancelled"
                self._pending = None
                pending.done.set()

    def execute(self, request: ExecutionRequest, execution: ExecutionState) -> dict:
        with self._lock:
            if execution.execution_id in self._cancelled_ids:
                raise RuntimeError("manual execution cancelled before startup")
            self._active_id = execution.execution_id
            self._instruction = request.subtask
        result = self._wait("execute", execution.execution_id, request.subtask)
        return {"executed": True, "execution_id": execution.execution_id, **result}

    def stop(self, execution_id=None) -> dict:
        self.cancel_pending(execution_id)
        with self._lock:
            execution_id = execution_id or self._active_id
            if self._active_id is None or (execution_id is not None and self._last_stopped == execution_id):
                return {"stopped": True, "manual": True, "already_inactive": True}
            instruction = self._instruction
        result = self._wait("stop", execution_id, instruction)
        with self._lock:
            self._last_stopped = execution_id
            self._active_id = None
        return {"stopped": True, "execution_id": execution_id, **result}

    def reset(self) -> dict:
        with self._lock:
            if self._active_id is not None:
                raise RuntimeError("manually stop the active task before reset")
            execution_id, instruction = self._last_stopped, self._instruction
        return {"reset": True, **self._wait("reset", execution_id, instruction)}

    def emergency_stop(self) -> dict:
        self.cancel_pending()
        # Only the operator can stop hardware in manual mode. The Runtime's
        # emergency latch prevents a subsequent execute until reset.
        return {**self.stop(), "emergency_stop": True, "hardware_estop": False}

    def status(self) -> dict:
        with self._lock:
            return {"provider": "manual", "active_execution_id": self._active_id,
                    "pending": self._pending.public() if self._pending else None,
                    "operator_timeout_s": self.operator_timeout_s,
                    "operator_page": "/manual"}

    def capabilities(self) -> dict:
        return {"driver": "manual", "arms": ["left", "right"],
                "operator_page": "/manual", "hardware_estop": False, "extra_tools": []}

    def close(self):
        with self._lock:
            self._closed = True
            if self._pending:
                self._pending.error = "manual driver closed"
                self._pending.done.set()
                self._pending = None
