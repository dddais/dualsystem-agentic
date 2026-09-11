# manual_bridge：保存和复用 instruction

`/manual` 的“任务指令”区支持两种输入方式。两者最终生成一个完整 instruction，
以相同文本交给 VLA 和 VLM（GRM Monitor）：

| 输入方式 | 网页操作 | 最终 instruction 示例 |
|---|---|---|
| 模板填目标 | 选择“放到黄色盘子”，输入 `red cup` | `pick the red cup and put it on yellow plate` |
| 完整指令 | 直接输入整段任务 | `Move the red cup beside the box, then release it.` |

页面区分已保存指令和编辑预览，操作方式为：

1. 选择模板并填写目标，或输入完整指令，点击 **保存指令**。保存本身不启动机器人或 Monitor。
2. loop 处于 ready 时点击 **自主运行 / 开始（A）**，使用当前已保存的指令启动本轮。
   Runtime 先准备 GRM 参考帧，就绪后设置指令并启动 VLA，再激活 GRM，无需第二次点击 A。
3. 空闲（I）结束本轮，随后选择 Homing（H）或遥操作调整（T → I），完成后返回 ready。
4. 下一轮直接按 **A**，持续复用已保存指令和 SAM3 检测目标，不需要再次填写或保存。
   每一轮仍创建新的 execution / Monitor 和参考帧，不复用上一轮评分。

执行中、归位或调整期间也可编辑并保存；改动只影响下一次开始，本轮 VLA / VLM 始终使用
点击 A 时固定的同一份文本和检测目标。草稿不会被状态刷新清空；草稿未保存时禁止开始，
可点击“撤销编辑”恢复已保存内容。多个页面同时编辑时，过期的保存和开始请求会被拒绝。

已保存指令存放在 **Runtime 进程内存**：刷新网页、完成循环或重启 loop 后仍可复用；
重启 Runtime 后需要重新保存。草稿只在当前页面保留。原 `manual` 模式仍沿用原来的
逐轮提交与人工确认方式。见 [状态规范](manual_bridge_lifecycle.md)。
文本首尾空白会去掉，内部换行和花括号会保留；完整指令不会再次套用目标模板。

## 预设模板

修改 loop 使用的 `examples/config.simple_loop.manual.yaml`：

```yaml
simple_loop:
  input_source: web
  # 网页的“默认模板”；终端模式也继续使用这一项。
  instruction_template: "pick the {target} and put it into the box"
  instruction_templates:
    放到黄色盘子: "pick the {target} and put it on yellow plate"
    移到盒子旁: "move the {target} next to the box"
```

`instruction_templates` 的键是下拉框显示名称，值是模板。支持最多 30 个额外模板，
每个模板只允许 `{target}` 占位符；`default` 名称保留给默认模板。
修改模板后重启 loop，网页会读取新列表。模板模式需要填写非空目标；保存后会保留该字段。
模板配置改变不会悄悄改写已保存的完整 instruction；需要采用新模板时，检查预览并再次保存。
完整指令模式不会复用旧的 SAM3 目标。

## SAM3 检测目标

instruction 描述整项任务，`target_queries` 描述要检测的物体：

```json
{
  "instruction": "把红色杯子放到盒子旁边，然后松开夹爪。",
  "target_queries": ["red cup"]
}
```

模板模式自动用填写的目标物体作为检测目标。完整指令模式可在“SAM3 检测目标”里
每行填一个物体名（最多 8 个），无需重复整条任务。
该字段留空时不发送 `target_queries`，Monitor 沿用现有配置映射和英文指令提取规则。
中文或复杂任务建议明确填写英文物体名；若 Monitor 无法解析目标，会报错，不会猜用
上一轮物体。它不改写传给 VLA/VLM 的 instruction。

## 更新与启动

本次“保存一次、跨轮复用”只需更新 **dualsystem-agentic**：更新运行 Runtime 和
loop / MCP 的机器上的该仓库，重启这两个进程，最后刷新 `/manual` 页面。

前提是 Scheduler 已支持此前自定义指令功能新增的 `set_prompt_text`。如果此前已经可以
从 manual UI 设置任意 instruction，本次无需更新 Scheduler、robot-bridge 或 GRM。
从尚不支持文本指令的旧版本部署时，才需要同时更新 robot-bridge 并重启 Scheduler；
`openpi_takeover` 自动继承该文本接口。

Runtime 使用 `robot_runtime/robot_runtime/configs/manual_bridge.runtime.yaml`：

```yaml
robot:
  driver: manual_bridge
  scheduler_url: ws://192.168.10.3:8088
  robot_url: ws://127.0.0.1:9946
  prompt_mode: text
```

地址沿用现场设置，`scheduler_url` 指向 Scheduler 的 `--control-port`。
`prompt_mode: text` 表示按原文设置 VLA prompt；旧的 `fixed` 模式仍支持预置索引和
`prompt_map`。从新网页提交的任务会显式携带 `options.prompt_mode: text`，确保
两端使用同一段最终文本，即使原 Runtime 配置中还保留了旧 `prompt_map`。
Scheduler 不支持文本接口时会明确报错，不会回退到其他预置任务。

在各自现有环境里更新安装、重启：

```bash
# Runtime 所在环境
cd /path/to/dualsystem-agentic
python -m pip install -e './robot_runtime[bridge]'
robot-runtime --config robot_runtime/robot_runtime/configs/manual_bridge.runtime.yaml \
  --host 0.0.0.0 --port 8767
```

另一个终端启动 loop，最后刷新 `/manual` 页面：

```bash
cd /path/to/dualsystem-agentic
export DUAL_FRANKA_RUNTIME_URL=http://127.0.0.1:8767
PYTHONPATH=src python examples/run_simple_robot.py \
  --config examples/config.simple_loop.manual.yaml --input-source web
```

VLM Monitor、SAM3 和 Policy Server 的接口无需修改。任意文本能够传到模型，并不
保证 checkpoint 能完成训练分布之外的任务；执行效果仍取决于所用 VLA 模型。

## 接口与验证

新版 loop 在 `POST /manual/input/open` 中发送 `instruction_templates` 和
`allow_full_instruction: true`。`GET /manual/status` 的 `instruction_editor` 返回已保存
`task`、保存版本 `revision`、模板列表和 `templates_revision`。

manual_bridge 网页使用 `POST /manual/instruction` 保存，模板模式示例：

```json
{"revision":"<当前保存版本>","templates_revision":"<当前模板版本>","mode":"template","template_id":"放到黄色盘子","target":"red cup"}
```

完整指令示例：

```json
{"revision":"<当前保存版本>","mode":"instruction","instruction":"Move the red cup beside the box.","target_queries":["red cup"]}
```

保存允许在执行和恢复期间进行。ready 阶段的 A 使用 `POST /manual/bridge/action`：

```json
{"name":"set_mode","args":{"mode":"autonomous","input_request_id":"<当前 ready ID>","instruction_revision":"<当前保存版本>"}}
```

Runtime 检查当前 ready 请求和保存版本，固定本轮 instruction 与检测目标，并生成一次启动许可。
loop 把这份任务经 MCP `execute` 交回 Runtime；Runtime 校验文本、检测目标和许可后，等待
参考帧就绪再启动 VLA。保存不产生启动许可，同一轮重复 A 不重复启动；停止锁存、恢复未完成、
过期 ready 请求或版本冲突都不能启动新一轮。已消费或取消的启动许可不能用于另一个 execution。

旧的 `POST /manual/task` 和目标输入协议保留，供原 `manual` 及旧客户端逐轮提交使用；
新的 manual_bridge 网页使用独立的保存和开始接口。

无真机测试：

```bash
PYTHONPATH=/path/to/robot-bridge python -m pytest -q \
  tests/test_saved_instruction.py tests/test_instruction_input.py \
  tests/test_manual_bridge_lifecycle.py tests/test_robot_bridge_wire.py

# 需 Playwright/Chromium；浏览器、HTTP 和 loop 使用真实实现，机器人和模型使用模拟实现。
PYTHONPATH=/path/to/robot-bridge python tests/validate_saved_instruction.py
PYTHONPATH=/path/to/robot-bridge python tests/validate_manual_dashboard.py \
  --driver manual_bridge --input-mode instruction --recovery teleop --auto-stop
PYTHONPATH=/path/to/robot-bridge python tests/validate_manual_dashboard.py \
  --driver manual_bridge --template 放到杯子里
```

验证覆盖连续三轮复用、每轮新 Monitor、执行和恢复期间保存、模板和完整指令、草稿保留与撤销、
刷新恢复、多页面版本冲突、启动许可及 VLA / VLM 文本一致性。Dashboard 验证还覆盖
真实 HTTP/MCP/Loop/Monitor 链路和视频 / GRM 进度导出；不向真机发送动作或加载真实模型权重。
