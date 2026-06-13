# dual_franka MCP Server

STDIO MCP adapter for a dual-Franka robot whose bridge is controlled over HTTP.

```text
AgenticRobotLoop
  -> stdio MCP: dual_franka_mcp_server
  -> HTTP: dual-Franka bridge
  -> robot
```

On the robot side, run `dual_franka_bridge.py`. It wraps local robot resources
into HTTP:

- images from `/tmp/img/base_0_rgb.jpg`, `/tmp/img/left_wrist_0_rgb.jpg`,
  `/tmp/img/right_wrist_0_rgb.jpg`
- monitor target written to `/tmp/subtask.txt`
- monitor result read from `/tmp/monitor_result.txt`; the recommended format is
  a single word: `running`, `success`, or `failed`
- `execute` automatically triggers `monitor` for the same subtask, so an
  `execute` tool call also updates `/tmp/subtask.txt`
- when a new subtask starts, the bridge resets `/tmp/monitor_result.txt` to
  `running` for that subtask so stale terminal results are not reused
- JSON monitor results are still accepted for compatibility, but are optional
- execution/control endpoints currently log placeholders

## Tools

| Tool | HTTP default | Loop role |
|------|--------------|-----------|
| `fetch_env` | hidden by default | structured scene state provider, only when configured |
| `monitor` | `POST /task/monitor` | returns `running` / `success` / `failed` |
| `execute` | `POST /task/execute` + `POST /task/monitor` | starts a subtask, writes monitor target, and returns `executed: true` |
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
| `DUAL_FRANKA_FETCH_ENV_HTTP` | unset / false |
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

`fetch_env` is intentionally hidden unless `DUAL_FRANKA_FETCH_ENV_HTTP=true` is
set. This keeps local VLMs from repeatedly calling an empty scene-state tool, and
keeps bridge bookkeeping such as camera file paths and last monitor requests out
of the planner's structured scene state until a real scene-graph/environment
provider is implemented. If `_fetch_env` is called directly while the HTTP
provider is disabled, it returns `{"environment": {}}` as a compatibility
placeholder.

## Local smoke test

```bash
python mcp_server/dual_franka_mcp_server/dual_franka_bridge.py --port 8767

PYTHONPATH=src python examples/run_online_robot.py \
  --config examples/config.dual_franka.yaml \
  --tasks "pick up the cube" \
  --print-components
```

For real hardware, replace `DUAL_FRANKA_BRIDGE_URL` and `dataloader.url` with the
robot bridge host.

`mock_dual_franka_bridge.py` is still available for a fully self-contained smoke
test that does not require image files or a monitor process.
