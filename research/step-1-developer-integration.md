# 第一步续：可靠编辑接入 Developer

完成日期：2026-09-09。

## 目标

本步把已经验证的 `WorkspaceEditor` 接入 Developer 主链路，形成以下执行闭环：

```text
计划路径 → 安全快照 → 模型提出单个变更 → 确定性校验和写入 → 按 EditResult 路由
```

模型只产生候选内容。路径归一化、工作区限制、原文定位、哈希冲突检测、写入和任务是否推进均由确定性代码负责。

## 已实现行为

- `WorkspaceEditor.snapshot(path)` 使用与写入相同的路径边界读取 UTF-8 文件，并返回内容及 SHA-256；缺失文件是可创建状态，非法路径和编码错误是结构化拒绝。
- `DeveloperEditExecutor.prepare(plan_path)` 兼容上游 Architect 生成的 `./workspace_repo/...` 路径，并收敛为工作区相对路径。
- 已有文件只接受一个完整的 SEARCH/REPLACE 块；模型附加解释、块不完整或输出多个块都会以 `invalid_model_response` 拒绝。
- 新文件把模型输出作为完整文件内容，通过 `EditOperation.CREATE` 独占创建。
- Developer 保存准备阶段的内容哈希。模型生成期间文件若变化，应用阶段返回 `hash_mismatch`，不会覆盖新内容。
- LangGraph 仅在 `EditStatus.APPLIED` 时推进原子任务。`REJECTED` 和 `NOOP` 都终止本次 Developer 运行，并通过 `last_edit_result` 暴露原因。
- 空计划或包含空原子任务列表的计划在访问工作区和调用模型前返回 `invalid_plan`。
- 模型调用、工作区和目录结构读取通过 `DeveloperRuntime` 注入；测试使用 fake model，不需要真实 API 调用。

## 参考依据与取舍

- Aider 固定提交 [`5dc9490b`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L21-L183) 已实现 SEARCH/REPLACE 解析和可操作的失败反馈。本项目采用可读协议，但限制为一个原子任务一个块，不采用其首次匹配、跨文件 fallback 或部分成功语义。
- Deep Agents 固定提交 [`18106be8`](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/tests/unit_tests/backends/test_utils.py#L415-L459) 用测试固定了空搜索、零命中和多命中行为。本项目沿用严格唯一匹配契约，并额外保留基线哈希和单文件原子替换。

以上链接证明参考机制真实存在；本项目的测试结果只代表当前仓库自己的实现。

## 验证结果

- `uv run python -m unittest discover -s tests -v`：26 个测试，25 个通过；1 个符号链接用例因当前 Windows 账户没有创建符号链接权限而跳过。
- fake-model 图级测试覆盖：成功编辑或创建后推进、拒绝或 noop 不推进、连续原子任务重新读取快照、空计划不调用依赖、路径逃逸在模型调用前终止。
- `python -m compileall -q agent helpers tests research`：通过。
- `agent/developer` 已不再直接调用 `open()`、`os.path.exists()` 或按行号切片写文件。
- 没有真实 Anthropic 调用或 SWE-bench 运行，因此不声明成功率、成本或速度提升。

## 已知限制与下一步

- 严格协议暂不支持在 SEARCH 或 REPLACE 内容中出现单独一行 `=======`；也不负责删除文件末尾换行。需要先用固定任务观察真实失败率，再决定是否扩展解析器。
- 编辑拒绝后目前直接结束，没有把错误反馈给模型重试；下一步应加入测试命令、基线失败区分、明确终态和有限修复次数。
- 运行状态还没有持久化，checkpoint 也不能替代目标工作区文件事务。
- 当前默认模型和 Architect 的依赖注入仍需单独收敛；本步只验证 fake-model 下的执行闭环。
