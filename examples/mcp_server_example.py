"""Runnable example MCP server exposing stub robot tools.

Start it over stdio via ``mcp_servers.example.json`` (the framework launches it),
or run directly for debugging: ``python examples/mcp_server_example.py``.

The tool bodies are stubs (no real robot); they exist so the full agentic loop
can be exercised over a real MCP transport.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo_robot")

# Scripted monitor outcomes per subtask index, for a deterministic demo.
_MONITOR_SCRIPT = ["running", "success", "running", "success"]


@mcp.tool()
def fetch_env() -> dict:
    """Return the current (stubbed) environment / scene state."""
    return {
        "objects": ["radio", "table", "power_button"],
        "gripper": "empty",
        "robot_pose": [0.0, 0.0, 0.0],
    }


@mcp.tool()
def monitor(subtask: str = "", subtask_index: int = 0) -> dict:
    """Report the status of the current subtask: running / success / failed."""
    status = _MONITOR_SCRIPT[subtask_index] if 0 <= subtask_index < len(_MONITOR_SCRIPT) else "success"
    return {"subtask": subtask, "subtask_index": subtask_index, "status": status}


@mcp.tool()
def execute(subtask: str = "") -> dict:
    """Execute a subtask directly on the (stubbed) robot."""
    return {"subtask": subtask, "executed": True}


if __name__ == "__main__":
    mcp.run()
