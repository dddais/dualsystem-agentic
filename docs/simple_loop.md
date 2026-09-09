# 无 VLM 的键盘循环

只有三个状态：`ready → executing → recovering → ready`。

| 状态 | 输入 | 动作与输出 |
|---|---|---|
| ready | 输入目标物体名词，或回车复用上一目标 | 模板生成 instruction，调 MCP `execute`；Runtime 等 Monitor 参考帧就绪后调用 driver |
| executing | execute 的初始状态，随后定期查询的 monitor 状态 | `running/progress` 继续查询；其他状态、错误进入 recovering |
| recovering | 执行结束或异常 | 依次调用 `stop_task`、`reset_task`，成功后回到 ready |

不加载 VLM、AgenticRobotLoop、DataLoader 或额外 executor。GRM Monitor 仍由 Robot Runtime 的 `remote_http` provider 接入，并自行采图。这里将用户所说的 `progress` 兼容为现有 Monitor 的 `running`；数值字段 `progress` 只显示，不用于状态分支。success 也会触发中止与恢复。

## 启动

先启动既有 Robot Runtime 和外部 Monitor（使用 GRM steering 时也需启动 SAM3），然后在仓库根目录：

```bash
conda activate dualsystem-agentic
export DUAL_FRANKA_RUNTIME_URL=http://127.0.0.1:8767
PYTHONPATH=src python examples/run_simple_robot.py --config examples/config.simple_loop.yaml
```

输入目标物体并回车，例如 `carrot` 或 `white cube`。恢复后直接回车会复用上一次目标；第一次还没有目标时，空行只提示输入。`q` / `quit` / `exit` 或 ready 下 Ctrl+C 退出。上一目标只保存在当前进程内，不跨重启保存。

在 [config.simple_loop.yaml](../examples/config.simple_loop.yaml) 中设置模板和默认目标：

```yaml
simple_loop:
  instruction_template: "pick the {target} and put it on yellow plate"
  default_target: null
  first_result_timeout_s: 120.0
  result_timeout_s: 120.0
  max_execution_s: 300.0
  require_steering: true
```

例如输入 `carrot` 后，发送 `subtask: "pick the carrot and put it on yellow plate"` 和独立的 `target_queries: ["carrot"]`。后者经 MCP、Robot Runtime 和 Monitor 原样送入 SAM3 的 `queries`，不再依赖从完整 instruction 提取名词。模板只允许 `{target}` 占位符，可写成中文；目标词也原样传递，不自动翻译，建议使用 SAM3 可识别的物体短语。设置 `default_target: carrot` 后，启动时即可直接回车。

调整查询间隔（秒）：

```bash
PYTHONPATH=src python -m dualsystem_agentic.simple_loop \
  --config examples/config.simple_loop.yaml --poll-interval 0.5
```

也可修改 YAML 的 `loop.monitor_poll_interval_s`。间隔是上次调用返回后等待的时间，实际周期还包括 HTTP 耗时。执行中 Ctrl+C 会先中止、恢复，然后退出。

`first_result_timeout_s` 限制首个评分的等待；`result_timeout_s` 检查结果年龄和评分轮数是否长期不变；`max_execution_s` 限制整轮时间。以上检查在工具返回后的循环中进行，单次网络调用还受 HTTP 超时约束。Runtime 另外在 driver 启动成功后按 `safety.max_execution_s` 启动独立定时器，即使客户端不再轮询也会尝试停止机器人。

默认 `require_steering: true`：已提交评分必须包含每个模式的 `steering.applied=true`、`degraded=false`，否则进入中止恢复。只做 local_memory/无干预连通性测试时显式设为 false。Monitor 自身仍可配置 baseline 降级，但严格 loop 不会将降级结果作为正常成功接受。

## 工具与接口

| MCP 工具 | 参数 | Robot Runtime 接口 |
|---|---|---|
| execute | `execution_id`、模板生成的 `subtask`、`subtask_index: 0`、`target_queries` | `POST /executions`；返回 execution/monitor ID 和初始状态 |
| monitor | execution/monitor ID、子任务文本及序号 | `POST /monitors/status` |
| stop_task | 本轮 `execution_id` | `POST /control/stop`；优先 driver.stop，再清理 Monitor |
| reset_task | 空对象 | `POST /control/reset`；调用 driver.reset |

MCP 的 execute 会在创建执行后立即查询一次 Monitor，因此循环直接使用该初始状态，不重复启动 Monitor。后续查询保留 `poll_count/result/error` 等诊断字段并携带 ID，拒绝错配响应。

客户端在发送请求前生成 execution ID；即使 execute 响应丢失，仍能停止对应任务。Runtime 记住提前到达的取消请求，延迟的 execute 不能在 stop 后启动。相同 ID 和请求可幂等重试，内容冲突则拒绝。一个 Runtime 同时只允许一个尚未停止的执行；终态后也应调用 stop 再开始下一任务。旧 ID 的停止请求不会停止新的执行。

恢复映射到 `reset_task`；专用配置显式设置 `DUAL_FRANKA_ENABLE_RESET: "true"`。其他恢复工具可用 `--recover-tool` 替换，但必须返回 `reset: true` 表示恢复完成；停止工具可用 `--stop-tool` 替换，需接受 execution ID 并返回 `stopped: true`。命名空间可用 `--namespace` 指定。

启动前检查四个工具是否存在。执行/监控调用失败同样进入 recovering；机器人 stop 失败不继续 reset，stop/reset 任一失败会保留 recovering 状态并退出。Monitor 清理失败单独返回 `monitor_cleanup_error`：机器人仍先停止，loop 显示警告并继续恢复。Runtime 保留本地取消状态，不再发布该会话的远端旧结果。

driver.execute 返回 `executed: true` 后激活评分，不等待整段任务结束；manual 会等待人工开始，robot_bridge 会等待配置的启动延时。返回 false、错误或缺少确认会导致启动失败并清理。stop/reset 的交接依据由 adapter 声明：manual 等待人工操作，robot_bridge 接收命令返回并等待归位延时，不轮询物理位姿。急停后必须 reset 才能再次执行。

Runtime 的同步 HTTP 路由在线程池执行，等待远端 Monitor 时相机和控制接口仍可响应。`safety.monitor_ready_timeout_s` 默认 30 秒；启动顺序是 monitor.start（`defer_inference: true`）→ 等待 GRM `warming_up=false` → driver.execute → `/monitors/activate` 开始评分。参考帧捕获期间评分保持关闭，避免动作前就产生终态。`activate` 由 Runtime 内部调用，不增加上游 MCP 操作步骤。

需同时更新并重启 Robot Runtime 和 GRM Monitor；Runtime 会拒绝不支持暂缓评分的旧 GRM 服务，以免静默回到错误时序。单独使用 Monitor、不传 `defer_inference` 时仍沿用自动开始评分的行为。远端请求还有自己的超时，简单 loop 配置的 MCP HTTP 超时为 120 秒，以容纳启动等待和失败清理。

默认 Dual-Franka 配置仍是 placeholder + 本地 JPEG；真机可选择新增的
[manual / robot_bridge adapter](robot_bridge_adapters.md)，两者直接从 Robot Server 采图。
manual 请使用 `examples/config.simple_loop.manual.yaml`，为人工等待保留更长的 HTTP 超时。
自动模式使用本页原有配置，停止与归位延时在 RobotDriver 内部处理。

实现：[simple_loop.py](../src/dualsystem_agentic/simple_loop.py)。测试：[循环测试](../tests/test_simple_loop.py)、[Runtime 故障与并发测试](../tests/test_runtime_contracts.py)、[跨仓库接口联调](../tests/validate_simple_stack.py)。修复与复查结果见 [系统复查报告](system_contract_review.md)。
