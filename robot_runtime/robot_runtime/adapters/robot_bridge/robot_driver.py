"""Operate existing scheduler controls, with configured settling delays."""

from __future__ import annotations

import threading
import time
from uuid import uuid4

from robot_runtime.core.types import ExecutionRequest, ExecutionState
from .clients import BridgeClient, duration


class RobotBridgeRobotDriver:
    def __init__(self, scheduler_url: str = "ws://127.0.0.1:8088", *,
                 robot_url: str = "ws://127.0.0.1:9946", timeout_s: float = 5.0,
                 prompt_map: dict | None = None, prompt_mode: str = "fixed", stop_delay_s: float = 1.0,
                 reset_delay_s: float = 8.0, start_delay_s: float = 0.5,
                 back_timeout_s: float = 300.0,
                 scheduler_client=None, robot_client=None,
                 emergency_scheduler_client=None, emergency_robot_client=None):
        self.scheduler_url, self.robot_url = scheduler_url, robot_url
        self.stop_delay_s = duration(stop_delay_s, "stop_delay_s", allow_zero=True)
        self.reset_delay_s = duration(reset_delay_s, "reset_delay_s", allow_zero=True)
        self.start_delay_s = duration(start_delay_s, "start_delay_s", allow_zero=True)
        self.back_timeout_s = duration(back_timeout_s, "back_timeout_s")
        if prompt_map is not None and (not isinstance(prompt_map, dict) or any(
            not isinstance(k, str) or not k.strip() or isinstance(v, bool)
            or not isinstance(v, (str, int)) or (isinstance(v, int) and v < 0)
            for k, v in prompt_map.items()
        )):
            raise ValueError("prompt_map must map instructions to prompt strings or nonnegative indices")
        self.prompt_map = dict(prompt_map or {})
        if prompt_mode not in {"fixed", "text"}:
            raise ValueError("prompt_mode must be fixed or text")
        self.prompt_mode = prompt_mode
        self._scheduler = scheduler_client or BridgeClient(scheduler_url, timeout_s=timeout_s, json_protocol=True)
        self._robot = robot_client or BridgeClient(robot_url, timeout_s=timeout_s)
        self._emergency_scheduler = emergency_scheduler_client or BridgeClient(
            scheduler_url, timeout_s=timeout_s, json_protocol=True)
        self._emergency_robot = emergency_robot_client or BridgeClient(robot_url, timeout_s=timeout_s)
        self._lock = threading.RLock()
        self._pending = None
        self._cancelled_ids: set[str] = set()
        self._active_id: str | None = None
        self._last_state: dict = {}
        self._last_control: dict = {}

    @staticmethod
    def _call(client, request):
        result = client.call(request)
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise RuntimeError(f"robot-bridge command rejected: {result}")
        return result

    def _state(self, client):
        state = self._call(client, {"cmd": "status"}).get("state")
        if not isinstance(state, dict) or not isinstance(state.get("actions"), list):
            raise RuntimeError("scheduler status lacks state/actions")
        with self._lock:
            self._last_state = dict(state)
        return state

    def _action(self, client, name, args=None, token=None):
        self._check_cancelled(token)
        return self._call(client, {"cmd": "action", "name": name, "args": args or {}})

    @staticmethod
    def _check_cancelled(token):
        if token is not None and token.is_set():
            raise RuntimeError("robot-bridge operation cancelled")

    def _begin(self, action, execution_id, cancelled=None):
        with self._lock:
            if action == "execute" and execution_id in self._cancelled_ids:
                raise RuntimeError("execution cancelled before driver startup")
            token = cancelled if cancelled is not None else threading.Event()
            self._check_cancelled(token)
            self._pending = (action, execution_id, token)
            return token

    def _finish(self, token):
        with self._lock:
            if self._pending is not None and self._pending[2] is token:
                self._pending = None

    def cancel_pending(self, execution_id=None):
        """Runtime calls this before waiting for its driver lock."""
        with self._lock:
            if execution_id is not None:
                self._cancelled_ids.add(execution_id)
            if self._pending and (execution_id is None or self._pending[1] == execution_id):
                self._pending[2].set()

    def _park(self, scheduler, robot, token=None):
        errors = []
        try:
            state = self._state(scheduler)
            actions = state["actions"]
            if "cancel_back" in actions and state.get("back", {}).get("phase") in {"queued", "running"}:
                self._action(scheduler, "cancel_back", token=token)
            if "set_mode" in actions:
                self._action(scheduler, "set_mode", {"mode": "idle"}, token)
            elif "toggle_single_step" in actions and isinstance(state.get("single_step"), bool):
                if not state["single_step"]:
                    self._action(scheduler, "toggle_single_step", token=token)
            else:
                raise RuntimeError("scheduler does not support idle or single-step pause")
        except Exception as exc:
            errors.append(f"pause: {exc}")
        # The stock scheduler applies controls at iteration boundaries. Clear
        # again after the configured grace period to discard a last in-flight
        # chunk. This is a timed soft stop, not physical motion verification.
        for attempt in range(2):
            self._check_cancelled(token)
            try:
                self._call(robot, {"cmd": "clear_actions"})
            except Exception as exc:
                errors.append(f"clear_actions: {exc}")
            if attempt == 0:
                if token is None:
                    time.sleep(self.stop_delay_s)
                else:
                    token.wait(self.stop_delay_s)
        # Even when scheduler control is down, attempt to empty the robot's
        # queue. Report failure so the loop does not proceed to homing.
        if errors:
            raise RuntimeError("; ".join(errors))

    def _prompt_index(self, request, state):
        prompts = state.get("prompts")
        if "set_prompt" not in state["actions"] or not isinstance(prompts, list) or not prompts:
            raise ValueError("scheduler must expose a fixed prompts list and set_prompt")
        selection = self.prompt_map.get(request.subtask, request.subtask)
        if isinstance(selection, int):
            index = selection
        else:
            if selection not in prompts:
                raise ValueError(f"instruction is not in scheduler prompts or prompt_map: {request.subtask}")
            index = prompts.index(selection)
        if not 0 <= index < len(prompts):
            raise ValueError(f"prompt index out of range: {index}")
        return index, prompts[index]

    def prompt_selection(self, request, state):
        mode = request.options.get("prompt_mode", self.prompt_mode)
        if mode == "text":
            if "set_prompt_text" not in state["actions"]:
                raise ValueError("Scheduler lacks set_prompt_text; update robot-bridge and restart Scheduler for custom instructions")
            prompt = request.subtask
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
                raise ValueError("instruction must be nonempty text (maximum 2000 characters)")
            return {"mode": "text", "index": None, "prompt": prompt.strip(),
                    "action": "set_prompt_text", "args": {"prompt": prompt.strip()}}
        if mode != "fixed":
            raise ValueError("prompt_mode must be fixed or text")
        index, prompt = self._prompt_index(request, state)
        return {"mode": "fixed", "index": index, "prompt": prompt,
                "action": "set_prompt", "args": {"index": index}}

    def execute(self, request: ExecutionRequest, execution: ExecutionState, *, cancelled=None) -> dict:
        token = self._begin("execute", execution.execution_id, cancelled)
        try:
            state = self._state(self._scheduler)
            selection = self.prompt_selection(request, state)
            index, prompt = selection["index"], selection["prompt"]
            with self._lock:
                self._active_id = execution.execution_id
            self._park(self._scheduler, self._robot, token)
            self._action(self._scheduler, selection["action"], selection["args"], token)
            state = self._state(self._scheduler)
            if state.get("prompt") != prompt:
                raise RuntimeError("scheduler did not select the requested prompt")
            if "set_mode" in state["actions"]:
                self._action(self._scheduler, "set_mode", {"mode": "autonomous"}, token)
            elif state.get("single_step") is True:
                self._action(self._scheduler, "toggle_single_step", token=token)
            else:
                raise RuntimeError("scheduler was resumed externally during task startup")
            token.wait(self.start_delay_s)
            self._check_cancelled(token)
            return {"executed": True, "execution_id": execution.execution_id,
                    "prompt": prompt, "prompt_index": index, "provider": "robot_bridge",
                    "prompt_mode": selection["mode"],
                    "completion_basis": "command_and_delay", "wait_s": self.start_delay_s}
        finally:
            self._finish(token)

    def stop(self, execution_id=None) -> dict:
        self.cancel_pending(execution_id)
        self._park(self._scheduler, self._robot)
        with self._lock:
            self._active_id = None
            self._last_control = {"control": "stop", "execution_id": execution_id}
        return {"stopped": True, "execution_id": execution_id,
                "completion_basis": "command_and_delay", "wait_s": self.stop_delay_s}

    def reset(self, *, cancelled=None) -> dict:
        token = self._begin("reset", self._last_control.get("execution_id"), cancelled)
        try:
            state = self._state(self._scheduler)
            if "homing" not in state["actions"]:
                raise RuntimeError("scheduler does not support homing")
            self._action(self._scheduler, "homing", token=token)
            token.wait(self.reset_delay_s)
            self._check_cancelled(token)
            return {"reset": True, "completion_basis": "command_and_delay",
                    "wait_s": self.reset_delay_s, "home": "robot_bridge_homing"}
        finally:
            self._finish(token)

    def begin_adjustment(self, *, cancelled=None) -> dict:
        """Enter teleoperation only after Runtime has stopped the task."""
        state = self._state(self._scheduler)
        if "set_mode" not in state["actions"] or "teleop" not in state.get("modes", []):
            raise ValueError("scheduler does not support teleoperation adjustment")
        return self._action(self._scheduler, "set_mode", {"mode": "teleop"}, cancelled)

    def back(self, *, cancelled=None) -> dict:
        token = self._begin("back", self._last_control.get("execution_id"), cancelled)
        operation_id = uuid4().hex
        requested = False
        try:
            state = self._state(self._scheduler)
            if not {"back", "cancel_back"}.issubset(state["actions"]):
                raise RuntimeError("Scheduler/Robot Server does not support back; update robot-bridge")
            self._park(self._scheduler, self._robot, token)
            requested = True  # A lost response can still mean motion was accepted.
            self._action(self._scheduler, "back", {"operation_id": operation_id}, token)
            deadline = time.monotonic() + self.back_timeout_s
            while True:
                self._check_cancelled(token)
                state = self._state(self._scheduler).get("back", {})
                if state.get("operation_id") != operation_id:
                    raise RuntimeError("Scheduler returned a mismatched back operation")
                if state.get("phase") == "completed":
                    if state.get("back") is not True:
                        raise RuntimeError("back completion lacks execution acknowledgement")
                    token.wait(self.stop_delay_s)
                    self._check_cancelled(token)
                    return {**state, "recovered": True, "recovery_method": "back", "homed": False,
                            "completion_basis": "sdk_dispatch_and_delay", "wait_s": self.stop_delay_s}
                if state.get("phase") in {"failed", "cancelled"}:
                    raise RuntimeError(state.get("error", "back failed"))
                if state.get("phase") not in {"queued", "running"}:
                    raise RuntimeError("Scheduler returned an invalid back phase")
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"back timed out after {self.back_timeout_s}s")
                token.wait(0.1)
        except Exception as exc:
            if requested:
                # Independent connections remain available after a lost reply.
                errors = []
                try:
                    self._action(self._emergency_scheduler, "cancel_back", {"operation_id": operation_id})
                except Exception as cancel_exc:
                    errors.append(f"cancel_back: {cancel_exc}")
                try:
                    self._park(self._emergency_scheduler, self._emergency_robot)
                except Exception as stop_exc:
                    errors.append(f"stop: {stop_exc}")
                if errors:
                    raise RuntimeError(f"{exc}; cleanup failed: {'; '.join(errors)}") from exc
            raise
        finally:
            self._finish(token)

    def end_adjustment(self, *, cancelled=None) -> dict:
        self._park(self._scheduler, self._robot, cancelled)
        self._check_cancelled(cancelled)
        return {"recovered": True, "recovery_method": "teleop_adjustment", "homed": False,
                "completion_basis": "operator_and_stop_delay", "wait_s": self.stop_delay_s}

    def emergency_stop(self) -> dict:
        self.cancel_pending()
        self._park(self._emergency_scheduler, self._emergency_robot)
        with self._lock:
            self._last_control = {"control": "emergency_stop", "execution_id": self._active_id}
            self._active_id = None
        return {"emergency_stop": True, "hardware_estop": False,
                "completion_basis": "command_and_delay"}

    def status(self) -> dict:
        with self._lock:
            return {"provider": "robot_bridge", "scheduler_url": self.scheduler_url,
                    "prompt_mode": self.prompt_mode,
                    "robot_url": self.robot_url, "active_execution_id": self._active_id,
                    "pending_action": self._pending[0] if self._pending else None,
                    "last_scheduler_state": dict(self._last_state)}

    def capabilities(self) -> dict:
        return {"driver": "robot_bridge", "arms": ["left", "right"],
                "hardware_estop": False, "reset_completion": "fixed_delay", "extra_tools": []}

    def close(self):
        self.cancel_pending()
        for client in (self._scheduler, self._robot, self._emergency_scheduler, self._emergency_robot):
            client.close()
