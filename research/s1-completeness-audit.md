# S1 完整性审计

审计日期：2026-09-10。结论分为源码已确认、测试已验证和仍待外部验证。

## 逐项结论

| 编号 | 审计结论 | S1 处理 |
|---|---|---|
| 1 | **问题存在。** `check_research_step` 同时存在条件边和无条件 `conduct_research` 边，invalid 分支仍可能研究。 | 删除无条件边，将 Architect 改为可注入 runtime；图事件测试确认 invalid 后先回到 planning，随后 valid 才进入 research。 |
| 2 | **问题存在。** 原默认 `claude-sonnet-4-20250514` 已于 2026-06-15 退役。 | 先修正为官方迁移目标，随后按实际可用凭据将生产默认切换为 DeepSeek 官方 `deepseek-v4-flash` 和 Anthropic 兼容端点；统一模型工厂仍保留显式 Anthropic 配置。`scripts/smoke_model.py` 覆盖真实文本、工具调用和结构化输出，联网执行仍需本机安全配置新密钥。 |
| 3 | **问题存在。** 四个读取工具直接接受模型路径并调用 `open`/`os.walk`。 | 新增统一 `WorkspacePathResolver`；search、definition、implementation、raw content 和 file tree 都经过相同边界并返回结构化结果，递归搜索不跟随 symlink/junction。 |
| 4 | **问题存在。** 每个 atomic task 原先立即写盘，后一步失败会留下前一步。 | 引入 `begin → stage... → commit` 文件事务。stage 只修改不可变 working copy；任一步失败丢弃整份 working copy；全部成功后重新检查原始哈希并写一次。 |
| 5 | **问题存在。** 空 tasks 一律映射为编辑错误，无法表达“已经满足，无需修改”。 | `ImplementationPlan.status` 明确区分 `ready` 与 `no_changes`；显式 no-change 必须 tasks 为空且提供 reason，隐式空计划仍是 `DeveloperErrorCode.INVALID_PLAN`。 |
| 6 | **问题存在。** 原 26 个测试没有覆盖所列矩阵。 | 新增 Architect invalid 路由、同文件两步成功/后步失败、读取工具越界、无 EOF newline、root 缺失、读取分类、目录回滚和 junction 等用例。 |
| 7 | **实现已有检查，但安全证据不足。** 原 symlink 测试在当前 Windows 权限下跳过，junction 没有独立证据。 | 新增不需要开发者模式的 Windows junction 测试并实际通过；编辑器和递归搜索都验证/跳过 junction。普通 symlink 用例仍因 WinError 1314 跳过。 |
| 8/12 | **问题存在，且是同一缺陷。** 已有文件在 apply 读取失败时误报 `WRITE_FAILED`。 | 所有读取阶段返回 `READ_FAILED`；准备临时文件、fsync、replace/link 才返回 `WRITE_FAILED`；并增加故障注入测试。 |
| 9 | **问题存在，而且字段有实际用途。** 原提案无法关联失败 atomic step。 | `EditProposal.task_id` 设为必填；Developer 生成稳定的 `task-N.step-N`；`EditResult.task_ids` 带回已 stage 和失败步骤。没有引入 RunRecorder。 |
| 10 | **问题存在。** 构造 `WorkspaceEditor` 时 `resolve(strict=True)` 直接抛异常。 | 构造不访问文件系统；首次操作返回 `WORKSPACE_NOT_FOUND` 或 `WORKSPACE_INVALID`。 |
| 11 | **描述需要收窄。** “目标已存在”本身不会创建父目录，因为目标存在意味着父目录存在；真正副作用发生在嵌套 CREATE 已 mkdir、后续 link/write 失败时。 | 最终 commit 前不创建目录；commit 失败时只逆序删除本次新建且仍为空的目录。测试注入 link 失败并确认顶层新目录不存在。 |
| 13 | **问题存在。** README 把 literal search 写成 semantic search，并混用了 pytest、`.env.local`、激活虚拟环境和旧仓库名。 | 改为实际的 workspace-bound literal search/tree-sitter、Architect/Developer 两角色、unittest、`.env`、`uv run` 和当前仓库地址；补充事务与 smoke 命令。 |

## 设计依据与取舍

- OpenAI Codex 固定提交 [`b5544d57`](https://github.com/openai/codex/blob/b5544d5732f40431c57bf6ca3bed5b127cdf27a0/codex-rs/apply-patch/src/invocation.rs#L205-L282) 会在验证阶段读取文件、计算新内容并聚合为 `ApplyPatchAction`，之后才执行写入。本项目采用“先计算、后提交”的边界，但允许同一文件的多个 Agent step 顺序作用到 working copy。
- Codex 的 [`ApplyPatchAction`](https://github.com/openai/codex/blob/b5544d5732f40431c57bf6ca3bed5b127cdf27a0/codex-rs/apply-patch/src/lib.rs#L137-L190) 把按文件计算完成的内容作为明确数据结构；本项目对应为不可变 `WorkspaceTransaction`，并额外保留原始哈希与 task IDs。
- Codex 的 Windows/UNC 与保留 symlink 语义实现见固定提交的 [`absolute-path`](https://github.com/openai/codex/blob/b5544d5732f40431c57bf6ca3bed5b127cdf27a0/codex-rs/utils/absolute-path/src/lib.rs#L132-L208)。本项目安全目标更窄：目标仓库内的模型文件工具拒绝任何 symlink/junction 路径，而不是保留逻辑别名。
- 唯一 SEARCH/REPLACE 匹配继续沿用 Deep Agents 固定提交 [`18106be8`](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/deepagents/backends/utils.py#L521-L578) 的严格失败契约，以及 Aider 固定提交 [`5dc9490b`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L21-L183) 的可读协议；没有采用 Aider 的首次匹配和部分成功语义。
- Anthropic 官方[退役表](https://platform.claude.com/docs/en/about-claude/model-deprecations)记录旧 Sonnet 4 的退役日期并给出 Sonnet 4.6 迁移目标；[模型 ID 文档](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions)用于核对可配置模型 ID。
- DeepSeek 官方[模型列表](https://api-docs.deepseek.com/quick_start/pricing/)确认当前模型 ID 为 `deepseek-v4-flash`；[Anthropic API 兼容说明](https://api-docs.deepseek.com/guides/anthropic_api/)确认 `https://api.deepseek.com/anthropic`、工具字段和 `tool_choice` 可用。本项目因此复用已有协议客户端，没有增加第二套模型 SDK。

## 当前验证边界

- 确定性与 fake-model 测试已覆盖本次 S1 行为。
- Windows junction 拒绝已在本机真实创建 junction 后验证。
- 普通 symlink 测试仍受当前 Windows 权限限制而跳过，但实现与 junction 共用 resolver 检查。
- 真实模型 smoke test 需要在被 Git 忽略的本机配置中提供有效密钥。脚本在缺少密钥时明确返回 skipped；通过前不能把“模型适配已完成”表述成“真实 Agent 已联网验证”。
