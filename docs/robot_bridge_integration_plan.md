# robot-bridge 真机接入方案

日期：2026-09-08。核对 robot-bridge 提交 `8ddccf0` 和当前 dualsystem-agentic / Robo-Dopamine-delivery 工作区。**本文是待实现方案，当前 Runtime 仍使用 placeholder driver。** 没有向真机发送动作。

二次审查已修订模型就绪、首块动作握手、instruction 切换和恢复方案。逐项输入输出、代码问题及离线复现证据见 [五项生命周期审查](robot_bridge_lifecycle_review.md)。其中 homing 缓冲溢出与 reset/急停竞态属于接入前必须处理的问题，不能靠补充到位检查掩盖。

建议在 **Robot Runtime 内新增 RobotBridgeDriver 和相机适配器**，由 Driver 管理一个后台 scheduler worker。复用 robot-bridge 的 `OpenPiScheduler` 转换逻辑、Policy Server、Robot Server；本部署不再启动原来的独立 scheduler。第一版可以不修改 robot-bridge 源码，但不能据此声称已有硬件急停、执行器确认或严格相机同步能力。

## 1. 系统模块与输入输出

| 模块 | 输入 | 输出 / 接收方 | 本次接入工作 |
|---|---|---|---|
| simple loop | ready 输入目标词，例如 `purple mug`；回车复用上一目标 | `subtask="pick the purple mug and put it on yellow plate"`、`target_queries=["purple mug"]`、execution ID → MCP | 已实现，保留 |
| MCP tools | execute / monitor / stop_task / reset_task | Runtime HTTP 请求；向 loop 返回状态和确认字段 | 保留现有协议 |
| Robot Runtime | `/executions`、`/monitors/status`、`/control/*` | 管理执行 ID、Monitor 生命周期、Driver；提供相机 HTTP | 注册真实 Driver/Camera；接入后台执行故障 |
| **RobotBridgeDriver（新增）** | `ExecutionRequest`、execution ID、stop/reset 请求 | 启停后台 worker；调用 Robot Server；返回 `executed/stopped/reset` | 实现动作控制和完成确认 |
| **受控 scheduler worker（新增）** | 完整 instruction、机器人观测、checkpoint metadata | Policy `infer`；处理预测后发送 Robot `execute` | 复用 OpenPiScheduler；增加取消检查、最终发送互斥、异常上报 |
| Policy Server（已有） | RGB 图像、状态序列、`prompt` | `actions` numpy 数组、`policy_timing` | 原样运行 |
| X1Pro Robot Server（已有） | get_obs / execute / clear_actions / homing | 图像和状态、动作队列、命令响应；向 SDK 发末端控制 | 原样运行；注意下文确认边界 |
| **RobotBridgeCameraProvider（新增）** | Robot Server 的一批三路 JPEG | Runtime 不可变图像快照和 metadata → GRM | 相机名映射、快照缓存、时效检查 |
| GRM Monitor | instruction、`target_queries`、Runtime 图像 | `running/success/failed`、评分、steering 诊断 → Runtime | 现有暂缓/激活和评分逻辑保留 |
| SAM3 | 冻结图像、`queries=["purple mug"]` | bbox/score → attention steering | 不从 VLA 输出猜目标词 |

`instruction_template` 仍在上游 config 中；完整 instruction 送给 VLA 的 `prompt`，同一份 instruction 也送给 GRM。目标短语独立送给 SAM3，无需 VLM 规划。模板任意文本在接口上可以传通，但是否符合 checkpoint 训练指令分布仍需要模型验证。

## 2. robot-bridge 当前能直接复用什么

两条数据链路都是 **WebSocket 二进制帧 + msgpack/numpy 编码**，使用 `robot_bridge.transport.WebSocketClient.call(dict)`。它们不是 HTTP JSON 接口。Policy 代码通用默认端口为 8000，部署常用 **8946**；Robot Server 文档默认 9000，X1Pro 部署脚本常用 **9946**，应按实际部署填写。

| 接收方 / 请求 | 响应 | 实际含义 |
|---|---|---|
| Policy：`{"cmd":"get_metadata"}` | `{"status":"ok","metadata":{...}}` | 提供 mode、状态历史/未来长度、动作维度、policy_hz、图像尺寸等 |
| Policy：`{"cmd":"infer", "images":..., "state":..., "prompt":...}` | `{"status":"ok","actions":...,"policy_timing":...}` | 返回预测动作，未执行 |
| Robot：`{"cmd":"get_obs", "slave_state_ts":[0.0], "image_ts":[0.0], "image_format":{"encoding":"jpeg","quality":90}}` | `{"status":"ok","obs":{"slave_state":...,"images":...,"obs_lag_ms":...}}` | 时间偏移相对于所需缓冲区的共同最新时刻；JPEG 为各相机 `list[bytes]` |
| Robot：`{"cmd":"execute","actions":{"arms":array},"blocking":false}` | `{"status":"ok","queued":N}` | 动作已入队，随后由执行进程发送给 SDK |
| Robot：`{"cmd":"clear_actions"}` | `{"status":"ok","dropped":N}` | 删除主进程未来动作，向执行进程排入清队列标记；保留历史/插值锚点 |
| Robot：`{"cmd":"is_idle"}` | `{"status":"ok","idle":true}` | 主进程没有未来动作，不证明机械臂已经静止 |
| Robot：`{"cmd":"homing"}` | `{"status":"ok"}` | 现有命令；有长轨迹超过执行缓冲容量的问题，第一版不直接作为 reset 实现 |

表内包含 numpy/bytes 的请求是 Python 对象示意，不能直接用 JSON/curl 发送。Robot 的 `arms` 在此控制器中是 `(N,14)` 的双臂末端位姿和夹爪指令，不能当成 14 个关节角。VLA 原始输出可能包含主从两部分、phase 等列，不能直接当作 `arms`。

必须复用 OpenPiScheduler 的以下逻辑：按 metadata 取历史/未来状态、构造 state 序列、复现训练图像尺寸、按 mode 选择动作列、首轮实测状态起步、后续 latency_step 截取、插值到 control_hz、维护 phase。新执行创建新 scheduler 上下文，避免继承上一任务的动作和 phase。

### 为什么不直接包装 8088 Web UI

控制面是另一套 JSON 文本 WebSocket，适合已有人工操作流程，但当前与 tool 语义有差距：

| 当前 UI 行为 | 不能直接满足的需求 |
|---|---|
| `set_prompt` 只接收训练 prompts 列表的 index | ready 每轮产生任意完整 instruction |
| `toggle_single_step` 是切换，`step` 是放行一块动作 | 幂等开始/停止；暂停不清队列 |
| homing 回调立即设置请求标志后返回 | `reset:true` 必须代表恢复已完成 |
| scheduler `_call_homing` 捕获错误后只写日志，外层仍清掉 pending | `homing_pending=false` 不能证明恢复成功 |
| `_chunk_invalidated()` 在 build_act_request 内检查 | 检查之后、实际发送 execute 之前仍可发生停止竞态 |

另外，退出 stock scheduler 的 `_shutdown()` 不负责清空未来动作，不能用杀进程替代 stop。`openpi_takeover` 的 idle/teleop/autonomous 可供以后接入主臂，但第一版不用它代替明确的任务控制协议。

## 3. Driver 与受控 worker 的实现约定

第一版采用 **Runtime 进程内后台线程**，不新增 HTTP 中间服务。Driver 是唯一任务控制入口，worker 负责 obs→infer→act。适配代码留在 dualsystem-agentic；依赖固定版本 robot-bridge，并对受保护成员的使用写兼容测试。

### execute：开始执行，不等待整项任务结束

1. 部署阶段先启动并加载常驻 Policy/GRM/SAM3。Policy 用 get_metadata 核对 checkpoint 和输入输出规格，再用合法观测做一次只推理、不发送动作的预热。Policy Manager 的 running 只通过 metadata 探测，不能替代首次 infer 验证；加载或预热失败时不放行任务。
2. Runtime 顺序修订为：Monitor start（`defer_inference:true`）→ 等参考帧就绪 → Driver.execute 启动 worker → **在 `_driver_lock` 外等待本 execution 的首块 execute 得到 `status:ok, queued>0`** → Monitor activate。Driver 的快速 `executed:true` 仅表示会话已启动；新增 Driver 首块动作状态/事件作为 Runtime 内部握手，不增加上游 tool。等待可取消、有超时，后台失败直接清理；HTTP 创建成功前完成该握手。第一块入队也只确认提交，不等于硬件已运动。
3. 每个 execution 构造 `OpenPiScheduler(..., prompt=request.subtask, control_port=None)` 的新实例，整个任务内重复使用；由适配子类明确关闭默认单步等待。不要每个动作块重建，也不要通过反复调用 toggle 猜测当前状态，或直接调用会启动键盘/UI 的 stock `run()`。
4. worker 复用 `build_obs_request()`、`build_policy_obs()`、`build_act_request()`、`after_execute()`，自行管理迭代和发送。保留 robot/policy 响应校验，错误进入失败状态；不要无限重试到上游仍以为正常运行。
5. **每次 execute RPC 前，在发送互斥区内重新检查 execution ID / generation / cancel**。取消后，即使模型刚返回结果，也只能丢弃。

Runtime 的 `_driver_lock` 包围 Driver.execute，所以不能把整个 VLA 循环或长时间模型请求放在该同步方法里。首轮动作等待期间保持 GRM 评分关闭，避免加载/首轮推理时间被算作任务无进展。等待前启动独立超时监督；activate 失败时停止已入队动作。现有 Runtime 立即 activate 的实现需要修改。

每个 execution 冻结 instruction 和目标词；只在 ready 接收新词，新词创建全新的 execution/Monitor/scheduler 上下文。切换目标不重载模型，也不调用 UI 的 set_prompt；旧推理结果丢弃。Policy Server 没有跨客户端推理锁，也没有 cancel_infer，所以同一模型实例不得并发运行旧、新任务推理；旧调用未确认结束时可以先完成物理停止，但下一次推理不能放行。

### stop：先禁止新动作，再清队列，最后确认稳定

`stop(execution_id)` 必须幂等；旧 ID 不能停止新任务。具体顺序：

1. 先撤销本轮发送权限、递增 generation / 设置 cancel。正在进行的推理可以结束，但不能再发送动作。
2. 在有期限的等待内，确认此前可能已进入发送区的 execute RPC 已返回。**清队列必须发生在最后一个旧 execute 已完成入队之后**；只在另一条连接提前发 clear 不能保证次序。
3. 经控制连接发 `clear_actions`，验证 status；再查询 `is_idle`。检查新鲜的实测 `slave_state`，在配置的连续窗口内位姿/夹爪变化小于阈值后，才允许返回 `stopped:true`。旋转比较使用正确的角度/旋转距离，不能直接混用米与角度阈值。
4. worker 后台故障同样触发这条停止路径。Runtime 随后清理 Monitor；现有 Monitor 清理异常不阻断物理停止的规则保留。

取消必须同时覆盖 autonomous worker 和 recovery worker。当前 Runtime.reset 长时间持有 `_driver_lock`，普通 stop 会在锁外等待，不能打断恢复；接入时要让 stop 先经独立控制路径发布取消，并把 reset 等待移出该锁。Driver 内部以 generation 和短发送互斥保证动作次序，而不是用一把长锁包住整个恢复过程。

X1Pro 的 clear 标记没有执行进程回执，而且底层控制器会继续追踪最后收到的目标。因此这是一种**受控停止并等待稳定**，不是即时刹停。若发送超时、观测过旧、执行进程无确认且无法从反馈确认静止，返回 `stopped:false` 并锁住后续执行/恢复；不能用 `idle:true` 替代确认。

本地发送锁只约束本适配器。Robot Server 没有 execution ID / generation / 单写入者租约，所以必须确保 stock scheduler、takeover、其他脚本和 UDP 生产者不会同时发动作。网络异常中未确认的旧请求仍可能在服务端稍后处理；重连后再发 clear 也不构成跨连接顺序证明。此时零修改方案不能自动认定恢复安全，需完成外部排查或使用第 7 节的服务端增强。

### reset：按恢复配置分段发送，并逐阶段验证

只有 stop 确认后才允许 reset。第一版明确把“初始状态”定义为 **配置中的固定 home 位姿和夹爪状态**，不是自动恢复本轮启动时姿态，也不包括物体/场景复原。若以后需要回到每轮起始姿态，应增加单独的捕获与恢复模式，不能混用同一个 home 配置。

当前 X1Pro homing 实现是：从实测位姿构造动作 → 张开夹爪（目标 4.5）→ 等待 → 双臂末端指令插值到零位 → 闭合夹爪（0.0）→ 清主进程动作历史。轨迹约 6 秒，调用 `execute(..., blocking=True)` 等主队列时间走完；**不是直接调用某个 SDK reset 并获取到位确认**。执行子进程捕获 SDK 异常后只记录 warning，Robot Server 仍可能返回 ok。

进一步发现：100 Hz 下 homing 一次生成 **597 个动作**，但执行进程 `TimestampedBuffer(maxlen=300)`。快速入队可使最早有效点变成第 2.98 秒，跳过张爪/等待和部分回零段；主进程 `action_buffer_size:2000` 不会改变子进程容量。末端最终到位也不能证明中间动作正确完成。因此撤回直接调用现有 homing 的建议。

零修改 robot-bridge 的推荐实现：在 Runtime 适配层新增 recovery worker，按 recovery_profile 生成“夹爪处理 → 分段回 home → 最终夹爪状态”的轨迹。复用现有 execute RPC，但采用小块（例如 50 点）和严格背压，上一块入队确认且队列排空/阶段反馈满足后再提交下一块；所有恢复发送也走 generation 检查和发送互斥。关键阶段需要实测完成确认，末端目标附带保持段，避免执行器只在 before/after 都存在时发送而漏掉最后一个点。回零旋转使用经过验证的旋转插值，不能无条件线性插值 Euler 角。

恢复目标、反馈单位、容差和阶段顺序必须与配置一致；现有 homing 的硬编码零位不能实现任意 recovery_profile。全部阶段及最终连续稳定窗口通过才返回 `reset:true`。取消、超时、RPC 结果不确定或到位失败均不能继续下一阶段/自动重试。下一轮新 scheduler 首轮使用实测 slave，旧 recovery 动作历史不作为初始 master；之后的历史窗口也需避免混入前一任务的命令，详见生命周期审查。

Runtime 另需加入恢复 generation / 急停 epoch：较早 reset 返回时，如果期间发生新的 stop/急停，不得报告恢复成功或清除新急停锁定。当前 reset 无条件把 `_estop_latched=False`，已用 placeholder Driver 离线复现此竞态。恢复期间禁止新 execute 进入 Monitor 参考帧采集；完成后再开放下一轮任务。

### emergency_stop 与后台状态

当前公开 Robot RPC 没有 `emergency_stop`。不要把 `clear_actions` 包装成 `emergency_stop:true`。Driver capabilities 应如实声明硬件急停不可用；请求时可以尝试受控停止，但最终返回不支持/失败，并保留 Runtime 急停锁定。真机硬件急停接通后再启用对应能力。

Driver.status 建议返回 `execution_id/state/last_error/last_action_at/worker_alive/stop_confirmed`。**目前 Runtime.monitor_status 只读取 Monitor，未把后台 Driver 故障并入结果**：接入时必须补充按 execution ID 的故障通知/查询，将本轮执行标为 failed，并由独立监督逻辑触发 stop；不能等 GRM 判断失败或只把错误暴露在 `/health`。旧 worker 的错误不能覆盖新任务，清理时不要让工作线程 join 自己。

## 4. 连接、超时和线程

| 连接 / 资源 | 用途 | 隔离原因 |
|---|---|---|
| Policy 连接 | metadata、infer | 模型慢不能阻塞停止 |
| worker 观测连接 | 带状态历史及 wait_condition 的 get_obs | 队列等待不能阻塞控制 |
| 动作连接 + 最终发送互斥 | 小而非阻塞的 execute RPC | 确认最后一次发送与 clear 的次序 |
| 控制连接 | clear_actions、is_idle | 与模型、相机、录制分开 |
| recovery 发送通道 | 分段 execute | 与 autonomous 共用最终发送仲裁，受恢复取消控制；不得与自主动作并发入队 |
| 相机 / 反馈读取连接 | 无 wait_condition 的图像和反馈采样 | 提供 Monitor 快照、停止和恢复验证；需要时分别独立 |

`WebSocketClient.call` 在整段 send/recv 上持锁；`close()` 也取同一把锁，所以另一线程 close 不是立即打断在途 RPC 的办法。RPC 没有可供调用方匹配迟到响应的请求 ID；超时连接需要退出使用并重建，避免把旧响应认作新响应。execute/homing 不自动重试；取消事件只能撤销后续发送，不能证明撤销了已经发出的指令。

统一配置连接/观测/推理/发送/stop/reset/首轮动作期限。底层期限应给上层留出失败清理时间；当前 MCP 的 HTTP 超时示例为 120 秒，不能把整个 reset 验证也配置为 120 秒后仍期望响应正常返回。录制与归档不放在关键控制路径，stock stop_recording 可能长时间归档。

## 5. 相机与 Monitor 接入

| Robot Server 图像名 | Runtime / GRM 图像名 |
|---|---|
| `face_view` | `cam_high` |
| `left_wrist_view` | `cam_left_wrist` |
| `right_wrist_view` | `cam_right_wrist` |

相机适配器在 ready 阶段也持续可用，使 Monitor 能在动作前抓到参考帧。单次 `get_obs(image_ts=[0.0], image_format={encoding:jpeg, quality:90})` 获取三路图像；Monitor 路径不带 VLA 的训练 resize，鱼眼校正继续由现有 GRM 预处理负责。SAM3 和 GRM 必须使用同一份校正后冻结图像，避免 bbox 坐标错位。

**要在 Runtime 侧补充不可变快照路径**：metadata 捕获一个 snapshot，`binary_endpoints` 指向 `/observations/snapshots/{snapshot_id}/{camera}.jpg`；三张 JPEG 均从该缓存读取，保留足够读取时间、过期明确报错。当前 metadata 路由硬编码 latest，且每个 JPEG 独立取 latest，单加 CameraProvider 不能保证同批。旧 latest API 可保留；GRM 已按 metadata 的相对路径下载，通常无需改下载协议。

快照 ID 用于定位缓存；每相机 `X-Frame-Id` 在无源帧 ID 时使用图像内容哈希，**不能每次 HTTP 读取生成新 UUID**，否则冻结图像会被误认为新帧。重复内容仍可能来自真实静止场景，哈希是保守兜底，不是完整源帧诊断。

X1Pro get_obs 内部用共同 latest_ts 查各路最近帧，但返回时丢弃了每帧的真实采样时间。现有 `obs_lag_ms` 能在服务端时钟下提示缓冲过旧，不能证明三帧采集时差。零修改方案应检查该 lag；同时在 Runtime 类型/API 中允许缺失采集时间，metadata 将接收时间单独标为 `received_at`，不伪造 `X-Timestamp`。GRM 当前无三路时间头时会记 `synchronization_verified:false`；若业务要求严格同步，则必须实施第 7 节时间戳增强后才能启用该保证。

## 6. 修改位置与配置草案

以下文件为**建议新增/修改**，不是已经实现的配置功能：

| 仓库 / 文件 | 工作 |
|---|---|
| dualsystem-agentic：`robot_runtime/robot_runtime/adapters/robot_bridge/robot_driver.py`（新增） | execute/stop/reset/急停能力声明，任务隔离和完成验证 |
| 同目录 `scheduler_worker.py`（新增） | OpenPiScheduler 适配子类、连接隔离、generation、首块动作确认和最终发送检查 |
| 同目录 `recovery_worker.py`（新增） | 固定 home 配置、分段轨迹/背压、阶段反馈、可取消恢复 |
| 同目录 `camera_provider.py`（新增） | Robot get_obs、相机名映射、不可变快照、帧时效 |
| `robot_runtime/robot_runtime/api/app.py` | 注册 `robot.type:x1pro`、`driver:robot_bridge`、相机 provider 和快照路由 |
| `robot_runtime/robot_runtime/core/runtime.py`、`core/types.py` | 首块动作握手、后台故障贯通、恢复 admission/取消/急停 epoch、快照/可选源时间字段、关闭后台 worker |
| `robot_runtime/robot_runtime/configs/x1pro.robot_bridge.yaml`（新增） | 真机适配配置 |
| `tests/` | 模拟 RPC、竞态、反馈验证和跨服务测试 |
| robot-bridge | 第一版无源码修改，作为固定版本依赖 |
| Robo-Dopamine-delivery | 保留评分和 steering 实现；按实际部署更新 Runtime 地址及相机校正配置 |

不要继续把 X1Pro 硬件类型声明为 dual_franka。上游现有 MCP 服务本质上是 Runtime HTTP 代理，可先保留其历史 namespace/环境变量名称兼容；硬件类型与 MCP 名字分开处理。

配置结构草案如下；所有地址和期限为示意值，停止/到位容差必须按真实反馈单位配置，缺少时应拒绝开启真实控制：

```yaml
robot:
  type: x1pro
  driver: robot_bridge
  robot_bridge:
    policy_url: ws://POLICY_HOST:8000
    robot_url: ws://ROBOT_HOST:9946
    scheduler_config: /path/to/robot-bridge/configs/scheduler/openpi.yaml
    first_action_timeout_s: 30.0
    stop_timeout_s: 10.0
    reset_timeout_s: 60.0
    recovery_profile: /path/to/validated_x1pro_home.yaml
camera:
  provider: robot_bridge
  robot_url: ws://ROBOT_HOST:9946
  snapshot_ttl_s: 30.0
monitor:
  provider: remote_http
  url: http://MONITOR_HOST:8877
  timeout: 30.0
safety:
  monitor_ready_timeout_s: 30.0
  max_execution_s: 300.0
  require_estop_ready: false  # 不代表已有硬件急停；按实际部署能力设定
```

部署时保留既有 Policy Server、X1Pro Robot Server 启动方式，在边缘机器启动集成了 worker 的 Runtime，再启动 SAM3/GRM 和上游 loop。原 `scripts/launch/x1pro.sh` 会自动启动 stock scheduler，不能原样与本方案同时使用；可在 dualsystem-agentic 新增启动脚本，只复用其 policy/robot 启动入口。robot-bridge 包要求 Python >=3.11，适配环境需安装其传输、numpy、scipy、OpenCV 等依赖，不必在 Runtime 加载 OpenPI 模型或 X1Pro SDK。

## 7. 零修改边界与可选最小增强

| 能力 | 零修改 robot-bridge | 如需更强保证，建议最小增强 |
|---|---|---|
| 每轮任意 instruction、VLA 连续执行 | 支持：受控 worker 复用 OpenPiScheduler | 无 |
| 正常连接下的受控停止、任务回零 | clear 加实测反馈；回零由适配层分段 execute 并逐阶段验证，绕开当前长 homing | 修复 homing 缓冲容量/背压，并将执行进程故障和清队列完成回执返回 Robot Server |
| 超时后阻止旧指令、多个客户端仲裁 | 无服务端保证；本地锁只覆盖自身 | Robot Server 对执行/恢复统一校验会话 generation、单写入者租约；过期操作拒绝入队 |
| 严格三相机时间对齐和源帧新鲜度 | 只有共同查询基准、lag 和内容哈希，无法完整证明 | get_obs 返回共同基准及每相机选中帧的源时间/帧 ID |
| 真正硬件急停 | 当前没有对应 RPC | 基于实际 SDK/底层接口实现独立急停及设备状态确认，不假设某个 ROS 模式就是从臂急停 |

这些增强应增加可选字段/命令，保留旧 RPC 调用兼容。没有业务要求的增强不必提前全做；无法确认停止或恢复时必须阻止自动回到 ready。

## 8. 实施顺序与验收

1. 先用 Mock Policy / Mock Robot 验证适配器与现有 loop/Monitor 协议，检查任意目标词和回车复用：VLA 收完整 prompt，SAM3 收独立目标词。
2. 加入故障和竞态测试：首次 infer 超时、首块动作前不得评分；stop 发生于推理中、build_act_request 后、execute 发送中；切换目标时旧推理未结束；恢复期间 stop/急停；长轨迹缓冲容量、各恢复阶段虚假 ok；后台故障、相机过旧和 snapshot 过期。要求 stop 成功后旧任务不能再发动作，reset 未到位/被打断不能返回 true，旧 reset 不能解除新急停。
3. 接真实相机和服务的只读接口，验证 metadata、图像命名、格式、状态单位和快照一致性；ready 及参考帧捕获阶段不发 execute。
4. 在真实操作条件具备时，再验证短动作、受控停止、home 到位和完整两轮流程。记录停止请求到实测稳定的耗时、首次动作延迟、恢复误差和模型效果；不能用 Mock 通过替代这一步。

本轮已复查现有简化 loop/Runtime 测试并重跑合成 HTTP/MCP 联调；这些覆盖现有框架，不覆盖尚未实现的 RobotBridgeDriver。现有修复、测试范围见 [system_contract_review.md](system_contract_review.md)，目标词用法见 [simple_loop.md](simple_loop.md)。

## 9. 代码依据

- robot-bridge：[README](../../robot-bridge/README.md)、[X1Pro 部署](../../robot-bridge/docs/tutorials/x1pro.md)、[Robot API](../../robot-bridge/docs/reference/robot-server-api.md)、[Policy API](../../robot-bridge/docs/reference/policy-server-api.md)、[控制面](../../robot-bridge/docs/reference/scheduler-control-ui.md)。
- 调度：[SchedulerBase.run_iteration](../../robot-bridge/robot_bridge/scheduler/base.py)、[OpenPiScheduler](../../robot-bridge/robot_bridge/scheduler/openpi.py)、[WebSocketClient](../../robot-bridge/robot_bridge/transport/websocket.py)。
- 动作确认：[Robot RPC wrappers](../../robot-bridge/robot_bridge/robot/controllers/base.py)、[X1Pro execute/clear_actions/homing](../../robot-bridge/robot_bridge/robot/controllers/x1pro/controller.py)、[执行子进程](../../robot-bridge/robot_bridge/robot/controllers/x1pro/exec_worker.py)、[底层控制说明](../../robot-bridge/docs/reference/ros2-interface.md)。
- 当前框架：[Runtime](../robot_runtime/robot_runtime/core/runtime.py)、[HTTP/工厂](../robot_runtime/robot_runtime/api/app.py)、[GRM 图像冻结](../../Robo-Dopamine-delivery/monitor_runtime/grm_backend.py)。
