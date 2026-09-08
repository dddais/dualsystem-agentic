# 简化 loop / Robot Runtime / GRM Monitor 接口修复与复查

日期：2026-09-08。对应当前工作区代码；运行入口和配置见 [simple_loop.md](simple_loop.md)。本次修改覆盖 `dualsystem-agentic` 与相邻的 `Robo-Dopamine-delivery`。

## 目标词现在如何传递

1. ready 输入 `purple mug`，保存为当前进程的上一目标；回车复用它。
2. `simple_loop.instruction_template` 中的 `{target}` 替换为该字符串，生成完整 `subtask`。
3. MCP execute 同时携带 `target_queries: ["purple mug"]` 和客户端预先生成的 execution ID。
4. Robot Runtime 的 `ExecutionRequest.target_queries` 保留该列表，RemoteHTTPMonitorProvider 将其放进 `/monitors/start` 请求。
5. GRM Monitor 的显式目标词优先于配置映射和英文解析，将它写进各模式 sample。
6. GroundingClient 把它作为 SAM3 `/grounding/detect` 的 `queries`，同时发送当前冻结 PNG、哈希和请求 ID。

完整 instruction 和定位名词分别传递；模板可以是中文，目标短语不自动翻译。Runtime、GRM 和 SAM3 对目标词列表统一要求 1–8 个非空字符串。

## 修复内容

| 原问题 | 当前行为 |
|---|---|
| Monitor 停止异常使机器人根本没收到 stop | driver.stop 优先执行；Monitor 清理异常单独返回 `monitor_cleanup_error`，不阻断成功的物理停止和后续恢复 |
| 同步 HTTP 调用阻塞 FastAPI 事件循环 | Robot Runtime 的同步路由交给工作线程，监控等待期间相机和控制请求可继续处理 |
| 动作开始后才获取 Monitor 起始帧 | start Monitor 时暂缓评分，GRM 报告参考帧就绪后启动 driver，再 activate 评分；参考帧等待有超时 |
| 目标词没有贯通 | 模板只生成 instruction；独立 `target_queries` 经每层转发至 SAM3 |
| 相机冻结/重复帧使 loop 永远 running | 检查首个评分等待、已提交轮数停滞、`result_age_s` 和整轮时限 |
| Driver 返回 false 仍报告执行启动成功 | 校验 `executed/stopped/reset` 明确确认及错误字段；启动失败执行清理 |
| steering 降级仍被上游当作正常结果接受 | 默认严格检查每个模式的 `applied=true/degraded=false`，缺失或降级进入恢复 |
| Runtime 每次 status 刷新时间，掩盖旧结果 | 保留远端实际推理时间和评分轮数，MCP 转发完整 `result/poll_count/error` |

在二次复查中还补充了停止与启动竞态处理：客户端提前分配 execution ID；Runtime 记录提前取消，拒绝冲突 ID/重叠执行；启动等待和 driver 启动前后检查取消；已取消会话的在途 status 不覆盖本地终态。旧 ID 停止请求不会中止新执行。独立 Runtime 定时器使用 `safety.max_execution_s`，不依赖客户端持续轮询。

最终联调复跑还发现快模型会在动作开始前产生终态。因此增加了 `defer_inference` 和 `/monitors/activate` 握手，明确分开参考帧采集与评分；新增测试确认 activate 前评分调用为零，且取消后不能重新激活。单独启动 Monitor 的旧调用默认仍自动评分。

## 验证

| 验证 | 结果 |
|---|---|
| dualsystem-agentic 全仓 pytest，既有 robo-dopamine 环境 | 151 passed、13 skipped、7 subtests passed；跳过项为该环境缺少 MCP SDK 的相关测试 |
| 既有 dualsystem-agentic 环境运行 simple loop unittest | 17 项全部通过，包含真实 MCP adapter 分发 |
| Robo-Dopamine-delivery steering unittest | 28 项全部通过，涵盖 bbox/token 对齐、mask hook、清理、在线事务和暂缓/激活评分握手 |
| 新增 Runtime 故障/并发用例 | 覆盖 Monitor 故障下的 driver.stop、参考帧等待与超时、启动中取消、提前取消、执行失败、旧 ID、独立定时器、HTTP 不阻塞、远端字段校验 |
| 跨仓库实际 HTTP + MCP stdio | 两轮完整通过：输入 `purple mug`，第二轮回车复用；SAM3 收到两次 `["purple mug"]`，4 个模式 sample 使用相同目标词；动作顺序为 execute/stop/reset 各两次 |

跨仓库脚本：[validate_simple_stack.py](../tests/validate_simple_stack.py)。它使用真实的简化 loop、MCP stdio、Robot Runtime HTTP API、RemoteHTTPMonitorProvider、GRM Monitor 生命周期、GroundingClient 与 SAM3 HTTP server。相机画面为持续更新的合成 JPEG，神经模型与物理 driver 使用替身，且脚本检查 driver.execute 之前参考帧已就绪。

本机复现命令（仓库根目录）：

```bash
/mnt/public1/dais/miniconda3/envs/robo-dopamine/bin/python tests/validate_simple_stack.py \
  --agent-python /mnt/public1/dais/miniconda3/envs/dualsystem-agentic/bin/python
```

## 当前边界

- 当前配置的 Robot Driver 仍是 placeholder，尚未加入真实机械臂控制。真实 driver 需要尽快确认动作启动，并明确确认 stop/reset 完成；返回 JSON 成功不能替代真实硬件验证。
- 相机仍由外部进程写入三张本地 JPEG；建议原子替换文件，并使用一致的时间基准。代码中的文件修改时间不是硬件同步触发证明。
- 本次没有重新加载真实 GRM/SAM3 模型或验证任务成功率。attention steering 的真实模型证据仍见 delivery 中已有验证报告；此次联调证明的是修复后的接口和生命周期能贯通。
- 一个 Runtime 同时只允许一个尚未停止的执行。调用方应在终态后 stop，再启动下一任务；简单 loop 已自动执行这一流程。传统 VLM loop 的调用方也需遵守此约定。
- 超时不强制杀死正在执行的远端模型；Monitor 的活动检查阻止取消后的结果发布。若 Monitor 服务断联，机器人停止优先完成，远端残留会话清理由部署方恢复连通后处理。

在上述测试和静态复查覆盖内，未再发现阻断当前简化 loop 调用闭环的问题。真实硬件恢复语义、模型效果和部署网络时延仍需对应环境实测。
