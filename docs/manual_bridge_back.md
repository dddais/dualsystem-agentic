# manual_bridge：Back 轨迹回退

停止本轮后，`/manual` 的 VLA 控制提供 **Back / 回退（B）**，与 Homing 同属于恢复选择。
点击后倒放双臂历史轨迹及夹爪动作，退到最近一次夹爪操作开始前 10 个策略步。
完成后仍保持停止，loop 留在 recovering，继续等待 Homing 或遥操作调整。
完成归位或调整后才返回 ready；再次按 A 创建新的 execution、Monitor 和起始参考帧。

## 回退的具体含义

以抓取并搬运物体为例：

1. t=1 开始张开夹爪，为抓取做准备。
2. 机械臂接近物体，夹爪收拢；抓住物体时可以停在半开状态。
3. 机械臂带着物体移动，然后操作员停止并选择 Back。
4. Back 按原搬运轨迹倒退，保持对应时刻的抓持开度；回到抓取位置后，
   反向回放原先收拢的动作，使夹爪松开、将物体留在原处。
5. 继续倒放接近和张开准备动作，退到 t=1 前 10 个策略步的状态。
   例如策略为 20 Hz，目标是 t=0.5；最终夹爪恢复 t=0.5 的历史开度，可能为闭合。

两臂共用时间轴，回放包含全部 14 维双臂位姿与夹爪开度。没有独立的终点强制张开阶段。
这里的“放回”是按历史指令反向运动的目的，尚无实测到位或物体释放成功反馈。

操作识别规则：

- 每只夹爪按相对开度变化识别张开/收拢，默认变化幅度至少 0.1 并持续 0.1 秒。
  不依赖完全闭合阈值，半开抓持也能识别。初始开度和未确认的短暂变化不构成操作。
- 同一夹爪的一次张开及随后的收拢配对为一次操作，起点保留在张开准备开始处。
  初始已经张开时的单独收拢、尚未配对的张开也能独立撤销。
- 起点取阈值确认前的极值平台末端，允许幅度阈值 10% 以内的噪声；前置步数从这里计算。
  选择最近确认变化的操作；两臂同时确认时取更早的起点，两臂仍一起倒放。
  这是根据夹爪指令识别的开合过程，不是视觉或接触感知确认的抓取事件。

轨迹来自 X1 Pro exec worker **成功下发的 SDK 目标**，不包含尚在队列中的预测，
也不是编码器实测轨迹。默认保留最近 120 秒、最多 `control_hz × 120` 个目标，
以原时间轴的 0.5 倍速度倒放。回退按执行 tick 流式发送，不将长轨迹塞入原有 3 秒缓冲。

成功后截断已撤销的分支并重建事件记录，后续 Back 可以继续撤销更早的操作；
回退自身不会创建新操作。正常停止保留历史，Homing、进入遥操作和改变整机姿态会清空历史。
Runtime 重启不会清 Robot Server 内存；Robot Server/exec worker 重启会丢失历史。

没有可用操作、准备张开已过期、缺少前置 10 步历史或 SDK 执行失败时，Back 返回失败，
恢复要求继续保留，不缩短回退距离。普通 Back 失败后仍可直接选择 Homing 或遥操作，
恢复调用继续等待，不需要因此重启 loop；软件急停或取消仍中断恢复调用。
手工搬动机械臂或改变底座等未经过当前控制链路的操作不在记录中；这类调整后应归位重新开始。

## 生命周期与停止

正常路径为：`执行 → 空闲 I → Back B → 已停止/等待恢复 → Homing H → ready`，
或 `执行 → 空闲 I → Back B → 已停止/等待恢复 → 遥操作 T → 空闲 I → ready`。
可以连续选择 Back；每次执行后都继续停留在同一次恢复中。
loop 没有单独的 stop 枚举，此处对应 **recovering**；Scheduler 保持 **idle/暂停**，GRM 保持停止。
执行中、准备参考帧、归位中、遥操作调整中和软件急停锁存期间不能选择 Back。
`reset_task` 仍然只代表 Homing；需要现有 `recover_task` loop 配置才能选择 Back。

点击只返回 accepted。Runtime 使用操作 ID 等待 Scheduler 的 `back.phase`；
只有 Robot Server 的执行进程成功下发最终历史位姿和夹爪目标，Scheduler 才报告 completed。
整段回放均属于 running，包括抓取位置处的释放动作；任何下发失败都不会放行下一轮。
随后 Runtime 再等待 `stop_delay_s`，在 `/manual/status` 的 `pending.last_back` 和
`last_operation.result` 中记录这次动作结果，例如：

```json
{"back": true, "recovered": false, "recovery_required": true, "homed": false,
 "completion_basis": "sdk_dispatch_and_delay", "gripper_policy": "replay_history",
 "pre_event_steps": 10, "step_hz": 20.0}
```

`recover_task` / `/control/recover` 此时继续等待，不返回 `recovered: true`。
后续 Homing 或遥操作调整完成才结束该调用，最终结果的 `back_results` 保存中间回退记录。
每次回退完成或普通失败后都会换一个新的 `request_id`，旧点击重试不会触发第二次回退。
网页显示“已停止 · 回退完成/回退失败”，保留 H、T、B；A 仍不可用。

这里的完成依据是 SDK 下发与配置等待，不是机械臂实测到位。
重复或过期网页点击沿用 request_id/execution_id 检查，不会重复发送回退。
命令响应丢失不会自动重发；Runtime 尝试取消回退并清队列。

“立即软件停止”可取消等待调度的 Back，也可中断回放中的任意阶段。
运行中的回退被中断后丢弃该历史，避免再次沿不完整的恢复轨迹移动。
软件停止仍会锁存，迟到成功结果不能解除锁存；只能通过 Homing 恢复。

## 部署与配置

需要更新两处源码并重启相关进程：

1. 从臂 Robot Server 和 Scheduler 更新 **robot-bridge**。当前执行端支持 X1 Pro；
   `openpi` 与 `openpi_takeover` 均支持调度。takeover 回退时主臂保持 idle，仅回退从臂。
2. Runtime 更新 **dualsystem-agentic**，重启 Runtime 和 loop/MCP，刷新网页。

Policy Server、GRM 和 SAM3 无需为 Back 更新。旧 Scheduler 或不支持 Back 的控制器仍可使用原模式，
网页禁用 Back。更新和重启服务前先停止现有任务。

Robot Server 的 `configs/robot_controllers/x1pro.yaml`：

```yaml
params:
  back:
    history_s: 120.0
    speed: 0.5
    gripper_change_threshold: 0.1
    debounce_s: 0.1
    pre_event_steps: 10
    pre_event_step_source: policy
```

`pre_event_steps` 是非负整数，默认 10。`policy` 使用 Scheduler 实际策略频率，
20 Hz 时为 0.5 秒；改为 `control` 则按执行进程控制频率换算，100 Hz 时为 0.1 秒。
Scheduler 自动将策略频率传给 Robot Server，不需要在 Runtime 重复配置。
旧版 Back 的 `closed_threshold` / `open_threshold` 应删除，改用 `gripper_change_threshold`。
已有自定义配置若仍写着 `gripper_change_threshold: 0.5`，需自行更新为 0.1；
只更新 Python 默认值不会覆盖 YAML 中明确写出的旧值。这是 SDK 0–4.5 指令量程的绝对变化量。

Runtime 的 `robot.back_timeout_s` 默认 300 秒。这是等待完成的最长时间，不是固定回退等待；
应覆盖历史长度除以速度以及现场推理/通信等待。速度范围为 `(0, 1]`。
状态中的 `duration_s` 与 `reverse_duration_s` 均表示整段倒放时长；Scheduler 另有 600 秒上限。
`interaction_start_ts`、`target_ts`、`pre_event_steps` 和 `step_hz` 可用于核对所选历史边界。
每次 Back 后重新开始 `operator_timeout_s` 的选择等待计时。
示例 loop 的 `DUAL_FRANKA_TIMEOUT_S` 已增至 1320 秒，覆盖等待选择、一次回退、
再次等待选择、遥操作调整及通信余量。多次长回退时应相应增加该 HTTP 超时。

HTTP 使用 `POST /manual/bridge/action`，`name: "back"`，`args.request_id` 为当前恢复请求 ID；
loop 尚未开始恢复时也可携带当前 `args.execution_id` 提前选择。推荐由网页自动填写。
不要在 manual_bridge 运行期间从独立 Scheduler 控制面绕过 Runtime 发起恢复。

## 没有回退事件时如何定位

旧提示 `no retained gripper interaction to undo` 同时用于无历史和无已确认夹爪事件，
不能仅凭这一句认定是物体太细。旧默认幅度阈值 0.5 会漏掉例如张开 0.2 的抓笔动作；
新版默认已改为 0.1，并保持 0.1 秒去抖。检测依据是下发指令，实测夹爪开度变化不直接参与检测。

新版失败状态包含 `error_code` 和 `diagnostics`；网页错误文字也给出历史采样数、时长、
有效阈值、去抖时间以及左右夹爪下发开度范围：

- `no_history`：没有足够的连续动作记录，检查是否刚重启/归位或清空了历史。
- `no_gripper_interaction`：有历史但没有保留的已确认开合。
  检查左右范围是否小于阈值；若变化足够大，也可能是变化未持续到去抖时间或事件已经过期。
- `insufficient_pre_event_history`：识别到了开合，但抓取准备开始前的指定步数历史不足。

例如 `threshold=0.1, left=[0.200, 0.240]` 表明保留历史内左夹爪只有 0.04 的变化，
仍小于 0.1；可据实际指令幅度调整阈值，同时确认抖动不会被识别为操作。
更新 Robot Server 并重启后旧内存历史会丢失，需要重新执行一轮，再停止测试 Back。

## 无硬件验证

```bash
python -m pytest tests/test_manual_back.py tests/test_manual_bridge_lifecycle.py
PYTHONPATH=/path/to/robot-bridge python -m pytest tests/test_robot_back_wire.py tests/test_robot_bridge_wire.py
python tests/validate_manual_shortcuts.py
```

robot-bridge 的 `tests/robot/controllers/x1pro/test_backtrack.py`、`test_back_worker.py` 和
`test_controller_back.py` 覆盖半开抓持、曲线路径和夹爪同步倒放、简化物体模型的放回过程、
策略/控制步换算、双臂时间轴、夹爪去抖、历史不足、连续回退，以及释放阶段的失败和中断。
现场仍需验证夹爪变化阈值、执行跟踪误差及物体实际放回效果。
