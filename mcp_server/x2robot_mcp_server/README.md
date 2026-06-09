# x2robot MCP Server

A real MCP server (STDIO) that bridges the **dualsystem-agentic** loop to the
**x2robot bridge HTTP API**. Modeled on RoboClaw's `x2robot_mcp_server`, but the
tool surface follows this project's planner roles.

## Architecture

```
AgenticRobotLoop ──(stdio MCP)──► x2robot_mcp_server ──(HTTP)──► x2robot bridge ──► robot
```

## Tools

| Tool | Role | Bridge call | Description |
|------|------|-------------|-------------|
| `fetch_env` | `fetch_env` | `GET /status` | Robot/scene state, merged into the loop environment. |
| `monitor` | `monitor` | `GET /status` | Subtask status: `running` / `success` / `failed`. |
| `execute` | `execute` | `POST /task/set_params` + `POST /task/start` | Run a subtask (prompt + policy), then auto-start. |
| `stop_task` | control | `POST /task/stop` | Stop the current task. |
| `reset_task` | control | `POST /task/reset` | Stop and reset arms to home. |
| `emergency_stop` | control | `POST /task/emergency_stop` | Immediate stop. |

Tool outputs are JSON strings, so the loop decodes each result into a dict
(`monitor` -> `{"status": ...}`, `fetch_env` -> environment fields).

> `monitor` reflects **execution progress** from the bridge only. Final task
> success usually needs visual verification by the planner, which receives the
> camera images separately through the loop.

## Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `X2ROBOT_BRIDGE_URL` | `http://localhost:8766` | Bridge base URL. |
| `X2ROBOT_POLICY_HOST` | `192.168.0.20` | Default inference host for `execute`. |
| `X2ROBOT_POLICY_PORT` | `57770` | Default inference port for `execute`. |
| `X2ROBOT_AUTO_START_DELAY` | `1.0` | Seconds to wait before auto-start. |

## Quick start (with the mock bridge)

```bash
# 1. Start the mock bridge (no robot needed)
python mcp_server/x2robot_mcp_server/mock_x2robot_bridge.py --port 8766

# 2. Run the loop against it
export OPENAI_BASE_URL=... OPENAI_API_KEY=...
dualsystem-agentic run \
  --config examples/config.x2robot.yaml \
  --task "把桌上的杯子放进收纳盒" \
  --image head=/path/to/obs.jpg
```

## Real robot

Set `X2ROBOT_BRIDGE_URL=http://<robot_ip>:8766` (in `examples/config.x2robot.yaml`
under the server's `env`) and ensure the x2robot bridge server is running on the
robot side.
