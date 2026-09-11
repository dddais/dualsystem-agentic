# manual_bridge：视频、GRM 评分与三视角原图录制

在 `/manual` 保存指令、填写采集人后，点击 Record 或按 `R`，同时记录机器人视频、
GRM 进度和每次评分对应的三视角原图。再次点击停止，等待页面显示“记录已保存”，
即可下载评分与图片 ZIP。采集人无需另点“应用”；Record 会带上输入框当前内容。
刷新或关闭浏览器不会中断 Runtime
后台的进度采集；Runtime 服务需要持续运行。

一次录制可以跨多轮 instruction / 启动 / 停止 / 归位。进度记录按
`execution_id`、`monitor_id` 和 `inference_step` 区分，保留每轮 instruction 与
`target_queries`。没有启动 GRM 的录制会生成有效的空表。

## 保存内容与位置

Runtime 默认保存到**启动进程工作目录**下的 `runs/recordings/<录制名称>/`：

| 文件 | 内容 |
|---|---|
| `manifest.json` | 完整命名指令、采集人、模型、视频录制 ID、机器人端 `episode_dir` / `archive`、起止时间、任务来源、评分/图片数量及完整性信息 |
| `progress.jsonl` | 每次 GRM 推理一行；包含时间、instruction、进度、状态、可移植的图片相对路径与 SHA-256，以及 `grm` 字段中的完整原始结果 |
| `progress.csv` | 融合进度，steering 与 baseline 各自的 forward / incremental / backward score 和 progress，分支差异、延迟、三视角图片相对路径；未启用模式留空 |
| `frames/task_001/step_000001/*.png` | 该次评分使用的 `cam_high.png`、`cam_left_wrist.png`、`cam_right_wrist.png`，按任务和推理步区分 |
| `<录制名称>.zip` | 停止后生成，包含上述元数据、评分文件和图片，可直接下载后离线使用 |

`grm` 保留 Monitor 原始记录，包括所有启用模式、双分支结果、观测元数据和 grounding
框。每行 `images` 将相机名映射到 ZIP 内的相对路径；`image_sha256` 可用于校验。
各模式和双分支共享该时间步的三视角当前观测，因此每步保存三张原图，不重复复制；
这些是实际评分输入的当前帧，不是下载时的实时画面，也不烧入框和分数。
各模式用作比较的初始/目标参考图不在这个三视角导出范围内。

机器人端沿用 robot-bridge 的 ROS bag 录制，停止后打包为 `.tar`。当前 X1 Pro 的包
含三路相机、双臂位姿/关节、夹爪、头部、升降、里程计、控制命令及动作来源 topic；
它不是直接生成的 MP4。**进度 ZIP 不包含这个 ROS bag**，通过 `manifest.video.episode_dir` 和
`manifest.video.archive` 找到对应的视频。Monitor 原始日志和图片仍保留在 Monitor
机器；Runtime 将评分对应的三张 PNG 实际复制进 ZIP，无需离线访问 Monitor 路径。

## 可读命名

Manual UI 沿用 Scheduler 的 `采集人@模型@时间` 形式，加入指令与防重名短编号：

```text
张三@my-run-1000@把胡萝卜放进盒子@2026_09_11_15_30_00@a1b2c3d4.zip
```

更新后的 Scheduler 向机器人传递相同 episode 名称，因此机器人端 `.tar` 与 Runtime
目录/ZIP 使用相同名称主体。原 Scheduler UI 不传 instruction 时保留原命名行为。

- 录制开始时固定名称。正在执行时使用本轮实际 instruction；尚未开始时使用 UI
  已保存的 instruction。未保存的指令草稿不用于命名，没有已保存指令则写“未设置指令”。
- 录制期间修改已保存指令或采集人，不会重命名当前文件；跨多轮任务时，各轮完整指令
  单独保存在 JSONL / CSV 与 `manifest.sources` 中。
- 空格、换行、路径分隔符等转换为 `-`。中文保留；长文本按 UTF-8 字节数截短并附哈希，
  防止超过文件系统长度限制。完整 instruction 仍保存在清单和评分记录中。

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
  临时断连后继续从游标补读；这里的“每个时间步”指录制时间窗内每次已发布的 GRM 推理，
  并非给每个 VLA 动作步或视频帧额外生成评分。
- 图片从按推理步索引的持久化接口读取，预览缓存淘汰后仍可补读。图片下载失败不阻塞
  后续评分入库；停止后，评分读到覆盖停止时间的 Monitor 水位且图片下载完成才标记完整。
  图片验证 PNG 格式，并与 Monitor 记录的输入 SHA-256 比对（若源记录提供该字段）。
- 超时缺图时，`manifest.missing_images` 列出具体 monitor、step、camera 和原因；每行
  `missing_images` 列出缺失相机，`images` 仅引用已保存文件。导出标记 `incomplete`，
  已收集的评分和图片仍可下载，页面显示错误。`planned_images` 保留原计划路径用于追查。
- 页面和清单会明确标记 `incomplete`（超时、来源丢失、视频启动失败或停止未确认）和
  `interrupted`（Runtime 在录制完成前关闭）。这些导出仍可下载，但不能视为完整数据。
- 老 Monitor 没有日志 API 时降级为缓存状态快照，清单标记 `capture_quality: snapshot_only`
  并显示警告，可能漏掉中间评分。没有持久化图片接口时尝试旧预览接口；已淘汰图片会明确
  记为缺失。老 Scheduler 没有录制元数据时使用 Runtime 观察到的
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
停止 / 打包、剩余评分补读和三视角图片下载；若设备归档很慢或有大量积压，可调大。进度写入失败会在页面
显示错误，后台重试；录制控制仍经原 Scheduler 执行，不依赖进度服务成功。

## 离线绘图

将下载 ZIP 解压，或直接使用 Runtime 的录制目录：

```bash
pip install matplotlib
python examples/plot_recording_progress.py \
  --recording-dir 'runs/recordings/你的录制名称'
```

生成 `progress.png`：上图为融合进度和可用的 baseline，下图为各模式原始评分；不同任务
独立画线，不跨任务连接。图中的 Task 编号对应 CSV 首次出现的 Monitor 顺序，完整
instruction 可查 CSV 或清单。可选 `--time-basis observation` 使用输入图像请求时间，
或 `--output /path/progress.svg` 导出矢量图。

## 部署更新

| 运行服务的机器 | 更新代码与重启服务 |
|---|---|
| Manual Runtime | 更新 `dualsystem-agentic`，重启 Runtime，刷新 `/manual` |
| Scheduler | 更新 `robot-bridge`，重启 Scheduler，按采集人、模型、指令命名视频并返回命名元数据 |
| GRM Monitor | 更新 `Robo-Dopamine-delivery`，重启 Monitor，提供只读日志分页与持久化评分图片接口 |

如果几个服务在同一台机器，则更新对应仓库并分别重启。当前改动不要求更新机器人控制器、
Robot Server、Policy Server、VLA 模型或 SAM3 代码。代码测试使用模拟环境，不包含真机部署或服务重启。

Runtime 增加了 Pillow 用于验证下载的 PNG。更新代码后，在 Runtime 环境中重新执行
`python -m pip install -e './robot_runtime[bridge]'`（从 `dualsystem-agentic` 根目录执行）。

Monitor 新接口为
`GET /monitors/{monitor_id}/records?execution_id=...&cursor=0&limit=100`。
游标是字节偏移，必须落在完整 JSONL 行边界；停止 Monitor 后仍可读取本次进程注册过的日志。
它不会触发推理或推进 Monitor 状态。

图片接口为
`GET /monitors/{monitor_id}/records/{inference_step}/{camera}.png?execution_id=...`。
它按原始日志解析图片，校验会话归属，只读取该会话目录内已记录的 PNG；磁盘读取不占用
推理结果发布锁。Monitor 停止任务后仍可读取，进程重启后不自动重新注册旧会话。
