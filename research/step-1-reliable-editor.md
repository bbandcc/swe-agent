# 第一步：可校验编辑内核

完成日期：2026-09-09。

## 目标与范围

本步把模型提出的文件修改收敛到一个确定性 seam：

```python
WorkspaceEditor.apply(EditProposal) -> EditResult
```

本文件记录编辑内核自身的交付；后续 LangGraph 接入已在 [step-1-developer-integration.md](step-1-developer-integration.md) 完成，失败重试和测试执行器仍留作独立切片。

## 已实现行为

- `EditProposal` 明确工作区相对路径、edit/create 操作、基线 SHA-256、旧文本和新文本。
- edit 只接受非空且唯一命中的旧文本；零命中和多命中结构化拒绝。
- 写入前核查文件 SHA-256，临时文件完成并刷盘后再次核查，降低读取后文件变化造成的覆盖风险。
- 已有文件使用同目录临时文件和 `os.replace`；新文件使用完成写入后的临时文件和独占 hard link，避免覆盖同名文件。
- 拒绝绝对路径、`..`、符号链接和 Python 可识别的 Windows junction；解析后的目标必须位于工作区根目录内。
- 只编辑 UTF-8 文本；统一 CRLF/LF 匹配，同时恢复原文件的统一换行风格。
- `EditResult` 区分 applied、rejected、noop，并返回错误码、前后哈希和 unified diff。

## 参考依据

- Deep Agents 固定提交 [`18106be8`](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/deepagents/backends/utils.py#L521-L578) 的唯一匹配与明确错误契约。
- Deep Agents 的 [`EditResult`](https://github.com/langchain-ai/deepagents/blob/18106be837bcdd9005b2dd73280d871a0621bf99/libs/deepagents/deepagents/backends/protocol.py#L294-L310) 结构化结果思路。
- Aider 固定提交 [`5dc9490b`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L21-L183) 的可操作失败反馈。本项目没有照搬其首次匹配和跨文件 fallback。

哈希预条件、提交前复查和单文件原子替换是本项目新增的实现选择，不表述为参考项目已经提供的保证。

## 验证结果

- `python -m unittest discover -s tests -v`：12 个测试，11 通过，1 个因当前 Windows 账户无创建符号链接权限而跳过；路径逃逸和绝对路径用例已执行通过。
- `python -m compileall -q agent helpers scripts langgraph_debug.py research/reproduce_baseline.py`：通过。
- `python research/reproduce_baseline.py`：原版四项缺陷仍可复现，作为后续 Developer 接入的对照。
- `uv build --offline`：首次暴露上游包自动发现失败；增加 setuptools 包发现与 prompt 资源配置后，sdist 和 wheel 均构建成功。
- 敏感值模式扫描：没有命中；ruff 与 pyright 当前未安装，因此没有虚报 lint/type-check 通过。

## 已知限制与下一步

- 文件仍可能在最后一次哈希核查与 `os.replace` 之间发生极短竞态；本模块不声称提供跨进程锁或跨文件事务。
- 原子创建依赖同一文件系统支持 hard link；不支持时返回 `write_failed`，不会退回到可能留下半文件的直接写入。
- Windows 符号链接测试因权限跳过；junction 拒绝代码需要在能创建 junction 的 CI 环境补充运行验证。
- Developer 接入已完成；下一步加入测试命令验收、基线失败区分和有限修复循环。
