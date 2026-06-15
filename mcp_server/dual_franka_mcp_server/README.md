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
python robot_runtime/api/app.py \
  --config robot_runtime/configs/dual_franka.runtime.yaml \
  --port 8767
```

The runtime owns `execution_id` / `monitor_id`, latest observations, monitor
state, and robot control placeholders. `dual_franka_bridge.py` is still kept as
a legacy compatibility/debug bridge for `/tmp/subtask.txt`,
`/tmp/monitor_result.txt`, and `/tmp/img` based deployments.

## Tools

| Tool | HTTP default | Loop role |
|------|--------------|-----------|
| `fetch_env` | hidden by default | structured scene/runtime state provider, only when configured |
| `monitor` | `POST /monitors/status` | returns `running` / `success` / `failed` |
| `execute` | `POST /executions` + `POST /monitors/status` | starts a subtask, returns `execution_id` and `monitor_id` |
| `stop_task` | `POST /control/stop` | stop |
| `reset_task` | `POST /control/reset` | reset |
| `emergency_stop` | `POST /control/emergency_stop` | emergency stop |
| `call_bridge` | configurable relative path | extra robot-specific runtime HTTP calls |

The project exposes these to the VLM as canonical names such as
`dual_franka___execute`.

## Configuration

Set these in `examples/config.dual_franka.yaml` under the MCP server `env` block:

| Variable | Default |
|----------|---------|
| `DUAL_FRANKA_BRIDGE_URL` | `http://localhost:8767` |
| `DUAL_FRANKA_FETCH_ENV_PATH` | `/environment` |
| `DUAL_FRANKA_FETCH_ENV_HTTP` | unset / false |
| `DUAL_FRANKA_MONITOR_PATH` | `/monitors/status` |
| `DUAL_FRANKA_EXECUTE_PATH` | `/executions` |
| `DUAL_FRANKA_STOP_PATH` | `/control/stop` |
| `DUAL_FRANKA_RESET_PATH` | `/control/reset` |
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
set. This keeps local VLMs from repeatedly calling an empty scene-state tool, and
keeps bridge bookkeeping such as camera file paths and last monitor requests out
of the planner's structured scene state until a real scene-graph/environment
provider is implemented. If `_fetch_env` is called directly while the HTTP
provider is disabled, it returns `{"environment": {}}` as a compatibility
placeholder.

## Local smoke test

```bash
python robot_runtime/api/app.py \
  --config robot_runtime/configs/dual_franka.runtime.yaml \
  --port 8767

PYTHONPATH=src python examples/run_online_robot.py \
  --config examples/config.dual_franka.runtime.yaml \
  --tasks "pick up the cube" \
  --print-components
```

For real hardware, replace `DUAL_FRANKA_BRIDGE_URL` and `dataloader.url` with the
robot runtime host.

`mock_dual_franka_bridge.py` is still available for a fully self-contained smoke
test that does not require image files or a monitor process.
