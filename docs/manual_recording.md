# manual_bridge：视频与 GRM 进度录制

在 `/manual` 点击 Record 或按 `R`，同时记录机器人视频与 GRM 进度。再次点击停止，
等待页面显示“记录已保存”，即可下载进度 ZIP。刷新或关闭浏览器不会中断 Runtime
后台的进度采集；Runtime 服务需要持续运行。

一次录制可以跨多轮 instruction / 启动 / 停止 / 归位。进度记录按
`execution_id`、`monitor_id` 和 `inference_step` 区分，保留每轮 instruction 与
`target_queries`。没有启动 GRM 的录制会生成有效的空表。

## 保存内容与位置

Runtime 默认保存到**启动进程工作目录**下的 `runs/recordings/rec-<uuid>/`：

| 文件 | 内容 |
|---|---|
| `manifest.json` | 视频录制 ID、机器人端 `episode_dir` / `archive`、起止时间、任务来源、条数和完整性信息 |
| `progress.jsonl` | 每次 GRM 推理一行；包含时间、instruction、进度、状态，以及 `grm` 字段中的完整原始结果 |
| `progress.csv` | 便于离线绘图的展开表，包括各模式 score / progress、baseline progress、分支差异和延迟 |
| `progress.zip` | 停止后生成，包含上述三个文件；页面可下载 |

现有视频录制仍由机器人端 robot-bridge 保存为 episode / ROS bag，停止后打包为
`.tar`。**进度 ZIP 不包含原始视频**，通过 `manifest.video.episode_dir` 和
`manifest.video.archive` 找到对应的视频。GRM 源图像和完整监控日志仍保存在 Monitor
机器的 `results/monitor_sessions/...`；进度 ZIP 保留图像路径、观测元数据和评分，
不复制这些源图像。

## 时间与完整性

- 录制区间使用 Scheduler 接受开始 / 停止请求的 Unix 时间。只导出 GRM
  `inference_updated_at` 在该闭区间内的评分，视频打包期间的新评分不会混入。
- `progress_time_unix_s` 是评分发布时间，`elapsed_s` 是它相对录制开始的秒数。
  `observation_time_unix_s` / `observation_elapsed_s` 单独保留 GRM 请求输入图像的时间。
  推理具有延迟，因此两种时间不相同；在录制开始前取图、开始后完成的评分可能有负的
  `observation_elapsed_s`。
- Scheduler、Monitor 和机器人运行于不同机器，需要同步系统时钟。请求时间只能给出
  近似视频边界，`video.start_confirmed_at` / `stop_confirmed_at` 是 RPC 返回时间，
  后者包含归档耗时。精确对齐视频帧时应使用 ROS bag 中的实际时间戳；本功能没有宣称帧级同步。
- Runtime 逐页读取 Monitor 已持久化日志，不靠网页刷新或 loop 的低频状态快照采样。
  临时断连后继续从游标补读；停止时读取到覆盖停止时间的 Monitor 水位后才完成。
- 页面和清单会明确标记 `incomplete`（超时、来源丢失、视频启动失败或停止未确认）和
  `interrupted`（Runtime 在录制完成前关闭）。这些导出仍可下载，但不能视为完整数据。
- 老 Monitor 没有日志 API 时降级为缓存状态快照，清单标记 `capture_quality: snapshot_only`
  并显示警告，可能漏掉中间评分。老 Scheduler 没有录制元数据时使用 Runtime 观察到的
  起止时间，无法提供视频路径，也无法保证捕获两次轮询间极短的录制。
- 更新后的 Scheduler 保留最近 32 次完成 / 失败录制的元数据，用于短暂断连和连续录制补齐。
  两边服务重启后不自动续接旧的录制或监控会话；磁盘文件保留。网页下载入口只索引当前
  Runtime 进程创建的导出，旧导出可从磁盘获取。正常操作建议先停止录制，待保存完成再重启。

## 配置

`manual_bridge` 默认启用；原 `manual` 不含 Scheduler Record，不启用此功能。
`robot_runtime/robot_runtime/configs/manual_bridge.runtime.yaml`：

```yaml
recording:
  enabled: true
  output_dir: runs/recordings
  poll_interval_s: 0.5
  finalize_timeout_s: 30.0
```

`output_dir` 可以设置绝对路径。`finalize_timeout_s` 从收到停止请求起计算，覆盖视频
停止 / 打包和剩余评分补读；若设备归档很慢或有大量积压，可调大。进度写入失败会在页面
显示错误，后台重试；录制控制仍经原 Scheduler 执行，不依赖进度服务成功。

## 离线绘图

将下载 ZIP 解压，或直接使用 Runtime 的录制目录：

```bash
pip install matplotlib
python examples/plot_recording_progress.py \
  --recording-dir runs/recordings/rec-你的录制ID
```

生成 `progress.png`：上图为融合进度和可用的 baseline，下图为各模式原始评分；不同任务
独立画线，不跨任务连接。图中的 Task 编号对应 CSV 首次出现的 Monitor 顺序，完整
instruction 可查 CSV 或清单。可选 `--time-basis observation` 使用输入图像请求时间，
或 `--output /path/progress.svg` 导出矢量图。

## 部署更新

| 运行服务的机器 | 更新代码与重启服务 |
|---|---|
| Manual Runtime | 更新 `dualsystem-agentic`，重启 Runtime，刷新 `/manual` |
| Scheduler | 更新 `robot-bridge`，重启 Scheduler，提供录制 ID / 时间 / 视频路径与历史 |
| GRM Monitor | 更新 `Robo-Dopamine-delivery`，重启 Monitor，提供只读日志分页接口 |

如果几个服务在同一台机器，则更新对应仓库并分别重启。当前改动不要求更新机器人控制器、
Policy Server 或 VLA 模型代码。

Monitor 新接口为
`GET /monitors/{monitor_id}/records?execution_id=...&cursor=0&limit=100`。
游标是字节偏移，必须落在完整 JSONL 行边界；停止 Monitor 后仍可读取本次进程注册过的日志。
它不会触发推理或推进 Monitor 状态。
