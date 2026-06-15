# dualsystem-agentic dev_0615 代码审核计划

生成日期：2026-06-15

## 1. 审核背景与基线

本计划面向 `dualsystem-agentic` 仓库 `dev_0615` 分支。当前本地基线为：

- 分支：`dev_0615`
- 工作树：干净

注意：本计划是正式代码审核的执行方案，不是最终审核结论。文中的“初步观察”仅用于确定审核重点，正式结论需要在后续逐项验证后沉淀到审核报告。

## 2. 审核目标

本次审核围绕最终开源交付质量展开，重点回答以下问题：

1. 仓库结构是否简洁清晰，核心框架、机器人适配、示例、文档是否边界明确。
2. 扩展性是否足够好：新增机器人、新增 MCP tool、新增 VLM provider、新增 dataloader 或 monitor provider 是否能快速配置或少量实现。
3. 是否存在多余代码、文件、文档、历史兼容入口、生成产物或调试残留。
4. 是否符合开源交付规范：license、README、安装方式、包路径、配置示例、安全与贡献文档、测试与 CI、路径可移植性等。
5. 是否存在会影响真实机器人部署的接口契约、异步状态机、安全边界、错误处理和日志问题。

## 3. 审核范围

重点范围：

- `src/dualsystem_agentic/`：agent core、配置工厂、CLI、runtime、MCP/VLM/executor/dataloader 抽象。
- `mcp_server/`：mock、x2robot、dual_franka MCP adapter。
- `robot_runtime/`：Dual-Franka runtime API、driver/camera/monitor provider。
- `examples/`：可运行示例配置与部署脚本。
- `docs/`、`README.md`、`README.zh-CN.md`：架构说明、部署文档和用户入口。
- `tests/`：覆盖范围、回归测试质量与真实部署风险。
- `pyproject.toml`、`.gitignore`：打包、依赖、入口、发布边界。

非重点但需抽样确认：

- 可视化脚本 `examples/visualize_run_video.py` 的依赖、定位和是否应作为核心发布内容。
- legacy bridge/debug 文件是否仍需保留，若保留是否应明确标记稳定性等级。

## 4. 当前结构初步画像

当前仓库大致分为三层：

- Agent SDK/core：`src/dualsystem_agentic`
- MCP adapter：`mcp_server/*_mcp_server`
- Robot runtime：`robot_runtime`

这个分层方向合理，已经具备以下扩展基础：

- MCP tool 通过 `list_tools()` 自动进入 registry 和 prompt。
- 多机器人通过 `namespace___tool_name` 避免工具重名。
- VLM、executor、dataloader、interaction、logging 都有 config-driven factory。
- Robot runtime 内部使用 `RobotDriver`、`CameraProvider`、`MonitorProvider` protocol。

正式审核需要重点确认这些抽象是否真正形成稳定边界，而不是仅在当前 Dual-Franka/x2robot 示例上可用。

## 5. 审核方法

### 5.1 静态结构审查

检查目录层级、包边界、命名一致性、是否存在 root-level package 与 `src/` package 混用导致的发布混乱。

重点文件：

- `pyproject.toml`
- `src/dualsystem_agentic/app.py`
- `src/dualsystem_agentic/config.py`
- `robot_runtime/api/app.py`
- `mcp_server/dual_franka_mcp_server/server.py`

输出：

- 结构问题清单。
- 推荐目标目录结构。
- 是否需要把 `robot_runtime`、`mcp_server` 合并进 `src/` 或保持独立包的判断。

### 5.2 扩展性审查

以两个场景做走查：

- 新增机器人 `new_robot`。
- 新增工具 `open_gripper` 或 `capture_depth`。

验证问题：

- 新机器人是否只需增加 `mcp_server/<robot>_mcp_server`、`robot_runtime/adapters/<robot>`、配置文件即可。
- `robot_runtime/api/app.py` 当前是否因硬编码 `dual_franka` 而限制扩展。
- `tool_roles` 是否足以处理非标准 `monitor/execute/fetch_env` 命名。
- 非标准工具是否能通过 MCP self-description 自动进入 prompt，无需修改 core。
- tool result contract 是否有清晰文档，如 `status`、`agentic_role`、`executed`、`scene_graph`。

输出：

- “新增机器人操作手册”差距清单。
- “新增 tool 操作手册”差距清单。
- 需要配置化或插件化的硬编码点。

### 5.3 核心状态机与行为审查

重点审查 `AgenticRobotLoop` 与 `OnlineAgentRuntime`：

- `reason -> act -> response` 的 phase 是否和文档一致。
- active execution 期间是否可靠阻止重复 execute。
- monitor poll、monitor timeout、terminal event 是否不会丢事件或重复触发。
- `max_steps` 与 `max_monitor_polls` 的语义是否清晰。
- dataloader 捕获失败是否只 warning 会掩盖真机观测异常。
- `task_complete` 与 active execution 的冲突处理是否足够安全。
- parse error、tool error、executor error 是否能被 planner 下一轮看到。

输出：

- 状态机 bug/risk 列表。
- 需要补充的单元测试和集成测试。
- 是否需要将状态机拆分为更小模块的建议。

### 5.4 配置与部署审查

检查所有 `examples/config*.yaml` 和 `robot_runtime/configs/*.yaml`：

- 配置项是否有统一 schema 或文档。
- 示例是否可在干净机器上运行。
- 是否存在绝对路径、个人路径、真实 token、内网地址、临时目录依赖。
- 是否清楚区分 mock、local model、real robot、legacy bridge、runtime deployment。
- 环境变量是否有统一命名和默认值说明。

已观察到的重点风险：

- 示例配置中存在 `/home/ubuntu/dais/models/RoboBrain2.5-4B` 绝对路径。
- Dual-Franka 文档多处使用 `/home/ubuntu/dais/...` 和 `/tmp/img`。
- README 提到 `examples/config.openai.yaml` 和 `examples/mcp_server_example.py`，当前 git 跟踪文件中未看到对应文件。

输出：

- 配置可移植性问题清单。
- 示例配置分层建议，如 `config.mock.yaml`、`config.openai.example.yaml`、`config.dual_franka.runtime.example.yaml`。
- 必要的 `.env.example` 或配置模板建议。

### 5.5 开源规范审查

检查项目是否具备开源交付基本材料：

- `LICENSE`
- `CONTRIBUTING.md`
- `SECURITY.md`
- `CODE_OF_CONDUCT.md` 或说明不采用
- `CHANGELOG.md` 或 release notes 策略
- README quickstart、architecture、configuration、testing、deployment、troubleshooting
- package metadata：作者、license file、classifiers、optional dependencies、Python versions
- CI：lint、type check、tests、package build
- 发布边界：sdist/wheel 是否包含不该包含的文件

已观察到的重点风险：

- `pyproject.toml` 声明 `license = {text = "Apache-2.0"}`，但仓库根目录未看到 `LICENSE` 文件。
- 未看到 `CONTRIBUTING.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`、`CHANGELOG.md`。
- 未看到 CI 配置。

输出：

- 开源交付缺口表。
- 最低必需补齐项和推荐增强项。

### 5.6 冗余与历史包袱审查

检查：

- legacy bridge 是否仍被使用，如 `dual_franka_bridge.py`。
- mock server、mock bridge、runtime placeholder 是否命名清晰，是否会误导真实部署。
- `examples/visualize_run_video.py` 是否属于核心交付，依赖是否应该放入 optional extra。
- 未跟踪但本地存在的生成产物是否被 `.gitignore` 覆盖。
- README/文档是否过长或重复，是否需要拆成 docs。

已观察到的重点风险：

- 本地目录存在 `__pycache__`、`.pytest_cache`、`.venv`、`runs`、`*.egg-info` 等生成产物，当前未被 git 跟踪且多数已忽略；正式审核需确认发布包不会包含它们。
- 文档中 legacy/debug/placeholder 描述较多，需要区分“稳定 API”和“兼容调试入口”。

输出：

- 可删除文件列表。
- 可归档文件列表。
- 文档合并/拆分建议。

### 5.7 安全与真实机器人风险审查

检查：

- 执行工具是否有 dry-run、安全模式、急停、权限边界。
- `call_bridge` 这类通用 HTTP tool 是否会暴露过宽能力。
- HTTP endpoint 是否需要认证、来源限制或部署说明。
- 错误时是否默认 `running` 会导致机器人或 monitor 状态长期挂起。
- 日志是否可能记录 API key、图像、用户任务中的敏感信息。
- open-source 示例是否包含密钥或可被误用的真实地址。

输出：

- 安全风险等级清单。
- 开源 README 中必须提示的安全免责声明。
- 真机部署前 checklist。

### 5.8 测试与质量门禁审查

建议执行：

```bash
python -m pytest
python -m compileall src mcp_server robot_runtime examples
python -m build
```

如果项目补齐工具链，进一步建议：

```bash
ruff check .
ruff format --check .
mypy src robot_runtime
twine check dist/*
```

重点测试缺口：

- 新机器人 adapter 的最小 contract test。
- MCP `list_tools -> registry -> parser -> call_tool` 端到端测试。
- monitor timeout、execute failure、tool failure、task_complete while running 的状态机测试。
- 示例配置 smoke test，确保 README 中命令真实可跑。
- package build test，确保 wheel 安装后 CLI 和 runtime 入口可用。

输出：

- 当前测试覆盖评价。
- 必须新增的阻断级测试。
- 推荐 CI workflow。

## 6. 详细检查清单

### 6.1 仓库结构

- [ ] 根目录文件是否只保留项目元信息、README、docs、examples、src、tests、runtime/adapter 必要目录。
- [ ] `src/` layout 是否统一，root package `robot_runtime` 是否有明确发布理由。
- [ ] `mcp_server` 是否应该作为包发布，还是作为 examples/tools 发布。
- [ ] README 中的架构图是否和实际目录一致。
- [ ] 中英文 README 内容是否同步。

### 6.2 扩展机器人

- [ ] `robot_runtime/api/app.py` 是否需要 registry/factory 机制替代硬编码 `dual_franka`。
- [ ] 新增 robot driver/camera/monitor provider 是否有模板和测试。
- [ ] runtime config 是否能声明 provider class/path 或 entry point。
- [ ] MCP adapter 是否能复用通用 HTTP adapter，减少每个机器人复制 server。
- [ ] robot namespace、tool role、dataloader endpoint 是否能纯配置完成。

### 6.3 扩展工具

- [ ] MCP self-description 是否包含完整 `inputSchema` 并进入 prompt。
- [ ] 非标准 tool output 是否有稳定 contract。
- [ ] `call_bridge` 是否应限制 path/method 或改名为 debug-only。
- [ ] tool collision 和 namespace mismatch 错误是否清晰。
- [ ] tool result 中的大型 payload 是否会污染 prompt 或日志。

### 6.4 冗余与过时内容

- [ ] `dual_franka_bridge.py` 是否继续保留；若保留，是否移动到 `legacy/` 或明确标注。
- [ ] mock bridge/server 是否需要全部发布，还是保留一个标准 mock。
- [ ] `config.dual_franka.yaml` 与 `config.dual_franka.runtime.yaml` 是否可合并或更清楚命名。
- [ ] README 提到但不存在的文件是否补齐或移除。
- [ ] docs 是否存在重复段落、过期部署路径或个人路径。

### 6.5 开源交付

- [ ] 补齐 `LICENSE` 并与 `pyproject.toml` 一致。
- [ ] 增加 `CONTRIBUTING.md`。
- [ ] 增加 `SECURITY.md`，说明真机安全和漏洞报告方式。
- [ ] 增加 `.env.example` 或配置模板。
- [ ] 增加 CI workflow。
- [ ] 增加 package build 检查。
- [ ] 确认 `README.md` quickstart 能在全新环境跑通。
- [ ] 确认没有真实 key、个人路径、内部机器名、不可公开数据。

## 7. 优先级建议

P0 阻断开源交付：

- 缺少 `LICENSE` 文件但声明 Apache-2.0。
- README 引用不存在的示例文件。
- 示例配置含个人绝对路径或疑似敏感配置。
- package build/安装后入口不可用。
- 真机执行相关接口缺少安全声明或误导性默认值。

P1 影响可维护性和扩展性：

- `robot_runtime/api/app.py` 对 `dual_franka`、`placeholder`、`local_files` 的硬编码。
- legacy bridge 与 runtime deployment 并存但稳定性边界不清楚。
- 配置没有 schema、模板或集中说明。
- 状态机过大，关键行为分散在 `AgenticRobotLoop.step` 内。

P2 推荐改进：

- 中英文文档同步机制。
- 增加 typed config validation，如 pydantic 或自定义 schema 校验。
- 增加 adapter 模板和 cookiecutter-like 示例。
- 增加 pre-commit、ruff、mypy。

## 8. 审核交付物

建议最终产出以下文件或内容：

- 代码审核报告：按严重程度列出问题、位置、影响、建议。
- 开源交付 checklist：标明已完成、需补齐、可延后。
- 扩展性评估：新增机器人和新增 tool 的实际步骤与摩擦点。
- 清理建议 PR：删除或归档冗余内容、修复 README 失效引用、替换绝对路径。
- 测试/CI 建议 PR：补 smoke tests、package build、lint/type check。

## 9. 建议执行顺序

1. 先跑测试和 package build，确认当前基线是否可交付。
2. 审 README 与 examples，修复阻断级失效路径和不可移植配置。
3. 审核心状态机和 MCP contract，找出真实行为风险。
4. 审 robot runtime 扩展边界，形成新增机器人模板建议。
5. 审开源材料和 CI，补齐最低发布门槛。
6. 最后做冗余清理，减少历史兼容内容对新用户的干扰。

