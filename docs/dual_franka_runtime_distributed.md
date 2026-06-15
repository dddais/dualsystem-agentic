# Dual-Franka Robot Runtime 系统结构与分布式部署

本文档说明 `dev_FSM_add_runtime` 分支中新的 Dual-Franka 真机运行结构、数据流，以及单机/多机分布式部署方法。

## 1. 系统结构

新的推荐结构是“薄 MCP adapter + 常驻 Robot Runtime”：

```text
Agent Machine
  examples/run_online_robot.py
  dual_franka_mcp_server/server.py
        |
        | HTTP tools
        v
Robot Machine
  robot_runtime/api/app.py
  RobotRuntime
    - ExecutionStore / MonitorStore
    - RobotDriver
    - CameraProvider
    - MonitorProvider
    - Safety/Control API
        |
        | optional HTTP
        v
Monitor Machine
  Robo-dopamine/monitor_runtime/service.py
```

各模块职责：

| 模块 | 职责 |
|------|------|
| Agent loop | VLM 高层推理、任务分解、选择 MCP tools、维护 `active_execution` |
| MCP adapter | 把 `execute/monitor/stop/reset` 转发到 Robot Runtime HTTP API；`fetch_env` 只在有结构化 scene graph provider 时启用 |
| Robot Runtime | 统一管理 `execution_id`、`monitor_id`、图像观测、执行状态、monitor 生命周期 |
| RobotDriver | 适配具体机器人控制接口；当前 Dual-Franka v1 是安全 placeholder，不直接移动硬件 |
| CameraProvider | 提供统一图像出口；当前 Dual-Franka v1 从 `/tmp/img` 读三路 JPEG |
| MonitorProvider | 本地 monitor 或远端 monitor service 的统一适配层 |
| Robo-Dopamine monitor service | 分布式 monitor contract skeleton；后续可接真实 GRM worker |

旧的 `dual_franka_bridge.py` 仍保留给兼容/debug 使用，但推荐真机入口已经变为 `robot_runtime/api/app.py`。

## 2. API 与数据流

### 2.1 Agent 侧工具流

```text
VLM planner
  -> tool call: dual_franka___execute(subtask)
  -> MCP stdio adapter
  -> POST http://robot-runtime:8767/executions
  <- execution_id, monitor_id, status

Agent loop:
  active_execution = {
    execution_id,
    monitor_id,
    subtask,
    status: running
  }

monitor polling:
  -> dual_franka___monitor(execution_id, monitor_id)
  -> POST http://robot-runtime:8767/monitors/status
  <- running | success | failed
```

Runtime 标准接口：

| Method | Path | 用途 |
|--------|------|------|
| `GET` | `/health` | runtime 健康状态 |
| `GET` | `/capabilities` | robot/camera/tools 能力描述 |
| `GET` | `/environment` | runtime 结构化状态 |
| `GET` | `/observations/latest` | base64 JSON 图像观测，供 agent `HTTPDataLoader` 使用 |
| `GET` | `/observations/latest/metadata` | 最新 frame metadata 与 binary image endpoints |
| `GET` | `/observations/latest/{camera}.jpg` | 单路 binary JPEG，推荐 monitor 使用 |
| `POST` | `/executions` | 启动一个 subtask，返回 `execution_id`/`monitor_id` |
| `POST` | `/monitors/status` | 查询 monitor 状态 |
| `POST` | `/control/stop` | 停止当前执行 |
| `POST` | `/control/reset` | reset |
| `POST` | `/control/emergency_stop` | 急停 |

### 2.2 图像数据流

```text
Robot cameras / image producer
  -> /tmp/img/base_0_rgb.jpg
  -> /tmp/img/left_wrist_0_rgb.jpg
  -> /tmp/img/right_wrist_0_rgb.jpg
  -> Robot Runtime CameraProvider
      -> GET /observations/latest
      -> Agent HTTPDataLoader
      -> VLM images

      -> GET /observations/latest/metadata
      -> GET /observations/latest/{camera}.jpg
      -> Monitor service / GRM worker
```

`/tmp/img` 现在只是 `DualFrankaLocalFileCameraProvider` 的内部实现，不再作为 agent、MCP、monitor 之间的通信协议。

Agent 继续使用兼容 `HTTPDataLoader` 的 base64 JSON endpoint：

```json
{
  "concatenated_image": "...base64 jpeg...",
  "cam_high": "...base64 jpeg...",
  "cam_left_wrist": "...base64 jpeg...",
  "cam_right_wrist": "...base64 jpeg...",
  "frame_id": "frame-...",
  "timestamp": 1710000000.0
}
```

Monitor 推荐使用 metadata + binary JPEG endpoint，避免 base64 体积膨胀和 JSON 解码开销：

```bash
curl http://ROBOT_MACHINE_IP:8767/observations/latest/metadata
curl http://ROBOT_MACHINE_IP:8767/observations/latest/cam_high.jpg --output cam_high.jpg
curl http://ROBOT_MACHINE_IP:8767/observations/latest/cam_left_wrist.jpg --output cam_left_wrist.jpg
curl http://ROBOT_MACHINE_IP:8767/observations/latest/cam_right_wrist.jpg --output cam_right_wrist.jpg
```

`/observations/latest/metadata` 返回示例：

```json
{
  "frame_id": "frame-...",
  "timestamp": 1710000000.0,
  "cameras": ["cam_high", "cam_left_wrist", "cam_right_wrist"],
  "binary_endpoints": {
    "cam_high": "/observations/latest/cam_high.jpg",
    "cam_left_wrist": "/observations/latest/cam_left_wrist.jpg",
    "cam_right_wrist": "/observations/latest/cam_right_wrist.jpg",
    "concatenated_image": "/observations/latest/concatenated_image.jpg"
  }
}
```

### 2.3 分布式 monitor 数据流

远端 monitor 模式下：

```text
Agent Machine
  -> MCP execute
  -> Robot Runtime /executions

Robot Runtime
  -> RobotDriver.execute(...)
  -> RemoteHTTPMonitorProvider.start(...)
  -> POST http://monitor-machine:8877/monitors/start

Agent poll
  -> MCP monitor
  -> Robot Runtime /monitors/status
  -> RemoteHTTPMonitorProvider.status(...)
  -> POST http://monitor-machine:8877/monitors/status
```

Monitor service contract：

```text
POST /monitors/start
POST /monitors/status
POST /monitors/stop
GET  /health
```

`monitor_runtime/service.py` 当前提供 deterministic backend，主要用于验证分布式协议。真实 GRM worker 后续应接在同一 contract 后面。

真实 GRM worker 获取图像时推荐：

1. 从 `ROBOT_RUNTIME_URL` 请求 `/observations/latest/metadata`。
2. 根据 `binary_endpoints` 拉取需要的相机 JPEG bytes。
3. 解码 JPEG，运行 GRM。
4. 在 `/monitors/status` 响应中返回 `status`、`progress`、`frame_id`。

## 3. 单机部署

适用于 agent、runtime、monitor 都在同一台机器上的调试。

### 3.1 启动 Robot Runtime

```bash
cd /home/ubuntu/dais/dualsystem-agentic

python robot_runtime/api/app.py \
  --config robot_runtime/configs/dual_franka.runtime.yaml \
  --port 8767
```

当前默认 runtime 配置：

```yaml
robot:
  type: dual_franka
  driver: placeholder

camera:
  provider: local_files
  image_dir: /tmp/img

monitor:
  provider: local_grm
  default_status: running
  auto_success_after_polls: 0
```

说明：

- `driver: placeholder` 不移动真实硬件，只记录 execute 请求。
- `camera.provider: local_files` 从 `/tmp/img` 读取图像。
- `monitor.provider: local_grm` 当前接到 runtime-owned placeholder monitor provider；后续可替换为真实 GRM worker。

### 3.2 启动 Agent

```bash
cd /home/ubuntu/dais/dualsystem-agentic

PYTHONPATH=src python examples/run_online_robot.py \
  --config examples/config.dual_franka.runtime.yaml
```

agent config 中关键项：

```yaml
mcp:
  servers:
    - namespace: dual_franka
      args: ["mcp_server/dual_franka_mcp_server/server.py"]
      env:
        DUAL_FRANKA_BRIDGE_URL: http://localhost:8767
        # 默认不要开启 fetch_env；图像已经由 dataloader 注入。
        # 只有接入真实结构化 scene graph provider 后再打开。
        # DUAL_FRANKA_FETCH_ENV_HTTP: "true"
        DUAL_FRANKA_MONITOR_PATH: /monitors/status
        DUAL_FRANKA_EXECUTE_PATH: /executions

dataloader:
  provider: http
  url: http://localhost:8767/observations/latest
```

## 4. 多机分布式部署

推荐分成三类机器：

```text
agent-machine:
  run_online_robot.py
  dual_franka_mcp_server/server.py

robot-machine:
  robot_runtime/api/app.py
  robot control / cameras / image producer

monitor-machine:
  Robo-dopamine/monitor_runtime/service.py
  future GRM GPU worker
```

### 4.1 Robot Machine

编辑 runtime 配置，让 monitor 指向远端：

```yaml
robot:
  type: dual_franka
  driver: placeholder

camera:
  provider: local_files
  image_dir: /tmp/img

monitor:
  provider: remote_http
  url: http://MONITOR_MACHINE_IP:8877
  timeout: 30.0

safety:
  max_execution_s: 300
  require_estop_ready: false
```

启动：

```bash
cd /home/ubuntu/dais/dualsystem-agentic

python robot_runtime/api/app.py \
  --config robot_runtime/configs/dual_franka.runtime.yaml \
  --host 0.0.0.0 \
  --port 8767
```

检查：

```bash
curl http://ROBOT_MACHINE_IP:8767/health
curl http://ROBOT_MACHINE_IP:8767/capabilities
curl http://ROBOT_MACHINE_IP:8767/observations/latest
curl http://ROBOT_MACHINE_IP:8767/observations/latest/metadata
curl http://ROBOT_MACHINE_IP:8767/observations/latest/cam_high.jpg --output cam_high.jpg
```

### 4.2 Monitor Machine

当前 deterministic monitor service：

```bash
cd /home/ubuntu/dais/Robo-dopamine

python monitor_runtime/service.py \
  --host 0.0.0.0 \
  --port 8877 \
  --robot-runtime-url http://ROBOT_MACHINE_IP:8767
```

如果只想拉部分相机，可以重复指定 `--camera`：

```bash
python monitor_runtime/service.py \
  --host 0.0.0.0 \
  --port 8877 \
  --robot-runtime-url http://ROBOT_MACHINE_IP:8767 \
  --camera cam_high \
  --camera cam_left_wrist
```

调试时可让 monitor 自动成功，同时验证 binary JPEG 拉图链路：

```bash
python monitor_runtime/service.py \
  --host 0.0.0.0 \
  --port 8877 \
  --robot-runtime-url http://ROBOT_MACHINE_IP:8767 \
  --auto-success-after-polls 3
```

检查：

```bash
curl http://MONITOR_MACHINE_IP:8877/health
```

`/health` 中会显示 observation client 配置：

```json
{
  "provider": "deterministic",
  "observation_client": {
    "runtime_url": "http://ROBOT_MACHINE_IP:8767",
    "cameras": ["cam_high", "cam_left_wrist", "cam_right_wrist"],
    "transport": "http_binary_jpeg"
  }
}
```

### 4.3 Agent Machine

把 agent config 中 runtime 地址改为 robot machine：

```yaml
mcp:
  servers:
    - namespace: dual_franka
      env:
        DUAL_FRANKA_BRIDGE_URL: http://ROBOT_MACHINE_IP:8767
        # 默认不要开启 fetch_env；图像已经由 dataloader 注入。
        # 只有接入真实结构化 scene graph provider 后再打开。
        # DUAL_FRANKA_FETCH_ENV_HTTP: "true"
        DUAL_FRANKA_FETCH_ENV_PATH: /environment
        DUAL_FRANKA_MONITOR_PATH: /monitors/status
        DUAL_FRANKA_EXECUTE_PATH: /executions
        DUAL_FRANKA_STOP_PATH: /control/stop
        DUAL_FRANKA_RESET_PATH: /control/reset
        DUAL_FRANKA_ESTOP_PATH: /control/emergency_stop

dataloader:
  provider: http
  url: http://ROBOT_MACHINE_IP:8767/observations/latest
```

如果运行日志里连续多步只出现 `tools=[fetch_env]`，通常说明 `fetch_env`
被暴露成了一个空/弱结构化观察工具，VLM 在反复“观察”而不执行。此时应先关闭
`DUAL_FRANKA_FETCH_ENV_HTTP`，让 VLM 直接基于 dataloader 图像和 `execute`
tool 推进任务；等接入真实 scene graph provider 后再重新开启。

启动：

```bash
cd /home/ubuntu/dais/dualsystem-agentic

PYTHONPATH=src python examples/run_online_robot.py \
  --config examples/config.dual_franka.runtime.yaml
```

## 5. 端到端检查流程

### 5.1 图像检查

确认 robot machine 上有三路图像：

```bash
ls -lh /tmp/img/base_0_rgb.jpg
ls -lh /tmp/img/left_wrist_0_rgb.jpg
ls -lh /tmp/img/right_wrist_0_rgb.jpg
```

确认 runtime 能返回图像：

```bash
curl http://ROBOT_MACHINE_IP:8767/observations/latest
```

如果缺图，runtime 返回 `503`，message 中会列出缺失相机。

monitor 调试时优先检查 binary endpoint，因为它是后续 GRM worker 的推荐图像通道：

```bash
curl -I http://ROBOT_MACHINE_IP:8767/observations/latest/cam_high.jpg
```

### 5.2 执行/monitor 检查

```bash
curl -X POST http://ROBOT_MACHINE_IP:8767/executions \
  -H 'Content-Type: application/json' \
  -d '{"subtask": "pick up the cube", "subtask_index": 0}'
```

记录返回的 `execution_id` 和 `monitor_id`：

```bash
curl -X POST http://ROBOT_MACHINE_IP:8767/monitors/status \
  -H 'Content-Type: application/json' \
  -d '{"execution_id": "exec-...", "monitor_id": "mon-..."}'
```

### 5.3 Agent 检查

启动时可加：

```bash
PYTHONPATH=src python examples/run_online_robot.py \
  --config examples/config.dual_franka.runtime.yaml \
  --print-components
```

确认：

- `mcp_client` 正常启动。
- `dataloader` 是 `HTTPDataLoader`。
- `DUAL_FRANKA_BRIDGE_URL` 指向 robot runtime。
- dataloader URL 指向 `/observations/latest`。

### 5.4 常见报错：`dual_franka.execute` 指向 `/monitors/status`

如果运行日志中出现类似：

```text
tool_error=dual_franka.execute: ... /monitors/status
```

这通常不是 `execute` endpoint 本身的 URL 配错，而是 MCP adapter 的
`execute` tool 在 `POST /executions` 成功返回后，会立即做一次
`POST /monitors/status` 获取初始 monitor 状态。因此错误链路通常是：

```text
agent execute
  -> robot runtime /executions
  -> remote monitor /monitors/start
  -> robot runtime /monitors/status
  -> remote monitor /monitors/status
```

优先检查 robot runtime 的 `monitor.url` 指向的远端 monitor 服务：

```bash
curl http://MONITOR_MACHINE_IP:8877/health
```

再手动检查一次 execution + monitor status：

```bash
EXEC_JSON=$(curl -s -X POST http://ROBOT_MACHINE_IP:8767/executions \
  -H 'Content-Type: application/json' \
  -d '{"subtask": "pick up the cube", "subtask_index": 0}')
echo "$EXEC_JSON"
```

从返回中取出 `execution_id` 和 `monitor_id` 后：

```bash
curl -X POST http://ROBOT_MACHINE_IP:8767/monitors/status \
  -H 'Content-Type: application/json' \
  -d '{"execution_id": "exec-...", "monitor_id": "mon-..."}'
```

如果 monitor service 开启了 `--robot-runtime-url`，还需要确认 monitor machine
能访问 robot runtime 的图像 endpoint：

```bash
curl http://ROBOT_MACHINE_IP:8767/observations/latest/metadata
curl -I http://ROBOT_MACHINE_IP:8767/observations/latest/cam_high.jpg
```

新版本中，远端 monitor 查询失败会尽量返回规范 monitor 状态：

```json
{
  "status": "failed",
  "message": "monitor provider status failed",
  "error": "remote monitor ..."
}
```

如果仍然看到裸 HTTP 500，请确认 robot runtime、agent MCP server 和 monitor
service 都已经重启，并且运行的是当前分支的最新代码。

## 6. 当前限制与后续接入点

当前实现是 runtime 架构 v1：

- `RobotDriver` 是 placeholder，不会直接移动真实硬件。
- `local_grm` 暂时接 runtime-owned placeholder monitor provider。
- `monitor_runtime/service.py` 是分布式 monitor contract skeleton，尚未接完整 GRM 推理 worker。
- 图像仍由 `DualFrankaLocalFileCameraProvider` 从 `/tmp/img` 读取；这是 runtime 内部 adapter 实现，后续可替换为 ROS2、RTSP、shared memory 或 gRPC stream。

后续接真实机器人时，优先替换：

```text
robot_runtime/adapters/dual_franka/robot_driver.py
robot_runtime/adapters/dual_franka/camera_provider.py
robot_runtime/adapters/dual_franka/monitor_provider.py
```

换另一种机器人时，新增对应 adapter，例如：

```text
robot_runtime/adapters/<new_robot>/robot_driver.py
robot_runtime/adapters/<new_robot>/camera_provider.py
robot_runtime/configs/<new_robot>.runtime.yaml
```

Agent loop、MCP tool 语义和 monitor 状态协议保持不变。
