"""
dual_franka MCP Server (runtime HTTP adapter)
=============================================
STDIO MCP server that exposes the Dual-Franka Robot Runtime to the agentic loop.
All robot operations are forwarded to the runtime HTTP API:

- ``fetch_env``  -> robot/environment state over HTTP
- ``monitor``    -> subtask status over HTTP
- ``execute``    -> subtask execution plus initial monitor status/ids
- controls    -> stop/reset/emergency runtime calls over HTTP

The VLM sees these tools through the project registry as
``dual_franka___<tool_name>`` when using ``examples/config.dual_franka.runtime.yaml``.

Environment variables:
    DUAL_FRANKA_RUNTIME_URL      Runtime base URL (default: http://localhost:8767)
    DUAL_FRANKA_FETCH_ENV_PATH   Env/status path (default: /environment)
    DUAL_FRANKA_FETCH_ENV_METHOD Env/status method (default: GET)
    DUAL_FRANKA_FETCH_ENV_HTTP   Set true to forward fetch_env to the runtime.
    DUAL_FRANKA_MONITOR_PATH     Monitor path (default: /monitors/status)
    DUAL_FRANKA_MONITOR_METHOD   Monitor method (default: POST)
    DUAL_FRANKA_EXECUTE_PATH     Execute path (default: /executions)
    DUAL_FRANKA_EXECUTE_METHOD   Execute method (default: POST)
    DUAL_FRANKA_STOP_PATH        Stop path (default: /control/stop)
    DUAL_FRANKA_RESET_PATH       Reset path (default: /control/reset)
    DUAL_FRANKA_ENABLE_RESET     Set true to expose reset_task / recover_task (default: false)
    DUAL_FRANKA_ESTOP_PATH       Emergency stop path (default: /control/emergency_stop)
    DUAL_FRANKA_TIMEOUT_S        HTTP timeout seconds (default: 30)
    DUAL_FRANKA_UNKNOWN_STATUS   Fallback monitor status (default: running)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import anyio
import httpx
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger(__name__)

RUNTIME_BASE_URL = os.environ.get("DUAL_FRANKA_RUNTIME_URL") or "http://localhost:8767"
REQUEST_TIMEOUT_S = float(os.environ.get("DUAL_FRANKA_TIMEOUT_S") or 30.0)
UNKNOWN_STATUS = os.environ.get("DUAL_FRANKA_UNKNOWN_STATUS") or "running"

FETCH_ENV_PATH = os.environ.get("DUAL_FRANKA_FETCH_ENV_PATH") or "/environment"
FETCH_ENV_METHOD = os.environ.get("DUAL_FRANKA_FETCH_ENV_METHOD") or "GET"
FETCH_ENV_HTTP = (os.environ.get("DUAL_FRANKA_FETCH_ENV_HTTP") or "").lower() in {"1", "true", "yes", "on"}
MONITOR_PATH = os.environ.get("DUAL_FRANKA_MONITOR_PATH") or "/monitors/status"
MONITOR_METHOD = os.environ.get("DUAL_FRANKA_MONITOR_METHOD") or "POST"
EXECUTE_PATH = os.environ.get("DUAL_FRANKA_EXECUTE_PATH") or "/executions"
EXECUTE_METHOD = os.environ.get("DUAL_FRANKA_EXECUTE_METHOD") or "POST"
STOP_PATH = os.environ.get("DUAL_FRANKA_STOP_PATH") or "/control/stop"
RESET_PATH = os.environ.get("DUAL_FRANKA_RESET_PATH") or "/control/reset"
ESTOP_PATH = os.environ.get("DUAL_FRANKA_ESTOP_PATH") or "/control/emergency_stop"
ENABLE_RESET = (os.environ.get("DUAL_FRANKA_ENABLE_RESET") or "").lower() in {"1", "true", "yes", "on"}

_LAST_EXECUTION: dict[str, Any] = {}

app = Server("dual_franka_mcp_server")

# MCP 2.x removed these low-level decorators. Fail before registration with
# the interpreter and repair command, instead of an opaque AttributeError.
if not all(callable(getattr(app, name, None)) for name in ("list_tools", "call_tool")):
    try:
        sdk_version = version("mcp")
    except PackageNotFoundError:
        sdk_version = "unknown"
    raise RuntimeError(
        f"Incompatible MCP SDK {sdk_version} (Python: {sys.executable}): "
        "this server requires the MCP 1.x list_tools/call_tool decorators. "
        "Activate the loop environment and run "
        "python -m pip install 'mcp>=1.28.1,<2' from that environment; "
        "see docs/manual_start.md."
    )


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    tools: list[types.Tool] = []
    if FETCH_ENV_HTTP:
        tools.append(
            types.Tool(
                name="fetch_env",
                description=(
                    "Fetch structured scene graph/state for the dual-Franka planner. "
                    "Use only when a real structured scene provider is configured; "
                    "images are already fetched separately by the HTTP DataLoader."
                ),
                inputSchema={
                    "type": "object",
                    "required": [],
                    "properties": {
                        "include_status": {
                            "type": "boolean",
                            "description": "Optional runtime hint to include robot status in the environment payload.",
                        }
                    },
                },
            )
        )

    tools.extend(
        [
        types.Tool(
            name="monitor",
            description="Check current dual-Franka subtask status over HTTP; returns running / success / failed.",
            inputSchema={
                "type": "object",
                "required": [],
                "properties": {
                    "subtask": {"type": "string", "description": "Current subtask text for consistency checks."},
                    "subtask_index": {"type": "integer", "description": "0-based subtask index."},
                    "execution_id": {"type": "string", "description": "Optional runtime execution identifier."},
                    "monitor_id": {"type": "string", "description": "Optional runtime monitor identifier."},
                    "task_id": {"type": "string", "description": "Legacy robot-side task identifier."},
                },
            },
        ),
        types.Tool(
            name="execute",
            description=(
                "Start one dual-Franka subtask and return initial monitor status/ids."
            ),
            inputSchema={
                "type": "object",
                "required": ["subtask"],
                "properties": {
                    "subtask": {"type": "string", "description": "Subtask instruction to execute."},
                    "subtask_index": {"type": "integer"},
                    "execution_id": {"type": "string", "description": "Client execution ID for cancellation and idempotent retries."},
                    "target_queries": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "string"}},
                    "task": {"type": "string", "description": "Optional long-horizon task context."},
                    "left_arm": {"type": "string", "description": "Optional left-arm role/hint."},
                    "right_arm": {"type": "string", "description": "Optional right-arm role/hint."},
                    "bimanual_mode": {"type": "string", "description": "Optional bimanual coordination mode."},
                    "metadata": {"type": "object", "description": "Optional metadata passed through to the runtime."},
                    "options": {"type": "object", "description": "Optional execution options passed through to the runtime."},
                    "payload": {
                        "type": "object",
                        "description": "Runtime-specific fields merged into the execute request, overriding defaults.",
                    },
                },
            },
        ),
        types.Tool(
            name="stop_task",
            description="Stop the current dual-Franka task over HTTP.",
            inputSchema={"type": "object", "required": [], "properties": {"execution_id": {"type": "string"}}},
        ),
        types.Tool(
            name="emergency_stop",
            description="Emergency stop the dual-Franka runtime over HTTP.",
            inputSchema={"type": "object", "required": [], "properties": {}},
        ),
        ]
    )
    if ENABLE_RESET:
        tools.append(types.Tool(
            name="recover_task", description="After stop, wait for homing or operator teleoperation adjustment; optional Back stays in recovery. Returns recovered=true only after homing/adjustment.",
            inputSchema={"type": "object", "properties": {"execution_id": {"type": "string"}}}))
        tools.append(
            types.Tool(
                name="reset_task",
                description="Reset dual-Franka task/arms over HTTP.",
                inputSchema={"type": "object", "required": [], "properties": {}},
            )
        )
    return tools


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    async with httpx.AsyncClient(
        base_url=RUNTIME_BASE_URL,
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
        return await _request(client, "POST", STOP_PATH, json_data=arguments or None)
    if name == "reset_task" and ENABLE_RESET:
        return await _request(client, "POST", RESET_PATH)
    if name == "recover_task" and ENABLE_RESET:
        return await _request(client, "POST", "/control/recover", json_data=arguments or None)
    if name == "emergency_stop":
        return await _request(client, "POST", ESTOP_PATH)
    raise ValueError(f"unknown tool: {name}")


async def _fetch_env(client: httpx.AsyncClient, arguments: dict) -> dict:
    if not FETCH_ENV_HTTP:
        return {
            "agentic_role": "fetch_env",
            "environment": {},
            "message": (
                "No structured scene JSON provider is configured for dual-Franka. "
                "Use the attached VLM images for visual observations."
            ),
        }

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
    arguments = _hydrate_monitor_arguments(arguments)
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
        "execution_id": data.get("execution_id", arguments.get("execution_id") or arguments.get("task_id")),
        "monitor_id": data.get("monitor_id", arguments.get("monitor_id")),
        "task_id": data.get("task_id", arguments.get("task_id")),
        "progress": data.get("progress"),
        "updated_at": data.get("updated_at"),
        "error": data.get("error"),
        "poll_count": data.get("poll_count"),
        "result": data.get("result", {}),
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
    if execute_data.get("executed") is False or _derive_monitor_status(execute_data) == "failed":
        monitor_data = _monitor_data_from_failed_execute(arguments, str(subtask), execute_data)
    else:
        monitor_data = await _monitor(client, _build_execute_monitor_payload(arguments, str(subtask), execute_data))
    monitor_status = str(monitor_data.get("status") or "running")
    execution_id = execute_data.get("execution_id") or execute_data.get("id") or execute_data.get("task_id")
    monitor_id = execute_data.get("monitor_id") or monitor_data.get("monitor_id")
    _remember_execution(
        {
            "subtask": subtask,
            "subtask_index": monitor_data.get("subtask_index", arguments.get("subtask_index")),
            "execution_id": execution_id,
            "monitor_id": monitor_id,
            "task_id": monitor_data.get("task_id", arguments.get("task_id")) or execution_id,
        }
    )
    return {
        "agentic_role": "execute",
        "executed": bool(execute_data.get("executed", True)),
        "subtask": subtask,
        "subtask_index": monitor_data.get("subtask_index", arguments.get("subtask_index")),
        "execution_id": execution_id,
        "monitor_id": monitor_id,
        "task_id": monitor_data.get("task_id", arguments.get("task_id")) or execution_id,
        "status": monitor_status,
        "monitor_status": monitor_status,
        "progress": monitor_data.get("progress"),
        "error": monitor_data.get("error"),
        "poll_count": monitor_data.get("poll_count"),
        "result": monitor_data.get("result", {}),
        "execute": execute_data,
        "monitor": monitor_data,
    }


def _monitor_data_from_failed_execute(arguments: dict, subtask: str, execute_data: dict) -> dict:
    return {
        "status": "failed",
        "subtask": execute_data.get("subtask", subtask),
        "subtask_index": execute_data.get("subtask_index", arguments.get("subtask_index")),
        "execution_id": execute_data.get("execution_id") or execute_data.get("id") or arguments.get("execution_id"),
        "monitor_id": execute_data.get("monitor_id") or arguments.get("monitor_id"),
        "task_id": execute_data.get("task_id", arguments.get("task_id")),
        "progress": execute_data.get("progress"),
        "updated_at": execute_data.get("updated_at"),
        "error": execute_data.get("error"),
        "monitor": execute_data,
    }


def _remember_execution(data: dict) -> None:
    for key in ("subtask", "subtask_index", "execution_id", "monitor_id", "task_id"):
        value = data.get(key)
        if value is not None:
            _LAST_EXECUTION[key] = value


def _hydrate_monitor_arguments(arguments: dict) -> dict:
    payload = dict(arguments or {})
    if "execution_id" not in payload and _LAST_EXECUTION.get("execution_id"):
        payload["execution_id"] = _LAST_EXECUTION["execution_id"]
    if "monitor_id" not in payload and _LAST_EXECUTION.get("monitor_id"):
        payload["monitor_id"] = _LAST_EXECUTION["monitor_id"]
    if "task_id" not in payload and _LAST_EXECUTION.get("task_id"):
        payload["task_id"] = _LAST_EXECUTION["task_id"]
    return payload


def _build_execute_payload(arguments: dict) -> dict:
    subtask = arguments["subtask"]
    payload = {
        "subtask": subtask,
        "prompt": subtask,
        "instruction": subtask,
    }
    for key in ("task", "subtask_index", "execution_id", "target_queries", "left_arm", "right_arm", "bimanual_mode", "metadata", "options"):
        value = arguments.get(key)
        if value is not None:
            payload[key] = value
    if isinstance(arguments.get("payload"), dict):
        payload.update(arguments["payload"])
    payload["subtask"] = subtask
    if arguments.get("subtask_index") is not None:
        payload["subtask_index"] = arguments["subtask_index"]
    for key in ("execution_id", "target_queries"):
        if arguments.get(key) is not None:
            payload[key] = arguments[key]
    return payload


def _build_execute_monitor_payload(arguments: dict, subtask: str, execute_data: dict | None = None) -> dict:
    payload = {"subtask": subtask}
    execute_data = execute_data or {}
    for key in ("subtask_index", "task_id", "execution_id", "monitor_id"):
        value = arguments.get(key)
        if value is not None:
            payload[key] = value
    for source_key, target_key in (
        ("execution_id", "execution_id"),
        ("id", "execution_id"),
        ("task_id", "task_id"),
        ("monitor_id", "monitor_id"),
    ):
        value = execute_data.get(source_key)
        if value is not None and target_key not in payload:
            payload[target_key] = value
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
    if text in {"running", "progress", "executing", "busy", "in_progress", "started", "active"}:
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
    safe_path = _safe_relative_path(path)
    try:
        response = await client.request(method, safe_path, json=json_data, params=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(_http_error_message(exc.response, method, safe_path)) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"{method} {safe_path} request failed: {exc}") from exc
    if not response.content:
        return {}
    payload = response.json()
    return _unwrap_runtime_response(payload)


def _unwrap_runtime_response(payload: Any) -> dict:
    if isinstance(payload, dict):
        if payload.get("success") is False or payload.get("ok") is False:
            raise RuntimeError(str(payload.get("message") or payload.get("error") or "runtime request failed"))
        if "data" in payload and isinstance(payload["data"], dict):
            return payload["data"]
        return payload
    return {"data": payload}


def _http_error_message(response: httpx.Response, method: str, path: str) -> str:
    status_code = getattr(response, "status_code", "unknown")
    url = str(getattr(response, "url", path))
    detail = _response_error_detail(response)
    message = f"{method} {url} returned HTTP {status_code}"
    if detail:
        message = f"{message}: {detail}"
    return message


def _response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        text = getattr(response, "text", "") or ""
        return text.strip()[:500]
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if message:
            return str(message)
        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get("message") or data.get("error")
            if nested:
                return str(nested)
    try:
        return json.dumps(payload, ensure_ascii=False)[:500]
    except TypeError:
        return str(payload)[:500]


def _safe_relative_path(path: str) -> str:
    if "://" in path or not path.startswith("/") or ".." in path.split("/"):
        raise ValueError(f"runtime path must be a safe relative path starting with '/': {path!r}")
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
