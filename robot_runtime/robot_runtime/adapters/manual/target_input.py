"""One leased ready prompt, shared by the loop and the operator page."""

from copy import deepcopy
from string import Formatter
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

    def open(self, request_id, instruction_template, last_target, *,
             instruction_templates=None, allow_full_instruction=False):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("request_id is required (maximum 128 characters)")
        templates = _templates(instruction_template, instruction_templates)
        if not isinstance(allow_full_instruction, bool):
            raise ValueError("allow_full_instruction must be boolean")
        if not isinstance(last_target, str):
            raise ValueError("last_target must be text")
        with self._lock:
            current = self._current()
            if current and current["request_id"] != request_id:
                raise ValueError("another loop is already waiting for a target")
            if not current:
                self._request = {"request_id": request_id, "instruction_template": instruction_template,
                                 "last_target": last_target, "target": None, "task": None,
                                 "instruction_templates": templates,
                                 "input_modes": ["template", "instruction"] if allow_full_instruction else ["template"]}
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
            if current["task"] is not None:
                raise ValueError("a task has already been submitted for this round")
            current["target"] = target
            return {"accepted": True, "request_id": request_id, "target": target}

    def submit_task(self, request_id, *, mode, template_id="default", target="",
                    instruction="", target_queries=None):
        with self._lock:
            current = self._current()
            if not current or current["request_id"] != request_id:
                raise ValueError("loop is not ready or this prompt has expired")
            if "instruction" not in current["input_modes"]:
                raise ValueError("restart an updated loop to enable instruction input")
            if mode == "template":
                if not isinstance(template_id, str) or template_id not in current["instruction_templates"]:
                    raise ValueError("unknown instruction template")
                if not isinstance(target, str) or len(target) > 200:
                    raise ValueError("target must be text (maximum 200 characters)")
                target = target.strip() or current["last_target"]
                if not target:
                    raise ValueError("enter a target object name")
                instruction = current["instruction_templates"][template_id].format(target=target)
                target_queries = [target]
            elif mode == "instruction":
                target, template_id = None, None
                if target_queries is not None:
                    if not isinstance(target_queries, list) or not 1 <= len(target_queries) <= 8 or any(
                        not isinstance(q, str) or not q.strip() or len(q) > 200 for q in target_queries
                    ):
                        raise ValueError("target_queries must contain 1..8 nonempty names (maximum 200 characters each)")
                    target_queries = list(dict.fromkeys(q.strip() for q in target_queries))
            else:
                raise ValueError("mode must be template or instruction")
            if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
                raise ValueError("instruction must be nonempty text (maximum 2000 characters)")
            task = {"mode": mode, "template_id": template_id, "target": target,
                    "instruction": instruction.strip(), "target_queries": target_queries}
            if current["task"] is not None:
                if current["task"] != task:
                    raise ValueError("a task has already been submitted for this round")
            elif current["target"] is not None:
                raise ValueError("a target has already been submitted for this round")
            current["task"] = task
            return {"accepted": True, "request_id": request_id, "task": deepcopy(task)}

    def close(self, request_id):
        with self._lock:
            if self._request and self._request["request_id"] == request_id:
                self._request = None

    def status(self):
        # Browsers cannot renew the lease: a dead loop must stop accepting input.
        with self._lock:
            return deepcopy(self._current())


def _templates(default, extra):
    if extra is not None and (not isinstance(extra, dict) or len(extra) > 30):
        raise ValueError("instruction_templates must be a mapping of up to 30 named templates")
    if any(not isinstance(name, str) or not name.strip() or len(name) > 100 or name == "default"
           for name in (extra or {})):
        raise ValueError("template names must be nonempty text; default is reserved")
    templates = {"default": default, **(extra or {})}
    for template in templates.values():
        if not isinstance(template, str) or len(template) > 2000:
            raise ValueError("instruction templates must be text (maximum 2000 characters)")
        fields = list(Formatter().parse(template))
        if not any(field == "target" for _, field, _, _ in fields) or any(
            field is not None and (field != "target" or spec or conversion)
            for _, field, spec, conversion in fields
        ):
            raise ValueError("instruction_template must contain {target} and no other placeholders")
    return templates
