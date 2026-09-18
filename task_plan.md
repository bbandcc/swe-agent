# SWE Agent 架构优化调研计划

## 状态文件治理（长期有效）

- 本文件是项目当前进度的唯一实时状态源；`MASTER_PLAN.md` 只维护总目标、阶段设计和 D 项，不记录日常进度。
- 每当阶段开始、完成/审核通过、出现 blocker/遗漏/风险、计划或验收变化、下一阶段变化时，必须同步更新本文件。
- 每次实质更新后，先执行 `git diff -- task_plan.md` 和 `git status`，再将本文件与相关代码/测试放入同一完整 checkpoint 提交；配置远程且具备权限时继续 push，并如实记录失败。
- 只有源码、测试和审核证据确认后，阶段才能标记为完成；不得按计划预先勾选。
- 新阶段开始前必须核对工作区文件、HEAD 中版本和远程跟踪分支；发现未提交或未推送内容时，先处理同步再开始。

### 当前状态文件基线

- S3.2 production 技术冻结点：`5850b8370c49f868e90aeffe9e6042f85eaa522c`；本轮继续完成 S3 continuation 的 D1/S1a 与 D2/S2b prerequisite。
- S3.2 的配置、身份、SQLite checkpoint、预算边界、start/resume 和现有图接入均已有源码与测试证据；D1/S1a“编辑最终净零结果”和 D2/S2b“对外结果契约”已有源码与测试证据；S3 尚未全部完成，当前不能进入 S4。
- 最新 Codex 本地验证：unittest 184 tests OK / 6 skipped；pytest 178 passed / 6 skipped / 147 subtests passed；Python compile、11 个 prompt render、CLI help、`git diff --check` 均通过。6 个 skip 仅为当前 Windows 普通 symlink 权限限制。
- 没有 GitHub CI 或独立外部测试证据，不作相应声明。
- 剩余能力：provider timeout/retry/attempt、secret-safe persistence、EventSink/RunRecord/artifact、workspace locking/policy、write/verification recovery 等仍未实现；exactly-once 不承诺。
- 后续技术切片继续按 MASTER_PLAN §5.2 执行；保留现有 S1/S2/S3 历史编号，不重新开启或重命名 S1。
- MASTER_PLAN 对应：当前实现事实冻结在 S3.2、D1/S1a 和 D2/S2b prerequisite，后续运行控制、轨迹和恢复要求继续按 §5.2 映射执行；本次不推进下一阶段。

## 当前文档任务：独立 MASTER PLAN（2026-09-16）

- 基线：`330f20f25426ce9b0ceb26ea926a11f6a8cb9d15`，开始时工作区干净。
- [x] 核对当前源码、测试、研究资料和外部固定版本机制。
- [x] 编写根目录 MASTER_PLAN.md，形成 D 项到实施阶段和评测的证据链。
- [x] 执行现有测试、文档审计，确认历史架构调研文件未改动。
- [x] 更新核验记录，中文 Conventional Commit 本地提交。

仅新增长期设计基线并追加过程记录，不修改生产代码、测试、依赖或历史架构调研。

验收：157 项测试中 151 通过、6 项普通 Windows symlink 权限跳过；compile、11 个 prompt render、CLI help 通过。MASTER_PLAN 的 12 个 D 项五字段结构、本地链接、围栏、五组可执行探针已校验；已细分锁/授权及隔离/真实评测，避免高风险改造捆绑。历史架构调研相对基线无差异。没有运行真实 provider 或 benchmark，不报告效果提升。

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

## 新增任务：S1 完整性审计与修正

- [x] 逐项复现并判定用户提出的 13 项问题，记录源码证据与范围判断。
- [x] 核查成熟项目的路径限制、同文件事务和模型配置实现，固定参考版本。
- [x] 通过公开 seam 写红测并完成 S1 内必要修正。
- [x] 在本机存在密钥时执行最小真实 API smoke test；否则留下可复现命令和明确阻塞原因。
- [x] 完成测试、语法、构建、静态检查、文档一致性和差异审计。
- [x] 更新 S1 审计报告并使用中文 Conventional Commit 提交。

### 已确认测试 seam

- Architect 编译图：从 invalid research 状态观察后续节点，不断言私有调用次数。
- 工作区文件接口：通过 `WorkspaceEditor`/同文件计划接口观察结构化结果与最终文件内容。
- 检索工具公开调用接口：用越界路径调用并观察结构化拒绝。
- Developer 编译图：观察 no-change、失败和同文件多 atomic task 的终态与文件内容。

### 设计结论

- 同文件事务由 editing 模块定义不可变 transaction，Developer 状态只保存并推进它；atomic proposal 顺序 stage，最后 commit 一次。
- `ImplementationPlan.status=no_changes` 且有 reason 才是无需修改；默认 ready 的空计划仍为 invalid。
- `EditProposal.task_id` 为必填，`EditResult.task_ids` 用于定位已 stage 与失败步骤，不引入完整 RunRecorder。

### 当前状态

13 项已完成源码审计，详细判断见 `research/s1-completeness-audit.md`。文件事务、统一路径 resolver、Architect 路由、no-change 语义、模型配置和错误分类已经落地。切换 DeepSeek 配置后共有 41 项测试：40 项通过，Windows junction 用例实际通过，1 项普通 symlink 用例因 WinError 1314 跳过。Python 语法、Prompt 渲染、图导入、diff whitespace 和离线 sdist/wheel 构建通过。真实 DeepSeek smoke 已验证鉴权、普通响应、工具调用和结构化输出。

## 新增任务：S1.1 DeepSeek 真实模型适配

- [x] 以 DeepSeek 官方文档核对当前模型 ID、专用 provider、API 端点和工具调用能力。
- [x] 通过公开模型配置 seam 写红测，将生产默认切换到 `deepseek-v4-flash`。
- [x] 统一 Architect/Developer 的模型构造入口，同时保留显式 Anthropic 配置。
- [x] 将真实 smoke 扩展为文本、强制工具调用和结构化输出三项检查。
- [x] 使用被 Git 忽略的本机密钥完成真实 API smoke。
- [x] 完成全量回归、构建检查并提交独立中文 Conventional Commit。

### 当前边界

smoke 通过只能证明密钥、端点、模型、工具调用和结构化输出与当前客户端兼容；它不等于完整 Architect → Developer 任务质量评测。完整效果仍需后续固定任务集、多次运行和执行器验收。

## 新增任务：S1 Final Gate

- [x] 统一默认读取工具与 Developer 编辑器的 workspace 配置来源。
- [x] 将 search 文件读取失败暴露为 warnings 与 skipped_files。
- [x] 在任何写盘前拒绝指向同一 canonical 文件的重复任务。
- [x] 增加父图 fake-model 状态传播和真实 ToolNode 循环回归。
- [x] 完成全量 S1 tests、compile、diff check 与 DeepSeek smoke。
- [x] 独立提交 S1 Final Gate。

### 范围边界

本步只关闭 S1 最终验收缺口；保留现有单文件事务、角色划分、模型 provider 和依赖主版本，不引入 S2 能力。

### 验收结果

统一工作区根、search 跳过文件诊断、重复 canonical 文件任务拒绝、父图状态传播与真实 ToolNode 循环均已覆盖。全量 48 项测试中 47 项通过，1 项因 Windows 缺少普通符号链接权限跳过；junction 用例通过。Python compile、Git diff whitespace 与真实 DeepSeek 三项 smoke 均通过。

## 新增任务：S2 测试验收和有限修复

### 固定范围

- 基线：`022371ee769269f38d33cfe2f799884822444505`。
- 只实现确定性 Verification Runner、baseline/post 判定、最多两次 repair 和顶层状态传播。
- 保留全部 S1 契约；不实现 checkpoint、预算、RunRecorder、repo map、正式 benchmark、新 Agent、Docker sandbox 或 provider/依赖升级。

### 阶段

- [x] 阅读 AGENTS.md、架构评审和当前 Developer/父图实现。
- [x] 固定 mini-swe-agent、SWE-bench、Aider 源码证据和适用边界。
- [x] 以公开 Verification 接口完成 runner 的 red → green 测试。
- [x] 以确定性判定接口覆盖 baseline/post 组合。
- [x] 接入父图并完成最多两次的 repair 闭环测试。
- [x] 运行全量测试、compile、diff check 并审计 S1 契约。
- [x] 更新研究文档。
- [x] 独立提交。

### 已确认 seam

- `VerificationRunner.run(VerificationSpec) -> VerificationResult`。
- baseline/post 的纯确定性判定函数。
- 注入 fake runtime、真实 Developer 编译子图和临时 workspace 的顶层图。

### 当前状态

实现、研究记录和最终验证已完成。全量 67 项测试中 66 项通过，1 项普通 symlink 用例因 Windows 权限跳过；compile、prompt 渲染和 diff check 通过，并以独立提交交付。

### 问题与处理

- GitHub 一次 TestSpec 猜测路径读取返回 404；改用固定提交的 SWE-bench grading 源码，不依赖错误路径。
- 后台参考研究因工具额度中断；主任务已直接核对三个项目的固定提交源码并写入 S2 研究文档。
- Windows venv 启动器的 timeout 测试一度留下短暂 cwd 句柄；runner 改为显式进程组/进程树终止，timeout 测试使用真实解释器进程验证。
- 抽取 verification controller 后，LangGraph 对 bound method 的 state 注解推断与父图 Pydantic state 不一致；父图增加带 `AgentState` 注解的薄 wrapper，保持 controller 与图 schema 解耦。

## 新增任务：S2 Final Gate

- [x] 让生产图从可信外部 JSON 配置加载 argv verification checks。
- [x] 增加整体 outcome，确保 Developer 失败或 NOOP 不被测试通过覆盖。
- [x] 将多文件 repair 收窄到最后提交并触发回归的文件。
- [x] 有限等待进程树清理，并验证 parent-child timeout。
- [x] 标记 verification 输出为不可信诊断数据并传递截断标志。
- [x] 运行全量测试、compile、prompt render 和 diff check。
- [x] 独立提交 S2 Final Gate。

验收：全量 77 项测试中 76 项通过，1 项普通 symlink 用例因 Windows 权限跳过；
compile、prompt render 与 diff check 通过。生产配置、整体终态、多文件 repair 和
parent-child timeout 均由顶层或公开接口测试覆盖。

并行执行 compile 与 prompt render 时曾因瞬时内存不足导致 Pydantic 初始化失败；
改为顺序执行后两项均通过，最终全量测试也在顺序执行下通过。

## 新增任务：S2 Acceptance Fix

- [x] repair canonicalization 与 S1 `os.path.normcase` 完全一致。
- [x] 修正 NO_CHANGES、PENDING、RUNNING 与 verification 的 outcome 优先级。
- [x] 在公开 spec 和环境配置入口拒绝非有限或非正 timeout。
- [x] 修正 `.env.example` JSON，并更新 README 主流程与 S2 research。
- [x] 完成全量验收并独立提交。

验收：83 项测试中 82 项通过，1 项普通 symlink 用例因 Windows 权限跳过；
compile、prompt render 与 diff check 通过。

## 新增任务：S2 Seal Fix

- [x] 父图所有 END 路径将残留 PENDING outcome 封口为 FAILED。
- [x] Windows timeout 先执行有界 PID tree taskkill，Job Object 作为 backstop。
- [x] 加强终态矩阵和 parent-child timeout 黑盒测试。
- [x] 完成全量验收并独立提交。

验收：84 项测试中 83 项通过，1 项普通 symlink 用例因 Windows 权限跳过；
compile、prompt render 与 diff check 通过。

## 新增任务：S3 Design Gate

### 固定范围

- 基线：`8d89670e97442f652d592a3e0f6f63c373fe3717`。
- 只设计最小 RunConfig、预算与 usage、运行轨迹、SQLite checkpoint 与恢复对账。
- 本轮不修改生产代码或依赖，不进入 S4/SWE-bench，不增加 Agent、UI、sandbox 或 provider。

### 阶段

- [x] 审计现有 S1/S2 的配置、状态、模型/工具边界、编辑 hash 与图装配 seam。
- [x] 核对 LangGraph 1.x 官方 SQLite saver 接口、thread_id 与子图传播行为。
- [x] 核对 mini-swe-agent 等固定版本源码中的预算、usage、trajectory 与恢复机制。
- [x] 设计公开 seam、状态字段、图节点、恢复状态机、预算规则和测试矩阵。
- [x] 明确拟修改文件、必要依赖变化、迁移顺序与已知限制。
- [x] 仅完成研究文档与本计划的检查并独立提交。

### 当前状态

Design Gate 已完成。仅修改 `research/s3-runtime-recovery.md` 和本计划；84 项 S1/S2 测试中 83 项通过、1 项因 Windows 普通 symlink 权限跳过，compile、11 个 prompt render、设计结构检查和 `git diff --check` 均通过。未修改生产代码或依赖。

## 新增任务：S3 Design Gate Seal

### 固定范围

- 基线：`06d0db01dd0e9a483386e3241afe9e75f5b74565`。
- 只修订 S3 设计文档与本计划；不修改生产代码、测试或依赖。
- 保留 S1/S2 契约与 Architect → Developer → bounded repair 主链；不进入 S4/SWE-bench，不增加 Agent、UI、sandbox 或 provider。

### 审查与设计封口

- [x] 逐项审核五项调整，确认均属于 S3 且优于原设计，并记录必要的保守边界。
- [x] 将预算改为 checkpoint 先行的 `reserve → dispatch → settle` graph steps；恢复遇到结果不确定的 `IN_FLIGHT` 调用停止，step 不回退。
- [x] 将 usage boundary 放在 raw model response 与 parser 之间；设计 secret-safe checkpoint/event 与 Runner pipe-drain 完整日志 artifact。
- [x] 定义 durable step/call/write identity、事件幂等键、严格分离的 start/resume，以及 pending write/repair 生命周期。
- [x] 补充 runtime root、model output-token limit、Agent/workspace revision 和 path-only WorkspaceIdentity 限制。
- [x] 保留公开 ToolNode，只增加前后预算节点；不包装私有 checkpoint 表或建立 telemetry 平台。
- [x] 补齐 crash 预算不回退、raw usage、secret canary、event replay、start/resume、完整日志 artifact，以及跨 Architect → Developer → repair → restart 的预算测试矩阵。
- [x] 完成 Markdown 结构/固定链接/禁止范围、全量 S1/S2、compile、prompt render 与 `git diff --check` 验证。
- [x] 仅提交 `research/s3-runtime-recovery.md` 与 `task_plan.md` 的独立中文 Conventional Commit。

### 当前状态

设计修订与验证已完成。全量 84 项 S1/S2 测试通过，41 项 unittest subtests 通过；Python compile、11 个 prompt render、Markdown 围栏、27 个固定 GitHub 源码链接和 `git diff --check` 均通过。S3 生产实现尚未开始，项目依赖文件未变化。

## 新增任务：S3 Design Seal 最终集成边界

### 固定范围

- 基线：`8808be1da787d79a2d5c84aaa5379d4ae941340b`。
- 只修订 `research/s3-runtime-recovery.md` 与本计划；不修改生产代码、测试或依赖。
- 保持 S3.1 为 Config + Identity，S3.2 才启用 SQLite + durable Budget；不进入 S4 或其他禁止能力。

### 审查与设计封口

- [x] 审核四项调整，确认都能加强 S3 恢复一致性且不扩大阶段范围。
- [x] 定义版本化 semantic RunConfig digest、纳入/排除字段，以及允许 API key 轮换的 resume binding。
- [x] 将本地 durable CLI/library seam 定为 S3 正式入口，明确 `langgraph.json:swe_agent` 仅为非 durable Studio/dev 兼容入口。
- [x] checkpoint 绑定 clean Agent commit；known mismatch 拒绝、unknown 告警，不设计 migration framework。
- [x] 分离业务 `max_steps` 与 LangGraph `recursion_limit`，固定当前拓扑公式、cycle 约束和最坏路径测试。
- [x] 完成指定文件范围、Markdown、全量 S1/S2、compile、prompt render 与 `git diff --check` 验证。
- [x] 使用独立中文 Conventional Commit 提交。

### 当前状态

设计修订与验收已完成。全量 84 项 S1/S2 测试与 41 项 unittest subtests 通过；Python compile、11 个 prompt render、Markdown 围栏、27 个固定 GitHub 源码链接和 `git diff --check` 通过。仅两份指定文档有差异；S3 生产实现尚未开始，项目依赖文件未变化。

## 新增任务：S3.1 Config + Identity

### 固定范围

- 基线：`1503e62efa32f95a45983961fc07b3f8ad6d6112`。
- 只实现 RunConfig、semantic digest、显式双 provider 装配、Identity 和纯 preflight。
- 不安装 SQLite，不增加 durable CLI，不把 BudgetController 或 reserve/dispatch/settle 接入生产图。

### TDD 纵向切片

- [x] 红测固定 RunConfig 数值、base URL、workspace/runtime 隔离和 symlink/junction 拒绝行为。
- [x] 实现显式 mapping loader、TokenPricing 和版本化 secret-free semantic digest。
- [x] 红测并实现 DeepSeek/Anthropic 显式 ModelSettings + output-token limit 装配，保留旧环境入口。
- [x] 红测并实现 path-only WorkspaceIdentity、clean/dirty/unknown AgentCodeRevision。
- [x] 红测并实现 Start/Resume request/result、injectable CheckpointLookup 与全部结构化拒绝路径。
- [x] 完成全量 S1/S2/S3.1、compile、prompt render、diff check、敏感值与禁止范围审计。
- [x] 更新最小文档并独立提交。

### 当前状态

S3.1 已完成。全量 124 项测试与 110 项 unittest subtests 通过；Python
compile、11 个 prompt render、diff whitespace、敏感值和禁止范围审计通过。
普通 symlink 与 Windows junction 用例均实际通过；仅有现有 tree-sitter
弃用告警。未修改依赖或生产图，max_steps/max_cost 只完成校验与语义绑定，
SQLite、durable CLI 和预算执行仍留在 S3.2。

## 新增任务：S3.1 Acceptance Fix

- [x] 显式 ModelSettings 装配拒绝所有未进入 semantic digest 的额外模型参数；旧 non-durable 环境入口保持兼容。
- [x] 将 WorkspaceIdentity 的持久化值校验与 `from_root()` 的实时文件系统校验分离。
- [x] 恢复 S1 对祖先 symlink/junction 的兼容，同时继续拒绝 workspace root 自身及内部目标链接。
- [x] 运行全量测试、compile、prompt render、diff check，并独立提交。

### 当前状态

三项边界修复及最终验收已完成。全量 127 项测试与 117 项 unittest
subtests 通过；Python compile、11 个 prompt render、diff whitespace 和
禁止范围审计通过。普通 symlink 与 Windows junction 用例均实际通过；仅有
现存 tree-sitter 弃用告警。未引入 S3.2 能力或依赖变化。

## 新增任务：S3.2 Checkpoint + Durable Budget

### 固定范围

- 基线：`5929328cbde1af10f27a5405ff80d08ebf0fba5f`。
- 只实现同步 SQLite durable runtime、严格 start/resume、可恢复预算和现有图调用边界。
- 不实现 trajectory、artifact/log spool、pending-write recovery、S4 或新 Agent/provider。

### TDD 纵向切片

- [x] 锁定 SQLite saver 3.1.1，验证父图 checkpointer 与默认子图传播、关闭重开恢复。
- [x] 实现不可变 Budget contracts，覆盖 step、usage、cost、deadline 与批量准入。
- [x] 实现 reserve → dispatch → settle durable boundary 和不确定调用恢复。
- [x] 接入 Architect、Developer 与 ToolNode，保持 S1/S2 主链和 non-durable 图行为。
- [x] 实现 library start/resume 与最小 CLI，覆盖所有 preflight 拒绝路径。
- [x] 完成全量 tests、compile、prompt render、diff check 和禁止范围审计。
- [x] 更新最小文档并独立提交。

### 当前状态

S3.2 实现与验收已完成。全量 157 项测试通过，6 项普通 Windows symlink 用例因当前账户缺少创建权限跳过；Windows junction、安全路径、真实 SQLite 关闭重开、父子图 checkpoint 传播、两类 crash window、预算跨 Architect → Developer → repair、deadline、secret canary 和全部 preflight 拒绝路径均实际通过。Python compile、11 个 prompt render、CLI help、SQLite saver 3.1.1 版本检查、非 durable 图导入、禁止范围/密钥扫描及 `git diff --check` 通过；仅有既存 tree-sitter 弃用告警。trajectory、artifact/log spool、pending-write/hash recovery 和 exactly-once 仍未实现。

## 新增任务：S3.2 Acceptance Fix

### 固定范围

- 基线：`330f20f25426ce9b0ceb26ea926a11f6a8cb9d15`；保留其后的计划文档提交。
- 只修正 cost gate、wall-clock deadline、真实生产链恢复集成和 usage 来源标签。
- 保持现有 ToolNode、S1/S2 主链与 non-durable 图；不进入 S3.3 或 S4。

### TDD 验收切片

- [x] 证明 max_cost/cost_unknown 只阻止下一次 MODEL，当前已请求的 TOOL 批次仍受 step/deadline 准入。
- [x] 在 model/tool 返回后检测 deadline overrun，完整 settle 结果并阻断 commit、verification 与后续副作用。
- [x] baseline/post verification 按 remaining time 缩短单项 timeout，deadline 耗尽时零 subprocess。
- [x] 用真实 SQLite 与实际 parent/Architect/Developer 图完成 post regression、repair、关闭重开 resume。
- [x] 将配置估算统一标记为 `configured_estimate`，保留 pricing source 在配置摘要中的来源语义。
- [x] 更新 README 的实际能力描述，运行全量 tests、compile、prompt render 与 diff check。
- [x] 使用独立中文 Conventional Commit 提交，不 amend 既有提交。

### 当前状态

四项 acceptance 边界已完成。全量 162 项测试中 156 项通过、6 项因当前
Windows 账户缺少普通 symlink 创建权限跳过；真实 junction、cost gate、
model/tool deadline overrun、verification 零进程、真实 SQLite 生产链关闭重开
repair 均通过。Python compile、11 个 prompt render、CLI help、SQLite saver
3.1.1、non-durable 图导入和 `git diff --check` 通过；仅有既存 tree-sitter
弃用告警。同步 provider/tool 调用仍只能在返回后检测越期，S3.3 能力未实现。

## 新增任务：S3.2 Final Seal

### 固定范围

- 基线：`48738bd5d1489e31f51f96be6cb2e80c5a4148bd`。
- 只封闭 reserve→dispatch deadline、commit overrun、recursion-limit 证明和恢复序列输入边界。
- 不修改阶段顺序、`MASTER_PLAN.md` 或 `notes.md`，不进入 S1a/S3.3/S4。

### TDD 验收切片

- [x] reserve 后、dispatch 前越期时零外部调用，step 保留且 reservation 确定性终结。
- [x] commit 成功返回后越期时保留真实文件与 EditResult，并阻止后续节点。
- [x] 用实际 compiled graph 最坏路径证明现有 recursion-limit 公式充足。
- [x] checkpoint 值对象只归一化合法序列，拒绝 string 和 scalar。
- [x] 运行全量 tests、compile、11 prompt render、CLI 与 diff check。
- [x] 使用独立中文 Conventional Commit 提交，不 amend。

### 当前状态

Final Seal 实现与验收已完成。全量 168 项测试中 162 项通过、6 项因当前
Windows 账户缺少普通 symlink 创建权限跳过；junction 用例通过。fake-clock
覆盖 MODEL/ToolNode reserve 后越期零调用、SQLite 中断恢复后确定性 settle，
以及 commit 成功返回后越期保留文件与 EditResult。实际 compiled graph 以
40 个业务 step 的单工具循环正常到达 `MAX_STEPS_EXCEEDED`，证明现有
`max(200, 8*max_steps+64)` 公式充足，无需调整。Python compile、11 个
prompt render、CLI help 和 `git diff --check` 通过。

## S3 治理/测试封口（当前状态同步）

S3.2 技术验收已完成并冻结，当前冻结 commit 为
`5850b8370c49f868e90aeffe9e6042f85eaa522c`。最新 Codex 本地验证为
169 total / 163 passed / 6 skipped，129 个 unittest subtests 通过；Python
compile、11 个 prompt render、CLI help 和 `git diff --check` 均通过。6 个
skip 仅来自当前 Windows 账户缺少创建普通 symlink 的权限；没有执行 GitHub
CI 或独立外部测试，未作相应声明。

当前仍未实现：model provider 显式 timeout/retry/attempt 语义、secret-safe
full-state persistence、EventSink / RunRecord、完整 verification artifact
spool、workspace/thread 独占、read/write authorization policy、pending-
write/hash recovery、verification execution recovery；exactly-once 不承诺。
当前不进入 S4；后续继续完成 `MASTER_PLAN` §5.2 中属于运行控制、轨迹和恢复
的剩余要求，并保留现有 S1/S2/S3.1/S3.2 阶段编号体系。
