# dualsystem-agentic

An agentic robot Dual-System framework for **long-horizon** tasks.

A high-level **VLM planner** reads a long-horizon instruction, decides which
**MCP tools** to call (`fetch_env`, `monitor`, `execute`, ...), breaks the task
into subtasks, and emits the next subtask. A **monitor** tool reports the subtask
status (`running` / `success` / `failed`); that feedback drives the planner's next
decision. A downstream **VLA executor** runs the current subtask.

```
long-horizon instruction
        │
        ▼
   VLM planner ──(tool_calls)──► MCP servers (fetch_env / monitor / execute, per namespace)
        ▲  │                              │
        │  │◄──── monitor status ─────────┘
        │  │  (running / success / failed)
        │  ▼
        │  current subtask ──► VLA executor (downstream robot)
        │        │
        │        ▼
        │   replan next step (loop)
        │
   DataLoader ──capture()──► CameraFrame (images injected into VLM context)
        ▲
        │
   HTTP / Mock / Static (CLI --image)
```

Design goals: a small, modular core; the VLM works with **both local models and
commercial APIs**; tools are reached through the **real MCP protocol** so different
robots map to different MCP servers; modules are easy to add or remove.

## Install

```bash
pip install -e .                 # core (pyyaml, pillow)
pip install -e ".[mcp]"          # + real MCP transport (official mcp SDK)
pip install -e ".[local-qwen]"   # + local Qwen2.5/3-VL planner
pip install -e ".[dev]"          # + pytest
```

## Layout

| Module | Role |
|--------|------|
| `core/types.py` | Data structures: `ToolCall`/`ToolResult` (with `namespace`), `MonitorStatus`, `AgenticPlannerInput`/`Output`, `AgenticSessionState`, `ExecutorInput`/`Output`, `AgenticStepResult`. |
| `core/prompts.py` | `build_agentic_prompt`: renders planner context (tools by namespace, monitor status, environment, last tool results) into a prompt. |
| `core/parser.py` | `parse_agentic_planner_output`: robust JSON-in-text parsing of the planner reply. |
| `core/loop.py` | `AgenticRobotLoop`: planner → tools → monitor feedback → executor handoff. |
| `vlm/` | `VLMPlanner` protocol + `OpenAICompatibleVLMPlanner` (API) + `LocalQwenVLMPlanner` (local) + `CallablePlanner`. |
| `mcp/` | `MCPToolClient` protocol, `MCPServerConnection` (one server), `MCPServiceManager` (namespace routing over a background loop), `FakeMCPToolClient` (in-process). |
| `executor/` | `ExecutorClient` protocol + `HTTPExecutorClient`. |
| `io/dataloader.py` | `DataLoader` protocol + `HTTPDataLoader` (bridge camera endpoint) + `MockDataLoader` (synthetic) + `StaticDataLoader` (CLI `--image`). |
| `app.py` | Config-driven app builders shared by CLI and robot deployment scripts. |
| `runtime.py` | `OnlineAgentRuntime`: keeps components alive, waits for user tasks, resets session state per task, and returns to waiting after completion/failure. |
| `interaction.py` | `InteractionLayer` protocol + dependency-free `ConsoleInteractionLayer` / `TuiInteractionLayer`. |
| `run_logger.py` | Optional JSONL run/session/step logger with image files saved by reference. |
| `config.py` / `cli.py` | YAML config + factories + the `run` and `online` commands. |

## Planning protocol (decompose, then select / revise)

The first time a long-horizon task arrives, the planner **decomposes** it into an
ordered `subtasks` list. On every later step it **selects** the current subtask from
that list by `subtask_index`, and may **revise** the list (return a new full
`subtasks`) when a subtask fails or the plan needs to change. Selecting by index
alone is enough — `current_subtask` defaults to `subtasks[subtask_index]`.

The planner returns a single JSON object. One code path works for both local and
API models. Native function-calling can be added later behind the same `VLMPlanner`
protocol without touching the loop.

```json
{
  "tool_calls": [
    {"name": "demo_robot___fetch_env", "arguments": {}},
    {"name": "demo_robot___monitor", "arguments": {"subtask": "turn on the radio", "subtask_index": 0}}
  ],
  "subtasks": ["turn on the radio", "tidy the table"],
  "subtask_index": 0,
  "current_subtask": "turn on the radio",
  "task_complete": false
}
```

The planner calls tools by the **canonical tool name** shown in the catalog:
`namespace___tool_name`, for example `demo_robot___monitor`. This is the stable
VLM-facing name; the loop/parser splits it back into the internal
`namespace + tool_name` route. The old explicit
`{"namespace": "demo_robot", "name": "monitor"}` form and legacy
`demo_robot/monitor` names remain supported, but prompts prefer canonical names.

The tool catalog — canonical names, descriptions, and parameter schemas — comes
entirely from `tool_client.list_tools()` (MCP self-description, including each
tool's `inputSchema`), so newly added MCP tools are automatically visible to the
planner without config changes.

A status-reporting tool (the `monitor` role) is expected to return
`{"status": "running|success|failed", ...}`. The status is written to the session
state and fed back to the planner next step. If the planner calls the MCP `execute`
tool, the downstream `ExecutorClient` is skipped for that step (the robot executes
via its own MCP server instead).

By convention, the standard role tool names are `fetch_env`, `monitor`, and
`execute`; example configs do not repeat them. If a robot exposes different tool
names, map only those roles:

```yaml
loop:
  max_steps: 20
  tool_roles:
    fetch_env: observe_scene
    monitor: check_status
    execute: run_subtask
```

Most new tools need no role mapping at all. The loop also recognizes common
structured outputs from any tool:

| Tool output | Loop effect |
|-------------|-------------|
| `{"status": "running|success|failed"}` | update monitor feedback |
| `{"environment": {...}}` or `{"env": {...}}` | merge scene state into planner context |
| `{"executed": true}` | treat action as already executed by MCP and skip downstream executor |
| `{"agentic_role": "monitor|fetch_env|execute"}` | explicit role hint for unusual outputs |

## MCP namespaces (different robots = different servers)

Each MCP server is registered under a `namespace` (one robot per namespace). Tools
are exposed as `namespace___tool_name`, which avoids collisions across multiple
robots or capability servers. Add a robot by adding one entry to the servers file;
new tools inside that server enter the registry and prompt automatically through
MCP `list_tools()`:

```json
{
  "servers": [
    {"namespace": "demo_robot", "transport": "stdio", "command": "python", "args": ["examples/mcp_server_example.py"]},
    {"namespace": "franka_arm", "transport": "sse", "url": "http://localhost:8931/sse"}
  ]
}
```

`examples/mcp_server_example.py` is a runnable MCP server (FastMCP) exposing stub
`fetch_env` / `monitor` / `execute` tools, so the whole loop can be exercised over
a real MCP transport. Replace it with your robot's own MCP server.

For offline verification without any robot or network, use `mcp_server/mock_mcp_server/`
(in-memory state machine, see `examples/config.mock.yaml`). For a more realistic
adapter, `mcp_server/x2robot_mcp_server/` bridges the loop to the x2robot bridge
HTTP API (with a dependency-free mock bridge for local testing); see
`examples/config.x2robot.yaml` and that server's README.
For dual-Franka deployment, see `mcp_server/dual_franka_mcp_server/` and
`examples/config.dual_franka.yaml`: images come from `HTTPDataLoader`, while
`monitor`, `execute`, and extra robot controls are forwarded to the robot HTTP
bridge by the MCP adapter.

For config-only smoke tests, `mcp.provider: fake` can declare in-process tools
directly in YAML. This is useful for testing the online loop without the MCP SDK:

```yaml
mcp:
  provider: fake
  tools:
    - namespace: mock
      name: monitor
      result: {status: running}
      echo_args: true
```

`vlm.provider: scripted` similarly returns configured planner JSON objects instead
of calling a real model. It is only intended for demos and regression tests.

## Image acquisition (DataLoader)

The VLM planner must see camera images to make decisions. Images come from a
**DataLoader**, separate from MCP tools (MCP carries structured data; the
DataLoader carries visual observations).

| Provider | Config key | Use case |
|----------|-----------|----------|
| `http` | `dataloader.url` | Poll a camera HTTP endpoint (e.g. x2robot bridge `/cameras/concatenated`). |
| `mock` | — | Generate synthetic JPEG frames for offline testing. |
| `static` | — | Wrap CLI `--image` files (backward compatible). |
| `none` | — | No automatic images; VLM is text-only. |

The loop **captures a fresh image every step** before calling the VLM. When the
planner calls `fetch_env`, a new capture is triggered so the next step shows the
latest scene.

```yaml
dataloader:
  provider: http
  url: http://192.168.0.20:8766/cameras/concatenated
  timeout: 10.0
  image_key: concatenated_image
  label: main
```

## Run

The one-shot command is unchanged: it runs one task and prints one
`AgenticStepResult` JSON line per step.

```bash
dualsystem-agentic run \
  --config examples/config.mock.yaml \
  --task "turn on the radio and tidy the table"

# Static images via CLI remain backward compatible.
dualsystem-agentic run \
  --config examples/config.openai.yaml \
  --task "turn on the radio and tidy the table" \
  --image main=./obs.jpg
```

The recommended online robot entrypoint is the config-driven script
`examples/run_online_robot.py`. It initializes the VLM, MCP client, executor, and
DataLoader once, then keeps waiting for long-horizon tasks. Each entered task gets
a fresh `AgenticSessionState`; after task completion, max steps, error, or Ctrl-C
during a task, the process returns to the prompt. Use `/quit` or `/exit` to stop.

```bash
# Offline mock robot: scripted VLM + fake MCP + mock images.
PYTHONPATH=src python examples/run_online_robot.py --config examples/config.mock.yaml

# Real robot/simulator: switch only the config.
PYTHONPATH=src python examples/run_online_robot.py --config examples/config.x2robot.yaml
PYTHONPATH=src python examples/run_online_robot.py --config examples/config.dual_franka.yaml

# Non-interactive debug tasks.
PYTHONPATH=src python examples/run_online_robot.py \
  --config examples/config.mock.yaml \
  --tasks "pick up the cup" "place it on the shelf" \
  --print-components
```

The CLI command is a thin wrapper around the same `dualsystem_agentic.app`
builders:

```bash
dualsystem-agentic online --config examples/config.mock.yaml
dualsystem-agentic online --config examples/config.mock.yaml --max-steps 8
dualsystem-agentic online --config examples/config.mock.yaml --log-dir runs/debug
dualsystem-agentic online --config examples/config.mock.yaml --no-log
```

Console interaction is configured under `interaction`:

```yaml
interaction:
  provider: console
  show_raw_json: false
```

For an in-terminal TUI, switch the provider:

```yaml
interaction:
  provider: tui
  prompt: "robot> "
  max_log_lines: 1000
```

The TUI uses Python's standard `curses` module and automatically falls back to the
console interaction when stdin/stdout are not TTYs.

Optional run logging is configured under `logging`. When enabled, each process run
creates a run directory with `events.jsonl`; each user task is a session. Step
events include planner input, rendered prompt, raw VLM output, parsed output, tool
results, executor output, monitor status, and the stop reason. Images are saved as
files under the session/step directory, while JSONL stores only path/hash/size
references.

```yaml
logging:
  enabled: true
  root_dir: runs
  save_images: true
```

## Programmatic use

```python
from dualsystem_agentic import AgenticRobotLoop, CallablePlanner, ExecutorOutput, FakeMCPToolClient

tools = FakeMCPToolClient()
tools.register("monitor", lambda args: {"status": "running"}, namespace="demo_robot")

class Executor:
    def execute(self, executor_input):
        return ExecutorOutput.success({"ack": executor_input.subtask})

loop = AgenticRobotLoop(my_vlm_planner, tools, Executor())
results, state = loop.run("turn on the radio", max_steps=10)

# For repeated online tasks, wrap the same loop in OnlineAgentRuntime.
```

## Structure notes

The code is intentionally split by runtime boundary rather than by robot type:
`core/` owns the agent loop and JSON-safe state, `vlm/` owns planner adapters,
`mcp/` owns tool routing, `executor/` owns downstream execution, `io/` owns
observations, `app.py` owns config-driven wiring, and `runtime.py` /
`interaction.py` / `run_logger.py` own online operation. The main simplification
is to keep robot-specific behavior in config and MCP servers instead of adding
per-robot branches to `AgenticRobotLoop` or `cli.py`.

## Test

```bash
pytest
```
