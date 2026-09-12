# manual_bridge：VLA 控制与 loop 状态

`/manual` 的 **VLA 控制**是统一操作入口。“本轮状态”只显示状态和 instruction，
不再提供第二组开始 / 停止 / 归位按钮。原 `manual` 仍使用人工完成确认。

## 状态与操作

| 当前状态 | VLA 控制 | 成功后的状态 |
|---|---|---|
| 任意阶段 | 保存模板目标或完整 instruction | 更新下次开始使用的指令；当前 loop / VLA 状态不变 |
| ready，已有保存指令 | 自主运行 A | 固定本轮 instruction，创建 execution / Monitor，准备参考帧 |
| 准备参考帧 | 等待；可用空闲取消本轮 | 就绪后自动设置本轮指令、启动 VLA，等待启动延时，再激活 GRM，进入执行 |
| 启动中 / 执行中 | 空闲 I | 取消本轮、暂停 Scheduler、清理动作队列，完成停止后关闭本轮 Monitor |
| 执行中，GRM 判定成功或失败 | auto_stop: false 时继续执行和评分；true 时自动停止 | false 保持执行中；true 停止后等待恢复选择 |
| 等待恢复 | Homing H | 归位中，等待配置的归位时间后进入 ready |
| 等待恢复 | Back B（新版 X1 Pro 支持） | 倒放双臂轨迹和夹爪动作，退到上次抓取准备开始前 10 个策略步；执行端报告完成后进入 ready |
| 等待恢复 | 遥操作 T | 进入调整；VLA 自主运行和 GRM 评分保持停止 |
| 调整中 | 空闲 I | 结束遥操作，清理动作队列并等待停止延时，进入 ready |
| 软件停止已锁存 | Homing H | 归位成功后解除锁存；此时不允许通过遥操作调整解除锁存 |

停止后的正常路径为：

- 停止 → Homing 归位 → ready → 下一轮。
- 停止 → Back 回退 → ready → 下一轮。详见 [Back 轨迹回退](manual_bridge_back.md)。
- 停止 → 遥操作调整 → 空闲结束调整 → ready → 下一轮。

**调整完成由切回空闲表达**，不额外添加“已调整”确认按钮。执行中不允许直接切遥操作，
需要先停止本轮；调整中不能直接切自主运行。执行或调整中可以保存下一轮指令，当前任务保持不变。
归位或调整完成后，下一轮直接按 A 复用已保存指令，无需重复填写或保存；每轮仍生成新的
execution / Monitor ID 和起始参考帧，不继承上一轮评分。参考帧就绪后不再需要第二次按 A。

保存与开始的具体规则见 [保存和复用 instruction](manual_bridge_instructions.md)。指令保存在
Runtime 内存中，可跨网页刷新、loop 重启复用；重启 Runtime 后需重新保存。
旧客户端通过 `/manual/task` 提交或直接调用 execute 时，仍保留原有等待 A 的交接入口。

若 Scheduler 没有 takeover / teleop 能力，遥操作按钮禁用。自主和空闲仍可使用
普通 Scheduler 的连续运行 / 单步暂停能力。`S` 和 `Enter` 保留执行中的单步控制；
它们不结束本轮。`Space` 在 manual_bridge 不再执行全局生命周期操作。

这些映射针对 `/manual` 内的 VLA 控制。直接在独立 Scheduler 网页或终端改模式，
不会自动完成 Runtime 的任务、Monitor 和恢复状态交接。

## 可选自动停止

Runtime 配置 `robot_runtime/robot_runtime/configs/manual_bridge.runtime.yaml`：

```yaml
robot:
  driver: manual_bridge
  auto_stop: false
```

- `false`（省略该配置时的默认值）：持续执行与监控。GRM 的成功、失败、双分支差异超阈值均只作为
  当次评分结论；VLA 继续运行，GRM 继续产生新的图像、bbox 和分数，loop 保持 executing。
  点击空闲 / I 才停止本轮，然后选择归位或遥操作调整。页面显示“GRM 判定：成功 / 失败 · 持续监控”。
- `true`：GRM 仍在成功 / 失败时结束推理；loop 收到终态后请求停止，Runtime 自动发送空闲 / 暂停
  和清队列命令，等待 `stop_delay_s` 后交给操作员选择归位或调整。

`false` 的正常执行不会被 loop 的 `max_execution_s` 或 Runtime 的 `safety.max_execution_s`
自动结束。首帧等待、评分停滞 / 过期、通信或启动失败仍沿用原有异常处理；软件急停仍然有效，
等待开始 / 归位 / 调整的 `operator_timeout_s` 不变。此配置须重启 Runtime 生效，不动态更改进行中的任务。

持续模式仍保留原来的进度累计与成功 / 失败窗口规则，但判定不再锁存；后续评分可以改变判定。
开始新一轮才重置累计进度和 reference。录制会持续收集成功 / 失败之后的新评分，直到手动停止。

两种模式都不自动开始下一轮或归位。页面会显示自动停止是否启用。
停止包含软停止和配置延时，未新增实测停稳 / 姿态确认能力。

## Loop 和 MCP 配置

`examples/config.simple_loop.manual.yaml` 已设置：

```yaml
simple_loop:
  recover_tool: recover_task
```

MCP 中继续使用 `DUAL_FRANKA_ENABLE_RESET: "true"`，同时开放 `reset_task` 和新增
`recover_task`。后者调用 `POST /control/recover`，携带本轮 `execution_id`，等待恢复分支完成。

顶层 loop 仍是 ready → executing → recovering → ready；recovering 内部由 Runtime
负责停止、等待选择、归位或调整。`recover_task` 返回 `recovered: true` 才能进入下一轮。
调整结果明确为 `recovery_method: teleop_adjustment, homed: false`，不会冒充物理归位。
`reset_task` / `/control/reset` 仍只表示原归位流程；原 `manual` 的 recover_task 回退到人工归位。

使用旧 loop 配置（或 `--recover-tool reset_task`）时，恢复阶段只有 Homing，没有调整分支。
命令行 `--recover-tool` 优先于配置。

## 状态一致性与失败处理

- `/manual/bridge/action` 的 `set_mode` / `homing` / `back` 通过 Runtime 生命周期处理，不直接代理模式命令。
  等待操作时携带当前 `args.request_id`；执行中主动空闲携带当前 `args.execution_id`。
  ready 阶段按 A 时携带 `args.input_request_id` 和 `args.instruction_revision`。
  网页自动填写这些字段，用于拒绝过期操作。重复提交同一请求和动作不会重发命令。
- `/manual/status` 的 `vla_controls` 给出当前可用操作；`recovery_required` 为 true 时拒绝下一轮。
  `pending.phase` 包括 waiting / queued / running / adjusting / finishing；页面按这些状态显示提示。
- 刷新或关闭网页不会替操作员结束调整。重新打开页面可继续选择空闲。
  `operator_timeout_s` 分别限制等待选择和调整持续时间，默认各 300 秒。
- 调整超时、命令失败或软件停止时，会尝试切空闲并清队列，保留恢复未完成状态；不放行下一轮。
  恢复调用失败时 loop 沿用原来的失败退出策略。可以通过 Homing 处理恢复，然后重启 loop；
  最后操作的错误会显示在页面。软件停止后只允许 Homing 解除锁存。
- 归位或调整的迟到成功回执不能清除后来发生的软件停止锁存。
- Record / R 沿用视频与 GRM 进度录制逻辑；调整阶段 GRM 已停止，不会生成新的评分。

## 部署与验证

“持续监控”需要更新两处：

1. Runtime 和 loop / MCP 所在机器更新 `dualsystem-agentic`，重启相关进程。
2. GRM 服务器更新 `Robo-Dopamine-delivery`，重启 Monitor 服务，然后刷新 `/manual` 并开始新任务。

Runtime 自动将 `auto_stop: false` 转成 Monitor 的逐任务 `continuous_monitoring: true`，无需额外
编辑 Monitor YAML。旧 Monitor 未确认支持时，Runtime 会明确报错，不启动 VLA。
Scheduler、robot-bridge、Policy Server 和 SAM3 无需为本次修改更新。
原 manual、自动 robot_bridge 和 auto_stop: true 沿用终态结束流程。

使用模拟硬件进行验证：

```bash
python -m pytest tests/test_manual_continuous.py tests/test_manual_bridge_lifecycle.py tests/test_manual_bridge.py tests/test_simple_loop.py
python tests/validate_manual_shortcuts.py
python tests/validate_manual_dashboard.py --driver manual_bridge --input-mode instruction
python tests/validate_manual_dashboard.py --driver manual_bridge --input-mode instruction --recovery teleop --auto-stop
```

浏览器脚本需要 Playwright / Chromium 和 `robot-bridge`、`Robo-Dopamine-delivery` 源码；
模型、相机和机器人使用模拟实现，不向真机发送动作。
