# dual_franka MCP Server

STDIO MCP adapter for a dual-Franka robot runtime controlled over HTTP.

```text
AgenticRobotLoop
  -> stdio MCP: dual_franka_mcp_server
  -> HTTP: Robot Runtime API
  -> robot
```

Recommended robot-side entrypoint:

```bash
robot-runtime --port 8767
```

The runtime owns `execution_id` / `monitor_id`, latest observations, monitor
state, and robot control placeholders. The old file-based Dual-Franka bridge has
been removed; this adapter targets the runtime API only.

## Tools

| Tool | HTTP default | Loop role |
|------|--------------|-----------|
| `fetch_env` | hidden by default | structured scene/runtime state provider, only when configured |
| `monitor` | `POST /monitors/status` | returns `running` / `success` / `failed` |
| `execute` | `POST /executions` + initial `POST /monitors/status` query | controller-started subtask; returns `execution_id`, `monitor_id`, and initial status |
| `stop_task` | `POST /control/stop` | stop |
| `reset_task` | hidden by default; `POST /control/reset` when enabled | reset |
| `recover_task` | enabled by `DUAL_FRANKA_ENABLE_RESET`; `POST /control/recover` | homing or manual_bridge teleoperation adjustment |
| `emergency_stop` | `POST /control/emergency_stop` | emergency stop |

The agent loop starts robot motion with `decision="execute"` and injects the
configured execute tool call. The VLM should not write `dual_franka___execute`
tool calls directly.

## Configuration

Set these in `examples/config.dual_franka.runtime.yaml` under the MCP server
`env` block:

| Variable | Default |
|----------|---------|
| `DUAL_FRANKA_RUNTIME_URL` | `http://localhost:8767` |
| `DUAL_FRANKA_FETCH_ENV_PATH` | `/environment` |
| `DUAL_FRANKA_FETCH_ENV_HTTP` | unset / false |
| `DUAL_FRANKA_MONITOR_PATH` | `/monitors/status` |
| `DUAL_FRANKA_EXECUTE_PATH` | `/executions` |
| `DUAL_FRANKA_STOP_PATH` | `/control/stop` |
| `DUAL_FRANKA_RESET_PATH` | `/control/reset` |
| `DUAL_FRANKA_ENABLE_RESET` | unset / false |
| `DUAL_FRANKA_ESTOP_PATH` | `/control/emergency_stop` |

Image acquisition is not an MCP tool. It uses the main config `dataloader` section,
usually:

```yaml
dataloader:
  provider: http
  url: http://<robot_ip>:8767/observations/latest
  image_key: concatenated_image
  label: main
```

`fetch_env` is intentionally hidden unless `DUAL_FRANKA_FETCH_ENV_HTTP=true` is
set. This keeps local VLMs from repeatedly calling an empty scene-state tool
until a real scene-graph/environment provider is implemented. If `_fetch_env` is
called directly while the HTTP provider is disabled, it returns
`{"environment": {}}` as a compatibility placeholder.

`reset_task` is also hidden by default because the current runtime reset is a
placeholder unless your robot-side driver implements real reset behavior. Set
`DUAL_FRANKA_ENABLE_RESET=true` only after that behavior is safe for your setup.

## Adding or removing tools

The VLM-visible tool catalog comes from `list_tools()` in `server.py`; the agent
registry and prompt are populated from MCP `list_tools` automatically.

For an adapter-only change, add or remove the `types.Tool` entry in `list_tools()`
and the matching branch in `_dispatch()`. If the tool only needs a different
runtime path, prefer an env variable such as `DUAL_FRANKA_RESET_PATH` over code
changes.

For a robot-side action, also add the HTTP endpoint in `robot_runtime.api.app`,
wire it through `RobotRuntime`, and implement the behavior in the selected
`RobotDriver` adapter. Delete tools by removing them from `list_tools()` first;
once absent from that catalog, the VLM will no longer see them.

## Local smoke test

```bash
python robot_runtime/robot_runtime/api/app.py --port 8767

python examples/run_online_robot.py \
  --config examples/config.dual_franka.runtime.yaml \
  --tasks "pick up the cube" \
  --print-components
```

For real hardware, set `DUAL_FRANKA_RUNTIME_URL` and `dataloader.url` to the
robot runtime host.
