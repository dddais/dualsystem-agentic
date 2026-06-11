"""
dual_franka MCP Server (HTTP adapter)
=====================================
STDIO MCP server that exposes a dual-Franka robot bridge to the agentic loop.
All robot operations are forwarded to HTTP endpoints:

- ``fetch_env``  -> robot/environment state over HTTP
- ``monitor``    -> subtask status over HTTP
- ``execute``    -> subtask execution, followed by an automatic monitor call
- controls/other -> stop/reset/emergency/raw bridge calls over HTTP

The VLM sees these tools through the project registry as
``dual_franka___<tool_name>`` when using ``examples/config.dual_franka.yaml``.

Environment variables:
    DUAL_FRANKA_BRIDGE_URL       Bridge base URL (default: http://localhost:8767)
    DUAL_FRANKA_FETCH_ENV_PATH   Env/status path (default: /environment)
    DUAL_FRANKA_FETCH_ENV_METHOD Env/status method (default: GET)
    DUAL_FRANKA_MONITOR_PATH     Monitor path (default: /task/monitor)
    DUAL_FRANKA_MONITOR_METHOD   Monitor method (default: POST)
    DUAL_FRANKA_EXECUTE_PATH     Execute path (default: /task/execute)
    DUAL_FRANKA_EXECUTE_METHOD   Execute method (default: POST)
    DUAL_FRANKA_STOP_PATH        Stop path (default: /task/stop)
    DUAL_FRANKA_RESET_PATH       Reset path (default: /task/reset)
    DUAL_FRANKA_ESTOP_PATH       Emergency stop path (default: /task/emergency_stop)
    DUAL_FRANKA_TIMEOUT_S        HTTP timeout seconds (default: 30)
    DUAL_FRANKA_UNKNOWN_STATUS   Fallback monitor status (default: running)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import anyio
import httpx
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger(__name__)

BRIDGE_BASE_URL = os.environ.get("DUAL_FRANKA_BRIDGE_URL") or "http://localhost:8767"
REQUEST_TIMEOUT_S = float(os.environ.get("DUAL_FRANKA_TIMEOUT_S") or 30.0)
UNKNOWN_STATUS = os.environ.get("DUAL_FRANKA_UNKNOWN_STATUS") or "running"

FETCH_ENV_PATH = os.environ.get("DUAL_FRANKA_FETCH_ENV_PATH") or "/environment"
FETCH_ENV_METHOD = os.environ.get("DUAL_FRANKA_FETCH_ENV_METHOD") or "GET"
MONITOR_PATH = os.environ.get("DUAL_FRANKA_MONITOR_PATH") or "/task/monitor"
MONITOR_METHOD = os.environ.get("DUAL_FRANKA_MONITOR_METHOD") or "POST"
EXECUTE_PATH = os.environ.get("DUAL_FRANKA_EXECUTE_PATH") or "/task/execute"
EXECUTE_METHOD = os.environ.get("DUAL_FRANKA_EXECUTE_METHOD") or "POST"
STOP_PATH = os.environ.get("DUAL_FRANKA_STOP_PATH") or "/task/stop"
RESET_PATH = os.environ.get("DUAL_FRANKA_RESET_PATH") or "/task/reset"
ESTOP_PATH = os.environ.get("DUAL_FRANKA_ESTOP_PATH") or "/task/emergency_stop"

app = Server("dual_franka_mcp_server")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="fetch_env",
            description=(
                "Fetch the latest dual-Franka robot/environment state over HTTP. "
                "Images are fetched separately by the configured HTTP DataLoader."
            ),
            inputSchema={
                "type": "object",
                "required": [],
                "properties": {
                    "include_status": {
                        "type": "boolean",
                        "description": "Optional bridge hint to include robot status in the environment payload.",
                    }
                },
            },
        ),
        types.Tool(
            name="monitor",
            description="Check current dual-Franka subtask status over HTTP; returns running / success / failed.",
            inputSchema={
                "type": "object",
                "required": [],
                "properties": {
                    "subtask": {"type": "string", "description": "Current subtask text for consistency checks."},
                    "subtask_index": {"type": "integer", "description": "0-based subtask index."},
                    "task_id": {"type": "string", "description": "Optional robot-side task identifier."},
                },
            },
        ),
        types.Tool(
            name="execute",
            description=(
                "Execute one subtask on the dual-Franka HTTP bridge, then "
                "automatically trigger monitor for the same subtask."
            ),
            inputSchema={
                "type": "object",
                "required": ["subtask"],
                "properties": {
                    "subtask": {"type": "string", "description": "Subtask instruction to execute."},
                    "task": {"type": "string", "description": "Optional long-horizon task context."},
                    "left_arm": {"type": "string", "description": "Optional left-arm role/hint."},
                    "right_arm": {"type": "string", "description": "Optional right-arm role/hint."},
                    "bimanual_mode": {"type": "string", "description": "Optional bimanual coordination mode."},
                    "metadata": {"type": "object", "description": "Optional metadata passed through to the bridge."},
                    "options": {"type": "object", "description": "Optional execution options passed through to the bridge."},
                    "payload": {
                        "type": "object",
                        "description": "Bridge-specific fields merged into the execute request, overriding defaults.",
                    },
                },
            },
        ),
        types.Tool(
            name="stop_task",
            description="Stop the current dual-Franka task over HTTP.",
            inputSchema={"type": "object", "required": [], "properties": {}},
        ),
        types.Tool(
            name="reset_task",
            description="Reset dual-Franka task/arms over HTTP.",
            inputSchema={"type": "object", "required": [], "properties": {}},
        ),
        types.Tool(
            name="emergency_stop",
            description="Emergency stop the dual-Franka bridge over HTTP.",
            inputSchema={"type": "object", "required": [], "properties": {}},
        ),
        types.Tool(
            name="call_bridge",
            description=(
                "Call another relative HTTP endpoint on the dual-Franka bridge. "
                "Use for robot-specific utilities not modeled as standard tools."
            ),
            inputSchema={
                "type": "object",
                "required": ["method", "path"],
                "properties": {
                    "method": {"type": "string", "description": "HTTP method: GET, POST, PUT, PATCH, DELETE."},
                    "path": {"type": "string", "description": "Relative bridge path, e.g. /gripper/open."},
                    "body": {"type": "object", "description": "Optional JSON request body."},
                    "query": {"type": "object", "description": "Optional query parameters."},
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    async with httpx.AsyncClient(
        base_url=BRIDGE_BASE_URL,
        timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
        headers={"Content-Type": "application/json"},
    ) as client:
        data = await _dispatch(client, name, arguments or {})
    return [types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))]


async def _dispatch(client: httpx.AsyncClient, name: str, arguments: dict) -> dict:
    if name == "fetch_env":
        return await _fetch_env(client, arguments)
    if name == "monitor":
        return await _monitor(client, arguments)
    if name == "execute":
        return await _execute(client, arguments)
    if name == "stop_task":
        return await _request(client, "POST", STOP_PATH)
    if name == "reset_task":
        return await _request(client, "POST", RESET_PATH)
    if name == "emergency_stop":
        return await _request(client, "POST", ESTOP_PATH)
    if name == "call_bridge":
        return await _call_bridge(client, arguments)
    raise ValueError(f"unknown tool: {name}")


async def _fetch_env(client: httpx.AsyncClient, arguments: dict) -> dict:
    payload = {"include_status": arguments.get("include_status")} if arguments else None
    data = await _request(
        client,
        FETCH_ENV_METHOD,
        FETCH_ENV_PATH,
        json_data=payload if _method_has_body(FETCH_ENV_METHOD) else None,
        params=payload if not _method_has_body(FETCH_ENV_METHOD) else None,
    )
    return {"agentic_role": "fetch_env", **data}


async def _monitor(client: httpx.AsyncClient, arguments: dict) -> dict:
    data = await _request(
        client,
        MONITOR_METHOD,
        MONITOR_PATH,
        json_data=arguments if _method_has_body(MONITOR_METHOD) else None,
        params=arguments if not _method_has_body(MONITOR_METHOD) else None,
    )
    status = _derive_monitor_status(data)
    return {
        "status": status,
        "subtask": data.get("subtask", arguments.get("subtask")),
        "subtask_index": data.get("subtask_index", arguments.get("subtask_index")),
        "task_id": data.get("task_id", arguments.get("task_id")),
        "monitor": data,
    }


async def _execute(client: httpx.AsyncClient, arguments: dict) -> dict:
    subtask = arguments.get("subtask")
    if not subtask:
        raise ValueError("execute requires a 'subtask'")
    request_payload = _build_execute_payload(arguments)
    execute_data = await _request(
        client,
        EXECUTE_METHOD,
        EXECUTE_PATH,
        json_data=request_payload if _method_has_body(EXECUTE_METHOD) else None,
        params=request_payload if not _method_has_body(EXECUTE_METHOD) else None,
    )
    monitor_data = await _monitor(client, _build_execute_monitor_payload(arguments, str(subtask)))
    monitor_status = str(monitor_data.get("status") or "running")
    return {
        "agentic_role": "execute",
        "executed": bool(execute_data.get("executed", True)),
        "subtask": subtask,
        "subtask_index": monitor_data.get("subtask_index", arguments.get("subtask_index")),
        "task_id": monitor_data.get("task_id", arguments.get("task_id")),
        "status": monitor_status,
        "monitor_status": monitor_status,
        "execute": execute_data,
        "monitor": monitor_data,
    }


async def _call_bridge(client: httpx.AsyncClient, arguments: dict) -> dict:
    method = str(arguments.get("method") or "").upper()
    path = _safe_relative_path(str(arguments.get("path") or ""))
    body = arguments.get("body") if isinstance(arguments.get("body"), dict) else None
    query = arguments.get("query") if isinstance(arguments.get("query"), dict) else None
    return await _request(client, method, path, json_data=body, params=query)


def _build_execute_payload(arguments: dict) -> dict:
    subtask = arguments["subtask"]
    payload = {
        "subtask": subtask,
        "prompt": subtask,
        "instruction": subtask,
    }
    for key in ("task", "left_arm", "right_arm", "bimanual_mode", "metadata", "options"):
        value = arguments.get(key)
        if value is not None:
            payload[key] = value
    if isinstance(arguments.get("payload"), dict):
        payload.update(arguments["payload"])
    return payload


def _build_execute_monitor_payload(arguments: dict, subtask: str) -> dict:
    payload = {"subtask": subtask}
    for key in ("subtask_index", "task_id"):
        value = arguments.get(key)
        if value is not None:
            payload[key] = value
    return payload


def _derive_monitor_status(data: dict) -> str:
    for key in ("status", "task_status", "execution_status", "state"):
        value = data.get(key)
        if value is None:
            continue
        normalized = _normalize_status_text(str(value))
        if normalized:
            return normalized

    if data.get("running") is True or data.get("is_running") is True:
        return "running"
    if data.get("success") is True or data.get("completed") is True:
        return "success"
    if data.get("error") or data.get("failed") is True:
        return "failed"

    current_step = _optional_int(data.get("current_step") or data.get("step"))
    total_steps = _optional_int(data.get("total_steps") or data.get("max_steps"))
    if current_step is not None and total_steps is not None and total_steps > 0:
        return "success" if current_step >= total_steps else "running"

    normalized_fallback = _normalize_status_text(UNKNOWN_STATUS)
    return normalized_fallback or "running"


def _normalize_status_text(value: str) -> str | None:
    text = value.strip().lower()
    if text in {"running", "executing", "busy", "in_progress", "started", "active"}:
        return "running"
    if text in {"success", "succeeded", "done", "completed", "complete", "finished", "idle_success"}:
        return "success"
    if text in {"fail", "failed", "failure", "error", "aborted", "cancelled", "canceled", "stopped"}:
        return "failed"
    return None


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    json_data: dict | None = None,
    params: dict | None = None,
) -> dict:
    method = method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError(f"unsupported HTTP method: {method}")
    response = await client.request(method, _safe_relative_path(path), json=json_data, params=params)
    response.raise_for_status()
    if not response.content:
        return {}
    payload = response.json()
    return _unwrap_bridge_response(payload)


def _unwrap_bridge_response(payload: Any) -> dict:
    if isinstance(payload, dict):
        if payload.get("success") is False or payload.get("ok") is False:
            raise RuntimeError(str(payload.get("message") or payload.get("error") or "bridge request failed"))
        if "data" in payload and isinstance(payload["data"], dict):
            return payload["data"]
        return payload
    return {"data": payload}


def _safe_relative_path(path: str) -> str:
    if "://" in path or not path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"bridge path must be a safe relative path starting with '/': {path!r}")
    return path


def _method_has_body(method: str) -> bool:
    return method.upper() in {"POST", "PUT", "PATCH", "DELETE"}


def _optional_int(value: object | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    async def arun() -> None:
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    anyio.run(arun)
    return 0


if __name__ == "__main__":
    sys.exit(main())
