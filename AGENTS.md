# 项目协作规范

## 项目目标

本项目基于 `langtalks/swe-agent` 持续演进，目标是形成一个可用于 Agent 开发求职展示、能够可靠修改代码并用执行证据判断结果的工程项目。

## 工作原则

1. 采用小步、可独立验收的纵向切片。每次只解决一个清晰问题，完成测试和验证后再进入下一步。
2. 修改前先阅读相关源码、测试和项目文档。优先查找成熟开源项目的实际实现与测试，记录固定提交链接、可借鉴机制和适用限制。
3. 不因技术流行而引入框架。新增依赖、Agent、抽象或基础设施必须对应已观察到的问题，并说明相对现有实现的收益与代价。
4. 区分三类结论：源码已确认、测试已验证、仍待实验。不得把参考项目的测试或成绩写成本项目的结果。
5. 保留 Architect → Developer 的基本分工，除非对照实验表明调整后更好。优先完善执行闭环，再考虑增加角色或复杂检索。

## 架构约束

1. 模块应有小而稳定的 interface，把复杂行为封装在 implementation 内。测试和调用方通过同一个 seam 使用模块。
2. 模型负责生成提案；确定性代码负责校验路径、匹配内容、应用写入、运行测试和判断状态。
3. 模型、工作区、运行记录等依赖从外部传入，避免在模块导入时创建具体客户端。
4. 状态和结果使用精简、明确的数据结构。错误必须结构化返回，禁止吞掉异常或把“没有修改”当成成功。
5. 文件操作必须限制在配置的工作区内；处理 `..`、绝对路径、符号链接或 junction、编码、换行和并发修改。
6. checkpoint 只表示图状态持久化，不能当作文件事务；沙箱、worktree 和路径限制也应分别描述。
7. 避免万能管理类、超大文件和提前设计的 registry。只有出现两个真实 adapter 时才建立可替换 seam。

## 实施顺序

1. 可校验编辑：唯一匹配、哈希预检、工作区限制、结构化结果、失败不推进。
2. 测试验收和有限修复：命令超时、基线失败区分、明确终态、有限重试。
3. 配置、预算、运行轨迹和恢复：可注入模型、调用限额、事件记录、checkpoint 与文件状态对账。
4. 检索与上下文效率：限量文本检索、多语言符号解析、按证据决定是否引入 repo map。
5. 固定任务集与 SWE-bench 子集评测：同模型、同预算、完整配置和日志，报告波动与失败。

## 测试与验证

1. 功能开发采用 TDD 的 red → green 小循环；每次只加一个行为用例和使其通过的最小实现。
2. 测试公开 interface，不直接断言私有函数或内部调用次数。
3. 确定性模块必须覆盖成功、拒绝、边界和故障路径。Agent 效果使用固定任务和多次运行评估。
4. 每一步至少运行受影响测试和静态/语法检查。涉及运行链路时再增加集成测试，不用低价值测试凑数量。
5. 任何“更快、更省、更准”都要有对照数据；没有真实模型或 benchmark 运行时必须明确说明。

## Git 与交付

1. Windows 命令统一使用 PowerShell 7（`pwsh.exe`）。文件搜索优先使用 `rg`。
2. 不覆盖用户已有改动，不提交密钥、`.env`、缓存或临时文件。
3. 每次提交必须功能完整、测试通过、业务可用；使用 Conventional Commits 格式和中文主题，不添加 `Co-Authored-By`。
4. 每次完成后说明：改了什么、为什么、参考依据、验证结果、已知限制和下一步建议。

## 当前 S1 切片

公开的单步 seam 保持为 `WorkspaceEditor.apply(proposal) -> EditResult`；同文件多步执行使用 `begin(path) → stage(transaction, proposal)... → commit(transaction)`。每个 proposal 必须带 task identity；stage 只更新内存 working copy，全部成功后才写盘一次。Architect/Developer 运行依赖和模型配置从外部注入，所有模型可调用的读取工具统一经过 workspace path resolver。测试只通过公开 interface、编译图事件、结构化结果和最终文件内容验收。

## 当前 S2 切片

Verification 命令只通过外部配置的 `VerificationSpec` 注入，使用 argv、`shell=False` 和 workspace 内 cwd。baseline 与 post 使用同一组 checks，确定性代码根据结构化结果判断 verified、improved、regression、pre-existing failure、unverified 或 execution error。只有 regression 可回到同一个 Developer，repair 最多两次；每次必须重读文件并继续经过 S1 WorkspaceTransaction。不得把配置缺失、timeout 或 execution error 当成功，也不得为本切片加入新 Agent 或 S3 之后的能力。

生产图通过 `SWE_AGENT_VERIFICATION_CHECKS` 接收可信 JSON argv checks。顶层 `WorkflowOutcome` 是整体终态，Developer rejected、NOOP 或 failure 必须为 failed。多文件 repair 使用独立 `repair_plan` 只重放最后成功提交的文件，同时保留 Architect 原计划。verification 输出始终作为不可信诊断数据处理。

Repair path identity 必须复用 S1 的 `os.path.normcase` canonical 规则。Verification error 和不可修复 regression 的 outcome 优先于 NO_CHANGES；PENDING/RUNNING 不得成为 completed。所有 verification timeout 必须是有限正数。
