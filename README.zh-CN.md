# dualsystem-agentic

面向**长程任务**的 agentic 机器人 Dual-System 框架。

高层 **VLM 规划器**读取长程任务指令，决定调用哪些 **MCP 工具**（`fetch_env`、
`monitor`、`execute` 等），将任务拆分为子任务，并输出下一个子任务。`monitor`
工具上报当前子任务的状态（`running` / `success` / `failed`），这一反馈驱动规划器
的下一步决策。下游 **VLA 执行器**负责执行当前子任务。

```
长程任务指令
        │
        ▼
   VLM 规划器 ──(tool_calls)──► MCP servers（fetch_env / monitor / execute，按 namespace 区分）
      │                              │
        │◄──── monitor 状态 ───────────┘
        │  (running / success / failed)
        ▼
  当前子任务 ──► VLA 执行器（下游真机）
        │
        ▼
   规划下一步（循环）
```

设计目标：核心精简、模块化；VLM **同时支持本地模型与商用 API**；工具通过**真实
MCP 协议**接入，不同机器人对应不同 MCP server；模块易于增删。

## 安装

```bash
pip install -e .                 # 核心（pyyaml, pillow）
pip install -e ".[mcp]"          # + 真实 MCP 传输（官方 mcp SDK）
pip install -e ".[local-qwen]"   # + 本地 Qwen2.5/3-VL 规划器
pip install -e ".[dev]"          # + pytest
```

## 模块结构

| 模块 | 职责 |
|------|------|
| `core/types.py` | 数据结构：`ToolCall`/`ToolResult`（带 `namespace`）、`MonitorStatus`、`AgenticPlannerInput`/`Output`、`AgenticSessionState`、`ExecutorInput`/`Output`、`AgenticStepResult`。 |
| `core/prompts.py` | `build_agentic_prompt`：把规划上下文（按 namespace 列出的工具、monitor 状态、环境、上一轮工具结果）渲染成 prompt。 |
| `core/parser.py` | `parse_agentic_planner_output`：对规划器回复做鲁棒的 JSON-in-text 解析。 |
| `core/loop.py` | `AgenticRobotLoop`：规划器 → 工具 → monitor 反馈 → 执行器交接。 |
| `vlm/` | `VLMPlanner` 协议 + `OpenAICompatibleVLMPlanner`（API）+ `LocalQwenVLMPlanner`（本地）+ `ScriptedVLMPlanner`（离线脚本）+ `CallablePlanner`。 |
| `mcp/` | `MCPToolClient` 协议、`MCPServerConnection`（单 server）、`MCPServiceManager`（后台事件循环上的 namespace 路由）、`FakeMCPToolClient`（进程内）。 |
| `executor/` | `ExecutorClient` 协议 + `HTTPExecutorClient`。 |
| `io/dataloader.py` | `DataLoader` 协议 + `HTTPDataLoader`（相机/bridge）+ `MockDataLoader`（合成图）+ `StaticDataLoader`（CLI `--image`）。 |
| `app.py` | 基于 config 的应用装配层，CLI 和真机部署脚本共用。 |
| `runtime.py` | `OnlineAgentRuntime`：组件常驻，等待多条长程任务，每条任务独立 session，完成后回到等待状态。 |
| `interaction.py` | `InteractionLayer` 协议 + `ConsoleInteractionLayer` / `TuiInteractionLayer`。 |
| `run_logger.py` | 可选 JSONL 运行日志，图片单独落盘，JSONL 只存引用。 |
| `config.py` / `cli.py` | YAML 配置 + 工厂 + `run` / `online` 命令。 |

## 规划协议（先拆分，再选择 / 修订）

长程任务首次到来时，规划器先把它**拆分**为有序的 `subtasks` 列表；之后每一步从
列表中按 `subtask_index` **选择**当前子任务，并可在子任务失败或计划需要变化时
**修订**列表（重发完整的 `subtasks`）。只给 index 即可——`current_subtask` 默认取
`subtasks[subtask_index]`。

规划器返回单个 JSON 对象。本地模型与 API 模型共用一条代码路径。后续可在同一
`VLMPlanner` 协议下接入原生 function-calling，无需改动 loop。

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

规划器按工具清单里的 **canonical tool name** 调用工具，格式为
`namespace___tool_name`，例如 `demo_robot___monitor`。这是 VLM 暴露层的稳定名字；
loop/parser 会自动拆回内部的 `namespace + tool_name` 路由。旧的
`{"namespace": "demo_robot", "name": "monitor"}` 和 `demo_robot/monitor` 仍兼容，
但新 prompt 会优先展示 canonical 名。

prompt 里的工具清单——canonical 名称、描述、参数 schema——完全来自
`tool_client.list_tools()`（MCP 自描述，含每个工具的 `inputSchema`）。因此新增 MCP
工具后，规划器会自动看到，通常不需要改 config。

承担 `monitor` 角色的状态上报工具应返回 `{"status": "running|success|failed", ...}`。
该状态写入会话状态，并在下一步回喂给规划器。如果规划器调用了 MCP `execute` 工具，
则当前步跳过下游 `ExecutorClient`（改由机器人自己的 MCP server 执行）。

默认约定的角色工具名是 `fetch_env`、`monitor`、`execute`，示例配置不会重复列出这些
默认值。只有机器人 MCP server 使用了非标准名字时，才需要做角色映射：

```yaml
loop:
  max_steps: 20              # VLM 推理步数，不包含 monitor-only poll
  monitor_poll_interval_s: 1.0
  max_monitor_polls: 300
  tool_roles:
    fetch_env: observe_scene
    monitor: check_status
    execute: run_subtask
```

大多数新增工具不需要角色映射。loop 还会自动识别任意工具的常见结构化返回：

| 工具返回 | loop 效果 |
|----------|-----------|
| `{"status": "running|success|failed"}` | 更新 monitor 反馈 |
| `{"environment": {...}}` 或 `{"env": {...}}` | 合并场景状态到 planner context |
| `{"executed": true}` | 认为动作已由 MCP 执行，跳过下游 executor |
| `{"agentic_role": "monitor|fetch_env|execute"}` | 非常规返回的显式角色提示 |

## MCP 命名空间（不同机器人 = 不同 server）

每个 MCP server 以一个 `namespace` 注册（一台机器人一个 namespace）。工具注册后会
被统一暴露为 `namespace___tool_name`，避免多机器人、多 server 下的重名冲突。接入一台
机器人只需在 servers 文件里加一条；server 里的新工具会通过 MCP `list_tools()`
自动进入 registry 和 prompt：

```json
{
  "servers": [
    {"namespace": "demo_robot", "transport": "stdio", "command": "python", "args": ["examples/mcp_server_example.py"]},
    {"namespace": "franka_arm", "transport": "sse", "url": "http://localhost:8931/sse"}
  ]
}
```

`examples/mcp_server_example.py` 是一个可运行的 MCP server（基于 FastMCP），暴露
桩实现的 `fetch_env` / `monitor` / `execute` 工具，可用真实 MCP 传输跑通整个 loop。
接入真机时替换为你自己机器人的 MCP server 即可。

离线验证（无需任何真机或网络）可用 `mcp_server/mock_mcp_server/`（进程内状态机，
配置见 `examples/config.mock.yaml`）。更贴近真机的示例见
`mcp_server/x2robot_mcp_server/`，它把 loop 桥接到 x2robot 的 bridge HTTP API
（并附带一个零依赖的 mock bridge 便于本地联调）；配置见
`examples/config.x2robot.yaml` 及该 server 的 README。
dual-Franka 真机部署见 `mcp_server/dual_franka_mcp_server/` 和
`examples/config.dual_franka.yaml`：图像由 `HTTPDataLoader` 从 HTTP 获取，
`monitor` / `execute` / 其它控制工具由 MCP adapter 转发到 HTTP bridge。

仅做配置级冒烟测试时，也可以直接用 `mcp.provider: fake` 在 YAML 中声明进程内工具，
不依赖 MCP SDK：

```yaml
mcp:
  provider: fake
  tools:
    - namespace: mock
      name: monitor
      result: {status: running}
      echo_args: true
```

`vlm.provider: scripted` 可让规划器按配置返回 JSON 脚本，用于 demo 和回归测试；
真机/真实模型仍使用 `openai_compatible` 或 `local_qwen`。

## 图像采集（DataLoader）

VLM 规划器需要看到当前观测。图像由独立的 **DataLoader** 提供，MCP 负责结构化工具，
DataLoader 负责视觉观测。

| Provider | 配置项 | 用途 |
|----------|--------|------|
| `http` | `dataloader.url` | 轮询相机或机器人 bridge HTTP 端点。 |
| `mock` | — | 离线生成合成 JPEG 帧。 |
| `static` | — | 包装 CLI `--image` 文件。 |
| `none` | — | 不自动注入图像。 |

loop 会在每一步调用 VLM 前捕获新图像；当规划器调用 `fetch_env` 后，还会再次捕获，
让下一步看到更新后的场景。

## 运行

一次性运行一个任务，逐步输出 `AgenticStepResult` JSON：

```bash
dualsystem-agentic run \
  --config examples/config.mock.yaml \
  --task "turn on the radio and tidy the table"

# 静态图片兼容旧用法。
dualsystem-agentic run \
  --config examples/config.openai.yaml \
  --task "turn on the radio and tidy the table" \
  --image main=./obs.jpg
```

推荐的在线真机入口是配置驱动脚本 `examples/run_online_robot.py`。它会初始化一次
VLM、MCP、Executor、DataLoader，然后持续等待长程任务输入。每条任务使用新的
`AgenticSessionState`；任务完成、超过步数、报错或任务中 Ctrl-C 后，都会回到等待状态。
输入 `/quit` 或 `/exit` 退出。

```bash
# 离线 mock robot：scripted VLM + fake MCP + mock images。
PYTHONPATH=src python examples/run_online_robot.py --config examples/config.mock.yaml

# 真机/仿真：只切换配置。
PYTHONPATH=src python examples/run_online_robot.py --config examples/config.x2robot.yaml
PYTHONPATH=src python examples/run_online_robot.py --config examples/config.dual_franka.yaml

# 非交互调试任务。
PYTHONPATH=src python examples/run_online_robot.py \
  --config examples/config.mock.yaml \
  --tasks "pick up the cup" "place it on the shelf" \
  --print-components
```

CLI 命令只是同一套 `dualsystem_agentic.app` 装配逻辑的薄包装：

```bash
dualsystem-agentic online --config examples/config.mock.yaml
dualsystem-agentic online --config examples/config.mock.yaml --max-steps 8
dualsystem-agentic online --config examples/config.mock.yaml --log-dir runs/debug
dualsystem-agentic online --config examples/config.mock.yaml --no-log
```

交互层通过配置切换：

```yaml
interaction:
  provider: console
  show_raw_json: false
```

也可以使用无第三方依赖的 TUI：

```yaml
interaction:
  provider: tui
  prompt: "robot> "
  max_log_lines: 1000
```

TUI 基于 Python 标准库 `curses`，在非 TTY 环境会自动退回 console。

可选日志记录通过 `logging` 配置。启用后，每次进程运行会创建一个 run 目录，
同时写入完整结构化的 `events.jsonl`、面向人工排查的 `events.log`，以及专门查看
提示词的 `prompt.log`。JSONL 保留完整 run/session/step 事件，方便脚本解析；
`events.log` 会按会话和 step 摘要展示子任务、图片路径、工具调用、工具结果、
executor 结果、解析错误和停止原因；`prompt.log` 会按 step 分块保存完整渲染后的
planner prompt。图片会保存为文件，JSONL 只保存路径、hash 和大小引用，不直接嵌入
base64。

```yaml
logging:
  enabled: true
  root_dir: runs
  save_images: true
```

## 编程接口

```python
from dualsystem_agentic import AgenticRobotLoop, CallablePlanner, FakeMCPToolClient, ExecutorOutput

tools = FakeMCPToolClient()
tools.register("monitor", lambda args: {"status": "running"}, namespace="demo_robot")

class Executor:
    def execute(self, executor_input):
        return ExecutorOutput.success({"ack": executor_input.subtask})

loop = AgenticRobotLoop(my_vlm_planner, tools, Executor())
results, state = loop.run("turn on the radio", max_steps=10)

# 多条在线任务可复用同一个 loop，交给 OnlineAgentRuntime 管理。
```

## 结构精简建议

当前结构按运行边界拆分，而不是按机器人型号拆分：`core/` 管 agent loop 和状态，
`vlm/` 管规划器适配，`mcp/` 管工具路由，`executor/` 管下游执行，`io/` 管观测，
`app.py` 管 config 驱动装配，`runtime.py` / `interaction.py` / `run_logger.py` 管在线运行。
后续接新机器人时，优先把差异放进配置和 MCP server，避免在 `AgenticRobotLoop` 或
`cli.py` 中增加 per-robot 分支；demo/test 专用的脚本 VLM、fake MCP 也保持在边界适配层，
不污染核心 loop。

## 测试

```bash
pytest
```
