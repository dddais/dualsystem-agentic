# Manual 模式真机启动记录

本文记录从臂运行 simple loop / Robot Runtime、服务器运行 GRM Monitor / SAM3，
通过 SSH 双向端口转发联通的启动过程。VLA 的开始、停止和归位由操作员在已有
robot-bridge 控制界面完成，再在 Runtime 人工页面确认。

希望在同一页面直接点击控制 VLA，可使用新增的
`manual_bridge` [模式](robot_bridge_adapters.md#中间版本manual_bridge同页点击控制)：
将下文 Runtime 启动配置换为 `robot_runtime/robot_runtime/configs/manual_bridge.runtime.yaml`，
配置 Scheduler 控制地址并开启 `--control-port 8088`；其余 Loop、相机与 Monitor 步骤相同。
该模式下按钮直接发送命令并自动切换阶段。本文其余“已开始／已停止／已归位”说明
对应原 `manual` 模式。

网页还支持预设模板和完整 instruction 输入；manual_bridge 要将完整文本同时发送
给 VLA/VLM，需要更新 Scheduler 的文本接口，见 [自定义指令说明](manual_bridge_instructions.md)。

`/manual` 现在同时提供目标输入、三视角画面和 Monitor 得分。首次更新这项功能时，
需要同步从臂的本仓库，以及服务器 `Robo-Dopamine-delivery` 的 Monitor 修改，
然后重启 Runtime、Monitor 和 loop。SAM3 和 robot-bridge 沿用现有服务；SSH 转发
与端口无需新增。页面若仍显示旧版，刷新浏览器。

## 1. 机器、环境与通信接口


| 机器      | 运行内容                                             | 本文使用的仓库路径                                            |
| ------- | ------------------------------------------------ | ---------------------------------------------------- |
| 从臂      | 已有 Robot Server、Scheduler；新增 Runtime、simple loop | `/home/xr/dais/dualsystem-agentic`                   |
| GPU 服务器 | GRM Monitor、SAM3；沿用已有 VLA Policy Server          | `/mnt/public1/dais/workspace/Robo-Dopamine-delivery` |


主从臂和原有 VLA 部署的通信已经接通，沿用现场启动方式。以下仅新增 manual
框架的进程和转发，不需要重新启动一套 Robot Server 或机器人 SDK controller。
Runtime 读取 **从臂 Robot Server 的 WebSocket 9946**，不是 SDK 的 50051。


| 调用方向                            | 调用方使用的地址                 | 对应配置                                     |
| ------------------------------- | ------------------------ | ---------------------------------------- |
| 从臂 loop / MCP → 本机 Runtime      | `http://127.0.0.1:8767`  | `DUAL_FRANKA_RUNTIME_URL`                |
| 从臂 Runtime → 本机 Robot Server 相机 | `ws://127.0.0.1:9946`    | Runtime 的 `camera.robot_url`             |
| 从臂 Runtime → 服务器 Monitor        | `http://127.0.0.1:18877` | Runtime 的 `monitor.url`；经 SSH `-L`       |
| 服务器 Monitor → 从臂 Runtime 图像     | `http://127.0.0.1:18767` | Monitor 的 `robot_runtime_url`；经 SSH `-R` |
| 服务器 Monitor → 本机 SAM3           | `http://127.0.0.1:8878`  | steering 的 `grounding.url`               |


两条 HTTP 通路都需要：Runtime 启动和查询 Monitor，会话中的 Monitor 又要从
Runtime 拉取三路图像。只转发 Monitor 端口不能完成这一轮交互。

### 从臂环境：首次安装

使用 Python 3.12，Runtime 和 loop 共用一个独立环境。这里的简单 loop 不加载
VLM/VLA 权重，不需要在此环境安装 PyTorch 或 CUDA；VLA 仍在原部署环境推理。

```bash
cd /home/xr/dais/dualsystem-agentic
python3.12 -m venv venvs/dualsystem-manual
source venvs/dualsystem-manual/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[mcp]' 'mcp==1.28.1'
python -m pip install -e './robot_runtime[bridge]'
```

已有这个 venv 时直接激活即可，无需重建。MCP 这里固定为锁文件中的 `1.28.1`；
项目依赖范围为 `mcp>=1.28.1,<2`，原因及现有环境修复见第 6 节。

相机 provider 还需要现场 robot-bridge 的消息编解码包。如果该独立环境尚未安装，
安装已有源码（下面假设在同级目录；若现场路径不同，替换这一处路径）：

```bash
python -m pip install -e /home/xr/dais/robot-bridge
python -c 'import robot_runtime, robot_bridge.transport.codec, cv2, websockets; print("runtime/camera imports OK")'
python -m pip check
```

这一步安装 Python 包，不会启动机器人服务。Runtime 的 `[bridge]` 额外依赖提供
WebSocket、NumPy、OpenCV、msgpack；沿用 robot-bridge 自己的 codec，无需修改其源码。

服务器沿用已有 `robo-dopamine` 和 `rewardbench-sam3` conda 环境，分别启动 Monitor
和 SAM3。SAM3 服务使用标准库 HTTP server，不要求额外安装 FastAPI。

## 2. 配置检查



### 从臂：Runtime

修改 [manual.runtime.yaml](../robot_runtime/robot_runtime/configs/manual.runtime.yaml)，
关键字段如下，保留文件中其余相机与安全参数：

```yaml
robot:
  type: x1pro
  driver: manual
  operator_timeout_s: 300.0

camera:
  provider: robot_bridge
  robot_url: ws://127.0.0.1:9946

monitor:
  provider: remote_http
  url: http://127.0.0.1:18877
  timeout: 30.0
```

**本次 SSH 部署使用** `http://127.0.0.1:18877`**；若从臂仍沿用旧模板的** `8877`**，需要
改为** `18877`**。** `camera.robot_url` 指向实际 Robot Server；若它不在 Runtime
同一主机/网络空间，用已经打通的对应地址替换。Runtime 不通过 Scheduler 的 UI
控制端口取图。

### 从臂：loop

使用 [config.simple_loop.manual.yaml](../examples/config.simple_loop.manual.yaml)，
其中关键配置已有：

```yaml
# mcp.servers[0].env 下
DUAL_FRANKA_RUNTIME_URL: ${DUAL_FRANKA_RUNTIME_URL}
DUAL_FRANKA_ENABLE_RESET: "true"
DUAL_FRANKA_UNKNOWN_STATUS: failed
DUAL_FRANKA_TIMEOUT_S: 660.0
```

```yaml
simple_loop:
  input_source: web
  instruction_template: "pick the {target} and put it into the box"
  default_target: null
  first_result_timeout_s: 120.0
  result_timeout_s: 120.0
  max_execution_s: 300.0
  require_steering: false
```

输入 `carrot` 会生成 `pick the carrot and put it into the box`。按现场 VLA 固定指令
调整模板，操作员在原 UI 选择对应指令。Loop 同时传 `target_queries: [carrot]` 给
Monitor/SAM3，因此这条路径不依赖 steering 配置里的 `task_queries` 文本匹配。

Manual 默认允许 Monitor 的 baseline 降级，避免首帧 SAM3 候选歧义就中止动作。
`require_steering: false` 只控制 loop 是否因缺少有效干预而立即中止，不会关闭服务器
的 steering。每个降级评分会打印模式与原因（例如 `reason=ambiguous`）；有效干预
恢复后打印 resumed。成功/失败仍以 Monitor 状态为准，评分停滞、执行超时与接口
错误仍会触发停止和归位。严格要求每帧都施加干预的实验设回 true。

### 服务器：Monitor / SAM3

在 `/mnt/public1/dais/workspace/Robo-Dopamine-delivery` 核对：


| 文件                                                                   | 字段                             | 本次部署值                                                                                   |
| -------------------------------------------------------------------- | ------------------------------ | --------------------------------------------------------------------------------------- |
| `configs/monitor_steering.yaml`                                      | `port`                         | `8877`                                                                                  |
| 同上                                                                   | `backend` / `inference_engine` | `grm` / `hf`                                                                            |
| 同上                                                                   | `robot_runtime_url`            | `http://127.0.0.1:18767`                                                                |
| 同上                                                                   | `steering_config`              | `./steering.yaml`                                                                       |
| 同上                                                                   | `model_path`                   | `/home/dais/workspace/Robo-Dopamine/pretrained_models/Robo-Dopamine-GRM-2.0-8B-Preview` |
| 同上                                                                   | `goal_image`                   | `../examples/blank_goal.png`，有任务完成图时可替换                                                 |
| `configs/monitor_steering.yaml` / `configs/monitor_dual_branch.yaml` | `interval`                     | `0.1` 秒，每轮推理完成后的等待时间                                                                    |
| `configs/steering.yaml`                                              | `enabled`                      | `true`                                                                                  |
| 同上                                                                   | `grounding.url`                | `http://127.0.0.1:8878`                                                                 |
| `configs/sam3.yaml`                                                  | `host` / `port`                | `127.0.0.1` / `8878`                                                                    |
| 同上                                                                   | `model_path`                   | `/home/dais/workspace/model/sam3`                                                       |


服务器当前 Monitor YAML 已使用 `18767`。配置在进程启动时读取；修改 YAML 后要
重启对应服务，旧进程不会自动切换地址。不要再把 `robot_runtime_url` 配成无法直达
的从臂内网 IP。

本次 bbox 后处理与 GRM 批处理优化需要同步服务器的 `Robo-Dopamine-delivery`，
在任务结束后用原命令重启 **SAM3 和 Monitor**。Runtime、loop 和 SSH 配置不用调整。
单、双分支配置均新增 `hf_batch_size: 2`，每个独立模型一次生成 forward/incremental。
需要串行对比时，给 Monitor 启动命令加 `--hf-batch-size 1`，两个分支同时切换。
轮间等待仍是 0.1 秒，这不代表 10 Hz 推理。服务日志中的
`latency=…s` 表示本轮处理时间，`online_pred.jsonl` 的 `timing.prepare_ms / grounding_ms / grm_ms`
可用于拆分耗时；新评分周期仍需加上轮间等待。Loop 保持约每秒查询一次。

## 3. 按终端启动

先按现场已有方式启动 Robot Server、VLA Policy Server 和 Scheduler，保持机器人
暂停、场景处于初始状态。打开原有 VLA 控制 UI。以下长期运行的命令各占一个终端，
也可放在 tmux 中；已运行的服务/隧道无需重复启动。

### 从臂终端 A：SSH 双向转发

```bash
ssh -p 40239 -NT \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18877:127.0.0.1:8877 \
  -R 127.0.0.1:18767:127.0.0.1:8767 \
  dais@123.183.193.192
```

`-L` 在从臂监听 18877，转到服务器 Monitor 的 8877；`-R` 在服务器监听 18767，
转到从臂 Runtime 的 8767。登录成功后该终端没有 shell 提示符是正常的，保持运行。
两端转发仅监听各自的 `127.0.0.1`，不需要开启 `GatewayPorts`。

### 从臂终端 B：Robot Runtime

```bash
cd /home/xr/dais/dualsystem-agentic
source venvs/dualsystem-manual/bin/activate
export NO_PROXY=127.0.0.1,localhost
export no_proxy=$NO_PROXY
robot-runtime \
  --config robot_runtime/robot_runtime/configs/manual.runtime.yaml \
  --host 0.0.0.0 --port 8767
#manual bridge
robot-runtime --config robot_runtime/robot_runtime/configs/manual_bridge.runtime.yaml \
  --host 0.0.0.0 --port 8767
```



### 服务器终端 C：SAM3

```bash
conda activate rewardbench-sam3
cd /mnt/public1/dais/workspace/Robo-Dopamine-delivery
CUDA_VISIBLE_DEVICES=3 python -m sam3_runtime.service \
  --config configs/sam3.yaml
```



### 服务器终端 D：GRM Monitor

```bash
conda activate robo-dopamine
cd /mnt/public1/dais/workspace/Robo-Dopamine-delivery
export NO_PROXY=127.0.0.1,localhost
export no_proxy=$NO_PROXY
##attention steering
CUDA_VISIBLE_DEVICES=0 python -m monitor_runtime.service \
  --config configs/monitor_steering.yaml
##双分支
CUDA_VISIBLE_DEVICES=0,1 python -m monitor_runtime.service \
  --config configs/monitor_dual_branch.yaml
## 原版本
CUDA_VISIBLE_DEVICES=0 python -m monitor_runtime.service \
  --config configs/monitor.yaml
```

GPU 0 和 3 是示例，按服务器空闲 GPU 修改。YAML 的 `device: cuda:0` 对应各进程
可见的第一张 GPU。等待两个模型加载完再继续；Monitor 在模型就绪后才监听 HTTP。

### 检查两条转发与相机

在从臂另一个终端运行：

```bash
curl --noproxy '*' -fsS http://127.0.0.1:8767/health
curl --noproxy '*' -fsS http://127.0.0.1:18877/health
```

在服务器另一个终端运行：

```bash
curl --noproxy '*' -fsS http://127.0.0.1:8878/health
curl --noproxy '*' -fsS http://127.0.0.1:18767/health
curl --noproxy '*' -fsS http://127.0.0.1:18767/observations/latest/metadata
```

SAM3 应返回 `status: ready`；Monitor health 的 `data` 应有 `provider: grm`、
`engine: hf`、`steering_enabled: true`。Metadata 应包含 `cam_high`、
`cam_left_wrist`、`cam_right_wrist` 对应的 `binary_endpoints`，可将其中 JPEG
相对路径接在 `http://127.0.0.1:18767` 后查看实际画面。

Runtime health 不证明取图成功，Monitor health 也不主动探测 Runtime 或 SAM3。
因此需要单独检查 metadata，并在第一轮确认产生新的推理结果。

### 从臂终端 E：simple loop

```bash
cd /home/xr/dais/dualsystem-agentic
source venvs/dualsystem-manual/bin/activate
export DUAL_FRANKA_RUNTIME_URL=http://127.0.0.1:8767
python examples/run_simple_robot.py \
  --config examples/config.simple_loop.manual.yaml --input-source web
```

MCP server 由 loop 自动作为 stdio 子进程启动，无需另开 MCP 终端。该 YAML 的
`command: python` 通过 PATH 找解释器，所以每个启动终端都要激活正确 venv，并从
仓库根目录运行。

Manual YAML 已默认 `simple_loop.input_source: web`；这里显式写出 CLI 参数，也适用于
保留了旧 YAML 的现场。Loop 终端仍打印执行与评分日志，但 ready 阶段的输入转到
网页。若想恢复终端输入，改用 `--input-source terminal`，其他功能仍可在页面查看。
Web 输入地址优先使用该 MCP namespace 配置中的 `DUAL_FRANKA_RUNTIME_URL`，也可
用 `--runtime-url http://127.0.0.1:8767` 显式指定。

## 4. 每轮人工操作

打开 Runtime 人工页面，同时保留原有 VLA 控制 UI：

- 浏览器在从臂本机：`http://127.0.0.1:8767/manual`。
- 浏览器可直达从臂局域网：`http://192.168.31.174:8767/manual`，按实际 IP 替换。
- **浏览器在服务器上**：`http://127.0.0.1:18767/manual`，经反向隧道访问。

另一台电脑的浏览器中，`127.0.0.1` 指该电脑本身；需要使用该电脑已有的端口转发
或可直达的从臂地址。

1. 摆好场景、保持 VLA 暂停。页面显示 `ready` 后，在“目标物体”输入 `carrot`，
  核对预览的完整 instruction，再点击“提交目标”。
2. Runtime 启动 Monitor 会话并等初始参考帧就绪；人工页随后显示完整 instruction。
3. 在原 VLA UI 选择对应指令并开始执行，完成后在人工页点击“已开始”。Runtime
  此时激活 Monitor 评分，loop 开始轮询进度，页面显示最新得分与图像。
4. Monitor 成功/失败，或 loop 遇到执行异常/超时后，人工页提示停止。先在原 UI
  停止 VLA 和当前动作，再点击“已停止”。
5. 在原系统让机械臂归位，完成后点击“已归位”。Loop 返回 `[ready]`，才进入下一轮。

人工页的按钮只确认已经完成的操作，本身不会向 robot-bridge 发动作命令。manual
模式的停止和归位都需要上述人工交接。Monitor 会话由 Runtime 管理，不需要手动
调用 `/monitors/start`。目标输入留空可复用上一目标；网页模式在 ready 时于 loop
终端按 `Ctrl+C` 退出。终端输入模式仍可在 `[ready]` 输入 `q` 退出。
结束实验时，完成停止和归位后退出 loop，再关闭此次新增的 Runtime、Monitor、
SAM3 和 SSH 隧道终端。

一次人工操作默认等待最多 300 秒，MCP HTTP 超时 660 秒用于覆盖准备、人工等待
及异常清理。首个评分等待 120 秒、评分停滞 120 秒、执行 300 秒等预算从 execute
返回后计算。若现场操作或推理更慢，同步检查 Runtime 与 loop 的相关超时配置。

### 画面、bbox 和得分的对应关系

- **实时画面**：约每秒从 Runtime 获取一组相机快照；三路 JPEG 来自同一次采集请求。
同时显示最近一次 Monitor 得分。这里不叠加较早评分帧的 bbox。
- **GRM 评分画面**：显示该次推理的 `after_cam_high / after_cam_left_wrist / after_cam_right_wrist` 三张原图，与 bbox、评分轮次和得分一起更新。若 Monitor
配置了腕部去畸变，这里展示的也是去畸变后、送入 GRM 预处理器的原图。
- 勾选 **SAM3 bbox** 叠加该轮实际选中的检测框，模式下拉框选择 forward、incremental
或 backward 对应的检测结果。默认配置仅检测 `after_cam_high`，因此腕部没有框；
页面会区分“未检测此视角”“没有目标”“候选歧义”和检测降级，不补造 bbox。
- Monitor 区域显示融合进度、各模式原始 score/累计进度、推理轮次、结果年龄和进度
趋势。启用双分支时，额外显示 Baseline 融合进度、分支差值和阈值。

GRM 并非吃“画了框的图片”：它接收原图，bbox 用于 attention 干预。页面仅在
canvas 上绘框，不改模型输入、不额外调用 SAM3 或 GRM。GRM 的完整输入还含参考图、
目标图及 before 三视角；这里展示用户关注的当前三视角。评分刷新速度取决于实际
推理耗时，不等于相机刷新速度。

停止、归位后保留最后一轮评分画面。Monitor 进程保留最近 128 组已提交评分的图像
访问索引；被淘汰或服务重启后旧 URL 失效，页面明确显示不可用。原图仍留在服务器
会话输出目录。评分图经 Runtime 转发，浏览器只需要访问 `/manual` 所在地址。

网页不是任务队列：只有 loop 的当前 ready 请求能接受一个目标，同轮重复提交同一
目标幂等，过期请求和不同目标被拒绝。浏览器刷新后仍能看到当前请求；loop 断开后
ready 请求最多保留 15 秒，只有 loop 的轮询能续期。执行、停止或归位阶段无法提交
下一目标。页面刷新仅读取 Runtime 已缓存的 Monitor 结果，不额外推进评分状态。

## 5. 常见通信问题


| 现象                                              | 检查方式                                                                                                                                                           |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 从臂 `18877/health` 失败                            | 先在服务器确认 `8877/health`；再检查 SSH `-L` 和服务器模型加载日志                                                                                                                  |
| 服务器 `18767/health` 失败                           | 先在从臂确认 `8767/health`；再检查 SSH `-R`                                                                                                                              |
| health 正常但 metadata 返回 503                      | 查看 Runtime 日志；确认 Robot Server 的 9946 可达，三路图像正常，codec 可导入                                                                                                       |
| Monitor 仍请求旧内网 IP                               | 修改配置后重启 Monitor，并确认启动时使用的 `--config` 路径                                                                                                                        |
| SSH 提示端口已占用/转发失败                                | 检查是否已有同一条隧道；使用现有隧道或停止自己的旧隧道后重连                                                                                                                                 |
| 本地请求出现代理错误                                      | 确认启动服务的终端设置 `NO_PROXY` / `no_proxy`；诊断 curl 使用 `--noproxy '*'`                                                                                                 |
| 网页目标输入一直不可用                                     | 确认 loop 使用 `--input-source web`，处于 ready，且已完成上一轮人工归位；不要同时运行两个 loop                                                                                             |
| 有得分但“GRM 评分画面”没有图                               | 更新并重启服务器 Monitor；旧服务没有 `/monitors/frames/...` 接口。再开始一轮任务                                                                                                       |
| 首次得分后报 `missing or degraded attention steering` | 这是 loop 严格检查；查看 warning/error 中模式、`reason`、`applied` 和 `degraded`。允许 Monitor 的 baseline 降级时，将 **loop YAML** 的 `simple_loop.require_steering` 设为 false 后重启 loop |


未启用 tracking 的逐帧 SAM3 检测模式在前两个候选的置信度差不超过 0.05 时标记 `ambiguous`，并不选定 bbox。
Monitor 配置 `on_missing_bbox: baseline` 时，该帧继续无 attention 干预的评分；
下一帧仍重新检测。单纯设置 `require_steering: false` 不会解决检测歧义，也不能将
该帧当作“成功施加了 steering”的实验样本。UI 会显示歧义且不绘制选中框，详细
候选及置信度保存在服务器会话的 `online_pred.jsonl`。

启用 tracking 时，首次绑定改为选择通过检测阈值的最高分候选，分数接近也照常返回 bbox；同分时按检测器返回顺序取第一个。之后仍锁定该实例，失跟后持续空框，不会再从剩余候选中选择目标。

## 6. 修复 `Server` 没有 `list_tools` 的启动错误

报错发生在 MCP 子进程注册 tool 时：

```text
@app.list_tools()
AttributeError: 'Server' object has no attribute 'list_tools'
```

当前 server 使用 MCP Python SDK 1.x 的低层 `list_tools()` / `call_tool()`
装饰器。在干净环境安装 `mcp==2.2.0` 可复现同一错误，`mcp==1.28.1` 支持这些
API。此前 `pyproject.toml` 的 `mcp` 依赖没有上限，普通 pip 安装不会读取
`uv.lock`，可能装到不兼容的 2.x；现在已限定为 `mcp>=1.28.1,<2`，并增加启动时
的兼容错误提示。后面的 AnyIO `ExceptionGroup` 是子进程退出导致连接关闭的后续
异常，修复 SDK 后再重启 loop。

先在**从臂运行 loop 的同一终端**查看实际安装信息：

```bash
cd /home/xr/dais/dualsystem-agentic
source venvs/dualsystem-manual/bin/activate
python - <<'PY'
import sys
import mcp
from importlib.metadata import version
from mcp.server.lowlevel import Server
print('python:', sys.executable)
print('mcp:', version('mcp'), mcp.__file__)
print('list_tools:', callable(getattr(Server, 'list_tools', None)))
print('call_tool:', callable(getattr(Server, 'call_tool', None)))
PY
```

同步本次仓库修改后，重新安装兼容版本。即使尚未同步代码，下面的显式版本也能
修复已安装的 2.x，无需重建整个环境：

```bash
python -m pip install -e '.[mcp]' 'mcp==1.28.1'
python -m pip check
```

重跑上面的诊断，确认两个方法均为 `True`，解释器来自
`/home/xr/dais/dualsystem-agentic/venvs/dualsystem-manual/bin/python`。若版本正确却
仍然报错，检查打印的 `mcp.__file__` 是否来自该 venv，以及 YAML 的 `command`
是否使用了另一个解释器；可将它设为上述 Python 绝对路径。

可以先只检查 MCP 初始化和 tool 发现，不调用机器人工具：

```bash
python - <<'PY'
from dualsystem_agentic.config import build_mcp_client, load_config
config = load_config('examples/config.simple_loop.manual.yaml')
client = build_mcp_client(config.mcp)
try:
    names = {tool['name'] for tool in client.list_tools()}
    print('MCP tools:', sorted(names))
    assert {'execute', 'monitor', 'stop_task', 'reset_task'} <= names
finally:
    client.close()
PY
```

检查通过后，按第 3 节重启 loop。仓库回归测试
`tests/test_dual_franka.py::test_manual_loop_initializes_real_mcp_subprocess`
也会启动真实 SDK 子进程，完成初始化、tool 发现，再在 ready 输入 `q` 退出；不需要
机器人、Runtime、Monitor 或 GPU。

该测试显式使用 `--input-source terminal`。网页整轮联调脚本为
`tests/validate_manual_dashboard.py`，用模拟相机/GRM 和真实 Runtime、Monitor HTTP、
MCP、loop 验证提交目标、bbox 显示、人工开始/停止/归位，以及返回 ready。测试需要
Playwright/Chromium，生产运行页面无需安装浏览器测试依赖。

## 可选：SAM3 加速与连续跟踪

完整参数、耗时测量和回退方法见服务器仓库
[sam3_tracking.md](../../Robo-Dopamine-delivery/docs/sam3_tracking.md)。无需修改 robot-bridge 或 SSH 转发。

仅加速逐轮 SAM3 检测，可将服务器 SAM3 启动配置改为 `configs/sam3_fast.yaml`；它使用 BF16，bbox/分数可能有小幅数值差异。`configs/sam3.yaml` 保留 FP32。

启用“先检测，再持续跟踪”时，结束当前任务后重启相应服务。SAM3 终端：

```bash
cd /home/dais/workspace/Robo-Dopamine-delivery
conda activate rewardbench-sam3
# 3 是物理 GPU 示例；先用 nvidia-smi 选择实际可用、负载较低的 GPU。
CUDA_VISIBLE_DEVICES=3 python -m sam3_runtime.service --config configs/sam3_tracker.yaml
```

Monitor 终端（双分支）：

```bash
cd /home/dais/workspace/Robo-Dopamine-delivery
conda activate robo-dopamine

CUDA_VISIBLE_DEVICES=0,1 python -m monitor_runtime.service \
  --config configs/monitor_dual_branch.yaml \
  --tracking-config configs/tracking.yaml

#用一张卡
CUDA_VISIBLE_DEVICES=3 python -m monitor_runtime.service \
  --config configs/monitor_dual_branch.yaml \
  --tracking-config configs/tracking.yaml \
  --device cuda:0 \
  --baseline-device cuda:0
```

单分支改用 `configs/monitor_steering.yaml`。从臂 Runtime 和 Loop 的命令不变；若希望获得更连续的图像，将 `robot_runtime/robot_runtime/configs/manual.runtime.yaml` 的 `camera.cache_s` 从 `0.5` 改成 `0.1` 后重启 Runtime。实际刷新率还受三视角编码、SSH 带宽、取图和跟踪耗时限制。

后台 tracker 独立采图，只保留最新完成的一组“图像＋bbox”，GRM 不排队处理中间帧。
“GRM 评分画面”仍与该轮分数严格对应；持续评分时不会因为堆积旧帧而越来越滞后，但延时会波动。网络/推理停顿或任务结束后，旧画面的年龄仍会增加。
每个任务只在首帧检测并绑定一次实例：从通过检测阈值的候选中取最高分，分数接近也照常绑定，同分时按检测器返回顺序取第一个。后续不再重检测。首帧没有有效候选、失跟、框跳变或长时间断帧后，本任务持续返回空 bbox；只有结束当前任务并开始新任务才能重新绑定。默认缺失框策略下 GRM 继续评分，但不施加目标 attention，不会自动框选桌上剩下的同类物体。
升级实例锁定修复后需要重启服务器上的 **SAM3 和 Monitor** 并开始新任务；原有从臂配置和 SSH 转发无需调整。具体状态和保守跟踪阈值见上述 `sam3_tracking.md`。

回退时去掉 Monitor 的 `--tracking-config`，SAM3 改用 `sam3.yaml` 或 `sam3_fast.yaml`。

## Tracking 参数调整参考

抓取笔等细长物体时，优先提高**新图像进入 tracker 的实际频率**，再调整失跟阈值。帧间隔越长，目标被拿起时的位置变化越大，越容易触发当前的保守失跟判定。

以下数值按 2026-09-11 工作区配置记录，以启动命令实际指定的 YAML 为准。Runtime 的配置在从臂 `dualsystem-agentic` 仓库，SAM3 和 Monitor 的配置在服务器 `Robo-Dopamine-delivery` 仓库。

### 1. 采图频率、传输量与图像细节

Runtime 配置见 [manual.runtime.yaml](../robot_runtime/robot_runtime/configs/manual.runtime.yaml) 或 [manual_bridge.runtime.yaml](../robot_runtime/robot_runtime/configs/manual_bridge.runtime.yaml)；后台跟踪线程配置见服务器的 [tracking.yaml](../../Robo-Dopamine-delivery/configs/tracking.yaml)。


| 参数                                 | 当前值                              | 作用与调整建议                                                                            |
| ---------------------------------- | -------------------------------- | ---------------------------------------------------------------------------------- |
| Runtime `camera.cache_s`           | manual：`0.5`；manual_bridge：`0.1` | 从收到快照开始计算缓存有效期，期间复用同一快照；Monitor 跳过重复图像。建议先用 `0.1`。`0.5` 通常使新快照频率不超过约 2 Hz，实际还可能更低。 |
| Monitor tracking `poll_interval_s` | `0.1`                            | 后台“取图＋跟踪”循环的最小周期，耗时不足时才补等待。若实际每轮耗时 300 ms，改成 `0.01` 也无法达到 100 Hz。先保持 `0.1`。        |
| Runtime `camera.quality`           | `90`                             | robot-bridge 返回图像的 JPEG 质量。降低可减少传输量，但细小物体可能丢失细节。先保持，确认带宽是瓶颈后可试 `85`。               |
| Runtime `camera.size`              | 未设置，保留原尺寸                        | 可设置 `[height, width]`，减少图像尺寸和传输、编解码开销；也会改变 tracker 和 GRM 的输入细节。细笔场景不建议优先缩小。        |


`cache_s` 不是相机硬件帧率，也不会启动一个按此周期运行的相机采集线程。实际更新频率同时受缓存、后台最小周期、三视角取图与传输、编解码和 tracker 推理耗时限制。降到 `0.1` 后继续调小可能收益很小，却增加相机请求和 CPU、网络负载；不建议直接设为 `0`。

### 2. 实例跟踪与失跟判定

配置见服务器的 [sam3_tracker.yaml](../../Robo-Dopamine-delivery/configs/sam3_tracker.yaml)。顶层参数用于首次文本检测，`tracking` 下的参数用于连续实例跟踪。


| 参数                       | 当前值        | 作用与调整建议                                                                                                   |
| ------------------------ | ---------- | --------------------------------------------------------------------------------------------------------- |
| `tracking.min_score`     | `0.5`      | 跟踪目标存在分数的最低要求。提高会更早拒绝不可靠结果；降低可能减少漏跟，也更容易接受错误结果。先保持。存在分数不是物理身份的可信概率，即使为 `1.0` 也不能证明没有漂移。                   |
| `tracking.match_iou`     | `0.1`      | 本帧框与上一有效框的最小重叠比例，范围 `(0, 1]`。提高更严格，但快速运动、遮挡导致框缩小时更容易失跟；降低放宽位移容忍度，也削弱防跳框保护。建议先保持，优先提高新帧频率。                 |
| `tracking.max_gap_s`     | `2.0`      | 两次服务端 tracker 更新的间隔超过此值就终止定位。增大可容忍卡顿，但不能补回中间帧，较大的位移也更难判断身份。按“宁愿没有目标”的策略，先保持。                              |
| `tracking.memory_frames` | `32`       | 保存最近历史的上限，另外保留初始提示帧；不得低于模型 memory/pointer 窗口要求，当前模型至少为 `16`。增大占用更多显存，不意味着模型自动利用全部历史，也不能恢复已终止的定位。先保持 `32`。 |
| 顶层 `threshold`           | `0.3`      | 首次文本检测的候选筛选阈值。提高可能漏掉初始目标；它无法改善绑定后的跟踪。                                                                     |
| `tracking.dtype`         | `bfloat16` | 控制 tracker 的计算精度。A100 上建议保持；FP32 通常更慢，跟踪准确率收益尚未验证。顶层 `dtype` 单独控制首次检测模型。                                  |


当前策略只在首次跟踪图像中检测并绑定一次，并选择通过检测阈值的最高分候选，不再因多个候选分数接近而拒绝初始化。首帧无有效候选，以及后续空 mask、低置信度、框跳变、断帧或跟踪异常，都会让本任务持续返回空框。调整参数不会自动找回原任务的目标，必须重新开始任务。旧配置中的 `redetect_interval_s` 已不生效，不需要通过调大它来关闭重检测。

防跳框检查能拦截明显的位置跳变，但连续重叠区域内的渐进漂移仍可能来自模型本身，不能仅凭这些阈值保证物理身份准确。

### 3. GPU 分配要同时检查检测模型与 tracker

`sam3_tracker.yaml` 中两个设备参数相互独立：

```yaml
device: cuda:3           # 首次文本检测模型
tracking:
  device: cuda:0         # 后续连续 tracker
```

这是本节写入时工作区的配置，不是所有部署都适用的推荐值。**只改顶层** `device` **不会把 tracker 搬到同一张卡。** 如果 SAM3 进程可见所有 GPU，上述配置会把 tracker 放到物理 GPU 0；若 GRM 也使用它，资源争用会拉长跟踪周期。应优先让 tracker 使用负载较低的 GPU。

YAML 中的编号是当前进程可见设备的编号，必须与 `CUDA_VISIBLE_DEVICES` 一起判断。例如使用上节的命令：

```bash
CUDA_VISIBLE_DEVICES=3 python -m sam3_runtime.service --config configs/sam3_tracker.yaml
```

此时进程只看到一张卡，应将 YAML 的顶层 `device` 和 `tracking.device` **都设为** `cuda:0`，它们均指物理 GPU 3。此时 `cuda:3` 是无效的进程内编号。运行前用 `nvidia-smi` 检查实际负载。

### 4. 容易混淆的时效与轮询参数


| 参数                                             | 当前值或位置                    | 实际作用                                                                                    |
| ---------------------------------------------- | ------------------------- | --------------------------------------------------------------------------------------- |
| Monitor tracking `max_frame_age_s`             | `2.0`                     | GRM 读取时允许的最大本地快照年龄，从该轮开始请求快照计算，包含取图和跟踪耗时；超龄拒绝用于新评分。调大只会接受更旧的画面，不会提高跟踪能力。                |
| Runtime `camera.snapshot_ttl_s`                | `30.0`                    | 保留固定快照供后续 HTTP 下载的时间，不是刷新间隔。                                                            |
| Runtime `camera.max_obs_lag_s`                 | `3.0`                     | robot-bridge 提供 `obs_lag_ms` 时，用于拒绝过旧观测；没有该字段时无法靠这个参数验证源图像时效。                           |
| SAM3 `tracking.session_ttl_s` / `max_sessions` | `60.0` / `8`              | 回收闲置会话、限制会话数量。旧任务会话过期后返回空框，不重新初始化。                                                      |
| Monitor `interval`                             | Monitor 主配置               | 一轮 GRM 推理完成后的等待时间，影响评分频率，不直接控制后台 tracker。                                               |
| Loop 的 Monitor 轮询周期                            | Loop 配置                   | 影响何时读取已发布的评分，不直接控制后台 tracker。                                                           |
| `on_missing_bbox`                              | steering 配置，默认 `baseline` | 决定缺失 bbox 时 GRM 如何处理：`baseline` 继续评分但不施加目标 attention；`error` 报告缺失错误。两者都不会触发 tracker 重选。 |




### 5. 建议的调整顺序与验证方法

1. 确认实际 Runtime 配置使用 `camera.cache_s: 0.1`，后台 `poll_interval_s: 0.1`，并核对 tracker 的 GPU 分配。
2. 先保留 `quality: 90`、原图尺寸、`min_score: 0.5`、`match_iou: 0.1`、`max_gap_s: 2.0` 和 `memory_frames: 32`。在相似场景下逐项调整，避免同时改动多个阈值后无法判断原因。
3. 查看每轮结果 `modes.<mode>.steering.grounding.after_cam_high` 中的 `tracking_state`、`loss_reason`、`tracker_frame_index` 和 `timing`。`discontinuous_bbox` 重点检查帧率和运动幅度；`update_gap` 检查取图、网络及 GPU 卡顿；`empty_mask` / `low_score` 检查遮挡和图像细节；`session_missing` 检查服务重启或会话过期。
4. 结合 `observation.tracking.cycle_ms` 和 `input_age_at_read_s` 看后台周期与图像年龄。`cycle_ms` 不包含循环末尾的补等待，也不能单独代表真实新帧频率；即使已失跟，后台仍会发布新的空框快照，所以 `frames_processed` 增长不等于成功跟踪帧数增长。
5. 结束当前任务后应用配置：Runtime 的 `camera.*` 改动需要重启从臂 Runtime；`tracking.yaml`、steering 配置或 Monitor 主配置改动需要重启 Monitor；`sam3_tracker.yaml` 和 SAM3 的 `CUDA_VISIBLE_DEVICES` 改动需要重启 SAM3。然后开始新任务，让 tracker 从新的首帧绑定目标。

## GRM 只运行 forward 模式

同步包含 `no_incremental` 开关的服务器代码后，可以通过 Monitor 主配置或命令行关闭 incremental。单分支与双分支均支持；SAM3 tracker 继续独立工作。

在服务器 `configs/monitor_dual_branch.yaml`（单分支为 `configs/monitor_steering.yaml`）中设置：

```yaml
no_backward: true
no_incremental: true
```

也可以保留 YAML，直接用 CLI 覆盖。以下使用物理 GPU 2、3，默认 YAML 的 `device: cuda:0` 和 `baseline_device: cuda:1` 分别映射到这两张卡：

```bash
CUDA_VISIBLE_DEVICES=2,3 python -m monitor_runtime.service \
  --config configs/monitor_dual_branch.yaml \
  --tracking-config configs/tracking.yaml \
  --no-backward \
  --no-incremental
```

若两个 GRM 分支共用物理 GPU 2，将环境变量改为 `CUDA_VISIBLE_DEVICES=2`，并在上述命令末尾增加 `--device cuda:0 --baseline-device cuda:0`。双分支仍加载两个独立模型实例，需要足够的单卡显存。

只运行 forward 后：

- 单分支每轮生成一个 forward 评分；双分支每轮各生成一个 forward 评分。
- `hf_batch_size: 2` 可以保留，它是每次生成的样本数上限；只有一个样本时，实际 batch 为 1，不会额外生成 incremental。
- 返回的 `progress` 直接采用 forward 进度，不再与 incremental 进度平均；双分支差值规则比较两边的 forward 进度。现有成功/失败阈值仍生效，评分模式切换后应结合任务结果判断阈值是否合适。
- 推理计算量和临时显存需求通常减少，两份 GRM 模型权重仍保留；实际延时需要测量，不能按样本数直接推算为原来的一半。

恢复 forward＋incremental 时，设置 `no_incremental: false`，或在启动命令中使用 `--incremental` 覆盖 YAML；继续保留 `no_backward: true`。结束当前任务后重启 Monitor 并开始新任务即可应用模式切换。
