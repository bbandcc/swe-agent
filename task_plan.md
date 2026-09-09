# SWE Agent 架构优化调研计划

## 目标
基于 langtalks/swe-agent 的源码与优质 GitHub 项目已实现机制，形成可验证的渐进优化方案。当前只调研，不修改业务代码。

## 阶段
- [x] 建立计划并确认范围。
- [x] 核查目标仓库版本、架构、调用链和实现缺口。
- [x] 核查参考项目实现、适用条件及移植成本。
- [x] 汇总优先级、模块边界、验证标准及首个改造切片。
- [x] 检查证据、诊断结果与提交范围，随本轮中文提交保存调研文档。

## 新增任务：完整项目导读
- [x] 确认本地源码与 GitHub 上游项目。
- [x] 盘点目录、依赖、配置、入口与运行方式。
- [x] 沿 Architect → Developer 主链路逐文件核查核心实现。
- [x] 核查工具模块、数据结构、提示词、异常路径与限制。
- [x] 编写并校验完整中文导读，使用中文 Commit Message 提交。

## 原则
- 源码事实、设计推断、待实验收益明确分开。
- 不将新增框架或技术热度等同于改进。
- 固定源码版本，给出可定位的链接。

## 问题与错误
- 本地 Git 仓库没有历史提交；现有 .serena/ 不纳入本次提交。
- 一次工具结果序列化失败，已按独立结果重新获取；deepagents 分支经核实为 main。
- 本地没有 LangGraph/Tree-sitter 依赖；先做无模型、标准库隔离复现，完整运行留待实施阶段。
- 沙箱用户执行 Git 时触发 dubious ownership；后续命令使用单次 `-c safe.directory=...`，不修改用户全局 Git 配置。
- 初次 Markdown 围栏计数检查发现正文示例中含裸三反引号；已改写该句并重新检查。
- 上游研究初稿一度误判两个 dotfile 不存在；已用 GitHub `fetch_file` 核实固定提交中的 `.env.example` 与 `.python-version` 并修正。
- 首次暂存后的 `git diff --cached --check` 发现 5 处 Markdown 强制换行尾空格；提交被中止，清理后重新检查。

## 状态
新增任务已完成。主导读为 research/swe-agent-complete-guide.md，上游与官方资料核查为 research/swe-agent-upstream-notes.md。Markdown 围栏/链接/UTF-8、Git diff whitespace、Python 语法及四项基线行为复现均已检查；未修改上游业务源码，也未执行需要有效 Anthropic 模型的端到端运行。

## 新增任务：规范与第一步可靠编辑

- [x] 将认可的优化方案固化为根目录 AGENTS.md。
- [x] 导入固定提交的上游业务源码并核对来源。
- [x] 以 WorkspaceEditor 公开 interface 完成可靠编辑的 TDD 纵向切片。
- [x] 运行编辑模块测试、项目语法检查和原始缺陷回归诊断。
- [x] 更新证据和状态，随本轮中文 Commit Message 提交。

### 本步 seam 与范围

- 公开 seam：`WorkspaceEditor.apply(EditProposal) -> EditResult`。
- 本步包含：工作区相对路径约束、唯一精确匹配、基线哈希预检、单文件原子替换、结构化 applied/rejected/noop 结果。
- 本步不接 LangGraph Developer，不加入模型重试、测试执行器、checkpoint 或 repo map；这些分别作为后续可验证切片。

### 状态

AGENTS.md 已单独提交。上游源码和静态资源已导入并标记固定来源；WorkspaceEditor 通过 12 个测试中的 11 个，1 个符号链接用例因 Windows 权限跳过；Python 语法、原版缺陷复现与离线构建检查通过。Developer 接入留作下一步独立切片。

### 本步问题与处理

- `git clone` 在沙箱外访问 GitHub 时连接被重置；确认没有留下 `.research-cache/upstream`，改用已连接的 GitHub 插件按固定提交读取文本源码。
- 首次 `uv build --offline` 在沙箱内无法读取 uv 缓存；提升权限重跑后确认上游缺少包发现配置，setuptools 将 `agent/static/helpers/research` 误判为多个顶层包。已限制只发现 `agent*`、`helpers*`，并显式包含 prompt Markdown，等待重新构建验证。
- 修正包发现后 `uv build --offline` 成功生成 sdist 与 wheel；build/dist/egg-info 均由上游 `.gitignore` 排除。
- 当前环境没有 ruff 或 pyright，未安装新工具，也不将 Python 语法检查表述为 lint/type-check。

## 新增任务：将可靠编辑接入 Developer

- [x] 复核 Developer 的路径、模型输出解析、写入和路由边界。
- [x] 先写失败测试，固定工作区读取、单块协议、成功推进和失败终止行为。
- [x] 用最小实现替换 Developer 的直接文件读写与行号切片。
- [x] 运行受影响测试、图级集成测试、语法检查和离线构建。
- [x] 更新实现依据与已知限制，使用中文 Commit Message 提交。

### 本步 seam 与范围

- `WorkspaceEditor.snapshot(path) -> WorkspaceSnapshot`：以和写入相同的工作区边界读取 UTF-8 内容及 SHA-256。
- `DeveloperEditExecutor.prepare(plan_path) -> WorkspaceSnapshot`：兼容现有计划里的 `./workspace_repo/` 前缀，并将路径收敛为工作区相对路径。
- `DeveloperEditExecutor.apply(snapshot, model_output) -> EditResult`：新文件把模型输出作为完整内容；已有文件只接受一个严格 SEARCH/REPLACE 块，再交给 `WorkspaceEditor.apply`。
- Developer 仅在 `EditStatus.APPLIED` 时推进任务；rejected 与 noop 都结束当前运行并保留结构化结果。
- 本步顺带处理空任务计划，避免在第一个任务索引处崩溃；不加入模型重试、测试执行器、checkpoint、预算或 repo map。

### 参考实现边界

- 采用 Deep Agents 固定提交中的唯一匹配与结构化结果思想；基线哈希和原子写入仍是本项目自己的保证。
- 采用 Aider 固定提交中可读 SEARCH/REPLACE 协议的思想，但不采用首次匹配、跨文件 fallback 或部分成功语义。

### 当前状态

本切片已完成。26 个测试中 25 个通过，1 个符号链接用例因 Windows 权限跳过；锁文件检查、Python 语法、Prompt 渲染、图导入、敏感值扫描、Git diff whitespace、离线 sdist/wheel 构建均通过。未运行真实 Anthropic 模型或 benchmark，不声明 Agent 效果提升。

### 本步问题与处理

- `uv sync --locked --offline` 因本机缓存缺少 `tree-sitter==0.21.3` 失败；随后按现有锁定版本联网同步依赖，没有升级业务依赖。
- 当前 uv 版本要求将旧版 `uv.lock` 从 revision 1 更新到 revision 3；解析结果仍为 66 个锁定包，等待构建复核。
- 当前开发依赖不含 ruff，`python -m ruff` 无法运行；本步只报告实际完成的测试、语法、构建和 diff 检查。
