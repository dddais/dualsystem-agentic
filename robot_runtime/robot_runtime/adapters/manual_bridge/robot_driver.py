"""Keep manual handoffs, but complete them by sending real bridge commands.

The HTTP click only releases a gate. The existing runtime worker executes the
command and waits for its completion, so a lost HTTP reply cannot replay motion
and monitor activation remains downstream of successful VLA startup.
"""

from dataclasses import dataclass, field
import threading

from robot_runtime.adapters.manual.robot_driver import ManualRobotDriver, _OperatorRequest
from robot_runtime.adapters.robot_bridge.clients import BridgeClient
from robot_runtime.adapters.robot_bridge.robot_driver import RobotBridgeRobotDriver


@dataclass
class _ControlRequest(_OperatorRequest):
    clicked: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    phase: str = "waiting"
    result: dict = field(default_factory=dict)

    def public(self):
        return {**super().public(), "phase": self.phase, "error": self.error}


class ManualBridgeRobotDriver(ManualRobotDriver):
    # Lifecycle controls are handled by the runtime, never by a raw proxy.
    SETTINGS = {"toggle_recording", "set_person", "set_phase", "toggle_phase_lock",
                "adjust_latency", "adjust_move_steps", "set_gripper_map", "set_prompt"}
    MOTION = {"set_mode", "toggle_single_step", "step"}

    def __init__(self, *, operator_timeout_s=300.0, bridge_driver=None,
                 control_client=None, **bridge_config):
        super().__init__(operator_timeout_s=operator_timeout_s)
        self.bridge = bridge_driver or RobotBridgeRobotDriver(**bridge_config)
        # Dashboard polling/logs must not wait behind a lifecycle command.
        self._control = control_client or BridgeClient(
            self.bridge.scheduler_url, timeout_s=bridge_config.get("timeout_s", 5.0),
            json_protocol=True)
        self._last_operation = None
        self._execute_args = None

    def execute(self, request, execution):
        self._execute_args = (request, execution)
        return super().execute(request, execution)

    def acknowledge(self, request_id):
        raise ValueError("manual_bridge requires /manual/action; acknowledgement cannot complete a robot command")

    def request_action(self, request_id):
        with self._lock:
            if request_id in self._acknowledged:
                return {"accepted": True, "already_requested": True, "request_id": request_id}
            pending = self._pending
            if pending is None or pending.request_id != request_id or pending.error:
                raise ValueError("manual request expired or does not match the pending action")
            self._acknowledged.add(request_id)
            pending.phase = "queued"
            pending.clicked.set()
            return {"accepted": True, "request_id": request_id}

    def _wait(self, action, execution_id, instruction):
        with self._lock:
            if self._closed:
                raise RuntimeError("manual_bridge driver is closed")
            if action == "execute" and execution_id in self._cancelled_ids:
                raise RuntimeError("manual execution cancelled before startup")
            pending = self._pending
            owner = pending is None
            if pending and (pending.action, pending.execution_id) != (action, execution_id):
                raise RuntimeError(f"operator is still handling {pending.action}")
            if owner:
                pending = _ControlRequest(action, execution_id, instruction)
                self._pending = pending
        if owner:
            try:
                clicked = pending.clicked.wait(self.operator_timeout_s)
                with self._lock:
                    if pending.error:
                        raise RuntimeError(pending.error)
                    if not clicked:
                        raise RuntimeError(f"operator {action} timed out after {self.operator_timeout_s}s")
                    pending.phase = "running"
                if action == "execute":
                    result = self.bridge.execute(*self._execute_args, cancelled=pending.cancelled)
                elif action == "stop":
                    result = self.bridge.stop(execution_id)
                else:
                    result = self.bridge.reset(cancelled=pending.cancelled)
                with self._lock:
                    if pending.error:
                        raise RuntimeError(pending.error)
                    pending.result = {**result, "manual": True, "provider": "manual_bridge",
                                      "operator_triggered": True, "request_id": pending.request_id}
                    pending.phase = "completed"
            except Exception as exc:
                with self._lock:
                    pending.error = str(exc)
                    pending.phase = "failed"
            finally:
                with self._lock:
                    self._last_operation = {**pending.public(), "result": dict(pending.result)}
                    if self._pending is pending:
                        self._pending = None
                    pending.done.set()
        else:
            # Concurrent cleanup shares one click and one physical command.
            pending.done.wait()
        if pending.error:
            raise RuntimeError(pending.error)
        return dict(pending.result)

    def cancel_pending(self, execution_id=None):
        with self._lock:
            if execution_id is not None:
                self._cancelled_ids.add(execution_id)
            pending = self._pending
            if pending and pending.action in {"execute", "reset"} and (
                execution_id is None or pending.execution_id == execution_id
            ):
                pending.error = f"manual {pending.action} cancelled"
                pending.cancelled.set()
                pending.clicked.set()
        self.bridge.cancel_pending(execution_id)

    def emergency_stop(self):
        self.cancel_pending()
        result = self.bridge.emergency_stop()
        with self._lock:
            self._last_stopped, self._active_id = self._active_id, None
            if self._pending and self._pending.action == "stop":
                self._pending.error = "manual stop superseded by emergency stop"
                self._pending.clicked.set()
        return {**result, "provider": "manual_bridge"}

    def scheduler_status(self):
        state = self.bridge._state(self._control)
        with self._lock:
            instruction = self._instruction
        selection = None
        error = None
        if instruction:
            from robot_runtime.core.types import ExecutionRequest
            try:
                index, prompt = self.bridge._prompt_index(ExecutionRequest(instruction), state)
                selection = {"index": index, "prompt": prompt}
            except ValueError as exc:
                error = str(exc)
        return {"state": state, "selection": selection, "selection_error": error}

    def scheduler_action(self, name, args, *, cancelled=None):
        if name not in self.SETTINGS | self.MOTION:
            raise ValueError("use the staged start/stop/reset buttons for lifecycle controls")
        state = self.bridge._state(self._control)
        if name not in state["actions"]:
            raise ValueError(f"scheduler does not support {name}")
        if name == "set_mode" and args.get("mode") not in state.get("modes", []):
            raise ValueError("unsupported scheduler mode")
        if name == "set_prompt":
            index = args.get("index")
            if type(index) is not int or not 0 <= index < len(state.get("prompts", [])):
                raise ValueError("prompt index out of range")
        return self.bridge._action(self._control, name, args, token=cancelled)

    def scheduler_log(self, target, lines):
        if target not in {"scheduler", "policy", "robot", "master"} or not 1 <= lines <= 1000:
            raise ValueError("invalid log target or line count")
        return self.bridge._call(self._control, {"cmd": "get_log", "target": target, "lines": lines})

    def status(self):
        with self._lock:
            return {**super().status(), "provider": "manual_bridge", "control_mode": "bridge",
                    "scheduler_url": self.bridge.scheduler_url, "robot_url": self.bridge.robot_url,
                    "last_operation": self._last_operation}

    def capabilities(self):
        return {**super().capabilities(), "driver": "manual_bridge",
                "operator_triggered": True, "reset_completion": "fixed_delay"}

    def close(self):
        self.cancel_pending()
        with self._lock:
            if self._pending:
                self._pending.error = "manual_bridge driver closed"
                self._pending.clicked.set()
            super().close()
        self.bridge.close()
        self._control.close()
