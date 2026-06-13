# AgentLoop 异步 Monitor 状态机设计文档

本文档总结当前 AgentLoop 改造方案的目标效果、整体流程、状态机语义、VLM 决策协议，以及不同决策对应的下一步处理方式。

## 1. 设计目标

当前目标是将 AgentLoop 从“VLM 发起动作后阻塞等待 monitor 结果”的同步流程，改造成更适合真实机器人长程任务的异步控制器模型。

新的核心思想是：

- VLM 是高层持续推理 controller。
- `execute` 只负责启动一个机器人动作，不代表动作已经完成。
- `monitor` 作为异步监督模块运行，负责观察动作是否成功、失败或超时。
- monitor 的结果以事件形式进入 memory，并触发下一轮 VLM reason。
- VLM 主循环不被 monitor 阻塞，可以按周期继续推理、观察环境、更新 plan。
- 当某个 subtask 正在执行时，系统用硬约束禁止重复 execute，而不是只依赖 prompt。

目标流程可以概括为：

```text
init -> ready -> reason -> act -> response -> reason -> ...
                         ^
                         |
                 async monitor events
```

其中 `reason` 既可以由固定时序 tick 触发，也可以由事件触发。

## 2. 总体流程

### 2.1 init 阶段

`init` 是系统初始化阶段，负责构建运行所需组件：

- VLM planner
- MCP client / MCP service manager
- downstream executor
- dataloader
- interaction layer
- run logger
- config

这个阶段不处理具体任务，只做系统准备。初始化完成后进入 `ready`。

### 2.2 ready 阶段

`ready` 表示系统等待用户输入长程任务 instruction。

当用户输入任务后：

1. runtime 为该任务创建新的 `AgenticSessionState`。
2. 记录 task、session id、初始 memory。
3. 进入 `reason`。

每个长程任务应有独立 session state。任务完成、失败、达到 step 限制或被中断后，系统返回 `ready` 等待下一条任务。

### 2.3 reason 阶段

`reason` 是 VLM 高层推理阶段。VLM 输入应包含完整上下文：

- long-horizon task
- 当前 phase
- subtask plan
- current subtask
- subtask index
- active execution
- monitor status / monitor error
- pending events
- scene graph
- last tool results
- available tools
- camera images
- planner-visible metadata

VLM 根据这些信息输出一个“决策包”，决定下一步是执行工具、启动动作、观察环境、等待、更新 plan、取消动作、请求用户输入，还是结束任务。

### 2.4 act 阶段

`act` 负责执行 VLM 选择的工具调用。

常见 tool 类型包括：

- `fetch_env` / `observe_scene`: 获取场景结构化信息，并更新 scene graph。
- `execute`: 启动机器人动作。
- `monitor`: 兼容旧式同步 monitor，或作为 `start_monitor` 使用。
- `stop` / `cancel_execution` / `emergency_stop`: 安全停止或取消动作。
- 其它机器人自定义 MCP tools。

如果调用的是 `execute`：

1. loop 调用 MCP `execute` 或 downstream executor。
2. 如果启动成功，创建 `active_execution`。
3. 自动调用 monitor 工具作为 `start_monitor`。
4. 不等待动作完成，直接进入 `response`。

`execute` 的语义是“启动动作”，不是“动作完成”。

### 2.5 response 阶段

`response` 负责把上一阶段的结果写入 memory 和日志。

典型更新包括：

- tool results
- scene graph
- subtask plan
- current subtask
- monitor status
- active execution
- pending events
- parse error
- executor/tool error
- run logger step event

`response` 后通常回到 `reason`。如果没有立即 reason 的必要，则等待下一次 tick 或 event。

### 2.6 scene graph 与 metadata

VLM 输入不是单纯的 memory，而是一个 planner context。planner context 由多类来源拼接：

```text
planner context =
  task/session memory
  + runtime state
  + scene graph
  + perception observations
  + tool registry
  + planner-visible metadata
  + last results/events
```

其中 scene graph 是任务期间持续维护的结构化世界模型。它描述机器人当前理解的场景，而不是泛泛的环境变量。

典型 scene graph 内容包括：

```json
{
  "objects": {
    "cup_1": {
      "class": "cup",
      "pose": [0.3, 0.1, 0.2],
      "state": "upright"
    },
    "table_1": {
      "class": "table"
    }
  },
  "relations": [
    ["cup_1", "on", "table_1"]
  ],
  "robot": {
    "gripper": "empty"
  }
}
```

scene graph 和图片不同：

- 图片是原始视觉观测。
- scene graph 是工具、感知模块或 VLM 抽取出的结构化状态。

当前代码为了兼容历史命名，内部字段仍可暂时叫 `environment`。但在 prompt、文档和协议语义中，应将它理解为 `scene_graph`。后续可以逐步增加 alias 或迁移 dataclass 字段名。

metadata 不是 scene graph，也不一定是 memory。metadata 更像运行背景或部署上下文，例如机器人型号、workspace、安全模式、camera profile、run id、bridge url 等。

为了避免 VLM 上下文过长，应将 metadata 分层：

```text
metadata
  -> planner_visible_metadata
  -> executor_metadata
  -> logging/debug metadata
```

只有 `planner_visible_metadata` 应进入 VLM prompt。

适合给 VLM 的 metadata：

```json
{
  "robot_type": "dual_franka",
  "workspace": "lab_table",
  "safety_mode": "normal",
  "available_camera_views": ["front", "left_wrist", "right_wrist"]
}
```

不建议给 VLM 的 metadata：

```json
{
  "run_id": "run_20260611_001",
  "session_id": "session_0001",
  "bridge_url": "http://robot.local:8767",
  "api_key": "...",
  "log_dir": "runs/debug",
  "internal_trace_id": "..."
}
```

推荐原则：

- 会影响规划决策的少量稳定信息，可以给 VLM。
- 只用于 executor、logger、调试、追踪的信息，不应进入 VLM。
- 默认不要把完整 metadata 原样塞进 prompt。
- executor 仍可以收到完整 metadata。
- logger 可以记录完整 metadata 以便 debug。

### 2.7 async monitor 旁路

monitor 是主循环旁路上的异步监督模块。

执行动作后，系统进入：

```text
active_execution.status = running
```

monitor 可以通过两种方式产生状态：

1. v1 兼容方式：runtime 周期性调用旧式 `monitor` tool。
2. 未来扩展方式：外部 monitor 模块主动推送事件。

monitor 结果统一转换为事件：

```text
monitor_running
monitor_success
monitor_failed
monitor_timeout
```

这些事件进入 `pending_events`，并设置：

```text
reason_requested = true
```

如果是 `monitor_success` 或 `monitor_failed`，表示当前 active execution 已经到达 terminal 状态，下一轮 reason 应根据结果决定继续下一步、retry、replan 或结束任务。

## 3. 状态与 Memory 设计

### 3.1 phase

建议显式维护以下 phase：

```text
init
ready
reason
act
response
done
error
```

phase 的作用是让 runtime、logger、VLM prompt 都能清楚知道当前系统处于哪种控制阶段。

### 3.2 active_execution

`active_execution` 表示当前正在执行或最近被 monitor 监督的 subtask。

建议结构：

```json
{
  "subtask": "pick up the cup",
  "subtask_index": 1,
  "execution_id": "exec-123",
  "monitor_id": "mon-456",
  "status": "running",
  "error": null,
  "namespace": "demo_robot",
  "started_at": 123456.7,
  "updated_at": 123457.8
}
```

其中：

- `status=running`: 动作仍在执行，禁止重复 execute。
- `status=success`: monitor 判断动作成功。
- `status=failed`: monitor 判断动作失败或超时。

### 3.3 pending_events

`pending_events` 是事件队列。它把异步世界的变化带回 VLM。

建议事件结构：

```json
{
  "event_type": "monitor_success",
  "source": "monitor",
  "message": null,
  "created_at": 123456.7,
  "data": {
    "subtask": "pick up the cup",
    "subtask_index": 1,
    "execution_id": "exec-123",
    "monitor_id": "mon-456",
    "status": "success"
  }
}
```

常见事件类型：

```text
monitor_running
monitor_success
monitor_failed
monitor_timeout
execute_failed
tool_failed
user_interrupt
safety_stop
scene_changed
```

v1 最核心的是 monitor 和 execute 相关事件。

### 3.4 reason_requested

`reason_requested` 表示是否需要尽快进入下一次 VLM reason。

典型设置为 true 的情况：

- 新任务开始。
- tool 返回重要结果。
- monitor success。
- monitor failed。
- monitor timeout。
- execute failed。
- 外部 safety signal。
- 用户中断或补充 instruction。

### 3.5 last_reason_at

`last_reason_at` 记录上一次 VLM reason 的时间，用于周期触发。

runtime 可根据：

```yaml
loop:
  reason_interval_s: 1.0
```

决定是否需要执行周期性 VLM reason。

## 4. 触发机制

系统同时使用两类触发：

### 4.1 tick 触发

tick 是时序驱动。

典型配置：

```yaml
loop:
  reason_interval_s: 1.0
  monitor_poll_interval_s: 0.2
```

含义：

- `reason_interval_s`: VLM 主循环周期推理间隔。
- `monitor_poll_interval_s`: legacy monitor 轮询间隔。

两者应独立配置。monitor 可以高频，VLM reason 可以低频。

tick 可用于：

- 周期性 reason。
- 刷新图像。
- 轮询旧式 monitor。
- 检查 timeout。
- 观察环境变化。

### 4.2 event 触发

event 是异步变化驱动。

典型事件：

- `monitor_success`
- `monitor_failed`
- `monitor_timeout`
- `execute_failed`
- `safety_stop`
- `user_interrupt`

event 到来后应立即设置：

```text
reason_requested = true
```

这意味着 VLM 不必等到下一次周期 tick，就能尽快处理关键状态变化。

### 4.3 两类触发的关系

推荐策略：

- 没有事件时，按 `reason_interval_s` 周期 reason。
- 有关键事件时，立即 reason。
- monitor 运行中，即使 VLM reasoning budget 已用尽，也应继续 monitor，直到 success/failed/timeout。
- terminal monitor event 到来后，如果 VLM budget 已耗尽，可以停止任务并返回 `max_steps` 或对应 stop reason。

## 5. VLM 决策协议

当前兼容协议主要依赖这些字段：

```json
{
  "tool_calls": [],
  "subtasks": [],
  "subtask_index": 0,
  "current_subtask": "pick up the cup",
  "should_execute": true,
  "task_complete": false
}
```

为了让状态机更清晰，建议后续扩展显式 `decision` 字段：

```json
{
  "decision": "execute",
  "tool_calls": [],
  "subtasks": [],
  "subtask_index": 0,
  "current_subtask": "pick up the cup",
  "should_execute": true,
  "task_complete": false,
  "message": ""
}
```

推荐 decision 枚举：

```text
plan
execute
observe
wait
replan
cancel
complete
ask_user
noop
```

v1 可以先兼容旧字段；后续再让 prompt 强制 VLM 输出 `decision`。

## 6. VLM 决策到下一步处理的映射

### 6.1 plan / update_plan

典型输出：

```json
{
  "decision": "plan",
  "subtasks": ["approach the cup", "pick up the cup", "place it on the shelf"],
  "subtask_index": 0,
  "should_execute": false,
  "task_complete": false
}
```

处理方式：

1. 更新 `state.subtasks`。
2. 更新 `state.subtask_index`。
3. 如果给出 `current_subtask`，更新当前 subtask。
4. 不调用 execute。
5. 进入 `response`。
6. 等待下一次 reason 或 tool/event 触发。

注意：

- 如果当前有 running active execution，plan 可以更新，但不应切换正在执行的事实。
- planner 可以提前修订后续 plan，但不能让系统误以为当前动作已经结束。

### 6.2 execute

典型输出有两种。

通过 MCP execute：

```json
{
  "decision": "execute",
  "tool_calls": [
    {
      "name": "demo_robot___execute",
      "arguments": {
        "subtask": "pick up the cup"
      }
    }
  ],
  "current_subtask": "pick up the cup",
  "task_complete": false
}
```

通过 downstream executor：

```json
{
  "decision": "execute",
  "current_subtask": "pick up the cup",
  "should_execute": true,
  "task_complete": false
}
```

处理方式：

1. 检查是否已有 `active_execution.status=running`。
2. 如果已有 running execution，拒绝重复 execute，记录 parse/error。
3. 如果没有 running execution，调用 MCP execute 或 downstream executor。
4. execute 成功后创建 `active_execution`。
5. 自动调用 monitor 作为 `start_monitor`。
6. 写入 `monitor_running` event。
7. 进入 `response`。

硬约束：

```text
active_execution.status == running 时禁止重复 execute
```

除非 VLM 显式调用 cancel/stop，并且系统确认旧动作已结束，否则不能启动新动作。

### 6.3 observe / fetch_env

典型输出：

```json
{
  "decision": "observe",
  "tool_calls": [
    {
      "name": "demo_robot___fetch_env",
      "arguments": {}
    }
  ],
  "should_execute": false,
  "task_complete": false
}
```

处理方式：

1. 调用场景观察工具。
2. 如果 tool result 包含 `scene_graph`、`environment` 或 `env`，合并到当前 scene graph。
3. 可触发新的图像 capture。
4. 写入 last tool results。
5. 进入 `response`。
6. 下一轮 reason 使用更新后的 scene graph。

执行中也允许 observe。observe 不应影响 active execution。

### 6.4 monitor / status

典型输出：

```json
{
  "decision": "observe",
  "tool_calls": [
    {
      "name": "demo_robot___monitor",
      "arguments": {
        "subtask": "pick up the cup",
        "subtask_index": 1
      }
    }
  ],
  "should_execute": false
}
```

处理方式：

1. 兼容旧式同步 monitor：直接调用 monitor tool。
2. 将返回状态转换为事件：
   - `running` -> `monitor_running`
   - `success` -> `monitor_success`
   - `failed` -> `monitor_failed`
3. 更新 `monitor_status` 和 `active_execution.status`。
4. 如果 success/failed，结束当前 running 状态。
5. 设置 `reason_requested=true`。

推荐新语义：

- VLM 不需要频繁主动调用 monitor。
- runtime 或外部 monitor 模块负责异步 poll/push。
- VLM 只消费 monitor events。

### 6.5 wait

典型输出：

```json
{
  "decision": "wait",
  "current_subtask": "pick up the cup",
  "should_execute": false,
  "task_complete": false
}
```

处理方式：

1. 不调用任何 execute。
2. 不清除 active execution。
3. 保持 `active_execution.status=running`。
4. 进入 `response`。
5. 等待下一次 tick 或 monitor event。

wait 适用于：

- 机器人正在执行动作。
- VLM 暂时没有新动作要发。
- 系统只需要等待 monitor 给出结果。

### 6.6 replan / retry

典型触发：

```text
monitor_failed
monitor_timeout
scene_changed
execute_failed
```

典型输出：

```json
{
  "decision": "replan",
  "subtasks": [
    "move obstacle away",
    "retry picking up the cup",
    "place it on the shelf"
  ],
  "subtask_index": 0,
  "current_subtask": "move obstacle away",
  "should_execute": true
}
```

处理方式：

1. 根据失败原因更新 plan。
2. 如果旧 active execution 已经 terminal，可执行新的 subtask。
3. 如果旧 active execution 仍 running，必须先 wait 或 cancel。
4. retry 应是新的 execute，而不是重复启动同一个 running execution。

### 6.7 cancel / stop

典型输出：

```json
{
  "decision": "cancel",
  "tool_calls": [
    {
      "name": "demo_robot___stop",
      "arguments": {
        "execution_id": "exec-123"
      }
    }
  ],
  "should_execute": false
}
```

处理方式：

1. 调用 stop/cancel/emergency_stop tool。
2. 更新 active execution 状态。
3. 写入 cancellation event。
4. 进入 `response`。
5. 下一轮 reason 决定 retry、replan 或 abort。

取消类工具应作为 safety path 处理，不应被普通 execute 规则拦截。

### 6.8 complete

典型输出：

```json
{
  "decision": "complete",
  "task_complete": true
}
```

处理方式：

1. 检查是否存在 `active_execution.status=running`。
2. 如果没有 running execution，接受任务完成。
3. 进入 `done`，然后 runtime 返回 `ready`。
4. 如果仍有 running execution，拒绝完成，记录 error，并请求下一轮 reason。

约束：

```text
running execution 存在时，不接受 task_complete=true
```

这样可以避免 VLM 在机器人动作尚未完成时提前宣布任务结束。

### 6.9 ask_user

典型输出：

```json
{
  "decision": "ask_user",
  "message": "Which shelf should I place the cup on?",
  "should_execute": false,
  "task_complete": false
}
```

处理方式：

1. 暂停当前 reason-act loop。
2. 将问题展示给用户。
3. 进入 `ready` 或 `await_user`。
4. 用户补充信息后，写入 memory，再进入 reason。

当前代码尚未显式实现 `ask_user` 字段。v1 可以把它作为未来协议扩展。

### 6.10 noop / irrelevant

典型输出：

```json
{
  "decision": "noop",
  "message": "This instruction is unrelated to robot control.",
  "should_execute": false,
  "task_complete": false
}
```

处理方式：

1. 不调用工具。
2. 不更新 active execution。
3. 记录 message。
4. 回到 `ready` 或保持当前 session 等待新输入。

当前 parser 对完全无 plan、无 tool、无 task_complete 的输出会视为 parse error。后续如果需要支持闲聊或无关话语，应显式加入 `decision=noop`。

## 7. 安全与一致性规则

### 7.1 禁止重复 execute

核心规则：

```text
如果 active_execution.status == running，则禁止新的 execute。
```

允许的操作：

- observe
- fetch_env
- wait
- update plan
- cancel/stop
- emergency_stop

禁止的操作：

- 对同一 running subtask 再次 execute
- 在旧 execution 未结束时启动另一个普通 execute
- 用 `task_complete=true` 跳过 monitor terminal 状态

### 7.2 task_complete 必须等待动作结束

如果仍有 running execution：

```text
task_complete=true -> reject / error / reason_requested
```

只有没有 running execution，或最后 active execution 已经 terminal，才允许完成任务。

### 7.3 monitor terminal event 触发 re-reason

以下事件必须触发 reason：

```text
monitor_success
monitor_failed
monitor_timeout
execute_failed
```

其中：

- success: 进入下一个 subtask 或完成任务。
- failed: retry、replan、cancel 或 abort。
- timeout: 视为 failed 的一种，需要 VLM 决策恢复策略。

### 7.4 tick 和 event 不应互相替代

tick 解决“没有事件时也能持续观察”。

event 解决“关键变化要立即响应”。

两者必须同时存在。

## 8. Config 设计

建议 loop 配置包含：

```yaml
loop:
  max_steps: 20
  reason_interval_s: 1.0
  monitor_poll_interval_s: 0.2
  max_monitor_polls: 300
  include_metadata_in_prompt: false
  tool_roles:
    fetch_env: observe_scene
    monitor: check_status
    execute: run_subtask
```

字段含义：

- `max_steps`: VLM reasoning step 预算，不包含 monitor-only poll。
- `reason_interval_s`: VLM 主循环周期推理间隔。
- `monitor_poll_interval_s`: legacy monitor 轮询间隔。
- `max_monitor_polls`: 单个 execution 最大 monitor poll 次数。
- `include_metadata_in_prompt`: 是否把完整 metadata 放进 VLM prompt。推荐默认 false。
- `tool_roles`: 非标准工具名映射。

建议：

- `monitor_poll_interval_s` 通常应小于或等于 `reason_interval_s`。
- 真机动作较慢时，VLM reason 不宜太频繁。
- monitor 可以较频繁，以便及时捕捉 success/failed。
- VLM prompt 只应包含 planner-visible metadata，避免上下文污染。

## 9. 与当前代码的对应关系

当前代码中的主要模块对应：

- `OnlineAgentRuntime`: 外层 ready loop、tick/event 调度、monitor poll。
- `AgenticRobotLoop.step`: 一次 VLM reason + tool act + response。
- `AgenticRobotLoop.poll_monitor`: legacy monitor poll，产生 monitor event。
- `AgenticSessionState`: memory。
- `ActiveExecution`: 当前执行中的 subtask。
- `AgenticEvent`: monitor/tool/runtime 事件。
- `RunLogger`: 记录 phase、events、active execution、planner input/output。

当前 v1 仍兼容旧协议：

- 没有强制 VLM 输出 `decision`。
- 仍支持 `tool_calls`、`current_subtask`、`should_execute`、`task_complete`。
- `monitor` 工具名仍可复用为 `start_monitor`/`get_monitor_status`。
- 旧式同步 monitor 通过 runtime poll 兼容。

## 10. 推荐后续演进

### 10.1 引入显式 decision 字段

建议下一步让 prompt 要求 VLM 输出：

```json
{
  "decision": "execute",
  "tool_calls": [],
  "subtasks": [],
  "subtask_index": 0,
  "current_subtask": "...",
  "should_execute": false,
  "task_complete": false,
  "message": ""
}
```

这样 loop 不再需要靠字段组合推断 VLM 意图。

### 10.2 区分 start_monitor 和 get_monitor_status

当前为兼容旧 server，`monitor` 同时承担 start 和 poll。

未来可拆成：

```text
start_monitor
get_monitor_status
stop_monitor
```

这样 monitor 生命周期更清楚。

### 10.3 支持真正异步事件输入

v1 可以用 runtime poll。

未来可以支持：

- MCP server push event
- websocket/SSE
- robot bridge callback
- filesystem/event queue
- ROS topic

外部事件统一写入 `pending_events`，状态机不需要关心事件来源。

### 10.4 增加 await_user 阶段

如果需要支持用户澄清，应增加：

```text
await_user
```

用于区别普通 `ready` 和任务中途等待用户补充信息。

## 11. 总结

这个方案的目标不是简单增加一个 monitor poll，而是重构 AgentLoop 的控制语义：

- VLM 持续 reason。
- execute 非阻塞启动动作。
- monitor 异步监督动作。
- event 把异步结果带回 memory。
- tick 保证周期性更新。
- 硬约束防止重复 execute。
- task completion 受 active execution 状态约束。

这样整个系统更接近真实机器人运行方式：动作执行、监控、视觉观察和高层规划可以并行推进，而不是互相阻塞。
