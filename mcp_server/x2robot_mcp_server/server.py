"""
x2robot MCP Server (dualsystem-agentic adapter)
===============================================
A real MCP server (STDIO, low-level ``Server``) that bridges the dualsystem-agentic
loop to the x2robot Bridge HTTP API. Modeled on RoboClaw's x2robot_mcp_server, but
the tool surface follows this project's planner roles:

- ``fetch_env``  -> inspect the robot/scene state (merged into the loop environment)
- ``monitor``    -> report the current subtask status (running / success / failed)
- ``execute``    -> run a subtask on the robot (set params + auto-start)

plus the low-level controls ``stop_task`` / ``reset_task`` / ``emergency_stop``.

Tool outputs are JSON strings so the loop can parse them structurally (the MCP
client decodes each TextContent into a dict).

Environment variables:
    X2ROBOT_BRIDGE_URL        Bridge base URL (default: http://localhost:8766)
    X2ROBOT_POLICY_HOST       Default inference host (default: 192.168.0.20)
    X2ROBOT_POLICY_PORT       Default inference port (default: 57770)
    X2ROBOT_AUTO_START_DELAY  Seconds to wait before auto-start (default: 1.0)
"""

from __future__ import annotations

import json
import logging
import os
import sys

import anyio
import httpx
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger(__name__)

BRIDGE_BASE_URL = os.environ.get("X2ROBOT_BRIDGE_URL") or "http://localhost:8766"
DEFAULT_POLICY_HOST = os.environ.get("X2ROBOT_POLICY_HOST") or "192.168.0.20"
DEFAULT_POLICY_PORT = int(os.environ.get("X2ROBOT_POLICY_PORT") or 57770)
AUTO_START_DELAY_S = float(os.environ.get("X2ROBOT_AUTO_START_DELAY") or 1.0)
REQUEST_TIMEOUT_S = 30.0

app = Server("x2robot_mcp_server")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="fetch_env",
            description="获取 x2robot 当前的机器人/场景状态（运行模式、当前提示词、执行进度、机械臂位姿、推理服务器等）。",
            inputSchema={"type": "object", "required": [], "properties": {}},
        ),
        types.Tool(
            name="monitor",
            description="查询当前子任务的执行状态，返回 running / success / failed。",
            inputSchema={
                "type": "object",
                "required": [],
                "properties": {
                    "subtask": {"type": "string", "description": "当前子任务描述（用于一致性校验）。"},
                    "subtask_index": {"type": "integer", "description": "当前子任务在计划中的 0 基序号。"},
                },
            },
        ),
        types.Tool(
            name="execute",
            description="在 x2robot 上执行一个子任务：设置提示词与推理服务器参数并自动启动任务。",
            inputSchema={
                "type": "object",
                "required": ["subtask"],
                "properties": {
                    "subtask": {"type": "string", "description": "要执行的子任务指令（作为机器人 prompt）。"},
                    "policy_host": {"type": "string", "description": f"推理服务器地址（默认 {DEFAULT_POLICY_HOST}）。"},
                    "policy_port": {"type": "integer", "description": f"推理服务器端口（默认 {DEFAULT_POLICY_PORT}）。"},
                    "step_interval": {"type": "number", "description": "执行步间隔（秒），可选。"},
                },
            },
        ),
        types.Tool(
            name="stop_task",
            description="停止 x2robot 当前任务（running_mode=0）。",
            inputSchema={"type": "object", "required": [], "properties": {}},
        ),
        types.Tool(
            name="reset_task",
            description="停止当前任务并将 x2robot 机械臂重置到初始位姿。",
            inputSchema={"type": "object", "required": [], "properties": {}},
        ),
        types.Tool(
            name="emergency_stop",
            description="紧急停止 x2robot（立即 running_mode=0）。",
            inputSchema={"type": "object", "required": [], "properties": {}},
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
        return await _request(client, "GET", "/status")
    if name == "monitor":
        return await _monitor(client, arguments)
    if name == "execute":
        return await _execute(client, arguments)
    if name == "stop_task":
        return await _request(client, "POST", "/task/stop")
    if name == "reset_task":
        return await _request(client, "POST", "/task/reset")
    if name == "emergency_stop":
        return await _request(client, "POST", "/task/emergency_stop")
    raise ValueError(f"unknown tool: {name}")


async def _monitor(client: httpx.AsyncClient, arguments: dict) -> dict:
    status = await _request(client, "GET", "/status")
    return {
        "status": _derive_status(status),
        "subtask": arguments.get("subtask"),
        "subtask_index": arguments.get("subtask_index"),
        "running_mode": status.get("running_mode"),
        "current_step": status.get("current_step"),
        "total_steps": status.get("total_steps"),
    }


async def _execute(client: httpx.AsyncClient, arguments: dict) -> dict:
    subtask = arguments.get("subtask")
    if not subtask:
        raise ValueError("execute requires a 'subtask'")
    evaluate_params = {
        "prompt": subtask,
        "policy": {
            "host": arguments.get("policy_host") or DEFAULT_POLICY_HOST,
            "port": arguments.get("policy_port") or DEFAULT_POLICY_PORT,
        },
    }
    if arguments.get("step_interval") is not None:
        evaluate_params["step_interval"] = arguments["step_interval"]

    await _request(client, "POST", "/task/set_params", {"evaluate_params": evaluate_params})
    await anyio.sleep(AUTO_START_DELAY_S)
    await _request(client, "POST", "/task/start")
    return {"executed": True, "prompt": subtask, "auto_start_delay_s": AUTO_START_DELAY_S}


def _derive_status(status: dict) -> str:
    """Map bridge running state to a subtask status.

    Note: this reflects execution progress only. Final task success usually needs
    visual verification by the planner (which receives images separately).
    """
    running_mode = int(status.get("running_mode") or 0)
    current_step = int(status.get("current_step") or 0)
    total_steps = int(status.get("total_steps") or 0)
    if running_mode == 1:
        return "running"
    if total_steps > 0 and current_step >= total_steps:
        return "success"
    return "failed"


async def _request(client: httpx.AsyncClient, method: str, path: str, json_data: dict | None = None) -> dict:
    if method == "GET":
        response = await client.get(path)
    else:
        response = await client.post(path, json=json_data)
    response.raise_for_status()

    payload = response.json()
    if isinstance(payload, dict) and "success" in payload:
        if not payload.get("success"):
            raise RuntimeError(payload.get("message") or "bridge request failed")
        data = payload.get("data")
        return data if isinstance(data, dict) else {"data": data}
    return payload if isinstance(payload, dict) else {"data": payload}


def main() -> int:
    async def arun() -> None:
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    anyio.run(arun)
    return 0


if __name__ == "__main__":
    sys.exit(main())
