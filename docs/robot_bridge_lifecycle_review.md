# 真机接入方案：五项生命周期审查

2026-09-08，基于 robot-bridge `8ddccf0` 和当前两个上游工作区。审查范围是加载模型、开始推理、切换 instruction、停止执行、恢复初始状态。本次修改方案文档，未实现真实 Driver，未加载神经模型或向真机发命令。

**结论：总体分层可以保留，但原方案不能直接照写。必须补齐模型预热与首块动作握手，并撤回“直接用现有 homing 实现 reset”的建议。** 主方案已同步修订，见 [robot_bridge_integration_plan.md](robot_bridge_integration_plan.md)。

## 1. 五项操作的明确输入输出

| 操作 | 谁负责 | 输入 | 何时算成功 / 输出 |
|---|---|---|---|
| 加载模型 | GPU 侧 Policy Server；可由既有 Policy Manager 启动 | checkpoint 路径、GPU、OpenPI 配置（旧 checkpoint 才需显式指定） | metadata 校验成功且一次合法 infer 完成，适配层记录 `model_ready=true`；这是新增预检状态 |
| 开始推理 | Runtime → Driver → scheduler worker | execution ID、完整 instruction、已就绪的模型和 Monitor 参考帧 | worker 开始 infer；首块合法动作入队确认后激活 Monitor；HTTP execute 再返回成功 |
| 切换 instruction | ready 输入 → 新 execution | 新目标词，或回车复用；config 模板 | 本轮 VLA 的 prompt、GRM 的 subtask、SAM3 的 queries 全部来自同一轮冻结数据 |
| 停止执行 | Runtime → Driver 的控制路径 | execution ID / 取消 generation | 禁止后续动作、解决在途发送、clear、实测停稳后 `stopped:true`；不卸载模型 |
| 恢复初始状态 | Runtime → recovery worker | 已确认停止、配置的 home 位姿/夹爪/阶段与容差 | 各阶段执行和反馈验证通过，且未被新停止/急停打断后 `reset:true` |

`model_ready`、首块动作状态、recovery worker 等均为待实现设计。上游仍保留 `ready → executing → recovering → ready` 三状态，不要求用户操作新增中间状态。

## 2. 加载模型

### 当前代码实际做什么

VLA 加载链路是 `run_policy_server.py → create_policy_server_from_config → create_backend → OpenPiBackend.__init__ → create_trained_policy`。Backend 构造完成后才创建/监听 WebSocket 服务。模型在 GPU 进程中常驻，Scheduler 和 Robot Runtime 只通过 RPC 使用它；重建 Scheduler 不会重载权重。

OpenPiBackend 优先从 checkpoint 的 `metadata/train_config.yaml` 读取训练配置，无该文件才使用显式 `policy_config`。状态布局、动作 mode、图像尺寸等随后通过 `get_metadata` 提供给 Scheduler。Runtime 不应自行猜维度或额外加载一份模型。

两种现有加载入口都可复用，第一版建议采用已经部署好的常驻实例：

| 方式 | 接口 | 注意事项 |
|---|---|---|
| 固定部署 | `configs/policy_backends/openpi.yaml` 中 `params.policy_dir`，或 `RB_OPENPI_POLICY_DIR`；既有启动脚本 | Policy 部署常用端口是 8946，代码通用默认 8000；必须使用实际实例地址 |
| Policy Manager 管理 | JSON WebSocket `deploy(backend,host,path,port?,gpu?,...)`，随后轮询 `status` | deploy 的 ok 是受理；实例 running 是 metadata 探测通过；都不等于 infer 已验证 |

Policy Server 的公开命令只有 `infer/get_metadata/get_log`，没有 `load_model/unload_model/reset_policy`。如需切换 checkpoint，应在停止任务后通过部署层更换实例，重新探测 metadata 和预热，不把该操作塞进每次 execute。

### 原方案遗漏及修订

**[必须补充] 区分“模型加载”和“可完成推理”。** Policy Manager 的 `_probe()` 只发送 get_metadata，未做 infer。依赖实现可能在首次推理中做编译/初始化；即使加载通过，首轮仍可能发生输入变换、形状、显存或推理错误。本仓没有证明首次推理已预热。

新增部署预检：校验期望 checkpoint 的 `policy_dir` 及 metadata → 用 Scheduler 构造合法输入 → 调一次 infer → 校验动作 rank/width/horizon、非空、有限数值。**预热不能调用 build_act_request 推进正式任务上下文，也不能发送 Robot execute。** 使用独立的预检 Scheduler 或只调用 obs/input 构造，完成后销毁预检上下文；正式任务重新取实时观测，预热动作永远丢弃。

预检 prompt 来自单独配置的 `warmup_prompt` 或 checkpoint 的合法示例；若未提供，可在用户首次输入目标后、正式捕获 Monitor 参考帧前，用该 instruction 完成预热。不能为了预热强行要求 ready 每轮都输入完整句子，也不能把无目标时的空字符串当成已验证任务输入。

记录模型 URL、部署实例标识（若部署层提供）、metadata 摘要和预热结果。路径字符串本身不是权重哈希，也不能识别同路径被替换；约定任务期间模型不替换，连接异常/部署变更后取消任务并重新预检。加载超时与单次 infer 超时分开设置，不能用当前 WebSocketClient 默认 30 秒约束整个模型加载。

GRM 和 SAM3 也应常驻：GRM 在 GRMMonitorBackend 构造时加载，SAM3 在创建 HTTP Server 前构造 SAM3Detector。Monitor.start/stop 管理每轮参考帧和评分状态，不重新加载模型。启用 attention steering 时 GRM 必须使用 `inference_engine: hf`，并匹配已验证的 steering profile。它们的 health 也不能替代一次实际 GRM/SAM3 推理验证；预检结果不能写进正式 Monitor 的评分历史。

## 3. 开始推理

### 当前方案中的时序漏洞

**[必须修订] worker 启动不等于动作已开始。** 当前 Runtime 在 Driver.execute 返回后立即 activate Monitor。原方案让 Driver 启动后台线程就返回 executed:true；于是连接、metadata、首次 infer 都可能发生在 GRM 开始评分之后，机器人长时间不动会被计入失败窗口。这是原方案接入异步真实 Driver 后会引入的问题。

修订后的内部顺序：

1. 准入检查：模型就绪、无活动任务/恢复、无急停、旧发送结果可确定。
2. 冻结本轮 `execution_id/instruction/target_queries/model_identity`。
3. Monitor.start 暂缓评分，等待参考帧就绪。
4. Driver.execute 创建本轮 worker 并快速返回；worker 取观测、构造输入、infer、校验动作、发送首块。
5. Runtime 在 `_driver_lock` **之外**等待本 execution 的 `first_chunk_queued` 事件/状态；只有 Robot `status:ok` 且 queued 与本次提交点数一致并大于零才满足。
6. 激活 Monitor 评分，再返回上游 execute 成功。

等待过程中可取消、有首块动作期限和独立整轮定时器。worker 抛错、无有效动作、超时、错误 ID 均进入停止清理；activate 失败也必须停止已经提交的动作。不能持有 `_driver_lock` 等首次推理，否则 stop 无法响应。HTTP 超时需覆盖参考帧等待、首块等待和必要清理，重试沿用原 execution ID。

首块入队确认依然不是设备运动确认；本轮动作后的持续观测和 Driver 故障监督仍需运行。这个握手的作用是排除明确的首轮准备/推理空等时间。

### 复用 Scheduler 的具体要求

关闭 `OpenPiScheduler` 默认单步等待；每个任务保留一个实例并循环运行，**不是每个动作块重新建实例**。Runtime 管理 obs/policy/execute 连接和最终发送互斥，复用 build_obs_request/build_policy_obs/build_act_request/after_execute。

注意 build_act_request 在发送前就修改 `_phase/_auto_starting`。发送被取消、拒绝或结果不确定后，不得沿用该实例继续下一轮；将任务失败并销毁上下文，或显式实现经过测试的事务回滚。第一版选前者。

在发送前补齐数值和长度校验，且限制单块与累计未消费动作量；不能仅凭 Scheduler 当前的动作宽度检查认定任何输出都可入队。执行子进程容量上限同样适用于正常 VLA 动作，不能只在 recovery 中考虑。

## 4. 切换 instruction

**第一版仅支持任务之间切换，执行中不热改 instruction。** 这与用户的 ready 键盘循环一致。

例如第一轮输入 `carrot`，下一轮输入 `purple mug`：

| 字段 | 第一轮 | 第二轮 |
|---|---|---|
| execution ID / Monitor ID | A / MA | B / MB，必须新建 |
| VLA prompt、GRM subtask | `pick the carrot and put it on yellow plate` | `pick the purple mug and put it on yellow plate` |
| SAM3 queries | `["carrot"]` | `["purple mug"]` |
| VLA/GRM/SAM3 权重 | 常驻 | 复用 |
| Scheduler phase/首轮标志、Monitor 参考帧/平滑评分 | A 的状态 | 重新初始化 |

回车只复用目标词，仍然创建新 execution。任务文本和词列表作为不可变任务数据传给各层，不从一个全局可修改字符串实时读取。

`OpenPiScheduler(..., prompt=完整指令)` 支持任意字符串，每次 infer 都带 `prompt`；训练 prompts 列表只限制现有 UI 的 index 选择。无需调用 `_set_prompt(index)`，也无需重新加载模型。若调用 `_set_prompt`，它只使正在计算的旧 chunk 失效，并不清掉已经入队的旧动作或重置 GRM，因此不能作为此系统的完整切换实现。

**[必须补充] 同一 Policy 实例的旧推理必须收尾。** WebSocketServer 允许不同客户端同时进入 handler；Policy Server 和 OpenPiBackend 没有跨连接 infer 锁。本仓不能证明底层 policy 并发安全。旧 infer 超时后新建连接直接发下一轮，可能形成并行推理和迟到结果混淆。

适配层对同一 Policy 实例只保留一个在途 infer，跨 execution 也遵守。stop 可以不等待模型计算结束就停止机械臂，但下一轮发 infer 前必须确认旧调用已结束；超时关闭 socket 不能证明服务端已结束计算。结果不确定时把 policy 标为不可用，待部署层恢复并重新预检。正常切换不调用 Policy Manager.stop_instance；停止模型进程不能替代清理机器人动作队列。

GRM 已有 `_infer_lock` 和取消后提交检查，新 Monitor 不应继承旧评分；旧推理仍可能占着锁，所以 monitor.start 的参考帧就绪也不等于新评分能立即运行，仍保留首结果超时。

## 5. 停止执行

应分别记录以下状态，避免一个 stop 字段掩盖不同事实：

| 事实 | 确认方式 |
|---|---|
| 不再产生新的动作发送 | 本轮 generation 已取消，所有 autonomous/recovery 发送入口都校验它 |
| 已发出的旧 execute 不会排在 clear 后面 | 最终发送互斥与 RPC 明确回执；不能用不同连接的到达时间猜测 |
| 未来动作已清理 | clear_actions 响应及后续主队列状态；当前缺执行子进程完成回执 |
| 机械臂已稳定 | 新鲜实测位姿/夹爪连续窗口满足容差 |
| 模型在途调用已结束 | 独立推理状态；不是物理 stopped:true 的前提，但会约束下次 infer |

正常 stop 顺序仍是：取消生产者 → 解决最后一个发送 → clear → 新鲜反馈停稳 → stopped:true → Monitor 清理。重复 stop 幂等、旧 ID 不影响新任务。模型权重保持加载；不调用 UI 暂停、杀 scheduler 或 stop_instance 来冒充机器人停止。

**[必须修订] stop 要覆盖恢复过程。** 当前 Runtime.reset 在整个 Driver.reset 调用期间持 `_driver_lock`；普通 stop 也需要此锁，因而只能等恢复返回后才执行。改成短临界区记录 recovering/generation，长时间恢复等待在锁外；stop 先发布恢复取消，再通过相同最终发送仲裁清队列。恢复无 active execution 时也必须有可取消的 recovery token，不能只依赖当前 `_active_execution_id`。

Runtime 必须有显式 recovering 准入标志，阻止新 execute 在恢复过程中提前启动 Monitor、拍下错误起始参考帧。旧任务的重复 stop 如果确实属于当前恢复，应中断该恢复；属于更早代际的请求则不可影响新一轮。

clear_actions 只是在子进程通信队列里插入截断标记，子进程处理前仍可能继续发送；SDK/底层控制器也可能继续向最后目标运动。当前方案只承诺受控停止并等稳定。连接异常时无法解决旧 execute 次序、或不能确认真实状态，返回失败并拒绝继续自动恢复，不虚报 stopped:true。

## 6. 恢复初始状态

### 先固定“初始状态”的含义

第一版默认恢复配置中的固定 home，包括左右臂末端位姿、最终夹爪状态、阶段路径、单位和容差。现有 homing 的零位是双臂**末端指令零位**，不是自动识别的关节 home，也不是本轮开始时捕获的姿态。

若要“回到按 Enter 前的姿态”，需在动作前捕获并校验该轮起始状态，提供另一种恢复模式。机器人恢复姿态也不会自动把桌面物体放回原处；ready 只代表机器人可开始新任务。

### [阻断] 现有 homing 长轨迹会超过执行缓冲

`X1ProController.homing()` 在 100 Hz 下构造 597 点、约 5.97 秒轨迹，一次调用 execute 入队。主进程动作缓冲默认 2000，但 `exec_worker_main()` 自己创建固定 `TimestampedBuffer(maxlen=300)`，且在采样执行前循环排空通信队列。

用真实 homing 轨迹构造和真实 TimestampedBuffer 做了离线复现：不构造 SDK 控制器，以 stub execute 捕获动作，模拟快速到达执行缓冲。结果：

```text
homing_action_count = 597
first_queryable_timestamp_s = 2.98
has_before_anchor_at_1s = false
queryable_action_count = 300
```

这证明在快速入队条件下前 297 点已不在有效查询范围，执行器在早期没有 before 插值锚点；张爪、等待和一部分回零轨迹可能被跳过。它不是每次真机调度都必然按同一时序发生，但当前代码没有背压阻止该条件出现。仅增大 YAML 中的 action_buffer_size 不会改变子进程固定容量。

因此，“原样发 homing，最后检查 home”不足以解决问题：最终位置正确也无法发现中间没有先松开物体。**推荐零修改方式是把恢复编排放在 Runtime，分小块发送 execute，并验证各关键阶段。** 如坚持直接复用 homing，则必须先修复 robot-bridge 的执行缓冲/背压与回执，再验证完整轨迹。

### 推荐的恢复步骤

1. 确认自主 worker 已取消，最后动作发送已确认，清队列和停稳成功；冻结本次恢复 generation 与 home 配置。
2. 依恢复配置处理夹爪，确认实测完成，再开始回 home。不能把夹爪开合顺序和数值永远写死在 tool 中。
3. 从最新实测姿态生成到 home 的验证过的分段路径，旋转按正确旋转空间插值；例如每块至多 50 点。上一块排空后再提交下一块是第一版简单背压方案，可接受块间停顿；未来若优化连续性，需显式维护总待执行点数上限。
4. 每块都走最终发送互斥和取消检查；每个阶段等新鲜反馈确认。最后目标重复保持一段时间：当前执行器只在 before/after 都存在时发送插值点，队列尾部没有 after，不能假定最后一个点必定被发送。
5. 确认双臂 home 与最终夹爪状态，连续稳定窗口通过；配置窗口还应覆盖模型使用的状态历史长度及观测 lag 余量。该窗口内可以按背压发送已验证的 home 保持点，使下一轮历史窗口不会落到旧任务轨迹。随后确认队列排空；新 Scheduler 首轮从实测 slave 起步，后续 master 历史只能包含稳定 home 和本轮已确认的命令。
6. 锁内检查本 recovery generation/急停 epoch 未变化，再提交 reset:true、清理恢复状态、放行 ready。任一阶段失败/取消均清理剩余动作并返回错误。

不用 Robot Server homing 就不会自动清掉它的命令历史；仅创建新 Scheduler 并不清服务端缓冲。因此第 5 步的稳定历史窗口是零修改方案的一部分。其长度至少按 `state_history_size × state_step / policy_hz` 加允许的观测 lag 计算，需测试第二个 VLA chunk 的 master_state，而不只检查第一块。

### [阻断] 较早 reset 会清掉更新的急停锁定

当前 `RobotRuntime.reset()` 等 Driver.reset 返回后无条件执行 `_estop_latched=False`。emergency_stop 刻意不取 `_driver_lock`，所以能在 reset 期间发生。离线使用阻塞 reset 的 placeholder Driver 复现：

```text
reset 开始，尚未返回
emergency_stop 返回后：_estop_latched = true
较早 reset 返回 reset:true 后：_estop_latched = false
```

必须使用递增的急停/控制 epoch：reset 开始记录版本，期间新 stop/急停使版本变化并取消恢复；旧 reset 返回后不能报告成功或解除新锁定。只有明确发生在最后一次急停之后、且未被再次打断的 reset，才有资格解除该锁。这个问题在当前 Runtime 中存在，不是 robot-bridge Driver 才会引入。

另外，旧 homing 的 blocking execute 只等主队列没有未来点；若被 clear 打断，函数也可能返回，然后 handle_homing 报 ok。故即使修复缓冲，也仍需恢复操作身份、取消状态和实测结果，不能只看 RPC ok。

## 7. 接入前验收清单

| 项目 | 必须验证的情形 |
|---|---|
| 加载模型 | checkpoint/metadata 不匹配、加载失败、metadata 成功但首次 infer 失败；预热动作从未发给机器人 |
| 开始推理 | 首块前 GRM 评分为零；首块超时/动作非法/取消不误报启动；activate 失败触发停止 |
| 切换 instruction | carrot → mug → 回车复用；VLA/GRM/SAM3 三方字段一致；旧 infer 迟到不入队，新旧推理不并发 |
| 停止执行 | infer 中、execute 在途、恢复各阶段都能取消；stop 不依赖模型调用结束；无法确认时返回失败 |
| 恢复初始状态 | 597 点容量复现；张爪先于移动的反馈证据；尾点保持；reset 中 stop/急停；旧 reset 不清新锁；第二块 VLA 无旧历史污染 |

本次只做了静态审查及两项离线问题复现。没有运行真实模型、网络部署或机器人轨迹，因此没有宣称真实停止延迟、恢复精度或推理效果已达标。

## 8. 主要代码依据

- 加载和推理：[Policy Server](../../robot-bridge/robot_bridge/policy/server.py)、[OpenPiBackend](../../robot-bridge/robot_bridge/policy/backends/openpi.py)、[Policy Manager runner](../../robot-bridge/robot_bridge/policy_manager/runner.py)、[Manager API](../../robot-bridge/docs/reference/policy-manager-api.md)。
- instruction 与动作构造：[OpenPiScheduler](../../robot-bridge/robot_bridge/scheduler/openpi.py)、[SchedulerBase](../../robot-bridge/robot_bridge/scheduler/base.py)、[传输并发](../../robot-bridge/robot_bridge/transport/websocket.py)。
- 恢复轨迹与缓冲：[X1ProController](../../robot-bridge/robot_bridge/robot/controllers/x1pro/controller.py)、[exec_worker](../../robot-bridge/robot_bridge/robot/controllers/x1pro/exec_worker.py)、[TimestampedBuffer](../../robot-bridge/robot_bridge/robot/controllers/x1pro/buffers.py)。
- Runtime 时序和取消：[runtime.py](../robot_runtime/robot_runtime/core/runtime.py)。
- Monitor/SAM3 常驻加载：[GRM backend](../../Robo-Dopamine-delivery/monitor_runtime/grm_backend.py)、[SAM3 service](../../Robo-Dopamine-delivery/sam3_runtime/service.py)。
