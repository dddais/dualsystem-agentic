# Manual / manual_bridge / robot_bridge 真机接入

三个 RobotDriver 使用同一套 MCP、Runtime HTTP、GRM Monitor 接口。相机独立配置为
`camera.provider: robot_bridge`，直接读取已运行的 Robot Server，不再经过 `/tmp/img`。
Runtime 复用已有 SDK controller 和 VLA。自定义 instruction 需要更新 robot-bridge
的 Scheduler 文本 prompt 接口，详见 [模板与完整指令](manual_bridge_instructions.md)。

## 部署准备

- 服务器：沿用 VLA Policy Server，以及 GRM Monitor / SAM3。
- 从臂端：沿用 Robot Server、Scheduler；新增 Robot Runtime 和上游 loop。
- Scheduler 可以仍运行在原机器，Runtime 配置中的 `scheduler_url` 指向它。
- `manual_bridge` 和自动模式要求 Scheduler 控制端口已开启。旧 `fixed` 模式需要
  checkpoint 有固定 `prompts` 集合；`text` 模式需要 Scheduler 支持 `set_prompt_text`。
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

三份 Runtime YAML 都需要设置 `monitor.url` 为 GRM 服务器 HTTP 地址。
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
PYTHONPATH=src python examples/run_simple_robot.py --config examples/config.simple_loop.manual.yaml --input-source web
```

每轮操作：

1. 摆好场景、保持机器人暂停，在 `/manual` 页面 ready 阶段输入目标物体并提交。
2. Runtime 等 GRM 起始参考帧就绪，人工页显示本轮完整 instruction。
3. 在原 UI 选择对应指令并启动 VLA；在人工页点击“已开始”。此时激活 GRM 评分。
4. Monitor 到达终态或 loop 遇到异常后，人工页提示停止。人工停止动作，点击“已停止”。
5. 人工让机械臂归位，完成后点击“已归位”；loop 才返回 ready。

人工页只记录人工操作，没有向 robot-bridge 发送控制指令。不要在起始帧准备好前
开始动作，也不要在上一轮归位完成前提前确认。重复提交旧按钮不会确认下一操作。

页面可切换实时三视角和与得分对应的 GRM 三视角，后者叠加实际 SAM3 bbox；同时
显示融合进度及各模式分数。使用方式、双向 SSH 转发与更新步骤见
[manual_start.md](manual_start.md)。需要键盘输入时使用 `--input-source terminal`。

`robot.operator_timeout_s` 默认 300 秒。manual loop 配置将 MCP HTTP 超时设为
660 秒，容纳参考帧等待、人工操作和异常清理；实际运行/首评分超时从 execute
返回后开始计算。停止请求可取消尚未确认的开始/归位等待，取消开始后仍会提示人工
停止，以处理“已经操作机器人但还没点按钮”的情况。

人工操作也可由文本客户端接入：`GET /manual/status` 返回 `data.pending`，完成
该操作后向 `POST /manual/ack` 提交 `{"request_id":"<当前请求ID>"}`。
未知/过期 ID 返回 409；已经确认的 ID 幂等返回，但不会影响新请求。

## 中间版本：manual_bridge（同页点击控制）

保留 manual 的逐步操作流程，将 robot-bridge 的控制接入 `/manual`。
每一步点击直接发送命令，成功并完成配置的等待后自动放行 loop，无需切换网页或
再点击“已开始 / 已停止 / 已归位”。原 `manual` 与自动 `robot_bridge` 仍可独立选择。

在已有 Runtime 环境更新安装，并使用新增配置：

```bash
cd /path/to/dualsystem-agentic
python -m pip install -e './robot_runtime[bridge]'
robot-runtime --config robot_runtime/robot_runtime/configs/manual_bridge.runtime.yaml \
  --host 0.0.0.0 --port 8767
```

现场 Scheduler 需开启 `--control-port 8088`。配置中的 `robot.scheduler_url`、
`robot.robot_url` 和 `monitor.url` 分别指向 Scheduler 控制面、Robot Server 和 GRM。
新增 YAML 默认沿用 manual 的 Monitor SSH 转发 `http://127.0.0.1:18877`；直连时改为
实际服务地址。相机、GRM、SAM3 以及双向 SSH 转发沿用 [manual_start.md](manual_start.md)。

Loop 继续使用人工等待超时配置，在另一个终端运行：

```bash
export DUAL_FRANKA_RUNTIME_URL=http://127.0.0.1:8767
PYTHONPATH=src python examples/run_simple_robot.py \
  --config examples/config.simple_loop.manual.yaml --input-source web
```

只需打开 `http://<Runtime主机>:8767/manual`：

1. ready 时选择模板并填目标，或输入完整 instruction，提交后等待 GRM 起始参考帧。
2. 点击“启动 VLA”：暂停并清队列、设置本轮 instruction、启动 Scheduler；
   等 `start_delay_s` 后激活 GRM 评分。
3. 本轮结束后点击“停止 VLA”：暂停并清理队列，等 `stop_delay_s` 后再次清队列；
   成功后 loop 提示归位。
4. 点击“执行归位”：沿用 Scheduler homing，等 `reset_delay_s` 后返回 ready。

`robot.operator_timeout_s` 只约束等待点击，默认 300 秒。点击立即返回 accepted；
命令由持有 Runtime 执行锁的原工作线程处理，页面持续显示“执行中”。接口接收点击
不代表动作已完成，Monitor 激活和 loop 状态切换仍等待 driver 成功返回。
开始、停止和归位的延时策略与自动 `robot_bridge` 相同，**不代表实测停稳或归位姿态验证**。

页面增加 VLA 控制区，按 Scheduler 能力显示：训练指令、takeover 模式、录制与
采集人、phase 与锁定、latency_step / move_steps、单步、夹爪映射和四路日志。
开始／停止／归位通过本轮操作按钮处理；参考帧准备及交接期间禁用辅助控制，
任务启动后才可切换模式或单步。模式切换／暂停属于本轮执行内的操作，不结束 loop；
loop 仍根据 Monitor 结果进入停止、归位阶段。执行期间不能换训练 prompt，避免
VLA 与 GRM 的任务指令不一致。录制、phase 等辅助设置不会放行人工交接。

新网页支持模板和完整指令，最终 instruction 以同一文本交给 VLA 和 VLM，不经
`prompt_map` 替换。新增配置使用 `prompt_mode: text`；需要同步更新并重启 Scheduler。
页面会显示实际发送的文本，ready 阶段手选预置 prompt 不能覆盖本轮 instruction。
模板配置、SAM3 检测目标和更新步骤见 [模板与完整指令](manual_bridge_instructions.md)。
旧 `fixed` 模式仍按下文的 instruction / `prompt_map` 规则匹配预置任务。

“立即软件停止”使用 Runtime 的急停入口与独立 bridge 连接，可以取消等待点击、
启动或归位过程，不需要再次人工确认；停止后锁存，完成归位后才允许下一次执行。
它与自动 driver 一样是软件停止，`hardware_estop: false`。

### 网页键盘快捷键

`manual_bridge` 沿用 Scheduler 的按键映射，按钮右侧显示快捷键；“本轮操作”下方
可展开完整说明。快捷键与点击按钮经过同一套状态检查，只触发当前可用的操作。

| 按键 | 操作 |
|---|---|
| `R` | 开始 / 停止录制 |
| `I` / `T` / `A` | 空闲 / 遥操作 / 自主运行，任务启动后可用 |
| `S` / `Enter` | 切换单步模式 / 执行下一步；下一步仅在单步模式下可用 |
| `[` / `]` | 减少 / 增加 `latency_step` |
| `L` | 锁定 / 解锁 Phase |
| `P` | 切换 Scheduler 的数字键用途，网页显示当前为 Phase 或 Prompt |
| `0`–`9` | 设置当前用途对应的 Phase / Prompt，索引从 0 开始 |
| `Space` | 触发当前“启动 VLA / 停止 VLA / 执行归位”按钮 |
| `H` | 仅在本轮“执行归位”阶段触发归位 |

字母不区分大小写；输入框、下拉框、可编辑区域、中文输入法组合输入期间不响应。
忽略长按重复和 Ctrl / Alt / Meta 组合键。按钮获得焦点时，Enter / Space 保持
浏览器原生的按钮操作，不会同时触发全局快捷键。原 `manual` 模式仅使用 Space
确认本轮人工操作，录制等 Scheduler 快捷键仅在 `manual_bridge` 生效。

数字键使用网页显示的用途，调用明确的 `set_phase` 或 `set_prompt`；执行中仍然
禁止切换 Prompt。`P` 同步切换 Scheduler 的 `digit_mode`，与 Scheduler UI / 终端
共享状态。快捷键不会跳过参考帧准备或人工交接，也不会通过 `H` 直接发出裸 homing。

更新运行 Runtime 的机器上的 `dualsystem-agentic`，重启 Runtime 并刷新 `/manual`。
本次快捷键功能无需更新 robot-bridge、Policy Server 或 Monitor。
可用 `python tests/validate_manual_shortcuts.py` 做浏览器回归检查（需 Playwright / Chromium，模拟硬件）。

HTTP 客户端可使用：

| 接口 | 行为 |
|---|---|
| `GET /manual/status` | `control_mode: bridge`；`pending.phase` 为 waiting / queued / running，`last_operation` 保存完成结果或错误 |
| `POST /manual/action` | 提交 `{"request_id":"<本轮待操作ID>"}`，触发该操作；重复 ID 不重发命令，过期／未知 ID 返回 409 |
| `POST /manual/task` | ready 阶段提交模板目标或完整指令，详见自定义指令文档 |
| `GET /manual/bridge/status` | Scheduler 当前状态、本轮匹配 prompt、Runtime 当前允许的辅助动作 |
| `POST /manual/bridge/action` | `{"name":"set_phase","args":{"phase":2}}` 等辅助控制；后端检查本轮阶段，禁止绕过交接直接 homing |
| `GET /manual/bridge/log?target=scheduler&lines=200` | scheduler / policy / robot / master 日志，经 Runtime 转发 |

新模式的 `/manual/ack` 返回 409，不能用人工确认绕过真实命令。
命令失败不会放行成成功状态，页面显示 `last_operation.error`，Runtime 按原流程
进行停止清理。命令响应丢失时不自动重发；相机及评分仍使用原有独立链路。

## 自动版本：robot_bridge

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

三个 driver 共用以下配置：

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
  tests/test_runtime_contracts.py tests/test_simple_loop.py tests/test_manual_bridge.py

# 额外使用真实 robot-bridge 的传输层、Scheduler 和 Server；不连接硬件/GPU。
PYTHONPATH=/path/to/robot-bridge python -m pytest -q tests/test_robot_bridge_wire.py

# 浏览器 + 真实 HTTP/MCP/Loop/Monitor 服务，模拟机器人与模型（需 Playwright/Chromium）。
PYTHONPATH=/path/to/robot-bridge python tests/validate_manual_dashboard.py \
  --driver manual_bridge --monitor-repo /path/to/Robo-Dopamine-delivery \
  --screenshot /tmp/manual-bridge-dashboard.png
```

验证覆盖人工交接/取消、恢复等待期间拒绝新任务、固定 prompt 选择、延时归位、
控制失败后的清队列尝试、不可变三图快照和错误观测。上游联调用模拟 PolicyBackend
和 RobotController，真实 Scheduler 处理 JSON 控制、推理请求、暂停和 homing。
本地测试不证明物理归位效果或现场延时参数足够。

manual_bridge 的测试还覆盖点击后才发命令、参考帧与 Monitor 激活顺序、等待期间
显示执行中、重复／旧按钮不影响下一操作、失败不重发、取消与启动竞态、急停锁存、
辅助控制阶段检查，以及原生 robot-bridge 控制面的完整开始／停止／归位流程。

Runtime wheel 包含新 driver、配置和共享页面。浏览器脚本支持分别验证 `manual`
与 `manual_bridge` 的真实 HTTP/MCP/Loop/Monitor 链路，模型和机器人使用模拟实现。
