"""Tests for the dual_franka HTTP deployment adapter."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from dualsystem_agentic.io.dataloader import HTTPDataLoader


def _load_module(relative_path: str, name: str):
    path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_dual_franka_server_module():
    try:
        import mcp  # noqa: F401
    except ImportError:
        pytest.skip("mcp SDK is not installed")
    return _load_module(
        "mcp_server/dual_franka_mcp_server/server.py",
        "dual_franka_mcp_server_for_test",
    )


def test_dual_franka_monitor_status_normalization():
    server = _load_dual_franka_server_module()

    assert server._derive_monitor_status({"state": "executing"}) == "running"
    assert server._derive_monitor_status({"task_status": "completed"}) == "success"
    assert server._derive_monitor_status({"status": "fail"}) == "failed"
    assert server._derive_monitor_status({"error": "collision"}) == "failed"
    assert server._derive_monitor_status({"current_step": 2, "total_steps": 5}) == "running"
    assert server._derive_monitor_status({"current_step": 5, "total_steps": 5}) == "success"


def test_dual_franka_execute_payload_allows_runtime_specific_overrides():
    server = _load_dual_franka_server_module()

    payload = server._build_execute_payload(
        {
            "subtask": "pick up the cube",
            "task": "clean the table",
            "left_arm": "stabilize tray",
            "payload": {"priority": "high", "prompt": "custom prompt"},
        }
    )

    assert payload["subtask"] == "pick up the cube"
    assert payload["instruction"] == "pick up the cube"
    assert payload["task"] == "clean the table"
    assert payload["left_arm"] == "stabilize tray"
    assert payload["priority"] == "high"
    assert payload["prompt"] == "custom prompt"


def test_dual_franka_execute_identity_arguments_override_payload_passthrough():
    server = _load_dual_franka_server_module()

    payload = server._build_execute_payload(
        {
            "subtask": "pick bowl",
            "subtask_index": 0,
            "payload": {
                "subtask": "pick spoon",
                "subtask_index": 1,
                "prompt": "runtime specific prompt",
            },
        }
    )

    assert payload["subtask"] == "pick bowl"
    assert payload["subtask_index"] == 0
    assert payload["prompt"] == "runtime specific prompt"


def test_dual_franka_execute_returns_initial_monitor_status():
    server = _load_dual_franka_server_module()
    server._LAST_EXECUTION.clear()
    client = _RecordingHTTPClient(
        [
            {
                "success": True,
                "data": {
                    "executed": True,
                    "placeholder": True,
                    "execution_id": "exec-1",
                    "monitor_id": "mon-1",
                },
            },
            {
                "success": True,
                "data": {
                    "status": "running",
                    "subtask": "pick up the cube",
                    "subtask_index": 2,
                    "execution_id": "exec-1",
                    "monitor_id": "mon-1",
                },
            },
        ]
    )

    result = asyncio.run(
        server._execute(
            client,
            {
                "subtask": "pick up the cube",
                "subtask_index": 2,
            },
        )
    )

    assert [request["path"] for request in client.requests] == ["/executions", "/monitors/status"]
    assert client.requests[1]["json"]["subtask"] == "pick up the cube"
    assert client.requests[1]["json"]["subtask_index"] == 2
    assert client.requests[1]["json"]["execution_id"] == "exec-1"
    assert client.requests[1]["json"]["monitor_id"] == "mon-1"
    assert result["executed"] is True
    assert result["execution_id"] == "exec-1"
    assert result["monitor_id"] == "mon-1"
    assert result["status"] == "running"
    assert result["monitor_status"] == "running"
    assert result["monitor"]["monitor"]["subtask"] == "pick up the cube"


def test_dual_franka_monitor_uses_last_execution_ids_as_fallback():
    server = _load_dual_franka_server_module()
    server._LAST_EXECUTION.clear()
    client = _RecordingHTTPClient(
        [
            {
                "success": True,
                "data": {
                    "executed": True,
                    "placeholder": True,
                    "execution_id": "exec-1",
                    "monitor_id": "mon-1",
                },
            },
            {
                "success": True,
                "data": {
                    "status": "running",
                    "subtask": "pick up the cube",
                    "execution_id": "exec-1",
                    "monitor_id": "mon-1",
                },
            },
            {
                "success": True,
                "data": {
                    "status": "running",
                    "subtask": "pick up the cube",
                    "execution_id": "exec-1",
                    "monitor_id": "mon-1",
                },
            },
        ]
    )

    asyncio.run(server._execute(client, {"subtask": "pick up the cube"}))
    result = asyncio.run(server._monitor(client, {}))

    assert client.requests[-1]["path"] == "/monitors/status"
    assert client.requests[-1]["json"]["execution_id"] == "exec-1"
    assert client.requests[-1]["json"]["monitor_id"] == "mon-1"
    assert "subtask" not in client.requests[-1]["json"]
    assert "subtask_index" not in client.requests[-1]["json"]
    assert result["execution_id"] == "exec-1"
    assert result["monitor_id"] == "mon-1"


def test_dual_franka_execute_does_not_poll_monitor_after_failed_start():
    server = _load_dual_franka_server_module()
    server._LAST_EXECUTION.clear()
    client = _RecordingHTTPClient(
        [
            {
                "success": True,
                "data": {
                    "executed": False,
                    "status": "failed",
                    "execution_id": "exec-1",
                    "monitor_id": "mon-1",
                    "error": "remote monitor request failed",
                },
            },
        ]
    )

    result = asyncio.run(server._execute(client, {"subtask": "pick up the cube"}))

    assert [request["path"] for request in client.requests] == ["/executions"]
    assert result["executed"] is False
    assert result["status"] == "failed"
    assert result["monitor_status"] == "failed"
    assert result["monitor"]["error"] == "remote monitor request failed"


def test_dual_franka_request_includes_runtime_error_body():
    server = _load_dual_franka_server_module()
    client = _RecordingHTTPClient(
        [
            {
                "success": False,
                "message": "remote monitor returned HTTP 404: unknown monitor_id",
            },
        ],
        status_codes=[500],
    )

    with pytest.raises(RuntimeError, match="unknown monitor_id"):
        asyncio.run(server._request(client, "POST", "/monitors/status", json_data={"monitor_id": "mon-1"}))


def test_dual_franka_fetch_env_tool_is_hidden_until_http_provider_enabled():
    server = _load_dual_franka_server_module()
    original = server.FETCH_ENV_HTTP
    try:
        server.FETCH_ENV_HTTP = False
        tools = asyncio.run(server.list_tools())
        assert "fetch_env" not in {tool.name for tool in tools}

        server.FETCH_ENV_HTTP = True
        tools = asyncio.run(server.list_tools())
        assert "fetch_env" in {tool.name for tool in tools}
    finally:
        server.FETCH_ENV_HTTP = original


def test_dual_franka_runtime_adapter_does_not_expose_raw_http_tool():
    server = _load_dual_franka_server_module()

    tools = asyncio.run(server.list_tools())

    assert "call_bridge" not in {tool.name for tool in tools}


def test_dual_franka_reset_task_is_feature_flagged():
    server = _load_dual_franka_server_module()
    original = server.ENABLE_RESET
    try:
        server.ENABLE_RESET = False
        tools = asyncio.run(server.list_tools())
        assert "reset_task" not in {tool.name for tool in tools}

        server.ENABLE_RESET = True
        tools = asyncio.run(server.list_tools())
        assert "reset_task" in {tool.name for tool in tools}
    finally:
        server.ENABLE_RESET = original


def test_dual_franka_reset_task_dispatch_requires_feature_flag():
    server = _load_dual_franka_server_module()
    original = server.ENABLE_RESET
    client = _RecordingHTTPClient([])
    try:
        server.ENABLE_RESET = False
        with pytest.raises(ValueError, match="unknown tool"):
            asyncio.run(server._dispatch(client, "reset_task", {}))
        assert client.requests == []
    finally:
        server.ENABLE_RESET = original


def test_dual_franka_fetch_env_defaults_to_empty_structured_environment():
    server = _load_dual_franka_server_module()
    server.FETCH_ENV_HTTP = False
    client = _RecordingHTTPClient([])

    result = asyncio.run(server._fetch_env(client, {}))

    assert client.requests == []
    assert result["agentic_role"] == "fetch_env"
    assert result["environment"] == {}
    assert "No structured scene JSON provider" in result["message"]


def test_http_dataloader_accepts_wrapped_runtime_image_response():
    dataloader = HTTPDataLoader(url="http://unused", image_key="concatenated_image", label="main")
    frame = dataloader._parse_response(
        {
            "success": True,
            "data": {
                "concatenated_image": "x" * 120,
                "timestamp": 123.0,
            },
        }
    )

    assert frame is not None
    assert frame.images["main"].data == "x" * 120
    assert frame.timestamp == 123.0


class _RecordingHTTPClient:
    def __init__(self, responses: list[dict], *, status_codes: list[int] | None = None):
        self.responses = list(responses)
        self.status_codes = list(status_codes or [])
        self.requests: list[dict] = []

    async def request(self, method: str, path: str, *, json=None, params=None):
        self.requests.append({"method": method, "path": path, "json": json, "params": params})
        status_code = self.status_codes.pop(0) if self.status_codes else 200
        return _FakeHTTPResponse(self.responses.pop(0), status_code=status_code)


class _FakeHTTPResponse:
    def __init__(self, payload: dict, *, status_code: int = 200):
        self._payload = payload
        self.content = b"{}"
        self.status_code = status_code
        self.url = "http://runtime.local/test"
        self.request = None
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", str(self.url))
            raise httpx.HTTPStatusError(
                f"{self.status_code} error",
                request=request,
                response=self,
            )

    def json(self) -> dict:
        return self._payload
