# manual_bridge：模板与完整 instruction

`/manual` 的“目标任务”区支持两种输入方式。两者最终生成一个完整 instruction，
以相同文本交给 VLA 和 VLM（GRM Monitor）：

| 输入方式 | 网页操作 | 最终 instruction 示例 |
|---|---|---|
| 模板填目标 | 选择“放到黄色盘子”，输入 `red cup` | `pick the red cup and put it on yellow plate` |
| 完整指令 | 直接输入整段任务 | `Move the red cup beside the box, then release it.` |

页面预览最终文本；点击“提交任务”后本轮输入锁定。GRM 参考帧就绪后仍需点击
VLA 控制中的“自主运行 / 开始”（A）。空闲（I）结束本轮，随后选择 Homing（H）
或遥操作调整（T → I），完成后返回 ready。见 [状态规范](manual_bridge_lifecycle.md)。
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
修改模板后重启 loop，网页会读取新列表。模板模式的目标留空时复用上一次模板目标。
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

这项功能需要更新 **robot-bridge 和 dualsystem-agentic 两个仓库**。
原 Scheduler 的控制接口只能按索引选预置 prompt；新增 `set_prompt_text` 后才能
接收任意 instruction。更新后重启已有 Scheduler，保留原启动参数和控制端口。
使用 `openpi_takeover` 时自动继承这一接口，不需要额外改主臂服务。

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
# Scheduler 所在环境（按现场方式重新启动 Scheduler）
cd /path/to/robot-bridge
python -m pip install -e .

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
`allow_full_instruction: true`。网页使用 `POST /manual/task` 提交：

```json
{"request_id":"<当前 ready ID>","mode":"template","template_id":"放到黄色盘子","target":"red cup"}
```

或：

```json
{"request_id":"<当前 ready ID>","mode":"instruction","instruction":"Move the red cup beside the box.","target_queries":["red cup"]}
```

Runtime 根据受信任的模板生成完整指令，loop 通过原 MCP `execute` 发送同一个
`subtask` 给 driver 与 Monitor。只有 ready 阶段接收提交，重复相同任务幂等；
旧 ID、覆盖已提交任务或在执行／恢复期间提交都会拒绝。旧 loop 的目标输入协议保留。

无真机测试：

```bash
PYTHONPATH=/path/to/robot-bridge python -m pytest -q \
  tests/test_instruction_input.py tests/test_robot_bridge_wire.py

# 需 Playwright/Chromium；真实 HTTP/MCP/Loop/Monitor，模拟机器人和模型。
PYTHONPATH=/path/to/robot-bridge python tests/validate_manual_dashboard.py \
  --driver manual_bridge --input-mode instruction
PYTHONPATH=/path/to/robot-bridge python tests/validate_manual_dashboard.py \
  --driver manual_bridge --template 放到黄色盘子
```

本次验证：dualsystem-agentic 全仓 231 项测试与 9 个子测试通过；robot-bridge
Scheduler 的 42 项测试通过。浏览器验证覆盖两种新输入方式和原 manual 流程，
包括真实 HTTP/MCP/Loop/Monitor 链路；未连接真机或加载真实 VLA/GRM 权重。
