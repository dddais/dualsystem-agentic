# dualsystem-agentic

`dualsystem-agentic` 是一个面向长程机器人任务的分布式 agentic 控制框架：VLM 负责任务规划，`MCP_tools` 负责动作与状态接口，Robot Runtime 负责机器人侧执行、图像和监控。

## 整体功能

一句话：系统围绕一个全流程 loop 循环运行：获取用户长程任务，大脑 VLM 根据当前环境图像规划子任务，`execute` 执行子任务，`monitor` 监控结果，并用 monitor 状态驱动下一轮决策。

| 模块 | 作用 | 代码位置 |
|------|------|----------|
| loop 循环 | 维护长程任务闭环：接收任务、组织 VLM 输入、解析 planner JSON、注入 `execute`、接收 `monitor` 反馈并推进下一轮决策 | `src/dualsystem_agentic/core/loop.py`、`src/dualsystem_agentic/runtime.py` |
| MCP_tools | 将机器人能力封装成 VLM 可见工具；VLM 通过 tool list 选择能力，MCP adapter 再把调用转发给 Robot Runtime | `mcp_server/dual_franka_mcp_server/server.py`、`src/dualsystem_agentic/mcp/` |
| Session State | 保存一次长程任务的运行上下文，包括任务文本、子任务列表、当前子任务、monitor 状态、active execution、历史 tool 结果、环境信息和待处理事件；它负责把上一轮执行/监控结果带入下一轮 VLM 输入。 | `src/dualsystem_agentic/core/types.py::AgenticSessionState` |

当前 Dual-Franka 已实现的 `MCP_tools`：

说明：这些 tool 的 MCP 接口已经存在，但并不代表机器人端动作都已真实实现。当前 Dual-Franka 的 `RobotDriver` 仍是 placeholder；真实接入机器人端/远端服务的主要是 `monitor` 链路。

| Tool | MCP 状态 | 机器人端真实实现状态 | 作用 | Robot Runtime 接口 |
|------|----------|----------------------|------|--------------------|
| `execute` | 默认启用 | 接口已打通到 runtime，但当前 driver 是 placeholder，不会直接移动真实硬件 | 启动一个子任务，并返回 execution / monitor 标识 | `POST /executions` |
| `monitor` | 默认启用 | 已支持机器人端/远端 monitor provider；当前推荐通过 `remote_http` 接入外部 monitor 服务 | 查询当前子任务状态，返回 `running`、`success` 或 `failed` | `POST /monitors/status` |
| `stop_task` | 默认启用 | 接口已打通到 runtime，真实停止行为取决于后续接入的 driver | 停止当前任务 | `POST /control/stop` |
| `emergency_stop` | 默认启用 | 接口已打通到 runtime，真实急停行为取决于后续接入的 driver / safety 机制 | 急停 Robot Runtime | `POST /control/emergency_stop` |
| `fetch_env` | 条件启用 | 仅在有真实结构化环境 provider 时建议开启；默认不作为主要观测来源 | 获取结构化环境状态 | 默认转发 `/environment` |
| `reset_task` | 条件启用 | 默认隐藏；需要确认机器人侧 reset 行为安全并实现后再开启 | 复位任务或机械臂 | `POST /control/reset` |

新增 `MCP_tools` 的方式：

| 扩展步骤 | 修改位置 | 修改内容 |
|----------|----------|----------|
| 暴露工具 | `mcp_server/dual_franka_mcp_server/server.py::list_tools()` | 增加 `types.Tool`，写清 tool 名、描述和 `inputSchema` |
| 分发调用 | `mcp_server/dual_franka_mcp_server/server.py::_dispatch()` | 增加 tool 名对应分支，转发到已有或新增 HTTP 接口 |
| 扩展机器人侧能力 | `robot_runtime/robot_runtime/api/app.py`、`robot_runtime/robot_runtime/core/runtime.py`、driver adapter | 如果新 tool 需要真实机器人动作，同步新增 HTTP endpoint、runtime 方法和 driver 方法 |
| 配置给 Agent | `examples/config.*.yaml` | 确保 MCP server 已注册；启动后 tool 会通过 MCP `list_tools()` 自动进入 VLM prompt |

## 分布式部署框架

分为四部分：Agent Runtime 负责任务推理，MCP_tools Adapter 负责把能力包装成 tools，Robot Machine 负责打包机器人相关接口并暴露统一 HTTP API，Monitor 负责对子任务执行结果做独立判断。

| 部分 | 功能 | 运行位置 | 接口 |
|------|------|----------|------|
| Agent Runtime | 读取用户任务，调用 VLM 做长程规划，维护 session / subtask 状态，采集图像并触发 tool call | Agent 机器 | 读取 YAML 配置；通过 MCP stdio 连接 MCP_tools；通过 `DataLoader` 读取图像 |
| MCP_tools Adapter | 将机器人能力包装成 VLM 可见的 `MCP_tools`，例如 `execute`、`monitor`、`stop_task`、`emergency_stop`；把 tool call 转发到 Robot Runtime | 通常运行在 Agent 机器，作为 stdio MCP server 子进程 | 对 Agent 暴露 MCP stdio；对 Robot Runtime 调用 HTTP API |
| Robot Machine / Robot Runtime | 打包机器人相关接口并暴露：robot driver、camera provider、monitor provider、安全控制、执行状态管理 | Robot 机器，靠近真实机器人硬件 | 暴露 HTTP：`/executions`、`/monitors/status`、`/observations/latest`、`/control/*`、`/health`、`/capabilities` |
| Monitor | 根据子任务、图像和执行上下文判断当前子任务是否 `running/success/failed`；可作为独立服务由 Robot Runtime 的 `remote_http` provider 调用 | Monitor 机器或 Robot 机器；依实际部署而定 | 建议 contract：`POST /monitors/start`、`POST /monitors/status`、`POST /monitors/stop`、`GET /health` |

Monitor / GRM 服务接在 Robot Runtime 后面，由 `monitor.provider: remote_http` 适配；Agent 侧仍只看到 `monitor` 这个 MCP tool。详情参考 Robo-dopamine 仓库。

## 工作流及示例

### Loop 状态机

当前 loop 有轻量状态机，状态定义在 `src/dualsystem_agentic/core/types.py` 的 `AgenticPhase`：`init`、`ready`、`reason`、`act`、`response`、`done`、`error`。

```text
init
  |
  v
ready
  |
  | 用户输入 long-horizon task
  v
reason
  |
  | VLM 根据 task / images / session state / MCP tool list 输出 planner JSON
  v
解析与路由
  |
  +-- no tool_call / wait / observe --> response --> ready 或下一轮 reason
  |
  +-- decision="execute"
  |      |
  |      v
  |    act --> MCP execute --> active_execution=running --> response
  |                                               |
  |                                               v
  |                                      async monitor / poll_monitor
  |                                               |
  |                     running ------------------+
  |                                               |
  |                     success / failed / timeout
  |                                               |
  |                                               v
  |                                      pending_events 更新 session state
  |                                               |
  +-----------------------------------------------+
                                                  |
                                                  v
                                                reason

终止分支：
  task_complete=true -> done
  parse/tool/consistency error -> error
```

对应关系说明：

- `Memory` 模块；对应 `AgenticSessionState` 中的 `subtasks`、`subtask_statuses`、`last_tool_results`、`environment`、`active_execution`、`pending_events`，以及 loop 暂存的最近图像。
-  `MCP_tools` 可自定义扩展；当前代码只内置 `execute` 注入、`monitor` 状态解析和少量控制语义，不把 `start_monitor`、`generate_field`、`navigate_to` 等写死在 loop 中。

### 工作流示意图

```text
用户任务
  |
  v
OnlineAgentRuntime / examples/run_online_robot.py
  |
  v
AgenticRobotLoop.step()
  |
  +--> DataLoader.capture()
  |      |
  |      v
  |   最新图像
  |
  +--> MCPToolClient.list_tools()
  |      |
  |      v
  |   VLM 可见工具列表
  |
  v
VLMPlanner.generate(planner_input)
  |
  v
Planner JSON
  |
  +-- decision="execute"
  |      |
  |      v
  |   loop 注入 execute tool call
  |      |
  |      v
  |   dual_franka MCP adapter
  |      |
  |      v
  |   Robot Runtime POST /executions
  |
  +-- monitor tool call
         |
         v
      Robot Runtime POST /monitors/status
         |
         v
      running / success / failed
         |
         v
      事件回流，进入下一轮 planner
```

### Planner 协议

Planner 返回单个 JSON 对象。第一次通常给出完整计划；后续按 `subtask_index` 选择当前子任务。需要启动动作时设置 `decision="execute"`。

```json
{
  "decision": "execute",
  "subtasks": ["pick up the cup", "place the cup on the shelf"],
  "subtask_index": 0,
  "task_complete": false
}
```

Loop 会自动注入配置中的 `execute` tool call：

```json
{"subtask": "pick up the cup", "subtask_index": 0}
```

执行启动后，loop 保存 `active_execution`，再通过 `monitor` 获取 `running|success|failed`。当 monitor 返回终态后，事件进入下一轮 planner input，VLM 决定继续、重试、replan、取消或结束任务。

### Dual-Franka 分布式运行

以下命令假设 conda 环境已经按“环境准备”完成。运行时只需要激活环境并启动对应 Python 文件。

Robot 侧启动 Robot Runtime：

```bash
conda activate dualsystem-robot-runtime

python robot_runtime/robot_runtime/api/app.py \
  --config robot_runtime/robot_runtime/configs/dual_franka.runtime.yaml \
  --host 0.0.0.0 \
  --port 8767
```

Agent 侧启动在线任务入口：

```bash
conda activate dualsystem-agentic

export OPENAI_BASE_URL="https://..."
export OPENAI_API_KEY="..."
export DUAL_FRANKA_RUNTIME_URL="http://<robot-machine-ip>:8767"

python examples/run_online_robot.py \
  --config examples/config.dual_franka.runtime.yaml
```

配置中需要保证两处 runtime 地址一致：

- `mcp.servers[].env.DUAL_FRANKA_RUNTIME_URL`
- `dataloader.url`

建议交付时把真实地址和密钥写成环境变量，例如：

```yaml
vlm:
  provider: openai_compatible
  model: gpt-4o
  base_url: ${OPENAI_BASE_URL}
  api_key: ${OPENAI_API_KEY}

mcp:
  provider: sdk
  servers:
    - namespace: dual_franka
      transport: stdio
      command: python
      args: ["mcp_server/dual_franka_mcp_server/server.py"]
      env:
        DUAL_FRANKA_RUNTIME_URL: ${DUAL_FRANKA_RUNTIME_URL}

dataloader:
  provider: http
  url: ${DUAL_FRANKA_RUNTIME_URL}/observations/latest
  image_key: concatenated_image
  label: main
```

健康检查：

```bash
curl http://<robot-machine-ip>:8767/health
curl http://<robot-machine-ip>:8767/capabilities
curl http://<robot-machine-ip>:8767/observations/latest/metadata
```

## 代码结构

```text
src/dualsystem_agentic/
  core/          # agent loop、planner parser、prompt、JSON-safe types
  vlm/           # openai-compatible、本地 Qwen、scripted planner provider
  mcp/           # MCP connection、manager、registry、tool client abstraction
  io/            # DataLoader 与 image normalization
  executor/      # noop / HTTP executor 兼容层
  config.py      # YAML/JSON config 与组件 factory
  app.py         # config-driven app builder
  runtime.py     # 在线多任务 runtime
  interaction.py # console / curses TUI
  run_logger.py  # JSONL、prompt、图片引用日志
  cli.py         # 命令行入口

mcp_server/
  dual_franka_mcp_server/server.py # Dual-Franka MCP adapter

robot_runtime/
  robot_runtime/api/app.py         # FastAPI HTTP service
  robot_runtime/core/runtime.py    # execution / monitor / observation orchestration
  robot_runtime/adapters/          # robot driver、camera provider、monitor provider
  robot_runtime/configs/           # robot-side runtime config

examples/
  run_online_robot.py              # 在线任务入口
  config.dual_franka.runtime.yaml  # Dual-Franka runtime 配置
  visualize_run_video.py           # 日志视频渲染

tests/                             # loop、runtime、MCP adapter、CLI 等测试
```

## 可扩展模块及扩展方式

### 新增 VLM provider

| 项 | 内容 |
|----|------|
| 文件 | 新增 `src/dualsystem_agentic/vlm/<provider>.py`；修改 `src/dualsystem_agentic/config.py` |
| 改什么 | 实现 `generate(planner_input) -> str`，并在 `build_vlm()` 中按 `vlm.provider` 创建实例 |
| 原因 | Agent loop 只依赖 `VLMPlanner` 协议，新增模型不需要改 loop |

示例：

```python
# src/dualsystem_agentic/vlm/my_provider.py
from dualsystem_agentic.core.types import AgenticPlannerInput


class MyVLMPlanner:
    def __init__(self, model: str, api_key: str | None = None) -> None:
        self.model = model
        self.api_key = api_key

    def generate(self, planner_input: AgenticPlannerInput) -> str:
        return '{"decision": "plan", "subtasks": ["inspect the scene"], "subtask_index": 0, "task_complete": false}'
```

```python
# src/dualsystem_agentic/config.py
if provider == "my_provider":
    from dualsystem_agentic.vlm.my_provider import MyVLMPlanner

    if not config.model:
        raise ValueError("vlm.model is required for my_provider")
    return MyVLMPlanner(model=config.model, api_key=config.api_key)
```

配置示例：

```yaml
vlm:
  provider: my_provider
  model: my-vlm
  api_key: ${MY_VLM_API_KEY}
```

### 新增 DataLoader

| 项 | 内容 |
|----|------|
| 文件 | 修改 `src/dualsystem_agentic/io/dataloader.py` 和 `src/dualsystem_agentic/config.py` |
| 改什么 | 新增实现 `capture() -> CameraFrame | None` 的类；在 `build_dataloader()` 中注册 provider |
| 原因 | 图像输入与 MCP tool 解耦，可替换相机来源而不影响工具协议 |

示例：

```python
# src/dualsystem_agentic/io/dataloader.py
import time

from dualsystem_agentic.core.types import ImageInput
from dualsystem_agentic.io.dataloader import CameraFrame


class MyCameraDataLoader:
    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout

    def capture(self) -> CameraFrame | None:
        image_b64 = fetch_image_as_base64(self.url, timeout=self.timeout)
        return CameraFrame(
            images={"main": ImageInput(type="base64", data=image_b64, mime_type="image/jpeg")},
            timestamp=time.time(),
        )
```

```python
# src/dualsystem_agentic/config.py
if config.provider == "my_camera":
    if not config.url:
        raise ValueError("dataloader.url is required for my_camera")
    return MyCameraDataLoader(url=config.url, timeout=config.timeout)
```

配置示例：

```yaml
dataloader:
  provider: my_camera
  url: http://<camera-service>/latest
  label: main
```

### 新增 MCP server 或新机器人

| 项 | 内容 |
|----|------|
| 文件 | 新增 `mcp_server/<robot>_mcp_server/server.py`；新增或修改 `examples/config.<robot>.yaml` |
| 改什么 | 在 MCP server 中实现 `list_tools()` 和 `call_tool()`；在配置里注册 `mcp.servers[]` |
| 原因 | Agent 启动后会通过 MCP `list_tools` 自动获得工具清单，并把工具写入 planner prompt |

配置示例：

```yaml
mcp:
  provider: sdk
  servers:
    - namespace: new_robot
      description: New robot tools exposed as new_robot___<tool_name>.
      transport: stdio
      command: python
      args: ["mcp_server/new_robot_mcp_server/server.py"]
      env:
        NEW_ROBOT_RUNTIME_URL: ${NEW_ROBOT_RUNTIME_URL}
```

Loop 默认识别三个工具角色：

| 角色 | 默认 tool 名 | 期望行为 |
|------|--------------|----------|
| 环境获取 | `fetch_env` | 返回结构化 `scene_graph`、`environment` 或 `env` |
| 执行 | `execute` | 启动当前子任务，返回 execution / monitor 标识 |
| 监控 | `monitor` | 返回 `running`、`success` 或 `failed` |

如果新机器人使用不同 tool 名，在配置中声明映射：

```yaml
loop:
  tool_roles:
    fetch_env: observe_scene
    execute: run_subtask
    monitor: check_status
```

### 新增 tool

| 项 | 内容 |
|----|------|
| 文件 | Dual-Franka tool 修改 `mcp_server/dual_franka_mcp_server/server.py`；若需要机器人侧新能力，同时修改 `robot_runtime/robot_runtime/api/app.py`、`robot_runtime/robot_runtime/core/runtime.py` 和对应 driver |
| 改什么 | 在 `list_tools()` 增加 `types.Tool`；在 `_dispatch()` 增加分支；必要时新增 HTTP endpoint 和 `RobotRuntime` 方法 |
| 原因 | VLM 可见工具来自 MCP `list_tools()`，普通工具增删不需要改 agent loop |

只新增 MCP 转发工具的示例：

```python
# mcp_server/dual_franka_mcp_server/server.py
tools.append(
    types.Tool(
        name="open_gripper",
        description="Open the selected gripper.",
        inputSchema={
            "type": "object",
            "required": ["arm"],
            "properties": {
                "arm": {"type": "string", "enum": ["left", "right"]}
            },
        },
    )
)
```

```python
# mcp_server/dual_franka_mcp_server/server.py
if name == "open_gripper":
    return await _request(client, "POST", "/control/open_gripper", json_data=arguments)
```

如果 runtime 也要执行新动作，继续补齐：

```python
# robot_runtime/robot_runtime/api/app.py
@app.post("/control/open_gripper")
async def control_open_gripper(body: dict[str, Any]):
    return _ok(runtime.open_gripper(body))
```

```python
# robot_runtime/robot_runtime/core/runtime.py
def open_gripper(self, payload: JsonDict) -> JsonDict:
    return self.robot_driver.open_gripper(str(payload["arm"]))
```

新增后建议至少验证：

```bash
curl -X POST http://<robot-machine-ip>:8767/control/open_gripper \
  -H "Content-Type: application/json" \
  -d '{"arm":"left"}'
```

### 扩展 Robot Runtime / RobotDriver

| 项 | 内容 |
|----|------|
| 文件 | `robot_runtime/robot_runtime/adapters/dual_franka/robot_driver.py`、`robot_runtime/robot_runtime/api/app.py`、`robot_runtime/robot_runtime/core/runtime.py` |
| 改什么 | 用真实 driver 替换 `PlaceholderDualFrankaRobotDriver`；如增加 runtime 能力，同步补 endpoint、runtime 方法和 driver 方法 |
| 原因 | 保持 Agent/MCP/HTTP contract 稳定，机器人侧可独立升级 |

当前代码只支持：

- `robot.type: dual_franka`
- `robot.driver: placeholder`
- `camera.provider: local_files`
- `monitor.provider: local_memory`、`local_grm`、`remote_http`

接入真实硬件时，建议先保持 `/executions`、`/monitors/status`、`/observations/latest` 和 `/control/*` 的返回结构不变，只替换 driver 内部实现。

## 环境准备

Python 要求：`>=3.10`。推荐用两个 conda 环境隔离 Agent 侧和 Robot Runtime 侧。

Agent 环境：

```bash
cd dualsystem-agentic
conda create -n dualsystem-agentic python=3.10 -y
conda activate dualsystem-agentic

pip install -e ".[mcp]"
```

Agent 可选依赖：

```bash
pip install -e ".[local-qwen]" # 本地 Qwen VLM
pip install -e ".[dev]"        # pytest
```

Robot Runtime 环境：

```bash
cd dualsystem-agentic
conda create -n dualsystem-robot-runtime python=3.10 -y
conda activate dualsystem-robot-runtime

pip install -e ./robot_runtime
```

### 配置字段

| 字段 | 作用 |
|------|------|
| `vlm` | planner provider、模型、API 地址、采样参数、visual scene prepass |
| `mcp` | MCP servers 配置 |
| `executor` | `noop` 或 HTTP executor；当前 MCP `execute` 路径通常使用 `noop` |
| `loop` | `max_steps`、reason/monitor 间隔、tool role 映射 |
| `dataloader` | 图像来源 |
| `interaction` | `console` 或 `tui` |
| `logging` | run/session/step JSONL 日志和图片保存 |

配置字符串会经过环境变量展开：

```yaml
vlm:
  provider: openai_compatible
  model: gpt-4o
  base_url: ${OPENAI_BASE_URL}
  api_key: ${OPENAI_API_KEY}
```

## 日志与可视化

启用 `logging.enabled=true` 后，每次 run 会生成 `events.jsonl`、`events.log`、`prompt.log` 和按 step 保存的图片引用。渲染视频需要系统安装 `ffmpeg`：

```bash
python examples/visualize_run_video.py \
  --run-dir runs/run_YYYYMMDD_HHMMSS
```

## 交付注意事项

- `python robot_runtime/robot_runtime/api/app.py --host 0.0.0.0` 会暴露控制接口，请只在受控网络中使用，并为真实硬件增加访问控制和急停策略。
- 当前 Dual-Franka driver 是 placeholder；只有接入真实 `RobotDriver` 后才会移动硬件。
- `mcp_server/` 当前作为源码目录使用；如果只安装 wheel 而不保留仓库源码，需要为 MCP server 提供独立入口，或调整配置中的 `command/args`。
