# manual_bridge 指令与运行状态检查

本次重点检查网页任务输入、MCP/Loop、Runtime、Scheduler、Policy Server 和 GRM
之间的指令传递与状态判断。正常传值链路通过，但有两项运行状态问题和一项日志问题。
以下问题已用模拟硬件复现，尚未修改生产逻辑。

## instruction 是否传给 VLA

当前仓库中，网页提交的最终 instruction 能原样进入 OpenPI 的 `policy.infer(obs)`，
字段为 `obs["prompt"]`；首尾空白在任务输入时去除，内部换行和花括号保留。

| 环节 | 实际行为与代码 |
|---|---|
| 网页 → Loop | 模板由服务端展开；完整指令直接生成 `TaskInput`。`SimpleRobotLoop.run_cycle` 明确传 `options.prompt_mode="text"`，见 [simple_loop.py](../src/dualsystem_agentic/simple_loop.py)。 |
| Loop → Runtime | MCP 将同一文本传为 `subtask` 并保留 `options`；Runtime 将该文本交给 driver 和 Monitor，见 [server.py](../mcp_server/dual_franka_mcp_server/server.py)、[runtime.py](../robot_runtime/robot_runtime/core/runtime.py)。 |
| Runtime → Scheduler | `set_prompt_text` 设置文本后，回读 `state.prompt` 并比较；旧 Scheduler 缺少该接口会报错，text 模式不会回退到 `prompt_map`，见 [robot_driver.py](../robot_runtime/robot_runtime/adapters/robot_bridge/robot_driver.py)。 |
| Scheduler → VLA | `build_policy_obs` 写入 `prompt`，`run_iteration` 发送 infer 请求。Policy Server 仅删除 `cmd`，OpenPiBackend 直接调用 `_policy.infer(obs)`，见 [openpi.py](../../robot-bridge/robot_bridge/scheduler/openpi.py)、[base.py](../../robot-bridge/robot_bridge/scheduler/base.py)、[OpenPiBackend](../../robot-bridge/robot_bridge/policy/backends/openpi.py)。 |
| Runtime → VLM | RemoteHTTPMonitorProvider 发送相同 `subtask`；GRMMonitorBackend 用它生成每轮 sample 的 `task`。SAM3 的 `target_queries` 独立传递，不替换 instruction。 |

验证包括预置模板、任意英文指令、中文、换行、花括号，以及与 instruction 冲突的
旧 `prompt_map`。新增 `test_text_instruction_reaches_openpi_policy_infer_without_aliasing`
还覆盖了真实 OpenPiBackend 方法，仅将加载后的模型替换成记录输入的探针。

这证明传输与适配层交付了文本，不证明现场 checkpoint 的 tokenizer、输入 transforms
和语言条件实际有效。本地没有现场所用 OpenPI 环境和权重，未验证其截断长度、默认
prompt 处理或中文任务能力。

## 已复现的问题

### 1. 启动成功不代表 VLA 推理成功（高优先级）

`RobotBridgeRobotDriver.execute` 在发送恢复运行命令、等待 `start_delay_s` 后就返回
`executed=true`，完成依据是 `command_and_delay`。Runtime 随后激活 VLM，未等待
当前 instruction 的首次 VLA 推理成功。Scheduler 的推理失败也没有进入 Runtime
的执行状态。

复现：让 Policy Server 的模型抛出异常。观察到：

```json
{
  "driver_reports_executed": true,
  "completion_basis": "command_and_delay",
  "vlm_activated": true,
  "scheduler_iteration_result": "retry",
  "robot_action_chunks": 0
}
```

因此，网页“执行中”或 VLM 有评分不能作为 VLA 已正常执行的证据。
建议 Scheduler 暴露推理序号、实际 prompt、成功/失败及时间，Runtime 在收到本轮
首次有效推理回执后激活评分；持续推理故障也应反馈给 Loop，而不只留在 Scheduler 日志。

### 2. 外部修改 Prompt 后，VLA/VLM 指令可能不一致（高优先级）

manual_bridge 的辅助接口已在执行期间禁止 `set_prompt`，数字键也遵守该限制。
但原 Scheduler UI、终端或其他控制客户端仍能直接修改 Scheduler prompt。
`ManualBridgeRobotDriver.scheduler_status` 的 `selection_error` 只检查本轮指令是否
可被设置，不比较当前 `state.prompt` 与本轮任务，因此不会报告这种偏离。

复现：本轮启动后，经真实 Scheduler 控制 WebSocket 把 prompt 改为另一项任务：

```json
{
  "vlm_instruction": "把红杯放到 {box} 旁。\n然后松开夹爪。",
  "vla_received_instruction": "Move the OTHER cup to the shelf",
  "dashboard_selection_error": null
}
```

建议在 Runtime 的执行状态检查中持续比较实际 prompt 与本轮 instruction，发现
偏离时显式报错并按既有停止流程处理；网页同时展示期望与实际值。检查应放在
Runtime 服务端，不能只依赖操作员打开浏览器。

### 3. Scheduler 推理错误日志遗漏具体原因（中优先级）

Policy Server 的异常回复字段为 `message`，SchedulerBase.run_iteration 却读取
`policy_result.get("error")`，复现时 Scheduler 打印 `Policy inference failed: None`。
Policy Server 自己的日志仍保留原异常，但只看 Scheduler 日志会缺少诊断信息。
建议兼容读取 `message` / `error`，同时在状态中保留最近一次失败信息。

## 验证范围与现场限制

- Runtime/Loop/通信相关检查：70 项测试及 9 个子测试通过。
- Scheduler 的 openpi、openpi_takeover、control_server 检查：30 项测试通过。
- 新增 OpenPiBackend 输入探针：2 项测试通过。
- 浏览器：完整指令提交 → MCP/Loop → manual_bridge 启动 → GRM 评分/bbox → 停止 → 归位 → ready 通过。
- 故障注入：上述推理失败和外部 prompt 修改均已复现；这些是现有正常路径测试没有要求拒绝的情况。

所有控制操作仅针对本机模拟硬件。现场仅尝试只读 status：当前配置
`ws://192.168.10.3:8088` 和 SSH 别名对应的 `ws://192.168.31.118:8088` 均连接超时；
SSH 别名 `lm-x1pro-m` 也连接超时。本机 8767/18877/8088 未监听现场服务。
因此尚未核验现场运行版本、当前 checkpoint、真实推理日志和当前任务。

主要验证命令：

```bash
# dualsystem-agentic 环境，robot-bridge 位于相邻目录
PYTHONPATH=../robot-bridge python -m pytest \
  tests/test_instruction_input.py tests/test_robot_bridge_wire.py \
  tests/test_manual_bridge.py tests/test_simple_loop.py tests/test_runtime_contracts.py

# robot-bridge 环境
python -m pytest -q tests/scheduler/test_openpi.py \
  tests/scheduler/test_openpi_takeover.py tests/scheduler/test_control_server.py

# dualsystem-agentic 环境，需 Playwright/Chromium
PYTHONPATH=../robot-bridge python tests/validate_manual_dashboard.py \
  --driver manual_bridge --input-mode instruction
```
