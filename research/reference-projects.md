# 参考项目源码调研：mini-swe-agent 与 Aider

调研日期：2026-09-08。本文件只给出可移植机制及验证要求，不修改目标项目业务代码。证据来自 GitHub 插件读取的官方仓库、固定提交源码及测试。**本轮未安装或运行参考项目；“已有实现与测试”不等于“已验证移植后更优”。**目标项目缺口与最终排序由主报告对照目标源码确定。

## 固定版本与范围

| 项目 | 固定提交 | 核查范围 | 依赖与复用边界 |
| --- | --- | --- | --- |
| mini-swe-agent | [`04d809ceab9df28f9adaed044884180159172930`](https://github.com/SWE-agent/mini-swe-agent/commit/04d809ceab9df28f9adaed044884180159172930)，2026-09-03 | 默认 loop、预算、错误处理、执行超时、trajectory、SWE-bench runner 与测试 | [MIT](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/LICENSE.md)；[包配置](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/pyproject.toml)要求 Python ≥3.10，主要涉及 Pydantic 2、Jinja2、LiteLLM、OpenAI、datasets；借鉴 loop 不需要整体引入其 CLI/UI 依赖。 |
| Aider | [`5dc9490bb35f9729ef2c95d00a19ccd30c26339c`](https://github.com/Aider-AI/aider/commit/5dc9490bb35f9729ef2c95d00a19ccd30c26339c)，2026-05-22 | SEARCH/REPLACE、失败反馈、repo map、对应单元测试 | [Apache-2.0](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/LICENSE.txt)；repo map 关联 Tree-sitter/grep_ast、networkx、diskcache 等，见[依赖清单](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/requirements/requirements.in)。先学习模块契约；若复制代码，保留相应许可证、署名与修改说明。 |

## 机制一：有界 agent loop 与可以恢复的工具错误

mini-swe-agent 将模型查询、动作执行和观察构造分开；`run → step → query → execute_actions` 是小型顺序循环。`query()` 在新调用前检查 step/cost/wall time，调用后累计真实费用。`run()` 单独处理格式错误，累计连续错误数，并把解析失败但已经计费的响应费用计入预算；成功步骤会清空连续格式错误计数。达到阈值输出明确终止状态，避免模型反复产生非法动作。[实现：default.py L88–157](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py#L88-L157)

环境层将执行错误转换为含 `output/returncode/exception_info` 的观察；超时仍保留部分输出，给下一轮恢复提供证据。POSIX 超时时杀进程组；非 POSIX 分支只调用 `process.kill()`，不能宣称 Windows 子进程树也被完整清理。[实现：local.py L24–42、L72–92](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/environments/local.py#L24-L92)

**可移植设计建议**：在目标项目现有 loop 外增加一个轻量 `RunBudget`/终止状态契约；工具统一返回结构化成功/失败结果，预期失败供模型修正，系统异常保留 traceback 后退出。为格式错误、工具错误和模型网络重试分别设置计数，避免使用同一个无限重试分支。仅在已有失败样例证明需要时再加入相同动作去重。

**已存在的测试依据**：同一 fixture 覆盖文本动作、tool calling、Responses API 三类模型；有 step/cost 限制、超时恢复及部分输出测试，以及连续格式错误终止、成功后计数归零、错误响应计费测试。[test_default.py L112–230](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/tests/agents/test_default.py#L112-L230)、[L410–544](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/tests/agents/test_default.py#L410-L544)

**不能照搬的边界**：成本检查发生在下一次请求前，因此最后一笔请求可能使总费用超过阈值；wall time 检查也不会抢占正在执行的模型请求或工具。本项目若要求严格上限，还需单请求超时、输出 token 上限与预留额度。不要把预算终止当成任务完成。

**移植后的验收**：不调用真实模型，用确定性响应覆盖“无效参数→纠正”“超时→读取残留输出→恢复”“达到预算→不再发起调用”“错误格式连续达到阈值→准确退出”。检查错误响应是否计费，终止状态是否能由 API/UI 正确展示。这一方向优先于增加 agent 数量。

## 机制二：每步 trajectory 与可复现的小规模评测

mini-swe-agent 在每一步的 `finally` 保存轨迹；结构包含模型成本、API 次数、配置、agent/model/environment 类型、版本、消息、exit_status 与 submission，并带 `trajectory_format`。这让预算退出与异常也能留下诊断材料。[default.py L96–124、L159–190](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/agents/default.py#L96-L190)

SWE-bench runner 按 instance 创建运行目录与环境，运行后无论成功异常都会保存轨迹及预测文件；预测结构含 `instance_id/model_name_or_path/model_patch`。这是一条真实的批量任务入口，不只是 README 中提及 benchmark。[swebench.py L68–108](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/benchmarks/swebench.py#L68-L108)、[L122–177](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/src/minisweagent/run/benchmarks/swebench.py#L122-L177)

**可移植设计建议**：目标项目先实现独立 `RunRecorder`，保存 request/run ID、目标 Git commit、模型和推理配置、token/费用/耗时、工具参数与结果、patch、最终验证结果。模型上下文可以裁剪，完整事件记录独立保存。用同一组真实仓库任务、同一模型设置、同一预算比较原版和单项改版。固定任务及重复次数后，再计算任务解决率、有效 patch 率、恢复率、平均成本与耗时；样本较小时只称为内部回归集。

**已存在的测试依据**：`test_save.py` 验证保存后的类型信息和格式版本；`test_swebench_end_to_end` 用确定性模型运行测试仓库实例、检查预测和 trajectory；另有异常状态落盘测试。端到端用例需要其容器 fixture，不能据此声称本地环境已能运行。[test_save.py](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/tests/run/test_save.py)、[test_swebench.py L36–77](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/tests/run/test_swebench.py#L36-L77)、[L463–501](https://github.com/SWE-agent/mini-swe-agent/blob/04d809ceab9df28f9adaed044884180159172930/tests/run/test_swebench.py#L463-L501)

**不能照搬的边界**：`save()` 是整份 JSON `write_text`，不代表原子持久化、崩溃恢复、任务恢复或副作用去重；记录配置还需要脱敏。runner 生成 patch 与预测并不等于通过 SWE-bench 官方 harness，`Submitted` 更不代表问题被解决。若要在简历写 SWE-bench 数字，需实际跑对应数据集与官方评测，并说明版本、子集、模型和预算。

**移植后的验收**：中途工具异常、预算退出、进程终止后能得到明确完整或可判定不完整的运行记录；记录不含 API Key；同一回归任务能重放模型 fixture；测试验证结果与模型自报完成分离。第一版只做可诊断记录与对比数据，不承诺任意断点恢复。

## 机制三：编辑失败反馈与显式编辑验证

Aider `EditBlockCoder` 解析 SEARCH/REPLACE，尝试精确匹配及有限前导空白修正；失败时给出实际文件的相似片段，提示替换内容是否已存在，并告诉模型哪些块已成功、只重发失败块。`apply_edits_dry_run` 提供不写回文件的路径。[editblock_coder.py L21–124](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L21-L124)、[L134–183](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_coder.py#L134-L183)

**可移植设计建议**：先给目标编辑工具增加精确结构化错误：文件不存在、匹配为零、匹配多处、当前内容已变化；返回候选行范围，让模型重新读取后修正。以明确文件路径和旧内容/旧内容哈希为输入，在内存中生成 diff，通过适用的语法检查与测试后完成一次编辑。这里“哈希保护、唯一匹配和原子写入”是本项目应验证的设计要求，不能标作 Aider 已实现的能力。

**必须特别区分**：Aider 精确匹配是替换第一处，并没有拒绝多个匹配；它在指定文件失败后会尝试聊天中的其他文件，已成功块可以在其他块失败前落盘；因此不能直接视为“严格唯一定位”“跨文件事务”。代码里的编辑距离 fuzzy replacement 位于无条件 `return` 后，不能误称它启用了任意模糊替换。最值得学的是可操作的错误观察与针对失败块的重试，自动跨文件 fallback 不建议直接移植。

**已存在的测试依据**：缺失文件名/不完整编辑块、空白差异、多个匹配只替换第一处、普通写入、dry-run 不改变原文件均有测试。[test_editblock.py L130–166](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/tests/basic/test_editblock.py#L130-L166)、[L249–321](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/tests/basic/test_editblock.py#L249-L321)、[L363–439](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/tests/basic/test_editblock.py#L363-L439)

**移植后的验收**：CRLF/中文/无末尾换行、旧文本零命中、多命中、两块互相影响、读取后文件改变、重试不重复写入。指标是编辑成功率、误改率、恢复轮数与最终测试通过率；只提高 patch 应用率却误改位置不算更优。

## 机制四：带预算的符号级 repo map

Aider 用 Tree-sitter query 抽取 definition/reference，构建文件间加权有向图，再做个性化 PageRank；用户提及文件/符号和已在聊天中的文件会影响权重。排序后的定义渲染为紧凑代码上下文，以二分选择接近 token 预算的片段；有文件 mtime 缓存与 map refresh 策略。它提供的是代码关系导航，不需要先部署 embedding 数据库。[repomap.py L233–264](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L233-L264)、[L279–363](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L279-L363)、[L365–574](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L365-L574)、[L576–706](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L576-L706)

**可移植设计建议**：保持 `list/search/read` 原有能力，在真正出现跨文件定位困难的回归任务上增加一个可选 `RepositoryContextProvider`。第一阶段仅支持目标语言、函数/类定义和导入关系，返回候选文件与行号，让 agent 按需读取正文；记录定位使用的上下文成本。比较“原有检索”与“符号 map + 原有检索”，不要立即替换全部检索。

**已存在的测试依据**：刷新策略、符号包含、排除已提供文件、多语言 fixtures 和 sample codebase 快照都有测试。[test_repomap.py L49–157](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/tests/basic/test_repomap.py#L49-L157)、[L163–271](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/tests/basic/test_repomap.py#L163-L271)、[L287–506](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/tests/basic/test_repomap.py#L287-L506)

**不能照搬的边界**：长文本 token 计数采用抽样估计，选图允许约 15% 接近误差，不能直接称严格 token 限额；刷新策略也可能暂时复用旧 map。解析器/查询对语言有支持边界。它尚不能证明比目标项目现有检索更准；对小仓库还可能增加无收益的解析与依赖成本。[计数实现 L89–101](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L89-L101)、[选择实现 L674–706](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L674-L706)

**移植后的验收**：符号跨文件变更后缓存刷新、删除/重命名文件、不支持语言回退、最终上下文真实 token 计数。固定一组跨文件任务统计目标文件 recall@k、读取轮数、首轮输入 token、任务解决率；只有实际定位收益覆盖复杂度才进入主线。优先级晚于 loop/错误闭环、编辑可靠性和运行记录。

## 本轮决策建议

先修“可控执行 + 可靠编辑 + 可诊断记录”，并同步建立小规模回归集；确认跨文件定位是剩余主要失败原因后，再试 repo map。以上机制均有现成源码与对应测试可以学习，但每个移植项仍应先形成一个小范围、可撤销的对照实验。简历价值来自自己完成的边界设计、故障处理及实测数据，不来自叠加项目名称或未复现的榜单成绩。
