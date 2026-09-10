# dualsystem-robot-runtime

Robot-side HTTP runtime service for dualsystem-agentic deployments.

Install on the robot machine:

```bash
pip install -e ./robot_runtime
robot-runtime --port 8767
```

If this directory is moved into its own repository, install from that repository
root instead:

```bash
pip install -e .
robot-runtime --host 0.0.0.0 --port 8767
```

The runtime owns execution ids, monitor ids, observation endpoints, and robot
control endpoints. The agent communicates with it through the Dual-Franka MCP
adapter over HTTP.

The agent process does not import this package. It only needs the runtime URL,
for example `DUAL_FRANKA_RUNTIME_URL=http://ROBOT_MACHINE_IP:8767`, and matching
HTTP API contracts for `/executions`, `/monitors/status`, `/control/*`, and
`/observations/latest`.
# 当前接口约定补充

简化键盘 loop、模板目标词和最新接口修复见 [使用说明](../docs/simple_loop.md) 与 [系统复查报告](../docs/system_contract_review.md)。`/executions` 接受独立的 `target_queries` 和可选客户端 `execution_id`；先等待 GRM 参考帧，再启动 driver。一个执行终态后应调用 `/control/stop` 再开始下一执行。

支持 `robot.driver: manual`（人工操作页确认）、`robot.driver: manual_bridge`
（同页点击发送开始／停止／归位命令，自动切换阶段）和 `robot.driver: robot_bridge`
（固定 prompt、自动停止和延时归位），以及直接读取 Robot Server 三路 JPEG 的
`camera.provider: robot_bridge`。启动命令、依赖和配置见
[三个 adapter 的使用说明](../docs/robot_bridge_adapters.md)。

同页点击控制版本继续使用 `/manual` 和现有 manual loop 配置：

```bash
robot-runtime --config robot_runtime/configs/manual_bridge.runtime.yaml --port 8767
```
