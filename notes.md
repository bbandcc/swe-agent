# 调研证据索引

记录日期：2026-09-08。结论以读取到的固定提交源码为准；可运行性与收益需区分静态核查和实际验证。

## 目标
- https://github.com/langtalks/swe-agent

## 证据
### 目标仓库
- 固定提交：`5946af4f57cba03761015837ad5f87ef5c8d99e9`，提交日期 2026-03-28。
- 完整递归文件树已读取，无 tests/ 或 .github/workflows/；README 声明与源码实现分开判断。
- `uv.lock` 实际锁定 LangGraph 0.6.3、langchain-core 0.3.72、langchain-anthropic 0.3.5、gitingest 0.1.2、tree-sitter 0.21.3。
- `agent/graph.py:14-29`：两阶段顺序图，结束没有验证节点、没有显式 checkpointer。
- `agent/architect/graph.py:154-162`：同节点同时存在条件边与普通边。已核查 LangGraph 0.6.3 的 `graph/state.py:879-892`，编译分别挂载普通边和条件分支。
- `agent/developer/graph.py:127-191`：模型调用、正则解析和文件写入位于一个节点；只读取 old snippet 行号，没有比较旧内容；每个块单独写入。
- `agent/tools/search.py:88-102`：对外搜索工具固定 .py，非 embedding 语义检索；无结果数量上限。
- `agent/tools/codemap.py:17-52,113-148`：声明多种语言，但共享 Python grammar 查询；JS/TS 是否具体报错需装依赖验证。
- `agent/developer/graph.py:111-125` 与 architect 对应转换：只保留第一个 tool_call 的描述，并把 ToolMessage 转成人类消息。

### 本地诊断
- `research/reproduce_baseline.py`：从固定源码抽取原函数 AST，替换模型响应后，在临时文件上执行；未导入整个项目、未调用模型。
- 四项原始行为已复现，见 `research/baseline-diagnostics.json`。
- 从 Deep Agents 固定源码抽取 `perform_string_replacement`，唯一匹配成功、未命中拒绝、多命中拒绝、空匹配拒绝四项契约检查通过。空字符串错误文案常量以占位字符串注入；未测试其完整 backend。

### Deep Agents：实现与限制
- 固定提交 `18106be837bcdd9005b2dd73280d871a0621bf99`，读取时 main，提交日期 2026-09-07；源码项目版本 0.7.13，MIT。
- [工具结果协议](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/deepagents/backends/protocol.py#L278-L310)：WriteResult/EditResult，带 error/path/occurrences。
- [精确替换](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/deepagents/backends/utils.py#L521-L578)；[原项目测试](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/tests/unit_tests/backends/test_utils.py#L415-L459)。唯一匹配不能替代文件哈希校验、事务或测试验收。
- [文件后端](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/deepagents/backends/filesystem.py#L132-L223)：virtual_mode 的路径约束不等于沙箱。其 write/edit 也不能作为整批原子写入实现来宣传。
- [路径逃逸测试](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/tests/unit_tests/backends/test_filesystem_backend.py#L902-L928)。Windows junction/symlink 行为需另测。
- [上下文压缩与外存](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/deepagents/middleware/summarization.py)；[存储失败行为测试](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/tests/unit_tests/middleware/test_summarization_middleware.py#L1215-L1243)：尽管测试名写 aborts，实际断言是告警后仍压缩、file_path=None，不能仅凭名称判断。只能证明机制存在，不能推出本项目采用后 token 或成功率必然改善。
- [依赖](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/pyproject.toml#L22-L30)：langchain>=1.4、langchain-core>=1.6.2，与目标锁定的 0.3 系列不同；优先借鉴契约，不直接整体迁移。

### LangGraph：实现与限制
- 固定参考提交 `81bf17b23123e4ef8b9d5f49fa09a0122fc2edd1`。
- [SQLite saver](https://github.com/langchain-ai/langgraph/blob/81bf17b23123e4ef8b9d5f49fa09a0122fc2edd1/libs/checkpoint-sqlite/README.md)：适合本地、测试与轻量部署。使用文件路径才是跨进程持久化，不能把 :memory: 示例当持久化方案。
- [中断恢复测试](https://github.com/langchain-ai/langgraph/blob/81bf17b23123e4ef8b9d5f49fa09a0122fc2edd1/libs/langgraph/tests/test_interruption.py#L11-L50)：同 thread_id 恢复和状态历史有明确断言。
- 源码未显式配 saver 不代表 LangGraph 托管运行时绝对没有持久化；本项目缺少独立运行的配置与恢复证明，需分别验收。

### 独立参考调研
- mini-swe-agent、Aider、SWE-bench 的固定版本及实现细节见 `research/reference-projects.md`。

### SWE-bench 官方 harness 补充核查
- 固定提交 `02e7a74ffd0b707aab73d203fe87bdc7c76afc8e`；[MIT 许可](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/LICENSE)。独立报告中 SWE-bench 部分主要是 mini-swe-agent 的 runner，本节补充官方评测实现。
- [grading.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py#L287-L326)：FAIL_TO_PASS 和 PASS_TO_PASS 都达到要求才 FULL，不能以 patch 已应用作为 resolved。
- [run_evaluation.py](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/run_evaluation.py)：创建容器、应用补丁、执行带超时的评估并落盘结果。未在本轮运行。

## 证据边界
没有进行真实模型端到端运行、完整参考项目测试套件、Docker 沙箱测试或 SWE-bench 评测。方案收益属于待验证目标，诊断结果不代表完整性能基线。

## 第一步实现记录（2026-09-09）

- 已将上游固定提交 `5946af4f57cba03761015837ad5f87ef5c8d99e9` 的源码和资源导入当前仓库；依赖锁和 6 个静态图片的 Git blob 一致。README/pyproject 为有意修改，`.gitignore` 合并本地规则，其余文本去除行尾/EOF 空白并统一换行后与上游内容一致。
- 新增 `agent.editing` 深模块，公开 interface 为 `WorkspaceEditor.apply(EditProposal) -> EditResult`；接口行为与限制见 `research/step-1-reliable-editor.md`。
- 参考 Deep Agents 的唯一匹配和结构化结果，但实现没有复制其代码；增加 SHA-256 预检、提交前复查、路径约束和单文件原子替换。

### Developer 接入实现

- `WorkspaceEditor.snapshot` 复用写入路径边界，返回 UTF-8 内容、存在性、哈希和结构化错误。
- `DeveloperEditExecutor` 兼容 Architect 的 `./workspace_repo/` 前缀，并把一个严格 SEARCH/REPLACE 块转换为 `EditProposal`；不采用 Aider 的首次匹配或跨文件 fallback。
- `DeveloperRuntime` 将模型调用、工作区和目录结构读取注入图构建器；fake model 可以执行真实 LangGraph 路由测试。
- Developer 只有收到 `EditStatus.APPLIED` 才推进；拒绝、noop、空计划和非法路径会停止并保留 `last_edit_result`。
- 实现与限制记录在 `research/step-1-developer-integration.md`。
- TDD 期间实际观察到 red：模块缺失、哈希错误抛异常、多/零命中抛异常、`../` 成功改写工作区外文件、缺失文件抛异常、noop 被当 applied、create 未实现/覆盖冲突、非 UTF-8 异常、CRLF 匹配失败、父路径为文件时异常。逐项实现后转绿。
- 最终 12 个编辑测试中 11 个通过；符号链接用例因当前 Windows 账户缺少创建权限跳过。路径逃逸和绝对路径测试已通过，junction 仍待具备相应环境的 CI 验证。
- `uv build --offline` 暴露上游缺少 setuptools 包发现配置；限制为 `agent*`、`helpers*` 并包含 prompts 后，sdist/wheel 构建成功。

## 完整项目导读补充

### 已核查范围
- GitHub 插件确认上游默认分支最新提交仍为 `5946af4f57cba03761015837ad5f87ef5c8d99e9`；本地 README 的 Git blob SHA 与上游一致。
- 已逐文件读取主图、Architect/Developer 子图、三类状态、全部工具、全部实际加载的提示词、Prompt 加载器、运行配置与依赖锁。
- 核心源码通过 Python 语法编译检查；仓库没有 tests/、CI workflow 或 pyproject 开发依赖，不能把 README 中的 pytest/black/mypy 命令当成已经可运行的项目能力。

### 解释主线
- 真实执行链：用户消息写入 `implementation_research_scratchpad` → Architect 反复提出/评价/调查假设 → 输出 `ImplementationPlan` → Developer 按 file task 与 atomic task 双层下标推进 → 每个原子任务先研究再生成文本编辑块 → 直接写文件 → 下标耗尽即结束。
- 两个所谓 agent 实际是两个顺序执行的 LangGraph 子图，使用同一个固定 Claude Sonnet 4 型号；它们通过结构化计划传递结果，没有并行协作、评审或回退链。
- 项目最深的模块是 LangGraph 编排与 Pydantic 计划契约；最薄弱的 seam 是 Developer 的编辑落盘节点，它同时承担模型输出解析、定位、修改与写入。

### 技术栈真实用途
- Python 3.12、uv/pyproject/uv.lock：运行语言与依赖复现。
- LangGraph 0.6.3、langgraph-cli 0.3.6：状态图、条件路由、ToolNode、开发服务器与图注册。
- langchain-core 0.3.72、langchain-anthropic 0.3.5：消息、Prompt、工具装饰器、输出解析和 Claude 适配器。
- Pydantic 2.10.6：状态、ResearchStep/ResearchEvaluation、ImplementationPlan 数据校验。
- Gitingest 0.1.2：扫描 `./workspace_repo` 并返回目录树；当前每次还计算未使用的 summary/content。
- Tree-sitter 0.21.3 与 tree-sitter-languages 1.10.2：符号/函数提取；当前查询只按 Python AST 节点编写，JS/TS 映射不能等同于可靠多语言支持。
- LangSmith 0.4.11：通过环境变量启用链路追踪，业务源码没有直接调用。
- diff-match-patch、thefuzz、fuzzysearch：锁定为直接依赖，但当前业务路径没有实际使用；`diff_match_patch()` 只被实例化后闲置。

### 重要事实与缺口
- Anthropic 官方退役表确认源码硬编码的 `claude-sonnet-4-20250514` 已于 2026-06-15 退役，请求会失败；官方建议 `claude-sonnet-4-6`。因此固定提交当前不能原样端到端运行。
- README 所称 semantic search 实际为 Python 文件的大小写不敏感字面量检索；所称 validation 没有测试/构建节点；所称 resumability 没有显式 checkpointer。
- Architect 的 `check_research_step` 同时配置条件边和直连 `conduct_research` 的普通边，无效研究并不会被可靠阻断。
- Developer 只利用模型返回 old snippet 的首末行号，不校验原文；同一响应多块编辑会发生行号漂移；无法解析编辑块时仍推进。
- 新文件写入不创建父目录，现有文件以平台默认编码读写并统一换行；路径没有限定在工作区内，也没有事务、备份、测试或失败重试。
- `agent/prompts.py`、`agent/state.py`、`helpers/tools.py`、`developing_prompt.md` 及部分 runnable/依赖属于未接入或遗留代码。
