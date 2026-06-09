# dual_franka MCP Server

STDIO MCP adapter for a dual-Franka robot whose bridge is controlled over HTTP.

```text
AgenticRobotLoop
  -> stdio MCP: dual_franka_mcp_server
  -> HTTP: dual-Franka bridge
  -> robot
```

## Tools

| Tool | HTTP default | Loop role |
|------|--------------|-----------|
| `fetch_env` | `GET /environment` | scene/robot state |
| `monitor` | `POST /task/monitor` | returns `running` / `success` / `failed` |
| `execute` | `POST /task/execute` | starts a subtask and returns `executed: true` |
| `stop_task` | `POST /task/stop` | stop |
| `reset_task` | `POST /task/reset` | reset |
| `emergency_stop` | `POST /task/emergency_stop` | emergency stop |
| `call_bridge` | configurable relative path | extra robot-specific HTTP calls |

The project exposes these to the VLM as canonical names such as
`dual_franka___execute`.

## Configuration

Set these in `examples/config.dual_franka.yaml` under the MCP server `env` block:

| Variable | Default |
|----------|---------|
| `DUAL_FRANKA_BRIDGE_URL` | `http://localhost:8767` |
| `DUAL_FRANKA_FETCH_ENV_PATH` | `/environment` |
| `DUAL_FRANKA_MONITOR_PATH` | `/task/monitor` |
| `DUAL_FRANKA_EXECUTE_PATH` | `/task/execute` |
| `DUAL_FRANKA_STOP_PATH` | `/task/stop` |
| `DUAL_FRANKA_RESET_PATH` | `/task/reset` |
| `DUAL_FRANKA_ESTOP_PATH` | `/task/emergency_stop` |

Image acquisition is not an MCP tool. It uses the main config `dataloader` section,
usually:

```yaml
dataloader:
  provider: http
  url: http://<robot_ip>:8767/cameras/concatenated
  image_key: concatenated_image
  label: main
```

## Local smoke test

```bash
python mcp_server/dual_franka_mcp_server/mock_dual_franka_bridge.py --port 8767

PYTHONPATH=src python examples/run_online_robot.py \
  --config examples/config.dual_franka.yaml \
  --tasks "pick up the cube" \
  --print-components
```

For real hardware, replace `DUAL_FRANKA_BRIDGE_URL` and `dataloader.url` with the
robot bridge host.
