# Mock MCP Server

A fully self-contained MCP server for **offline loop verification**. No robot,
no bridge, no network — every tool response comes from an in-memory state
machine.

## State machine

```
IDLE ──execute()──► RUNNING ──(N × monitor polls)──► SUCCESS
  │                    │
  └──reset_task()      └──stop_task()──► IDLE
```

After `execute`, the mock tracks monitor poll count. After
`MOCK_COMPLETE_AFTER_POLLS` (default 3) polls, the status transitions
`running → success`. This gives the agentic loop a realistic running→success
sequence with zero external dependencies.

## Tools

| Tool | Description |
|------|-------------|
| `fetch_env` | Mocked scene state (objects, gripper, arm poses). |
| `monitor` | Current subtask status (`running` / `success` / `failed`). Advances the state machine on each call. |
| `execute` | Start a simulated subtask (sets running, resets poll counter). |
| `stop_task` | Stop the current simulated task. |
| `reset_task` | Reset all mock state to defaults. |

## Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `MOCK_COMPLETE_AFTER_POLLS` | `3` | Monitor calls before a task auto-completes. |
| `MOCK_EXECUTE_DELAY_S` | `0.1` | Simulated execution startup delay (seconds). |

## Usage

### 1. Add to the MCP servers config

```json
{
  "servers": [
    {
      "namespace": "mock",
      "transport": "stdio",
      "command": "python",
      "args": ["mcp_server/mock_mcp_server/server.py"]
    }
  ]
}
```

Or use the dedicated example config:

```bash
dualsystem-agentic run \
  --config examples/config.mock.yaml \
  --task "把桌上的杯子放进收纳盒" \
  --image main=./test_obs.jpg
```

### 2. Direct test via the MCP manager

```python
from dualsystem_agentic.mcp.connection import MCPServerConfig
from dualsystem_agentic.mcp.manager import MCPServiceManager

cfg = MCPServerConfig.from_dict({
    "namespace": "mock",
    "transport": "stdio",
    "command": "python",
    "args": ["mcp_server/mock_mcp_server/server.py"],
})
mgr = MCPServiceManager([cfg]).start()
print([t.get("name") for t in mgr.list_tools()])
mgr.call_tool("execute", {"subtask": "pick up cup"})
mgr.call_tool("monitor", {"subtask": "pick up cup", "subtask_index": 0})
mgr.close()
```
