# langtalks/swe-agent 上游源码研究笔记

> 研究日期：2026-09-08
> 上游仓库：<https://github.com/langtalks/swe-agent>
> 固定基线：[`5946af4f57cba03761015837ad5f87ef5c8d99e9`](https://github.com/langtalks/swe-agent/commit/5946af4f57cba03761015837ad5f87ef5c8d99e9)（2026-03-28，`fix: ignore workspace_repo directory instead of just its contents`）
> 证据边界：只采用该固定提交的项目源码/README，以及 LangChain、LangGraph、uv、Tree-sitter、Pydantic、GitIngest 等项目的一手文档。文中“源码事实”与“README 宣称”刻意分开。

## 1. 一句话结论

这是一个 **Python + LangGraph + Claude** 的实验性自动改码 Agent：用户把需求作为消息传入后，顶层图先调用“架构师”子图研究 `./workspace_repo` 并产出结构化 `ImplementationPlan`，再调用“开发者”子图逐文件、逐原子任务地研究并直接改写目标仓库。顶层流程是严格串行的 `START → architect → developer → END`，不是多个 Agent 同时协作。[源码：顶层图 L9-L29](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/graph.py#L9-L29)

项目定位仍是 **alpha 原型**。README 明确说功能会变化且部分能力是实验性的；源码也缺少测试、回滚、安全边界、执行后验证等生产级环节。[README：状态与功能](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/README.md#L7-L34)

## 2. 项目要解决什么问题

目标是把“高层软件需求”自动转换为代码修改，分两阶段降低一次性生成整套改动的风险：

1. **Architect（研究与规划）**：读取目标仓库树，提出研究假设，判断假设是否值得研究，调用源码搜索/结构分析工具，最后将发现压成结构化实施计划。
2. **Developer（实施）**：按计划遍历每个文件级任务及其原子任务；每步先补充上下文，再让 Claude 生成新文件或带行号的替换片段，随后直接写盘。

README 将用途列为特性开发、修 Bug、重构、文档和测试，但这些是目标场景，不代表仓库已经具备测试执行或结果验收能力。[README：架构与用途](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/README.md#L36-L67)

## 3. 实际执行链路

```text
用户消息
  ↓ AgentState.implementation_research_scratchpad
顶层 swe_agent
  ↓
swe_architect
  1) 生成下一条研究假设（结构化 ResearchStep）
  2) 评价假设是否有效（结构化 ResearchEvaluation）
  3) Claude 决定是否调用搜索/代码地图工具
  4) ToolNode 执行工具；若仍有工具调用则循环
  5) 将研究记录解析成 ImplementationPlan
  ↓
swe_developer
  1) 任务下标归零
  2) 读取目标文件与整个 workspace_repo 文件树
  3) Claude + 工具循环，补足当前原子任务上下文
  4) 新文件：整文件生成并写入
     旧文件：生成带行号替换块，按切片直接覆盖
  5) 原子任务/文件任务下标推进，直至结束
  ↓
LangGraph 最终状态
```

顶层共享状态只有研究消息和实施计划；计划的数据契约是 `ImplementationPlan.tasks[] → ImplementationTask(file_path, logical_task, atomic_tasks[]) → AtomicTask(atomic_task, additional_context)`。[源码：共享实体 L4-L14](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/common/entities.py#L4-L14)

### 3.1 Architect 的核心代码

- 四条模型链均在模块加载时创建，并把模型硬编码成 `claude-sonnet-4-20250514`：两条用 `with_structured_output` 生成/评价研究假设，一条 `bind_tools` 做工具调用，一条解析实施计划。[源码：Architect runnable L29-L41](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/architect/graph.py#L29-L41)
- 每轮把 `./workspace_repo` 交给 GitIngest，只取文件树；研究工具只能查看代码定义、函数实现、原始文件以及进行关键词搜索。[源码：研究节点 L47-L86](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/architect/graph.py#L47-L86)
- 模型产生工具调用时进入 `ToolNode`，工具结果追加到消息 scratchpad；不再请求工具时，使用 JSON 输出解析器再构造 Pydantic `ImplementationPlan`。[源码：计划提取与路由 L88-L127](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/architect/graph.py#L88-L127)
- 图设计意图是“假设无效则重提、有效则研究；研究与工具循环后输出计划”。但源码同时为 `check_research_step` 添加了条件边和一条无条件到 `conduct_research` 的边，见限制章节。[源码：Architect 图 L141-L174](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/architect/graph.py#L141-L174)

### 3.2 Developer 的核心代码

- 开始时把主任务/原子任务索引都置 0；每完成一个原子任务就推进索引，文件内完成后切到下一文件。[源码：索引推进 L33-L63](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/developer/graph.py#L33-L63)
- 每个原子任务先读目标文件；不存在时只把内容标记为 `This is a new file`。随后通过 Claude 的工具调用循环查看相关代码，直到模型认为信息足够。[源码：准备与研究路由 L66-L109](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/developer/graph.py#L66-L109)
- 新文件路径直接 `open(..., "w")` 写入。旧文件先给每行加 `行号|`，要求模型输出 `<code_change_request>`；程序用正则提取首尾行号，然后以 Python 列表切片替换并整文件覆盖。[源码：实际写盘 L128-L193](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/developer/graph.py#L128-L193)
- Developer 图是 `准备 → 研究/工具循环 → 写盘 → 推进索引 → 下一步/结束`。[源码：Developer 图 L197-L240](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/developer/graph.py#L197-L240)

## 4. 技术栈及每项用途

直接依赖声明以固定提交的 [`pyproject.toml`](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/pyproject.toml#L1-L18) 为准；`uv.lock` 当时解析为：`langchain-anthropic 0.3.5`、`langgraph 0.6.3`、`langgraph-cli 0.3.6`、`langsmith 0.4.11`、`gitingest 0.1.2`、`tree-sitter 0.21.3`、`tree-sitter-languages 1.10.2`、`diff-match-patch 20241021`、`thefuzz 0.22.1`、`fuzzysearch 0.7.3`；Pydantic 是传递依赖，锁定为 `2.10.6`。[锁文件](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/uv.lock)

| 技术 | 在本项目中的实际用途 | 说明 |
|---|---|---|
| Python 3.12+ | 全部业务代码、文件 I/O、正则切片改写 | 项目元数据明确要求 `>=3.12`。 |
| LangGraph | 定义顶层、Architect、Developer 三张状态图；节点、固定边、条件边、工具节点和循环 | 官方把 Graph API 概括为 State、Nodes、Edges；本项目正按此模式实现。[官方文档](https://docs.langchain.com/oss/python/langgraph/graph-api) |
| LangGraph CLI `[inmem]` | 读取 `langgraph.json` 并用 `langgraph dev` 启动本地开发服务器/Studio；`inmem` 是本地内存运行后端 | 配置公开 `agent`、`architect`、`developer` 三张图，并指定 `.env`。[源码](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/langgraph.json#L1-L9) |
| LangChain Core | 消息类型、PromptTemplate/ChatPromptTemplate、输出解析器、`@tool`、Runnable 管道 | 它是模型、Prompt、工具和 LangGraph 之间的接口层。 |
| `langchain-anthropic` / Claude | 所有规划、研究判断、工具选择、计划 JSON、diff 块和新文件内容生成 | 需要 `ANTHROPIC_API_KEY`；官方确认 `ChatAnthropic` 支持工具调用与结构化输出。[官方文档](https://docs.langchain.com/oss/python/integrations/chat/anthropic) |
| Pydantic | 定义图状态、研究输出与实施计划的数据结构，解析时进行字段/类型校验 | 官方文档说明 `BaseModel` 以类型注解驱动校验和序列化。[官方文档](https://pydantic.dev/docs/validation/latest/get-started/) |
| GitIngest | 扫描 `workspace_repo`，返回目录树作为每轮模型上下文 | 官方 Python API 返回 `(summary, tree, content)`；本项目只使用 `tree`。[官方文档](https://github.com/coderamp-labs/gitingest/blob/main/README.md) |
| Tree-sitter + `tree-sitter-languages` | 对 Python/JS/JSX/TS/TSX 建语法树，用 S-expression query 提取类、函数和方法 | Tree-sitter query 用结构化节点模式匹配语法树，而非文本正则。[官方文档](https://tree-sitter.github.io/tree-sitter/using-parsers/queries/1-syntax.html) |
| uv + `uv.lock` | 创建 `.venv`、解析/同步依赖、锁定可复现版本 | 官方说明 `uv sync` 会按项目元数据和 lockfile 同步环境。[官方文档](https://docs.astral.sh/uv/concepts/projects/sync/) |
| LangSmith | 可选的模型调用 tracing/调试 | 源码没有直接 import；仓库的 `.env.example` 通过 `LANGCHAIN_TRACING_V2`、`LANGCHAIN_API_KEY`、`LANGCHAIN_PROJECT` 配置。[源码](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/.env.example) |
| `diff-match-patch` | **当前有效流程未使用** | 仅 import 并实例化 `dmp`，后续改写仍是行号切片；不能把它描述成当前 diff 应用引擎。[源码 L5、L32](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/developer/graph.py#L5-L32) |
| `thefuzz`、`fuzzysearch` | **当前源码未使用** | 在依赖表中存在，但项目 Python 源码没有 import；可能是遗留或预留依赖。 |

## 5. 目录与模块职责

```text
agent/
├─ graph.py                 顶层串行编排：Architect → Developer
├─ state.py                 未被主流程引用的通用 State（疑似遗留）
├─ prompts.py               未被主流程引用的旧 Prompt 模块（疑似遗留）
├─ common/entities.py       Architect/Developer 之间的计划数据契约
├─ architect/
│  ├─ graph.py              假设生成、有效性判断、工具研究、计划提取
│  ├─ state.py              Architect 状态
│  └─ prompts/*.md          上述四阶段的 Markdown Prompt
├─ developer/
│  ├─ graph.py              原子任务循环、研究、diff 生成和直接写盘
│  ├─ state.py              Developer 状态与消息清空 reducer
│  └─ prompts/*.md          实施研究、diff、新文件 Prompt
└─ tools/
   ├─ search.py             递归、大小写不敏感的 Python 关键词搜索
   ├─ codemap.py            Tree-sitter 结构查询/函数实现/原文读取
   └─ write.py              文件创建/覆盖工具及 GitIngest 目录树

helpers/prompts.py          解析 Markdown 元数据，校验变量并构造 PromptTemplate
helpers/tools.py            工具描述格式化；主流程未引用
scripts/setup.py            从已有 .env 去值生成 .env.example
langgraph.json              LangGraph CLI 的图入口与 env 文件配置
langgraph_debug.py          导出子图并可直接调用 CLI dev
pyproject.toml              Python 元数据和直接依赖
uv.lock                     完整锁定依赖树
static/                     README 架构图、输入/输出截图
workspace_repo/             运行时由用户克隆的“待修改仓库”，已被 .gitignore 忽略
```

工具边界要特别注意：关键词搜索函数固定过滤 `.py`；代码地图虽然声称支持 Python、JavaScript、JSX、TypeScript、TSX，但不支持 Java、Go 等其他语言。[搜索源码 L93-L102](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/tools/search.py#L93-L102)；[代码地图语言表 L15-L27](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/tools/codemap.py#L15-L27)

## 6. 安装与运行：源码一致版

README 的 Quick Start 存在旧仓库名和 env 文件名错误，因此建议按当前仓库事实运行：

```powershell
git clone https://github.com/langtalks/swe-agent.git
Set-Location swe-agent

# 已安装 uv 的前提下，严格使用仓库锁文件同步
uv sync --locked

# langgraph.json 实际读取 .env
Copy-Item .env.example .env
# 然后填写：
# ANTHROPIC_API_KEY=...
# 可选 tracing：LANGCHAIN_TRACING_V2=true
#                LANGCHAIN_API_KEY=...
#                LANGCHAIN_PROJECT=...

git clone https://github.com/browser-use/browser-use.git .\workspace_repo

# 无需手工激活虚拟环境
uv run langgraph dev
```

随后从 LangGraph 开发界面选择 `agent` 图，把用户需求放进 `implementation_research_scratchpad` 的消息列表。图注册入口见 [`langgraph.json`](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/langgraph.json#L1-L9)。不过截至研究日期，若不先把源码中的硬编码模型迁移到可用模型，真正调用 Agent 会失败，见下一节。

若选择手工激活 Windows 环境，应使用 `.\.venv\Scripts\Activate.ps1`；README 的 `source .venv/bin/activate` 只适用于类 Unix shell。README 当前给出的 clone URL 仍是旧名 `swe-agent-langgraph.git`，并要求把确实存在的 `.env.example` 复制成 `.env.local`，但下一行又要求把 Key 写入 `.env`，而 `langgraph.json` 实际也读取 `.env`；因此应直接复制为 `.env`。[README Quick Start](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/README.md#L202-L239)；[`.env.example`](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/.env.example)

`scripts/setup.py` 也不是首次安装的完整解决方案：它只能在 `.env` 已存在时，反向生成去值后的 `.env.example`。[源码：setup L5-L35](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/scripts/setup.py#L5-L35)

## 7. 已知限制与源码风险

### 7.0 当前原样运行的阻塞：硬编码模型已退役

Architect 与 Developer 共用硬编码的 `claude-sonnet-4-20250514`，不能通过 `.env` 改模型。[Architect L35-L39](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/architect/graph.py#L35-L39)；[Developer L22-L31](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/developer/graph.py#L22-L31) Anthropic 官方记录显示该模型已于 2026-06-15 退役，退役模型请求会失败，官方给出的替代项是 `claude-sonnet-4-6`。[Anthropic 模型退役文档](https://platform.claude.com/docs/en/about-claude/model-deprecations) 因而固定提交只能用于研究/复现源码，若要实际执行，必须先进行模型兼容性迁移并验证旧版 `langchain-anthropic 0.3.5` 与新模型组合。

### 7.1 文档与仓库不一致

- README/CONTRIBUTING 的 clone 地址仍使用 `langtalks/swe-agent-langgraph.git`，当前仓库是 `langtalks/swe-agent`。
- `.env.example` 与 `.python-version` 在固定提交中都存在；真正的问题是 README 让用户复制成 `.env.local`，而 `langgraph.json` 配置读取 `.env`。`.python-version` 明确写的是 `3.12`。[`.python-version`](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/.python-version)；[`langgraph.json`](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/langgraph.json#L8)
- README/CONTRIBUTING 宣称可运行 `pytest`、coverage、Black、isort、mypy、flake8，并举出 `tests/test_architect.py`，但固定提交没有 `tests/`，`pyproject.toml` 也没有这些开发依赖或 dev dependency group。[README 测试说明](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/README.md#L284-L295)；[实际依赖](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/pyproject.toml#L7-L18)

### 7.2 安全边界不足

- 计划中的 `file_path` 来自 LLM；Developer 直接对该路径 `open` 读写，没有把解析后的路径强制限制在 `workspace_repo` 内，也没有 symlink/`..` 穿越防护。[源码：直接文件 I/O L89-L100、L128-L193](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/developer/graph.py#L89-L193)
- Prompt 虽要求路径以 `./workspace_repo/` 开头，但这只是语言指令，不是程序级安全校验。[计划 Prompt](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/architect/prompts/extract_implementation_plan.md#L23-L37)
- 没有备份、临时文件原子替换、Git 分支/commit、用户确认、权限沙箱或回滚机制；运行前应在干净工作树/隔离副本中使用。

### 7.3 改码可靠性不足

- 旧文件修改依赖 LLM 输出 XML-like 标签和行号，程序只用正则提取首尾行号，不验证 `original_code_snippet` 是否真的与磁盘内容匹配。
- 同一原子任务多个替换块都基于修改前的行号生成，却依次修改同一文件；前一个块增删行后，后续块的行号可能漂移。
- `diff-match-patch` 虽实例化却未使用；不存在模糊匹配、冲突检测、patch 成功标志。
- 新文件写入前不创建父目录；尽管 `agent/tools/write.py` 的 `create_file` 会 `makedirs`，Developer 并未调用它。[写工具源码 L6-L25](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/tools/write.py#L6-L25)
- 独立 `create_file` 的 docstring 说“文件已存在时拒绝覆盖”，实现却没有存在性判断并直接以 `w` 模式覆盖；若创建仓库根目录文件，`os.path.dirname(path)` 为空时还可能在 `makedirs("")` 失败。该工具当前虽未绑定主图，仍说明写入层尚未收敛。
- 没有格式化、lint、类型检查、编译、测试或执行结果验证节点；“Developer validates changes”是 README 描述，不是当前图中的可见执行步骤。

### 7.4 工作流边界问题

- Architect 的 `check_research_step` 同时存在条件边和无条件 `→ conduct_research` 边；当评价为无效时，源码仍可能并行触发研究分支，并与“重新提出假设”分支同时更新 scratchpad。这与注释表达的互斥意图不一致，至少属于高风险图定义。[源码 L152-L170](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/architect/graph.py#L152-L170)
- 顶层和两个子图编译时都没有显式 checkpointer；项目源码本身没有实现可恢复 checkpoint。README 所称“Resumability”不能仅凭当前图代码视为已实现。[顶层 compile L29](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/graph.py#L29)
- 空计划、空 `atomic_tasks`、模型 JSON/XML 格式错误、API/工具失败都没有显式恢复路径；多处直接索引 `[0]` 或当前下标，容易以异常终止。
- `recursion_limit=200` 只是防止循环无限增长的上限，不代表循环具有收敛判定；研究是否结束完全由模型“是否继续调用工具”决定。

### 7.5 能力范围与工程成熟度

- 模型 ID硬编码，不能仅靠 `.env` 切换供应商/模型；所有推理均同步 `.invoke()`，没有并行任务或批处理。
- 搜索只查 Python 文件；Tree-sitter 虽把五个后缀映射到 Python/JS/TS parser，却对所有语言复用 Python 风格的 `class_definition`、`function_definition`、`block` query，因此 JS/TS 定义提取并不可靠；所谓“Multi-Language Support”仍位于 README Roadmap，未实现。[查询源码 L15-L53](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/agent/tools/codemap.py#L15-L53)；[Roadmap](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/README.md#L318-L345)
- `thefuzz`、`fuzzysearch`、`diff-match-patch` 当前没有形成有效能力；`edit_according_to_diff_runnable` 也创建后未调用，存在明显原型/遗留代码。
- 项目包元数据描述仍是 `Add your description here`，没有 build system 或命令 entry point，主要依赖在仓库根目录运行 LangGraph CLI。[源码：pyproject L1-L18](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/pyproject.toml#L1-L18)
- 除 `tree-sitter` 外，直接依赖基本只设下限、没有主版本上限；若丢弃/升级 `uv.lock` 后重新解析，可能获得不兼容的新主版本。复现固定基线应使用现有 lockfile 和 `uv sync --locked`。

## 8. 理解这个项目时最容易混淆的几点

1. **“多 Agent”不等于并行**：这里是两个职责不同的子图串行交接。
2. **“智能 diff”实际是行号替换**：模型生成替换块，Python 用切片重写整文件；未使用声明的 diff-match-patch 算法。
3. **结构化计划不等于安全执行**：Pydantic 能校验字段类型，不能保证路径安全、业务正确或代码可运行。
4. **LangGraph 提供编排骨架，不自动提供业务质量**：节点、状态、循环清晰，但测试、回滚、审计、权限和验收仍需项目自己实现。
5. **路线图不是现状**：测试 Agent、错误修复 Agent、GitHub MCP、语义索引、多语言支持、交互式 UI 等均在未勾选 Roadmap 中。

## 9. 一手资料索引

- [固定提交](https://github.com/langtalks/swe-agent/commit/5946af4f57cba03761015837ad5f87ef5c8d99e9)
- [项目 README（固定提交）](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/README.md)
- [依赖声明（固定提交）](https://github.com/langtalks/swe-agent/blob/5946af4f57cba03761015837ad5f87ef5c8d99e9/pyproject.toml)
- [LangGraph Graph API 官方文档](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [ChatAnthropic 官方集成文档](https://docs.langchain.com/oss/python/integrations/chat/anthropic)
- [uv 同步与 lockfile 官方文档](https://docs.astral.sh/uv/concepts/projects/sync/)
- [Tree-sitter Query 官方文档](https://tree-sitter.github.io/tree-sitter/using-parsers/queries/1-syntax.html)
- [Pydantic 官方文档](https://pydantic.dev/docs/validation/latest/get-started/)
- [GitIngest 官方仓库文档](https://github.com/coderamp-labs/gitingest/blob/main/README.md)
