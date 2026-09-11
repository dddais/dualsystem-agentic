"""Read a ready-stage target through the Robot Runtime manual page."""

import json
import time
import urllib.error
import urllib.request
from uuid import uuid4
from dualsystem_agentic.task_input import TaskInput


class WebTargetInput:
    def __init__(self, runtime_url, instruction_template, *, last_target="", timeout=5.0,
                 instruction_templates=None, allow_full_instruction=False):
        if not runtime_url.startswith(("http://", "https://")):
            raise ValueError("web input requires an HTTP Robot Runtime URL")
        self.url = runtime_url.rstrip("/")
        self.instruction_template = instruction_template
        self.last_target = last_target
        self.timeout = timeout
        self.instruction_templates = instruction_templates or {}
        self.allow_full_instruction = allow_full_instruction

    def _request(self, method, path, payload=None):
        request = urllib.request.Request(self.url + path, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"web input HTTP {exc.code}: {detail}") from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"web input connection failed: {exc}") from exc
        if result.get("success") is not True:
            raise RuntimeError(result.get("message", "web input failed"))
        return result["data"]

    def __call__(self, prompt):
        request_id = uuid4().hex
        path = f"/manual/input/{request_id}"
        print(f"[ready] 在 {self.url}/manual 等待网页任务；已保存指令可复用，Ctrl+C 退出", flush=True)
        try:
            self._request("POST", "/manual/input/open", {"request_id": request_id,
                "instruction_template": self.instruction_template, "last_target": self.last_target,
                "instruction_templates": self.instruction_templates,
                "allow_full_instruction": self.allow_full_instruction})
            while True:
                result = self._request("GET", path)
                if result.get("task") is not None:
                    task = TaskInput.from_payload(result["task"])
                    if task.target:
                        self.last_target = task.target
                    return task
                if result.get("target") is not None:
                    self.last_target = result["target"]
                    return self.last_target
                time.sleep(0.5)
        finally:
            try:
                self._request("DELETE", path)
            except Exception:
                # The lease expires if the Runtime is disconnected.
                pass
