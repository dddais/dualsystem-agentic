"""HTTP executor adapter: POST the current subtask to an external VLA/robot service."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from dualsystem_agentic.core.types import ExecutorInput, ExecutorOutput


class HTTPExecutorClient:
    """Forward a subtask to an external VLA/robot endpoint over HTTP."""

    def __init__(self, endpoint: str, timeout: float = 30.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    def execute(self, executor_input: ExecutorInput) -> ExecutorOutput:
        payload = executor_input_to_payload(executor_input)
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return ExecutorOutput.failure(f"HTTP {exc.code}: {body}")
        except urllib.error.URLError as exc:
            return ExecutorOutput.failure(str(exc))

        status = str(response_data.get("status", "ok"))
        return ExecutorOutput(
            status=status,
            data=response_data.get("data") or {},
            error=response_data.get("error"),
            raw_response=response_data,
        )


def executor_input_to_payload(executor_input: ExecutorInput) -> dict[str, Any]:
    return {
        "task": executor_input.task,
        "subtask": executor_input.subtask,
        "metadata": executor_input.metadata or {},
    }
