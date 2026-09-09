# Manual / robot-bridge 真机接入

两个 RobotDriver 使用同一套 MCP、Runtime HTTP、GRM Monitor 接口。相机独立配置为
`camera.provider: robot_bridge`，直接读取已运行的 Robot Server，不再经过 `/tmp/img`。
本实现不修改 robot-bridge 源码，不启动第二个 SDK controller，不加载第二份 VLA。

## 部署准备

- 服务器：沿用 VLA Policy Server，以及 GRM Monitor / SAM3。
- 从臂端：沿用 Robot Server、Scheduler；新增 Robot Runtime 和上游 loop。
- Scheduler 可以仍运行在原机器，Runtime 配置中的 `scheduler_url` 指向它。
- 自动模式要求 Scheduler 控制端口已开启，且 checkpoint 有固定 `prompts` 集合。
  保留原 Scheduler 启动参数，增加 `--control-port 8088`。`openpi` 与
  `openpi_takeover` 都支持；无需为普通从臂部署额外启动主臂。

在已有 robot-bridge Python 环境（Python >= 3.11）中安装 Runtime：

```bash
cd /path/to/dualsystem-agentic
python -m pip install -e './robot_runtime[bridge]'
python -c 'import robot_bridge.transport.codec'
```

`robot_bridge` 来自现场已有安装；若 import 失败，在同一环境中安装现场源码
`python -m pip install -e /path/to/robot-bridge`。Loop 仍可使用原来的 Agent 环境。

两份 Runtime YAML 都需要设置 `monitor.url` 为 GRM 服务器 HTTP 地址。
GRM 配置的 `robot_runtime_url` 反向指向 `http://<从臂IP>:8767`。
Robot Server 是 WebSocket 地址（部署常用 9946），不是 SDK 的 50051。

## 版本一：manual

Runtime：

```bash
robot-runtime --config robot_runtime/robot_runtime/configs/manual.runtime.yaml \
  --host 0.0.0.0 --port 8767
```

打开 `http://<从臂IP>:8767/manual`，保留原有 robot-bridge UI。然后在 loop 环境运行：

```bash
export DUAL_FRANKA_RUNTIME_URL=http://127.0.0.1:8767
PYTHONPATH=src python examples/run_simple_robot.py --config examples/config.simple_loop.manual.yaml
```

每轮操作：

1. 摆好场景、保持机器人暂停，在 loop 终端输入目标物体。
2. Runtime 等 GRM 起始参考帧就绪，人工页显示本轮完整 instruction。
3. 在原 UI 选择对应指令并启动 VLA；在人工页点击“已开始”。此时激活 GRM 评分。
4. Monitor 到达终态或 loop 遇到异常后，人工页提示停止。人工停止动作，点击“已停止”。
5. 人工让机械臂归位，完成后点击“已归位”；loop 才返回 ready。

人工页只记录人工操作，没有向 robot-bridge 发送控制指令。不要在起始帧准备好前
开始动作，也不要在上一轮归位完成前提前确认。重复提交旧按钮不会确认下一操作。

`robot.operator_timeout_s` 默认 300 秒。manual loop 配置将 MCP HTTP 超时设为
660 秒，容纳参考帧等待、人工操作和异常清理；实际运行/首评分超时从 execute
返回后开始计算。停止请求可取消尚未确认的开始/归位等待，取消开始后仍会提示人工
停止，以处理“已经操作机器人但还没点按钮”的情况。

人工操作也可由文本客户端接入：`GET /manual/status` 返回 `data.pending`，完成
该操作后向 `POST /manual/ack` 提交 `{"request_id":"<当前请求ID>"}`。
未知/过期 ID 返回 409；已经确认的 ID 幂等返回，但不会影响新请求。

## 版本二：robot_bridge

Runtime：

```bash
robot-runtime --config robot_runtime/robot_runtime/configs/robot_bridge.runtime.yaml \
  --host 0.0.0.0 --port 8767
```

Loop 使用原有简单循环配置，无需人工页：

```bash
export DUAL_FRANKA_RUNTIME_URL=http://127.0.0.1:8767
PYTHONPATH=src python examples/run_simple_robot.py --config examples/config.simple_loop.yaml
```

调整配置：

```yaml
robot:
  type: x1pro
  driver: robot_bridge
  scheduler_url: ws://127.0.0.1:8088
  robot_url: ws://127.0.0.1:9946
  timeout_s: 5.0
  start_delay_s: 0.5
  stop_delay_s: 1.0
  reset_delay_s: 8.0
  prompt_map: {}
```

默认要求 loop 生成的 instruction 与 Scheduler `status.prompts` 中某项完全相同。
UI 上的 prompt 列表来自同一字段。如果上游模板与训练指令文本不同，可以配置别名：

```yaml
robot:
  # 其余连接配置同上
  prompt_map:
    pick the carrot and put it on yellow plate: 0
    pick the cup and put it on yellow plate: pick up the cup and place it on the yellow plate
```

值是从 0 开始的 prompt index 或已有 prompt 的完整字符串。别名需要保持任务语义
一致：GRM 收到的是上游 instruction，VLA 收到的是选中的训练 prompt；SAM3 仍使用
独立的 `target_queries`。不在列表/映射中的指令会失败，不默认退回第一个任务。

| 操作 | 普通 openpi | openpi_takeover |
|---|---|---|
| 开始 | 暂停并清队列 → 设置 prompt → 切出 single-step | idle 并清队列 → 设置 prompt → autonomous |
| 停止 | 读 single_step，仅在连续运行时切为暂停 | 请求 idle |
| 停止的共同步骤 | 清理从臂队列 → 等 stop_delay_s → 再清理一次 | 同左；原 Scheduler 的模式切换还会处理主臂 |
| 归位 | Scheduler homing → 等 reset_delay_s | 同左，沿用原 UI 的主从归位行为 |

开始后等待 `start_delay_s`，再返回并激活 Monitor。所有延时在 driver 内部执行，
因此简单 loop 和其他 Runtime 客户端不需要再加 sleep，也不需要人工确认或位姿轮询。
这些等待可配置；按现场推理、网络和归位耗时设置，归位延时要覆盖 Scheduler 尚在处理
的一轮推理以及 homing。普通 single-step 暂停仍可能保留一条等待执行的预测，随后
homing 会使其失效；本版本的正常循环是 stop 后 reset 再开始下一轮。

返回结果通过 `completion_basis: command_and_delay` 和 `wait_s` 明确标识“命令返回
并已等待”。这是按本次接入需求采用的延时策略，不代表实测停稳、首块动作入队或
归位姿态已验证。连接/命令错误仍上报失败；即使 Scheduler 不可达，也会尝试清理
Robot Server 队列，但不会继续自动归位。

`homing` 沿用 robot-bridge 已有实现：从臂张开夹爪、回双臂末端零位、闭合夹爪；
不恢复任意历史姿态或桌面物体。归位轨迹、执行缓冲和 SDK 行为仍由既有部署负责。
之前的 `robot_bridge_lifecycle_review.md` 记录了更严格的反馈/轨迹审查及上游缓冲问题；
本版本没有实现该文档的自建 recovery worker，也没有修复上游 homing 缓冲。
延时不能替代这些上游行为的真机验证。

`emergency_stop` 使用独立控制连接请求软件停止，并沿用 Runtime 的急停锁存；它没有
硬件急停接口，能力和返回值标明 `hardware_estop: false`。manual 下仍需人工停止。

## 三路相机 provider

两个 driver 共用以下配置：

```yaml
camera:
  provider: robot_bridge
  robot_url: ws://127.0.0.1:9946
  timeout_s: 5.0
  cache_s: 0.5
  snapshot_ttl_s: 30.0
  max_snapshots: 32
  max_obs_lag_s: 3.0
  quality: 90
  # size: [480, 640]  # 可选 [高度, 宽度]，默认保留原尺寸
```

Provider 用独立连接发送现有请求：

```python
{"cmd": "get_obs", "image_ts": [0.0], "image_format": {"encoding": "jpeg", "quality": 90}}
```

| Robot Server 字段 | Runtime 相机名 |
|---|---|
| face_view | cam_high |
| left_wrist_view | cam_left_wrist |
| right_wrist_view | cam_right_wrist |

按 HTTP 请求采集，`cache_s` 内复用一组，不额外运行后台采集进程。不会 configure
机器人或接触执行队列。`/observations/latest` 一次返回三张 base64 JPEG；
`concatenated_image` 是主视角兼容别名，不是拼接图。

`/observations/latest/metadata` 返回的 `binary_endpoints` 指向保留的固定快照，例如
`/observations/frames/<组ID>/cam_high.jpg`。现有 Monitor 已按该字段取图，因此无需
修改 Monitor：即使两次 JPEG GET 之间有人请求新画面，原来的三张仍来自同一次
get_obs。缓存超时/淘汰后返回 503，调用者重新取 metadata。最新 JPEG 路径也保留。

当前 Robot Server 没有返回逐相机采集时间和源帧 ID。Provider 对 JPEG 内容计算
稳定帧标识，重复内容不会伪装成新帧；timestamp 为 null，JPEG 不携带 X-Timestamp，
GRM 会记录 `synchronization_verified: false`。本机 `received_at` 单独用于诊断，
不当作 SDK 采集时间。同一 get_obs 请求的三路画面也不意味着硬件同步。

读取失败、视角缺失、JPEG 无效、上游 `obs_lag_ms` 超过阈值时，latest 接口返回
503，不刷新旧图。没有源时间戳时无法严格区分静止画面和相机停帧；仍依靠内容去重、
可用的 obs_lag_ms，以及现有 loop 的评分停滞/整轮超时共同处理。

## 软件验证

```bash
python -m pytest -q tests/test_bridge_adapters.py tests/test_robot_runtime.py \
  tests/test_runtime_contracts.py tests/test_simple_loop.py

# 额外使用真实 robot-bridge 的传输层、Scheduler 和 Server；不连接硬件/GPU。
PYTHONPATH=/path/to/robot-bridge python -m pytest -q tests/test_robot_bridge_wire.py
```

验证覆盖人工交接/取消、恢复等待期间拒绝新任务、固定 prompt 选择、延时归位、
控制失败后的清队列尝试、不可变三图快照和错误观测。上游联调用模拟 PolicyBackend
和 RobotController，真实 Scheduler 处理 JSON 控制、推理请求、暂停和 homing。
本地测试不证明物理归位效果或现场延时参数足够。

本次全仓回归：184 项测试通过，另有 7 个子测试通过；包括上述 3 项原生
robot-bridge 本地接口测试。Runtime wheel 也已验证包含人工页面及两份新增配置。
