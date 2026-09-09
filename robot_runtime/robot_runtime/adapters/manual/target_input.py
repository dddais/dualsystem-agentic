"""One leased ready prompt, shared by the loop and the operator page."""

from copy import deepcopy
import threading
import time


class ManualTargetInput:
    def __init__(self, *, lease_s=15.0, clock=time.monotonic):
        self._clock, self._lease_s = clock, lease_s
        self._lock = threading.Lock()
        self._request = None
        self._expires = 0.0

    def _current(self):
        if self._clock() >= self._expires:
            self._request = None
        return self._request

    def open(self, request_id, instruction_template, last_target):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("request_id is required (maximum 128 characters)")
        if not isinstance(instruction_template, str) or "{target}" not in instruction_template:
            raise ValueError("instruction_template must contain {target}")
        if not isinstance(last_target, str):
            raise ValueError("last_target must be text")
        with self._lock:
            current = self._current()
            if current and current["request_id"] != request_id:
                raise ValueError("another loop is already waiting for a target")
            if not current:
                self._request = {"request_id": request_id, "instruction_template": instruction_template,
                                 "last_target": last_target, "target": None}
            self._expires = self._clock() + self._lease_s
            return deepcopy(self._request)

    def poll(self, request_id):
        with self._lock:
            current = self._current()
            if not current or current["request_id"] != request_id:
                raise ValueError("ready prompt expired; restart the loop")
            self._expires = self._clock() + self._lease_s
            return deepcopy(current)

    def submit(self, request_id, target):
        if not isinstance(target, str) or len(target) > 200:
            raise ValueError("target must be text (maximum 200 characters)")
        with self._lock:
            current = self._current()
            if not current or current["request_id"] != request_id:
                raise ValueError("loop is not ready or this prompt has expired")
            target = target.strip() or current["last_target"]
            if not target or target.lower() in {"q", "quit", "exit"}:
                raise ValueError("enter a target object name")
            if current["target"] is not None and current["target"] != target:
                raise ValueError("a target has already been submitted for this round")
            current["target"] = target
            return {"accepted": True, "request_id": request_id, "target": target}

    def close(self, request_id):
        with self._lock:
            if self._request and self._request["request_id"] == request_id:
                self._request = None

    def status(self):
        # Browsers cannot renew the lease: a dead loop must stop accepting input.
        with self._lock:
            return deepcopy(self._current())
