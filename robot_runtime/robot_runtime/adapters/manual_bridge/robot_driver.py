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
    choice: str | None = None
    allow_adjustment: bool = False
    trigger: str = "operator"

    def public(self):
        return {**super().public(), "phase": self.phase, "error": self.error,
                "choice": self.choice, "allow_adjustment": self.allow_adjustment, "trigger": self.trigger}


class ManualBridgeRobotDriver(ManualRobotDriver):
    # Lifecycle controls are handled by the runtime, never by a raw proxy.
    SETTINGS = {"toggle_recording", "set_person", "set_phase", "toggle_phase_lock", "toggle_digit_mode",
                "adjust_latency", "adjust_move_steps", "set_gripper_map", "set_prompt"}
    MOTION = {"toggle_single_step", "step"}

    def __init__(self, *, operator_timeout_s=300.0, auto_stop=False, bridge_driver=None,
                 control_client=None, **bridge_config):
        super().__init__(operator_timeout_s=operator_timeout_s)
        if type(auto_stop) is not bool:
            raise ValueError("robot.auto_stop must be boolean")
        self.auto_stop = auto_stop
        self.bridge = bridge_driver or RobotBridgeRobotDriver(**bridge_config)
        # Dashboard polling/logs must not wait behind a lifecycle command.
        self._control = control_client or BridgeClient(
            self.bridge.scheduler_url, timeout_s=bridge_config.get("timeout_s", 5.0),
            json_protocol=True)
        self._last_operation = None
        self._execute_args = None
        self._operator_stop_ids = set()
        self._requested_controls = set()
        self._home_requested = False
        self._start_requested = set()

    def execute(self, request, execution):
        self._execute_args = (request, execution)
        return super().execute(request, execution)

    def acknowledge(self, request_id):
        raise ValueError("manual_bridge requires /manual/action; acknowledgement cannot complete a robot command")

    def request_start(self, execution_id):
        with self._lock:
            self._start_requested.add(execution_id)

    def request_stop(self, execution_id):
        with self._lock:
            self._operator_stop_ids.add(execution_id)

    def request_home(self):
        with self._lock:
            self._home_requested = True

    def recover(self, *, allow_adjustment=True):
        with self._lock:
            if self._active_id is not None:
                raise RuntimeError("stop the task before recovery")
        return self._wait("recover", self._last_stopped, self._instruction, allow_adjustment=allow_adjustment)

    def request_action(self, request_id, control=None):
        with self._lock:
            if (control is None and request_id in self._acknowledged) or (request_id, control) in self._requested_controls:
                return {"accepted": True, "already_requested": True, "request_id": request_id}
            pending = self._pending
            if pending is None or pending.request_id != request_id or pending.error:
                raise ValueError("manual request expired or does not match the pending action")
            control = control or {"execute": "autonomous", "stop": "idle", "reset": "homing", "recover": "homing"}[pending.action]
            allowed = self.pending_controls(pending)
            if control not in allowed:
                raise ValueError("VLA control is unavailable in the current loop phase")
            self._acknowledged.add(request_id)
            self._requested_controls.add((request_id, control))
            finishing = pending.phase == "adjusting"
            if not finishing:
                pending.choice = control
            pending.phase = "finishing" if finishing else "queued"
            pending.clicked.set()
            return {"accepted": True, "request_id": request_id}

    @staticmethod
    def pending_controls(pending):
        if pending.phase == "adjusting":
            return ["idle"]
        if pending.phase != "waiting":
            return []
        return {"execute": ["autonomous"], "stop": ["idle"], "reset": ["homing"],
                "recover": ["homing"] + (["teleop"] if pending.allow_adjustment else [])}[pending.action]

    def _wait(self, action, execution_id, instruction, *, allow_adjustment=False):
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
                pending = _ControlRequest(action, execution_id, instruction, allow_adjustment=allow_adjustment)
                self._pending = pending
                if action == "execute" and execution_id in self._start_requested:
                    self._start_requested.remove(execution_id)
                    pending.choice, pending.phase = "autonomous", "queued"
                    pending.clicked.set()
                elif action == "stop" and (self.auto_stop or execution_id in self._operator_stop_ids):
                    pending.trigger = "operator" if execution_id in self._operator_stop_ids else "automatic"
                    pending.choice, pending.phase = "idle", "queued"
                    pending.clicked.set()
                elif action in {"reset", "recover"} and self._home_requested:
                    self._home_requested = False
                    pending.choice, pending.phase = "homing", "queued"
                    pending.clicked.set()
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
                elif action == "recover" and pending.choice == "teleop":
                    self.bridge.begin_adjustment(cancelled=pending.cancelled)
                    with self._lock:
                        if pending.error:
                            raise RuntimeError(pending.error)
                        pending.clicked.clear()
                        pending.phase = "adjusting"
                    if not pending.clicked.wait(self.operator_timeout_s):
                        raise RuntimeError("teleoperation adjustment timed out; returning to idle")
                    self.bridge._check_cancelled(pending.cancelled)
                    result = self.bridge.end_adjustment(cancelled=pending.cancelled)
                else:
                    result = self.bridge.reset(cancelled=pending.cancelled)
                    if action == "recover":
                        result.update(recovered=True, recovery_method="homing", homed=True)
                with self._lock:
                    if pending.error:
                        raise RuntimeError(pending.error)
                    pending.result = {**result, "manual": True, "provider": "manual_bridge",
                                      "operator_triggered": pending.trigger == "operator", "request_id": pending.request_id}
                    pending.phase = "completed"
            except Exception as exc:
                if action == "recover" and pending.choice == "teleop":
                    # Never leave teleop running after a timeout/cancel/error.
                    try:
                        self.bridge.stop(execution_id)
                    except Exception as stop_exc:
                        exc = RuntimeError(f"{exc}; adjustment stop failed: {stop_exc}")
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
            self._home_requested = False
            if execution_id is not None:
                self._start_requested.discard(execution_id)
            else:
                self._start_requested.clear()
            if execution_id is not None:
                self._cancelled_ids.add(execution_id)
            pending = self._pending
            if pending and pending.action in {"execute", "reset", "recover"} and (
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
            request = self._execute_args[0] if self._execute_args else None
        selection = None
        error = None
        if request:
            try:
                selection = self.bridge.prompt_selection(request, state)
            except ValueError as exc:
                error = str(exc)
        return {"state": state, "selection": selection, "selection_error": error,
                "prompt_mode": self.bridge.prompt_mode}

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
        if name == "toggle_recording":
            starting = not state.get("recording")
            person = args.get("person", state.get("person"))
            if starting and "person" in args:
                if not isinstance(person, str) or len(person) > 64:
                    raise ValueError("recording person must be text (maximum 64 characters)")
                self.bridge._action(self._control, "set_person", {"person": person}, token=cancelled)
            # Use the original Scheduler API and naming rule. Instruction is
            # Runtime export metadata only; never a Scheduler recording argument.
            result = self.bridge._action(self._control, name, {}, token=cancelled)
            if starting and result.get("recording_info"):
                result = {**result, "recording_info": {**result["recording_info"],
                    "instruction": args.get("instruction"), "person": person, "model_name": state.get("model_name")}}
            return result
        return self.bridge._action(self._control, name, args, token=cancelled)

    def scheduler_log(self, target, lines):
        if target not in {"scheduler", "policy", "robot", "master"} or not 1 <= lines <= 1000:
            raise ValueError("invalid log target or line count")
        return self.bridge._call(self._control, {"cmd": "get_log", "target": target, "lines": lines})

    def status(self):
        with self._lock:
            return {**super().status(), "provider": "manual_bridge", "control_mode": "bridge",
                    "scheduler_url": self.bridge.scheduler_url, "robot_url": self.bridge.robot_url,
                    "auto_stop": self.auto_stop,
                    "pending_controls": self.pending_controls(self._pending) if self._pending else [],
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
