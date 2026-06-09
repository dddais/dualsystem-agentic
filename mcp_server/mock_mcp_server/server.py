"""
Mock MCP Server
================
A fully in-process mock MCP server for offline loop verification. No robot, no
bridge, no network — every tool response is generated from a small in-memory
state machine that simulates subtask execution progress.

State machine
-------------
```
IDLE ──execute()──► RUNNING ──(tick after N monitor polls)──► SUCCESS
  │                    │
  │                    └──stop_task() / reset_task()──► IDLE
  └──reset_task()──► IDLE
```

After `execute` is called, the mock tracks how many times `monitor` has been
polled. After ``MOCK_COMPLETE_AFTER_POLLS`` polls (default 3) the status
transitions from `running` to `success`. This lets the agentic loop observe a
realistic running→success sequence without any external service.

Environment variables:
    MOCK_COMPLETE_AFTER_POLLS  monitor calls before a task auto-completes (default: 3)
    MOCK_EXECUTE_DELAY_S       simulated execution startup delay in seconds (default: 0.1)
"""

from __future__ import annotations

import json
import logging
import os
import sys

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger(__name__)

COMPLETE_AFTER_POLLS = int(os.environ.get("MOCK_COMPLETE_AFTER_POLLS") or 3)
EXECUTE_DELAY_S = float(os.environ.get("MOCK_EXECUTE_DELAY_S") or 0.1)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

_state: dict = {
    "running": False,
    "current_subtask": "",
    "poll_count": 0,
    "total_subtasks_completed": 0,
    "left_arm_pos": [0.0] * 7,
    "right_arm_pos": [0.0] * 7,
    "scene_objects": ["cup", "table", "shelf"],
    "gripper": "empty",
}


def _reset() -> None:
    _state.update({
        "running": False,
        "current_subtask": "",
        "poll_count": 0,
        "total_subtasks_completed": 0,
        "left_arm_pos": [0.0] * 7,
        "right_arm_pos": [0.0] * 7,
        "gripper": "empty",
    })


# ---------------------------------------------------------------------------
# MCP app
# ---------------------------------------------------------------------------

app = Server("mock_mcp_server")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="fetch_env",
            description="获取模拟的机器人/场景状态（对象列表、夹爪、臂位姿等）。",
            inputSchema={"type": "object", "required": [], "properties": {}},
        ),
        types.Tool(
            name="monitor",
            description="查询当前子任务执行状态（running / success / failed）。每次调用推进模拟进度。",
            inputSchema={
                "type": "object",
                "required": [],
                "properties": {
                    "subtask": {"type": "string", "description": "当前子任务描述。"},
                    "subtask_index": {"type": "integer", "description": "当前子任务 0 基序号。"},
                },
            },
        ),
        types.Tool(
            name="execute",
            description="模拟执行一个子任务：启动模拟执行器，若干次 monitor 后自动完成。",
            inputSchema={
                "type": "object",
                "required": ["subtask"],
                "properties": {
                    "subtask": {"type": "string", "description": "要模拟执行的子任务指令。"},
                },
            },
        ),
        types.Tool(
            name="stop_task",
            description="停止当前模拟任务。",
            inputSchema={"type": "object", "required": [], "properties": {}},
        ),
        types.Tool(
            name="reset_task",
            description="重置模拟状态（机械臂归零、清除进度）。",
            inputSchema={"type": "object", "required": [], "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    data = await _dispatch(name, arguments or {})
    return [types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False))]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _dispatch(name: str, arguments: dict) -> dict:
    if name == "fetch_env":
        return _do_fetch_env()
    if name == "monitor":
        return _do_monitor(arguments)
    if name == "execute":
        return await _do_execute(arguments)
    if name == "stop_task":
        return _do_stop()
    if name == "reset_task":
        return _do_reset()
    raise ValueError(f"unknown tool: {name}")


def _do_fetch_env() -> dict:
    return {
        "running": _state["running"],
        "current_subtask": _state["current_subtask"],
        "total_subtasks_completed": _state["total_subtasks_completed"],
        "left_arm_pos": list(_state["left_arm_pos"]),
        "right_arm_pos": list(_state["right_arm_pos"]),
        "scene_objects": list(_state["scene_objects"]),
        "gripper": _state["gripper"],
    }


def _do_monitor(arguments: dict) -> dict:
    status = "failed"
    if _state["running"]:
        _state["poll_count"] += 1
        if _state["poll_count"] >= COMPLETE_AFTER_POLLS:
            _state["running"] = False
            _state["total_subtasks_completed"] += 1
            status = "success"
        else:
            status = "running"
    elif _state["poll_count"] >= COMPLETE_AFTER_POLLS and _state["current_subtask"]:
        status = "success"
    return {
        "status": status,
        "subtask": arguments.get("subtask"),
        "subtask_index": arguments.get("subtask_index"),
        "poll_count": _state["poll_count"],
        "current_subtask": _state["current_subtask"],
    }


async def _do_execute(arguments: dict) -> dict:
    subtask = arguments.get("subtask")
    if not subtask:
        raise ValueError("execute requires a 'subtask'")
    _state["running"] = True
    _state["current_subtask"] = subtask
    _state["poll_count"] = 0
    logger.info("execute: subtask=%r, will complete after %d monitor polls", subtask, COMPLETE_AFTER_POLLS)
    await anyio.sleep(EXECUTE_DELAY_S)
    return {"executed": True, "prompt": subtask}


def _do_stop() -> dict:
    _state["running"] = False
    logger.info("stop_task")
    return {"stopped": True}


def _do_reset() -> dict:
    _reset()
    logger.info("reset_task")
    return {"reset": True}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    async def arun() -> None:
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    anyio.run(arun)
    return 0


if __name__ == "__main__":
    sys.exit(main())
