
# SWE Agent with LangGraph - by [LangTalks](https://langtalks.ai)

<img src="./static/cover.png" width="400" alt="Cover">


![Alpha](https://img.shields.io/badge/status-alpha-orange) ![Python](https://img.shields.io/badge/python-3.12+-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Evolution status

This repository evolves the original LangTalks project through small, independently tested changes. S1 adds a shared workspace boundary and a deterministic file transaction to the Architect and Developer workflows. Model output is treated as a proposal: code validates the path, source hash, unique SEARCH/REPLACE match, encoding, and task identity. Atomic proposals for one file are staged on an in-memory working copy; the final version is committed once only when every proposal succeeds.

Run the deterministic and fake-model integration tests with:

```bash
uv run python -m unittest discover -s tests -v
```

See [UPSTREAM.md](UPSTREAM.md) for the pinned source revision and [AGENTS.md](AGENTS.md) for the incremental engineering rules.

A sophisticated AI-powered software engineering agent that automates code implementation through intelligent planning and execution. Built with LangGraph for reliable multi-agent workflows.

> ⚠️ **Alpha Status**: This project is in active development. Features may change and some functionality is experimental. Perfect for early adopters and contributors who want to shape the future of AI-powered development.

![Main Agent](./static/main_agent.png)

[end to end showcase](https://youtu.be/vJNqAgLzOSg)

## 🚀 Features

- **Intelligent Code Planning**: AI architect analyzes requirements and creates detailed implementation plans
- **Automated Code Generation**: Developer agent executes plans with precise file modifications
- **Multi-Agent Workflow**: Separate planning and implementation roles for better reliability
- **Codebase Understanding**: Workspace-bound literal search and tree-sitter code inspection
- **Incremental Development**: Atomic task proposals grouped into file transactions

## 🏗️ Architecture

The system uses a two-role LangGraph workflow:

### 1. Architect Agent - Research & Planning
![Architect Agent](./static/architect.png)

The architect agent:
- Researches the codebase structure and patterns
- Analyzes requirements and creates hypotheses
- Generates detailed implementation plans with atomic tasks
- Uses workspace-bound keyword search and tree-sitter structure tools

### 2. Developer Agent - Implementation
![Developer Agent](./static/developer.png)

The developer agent:
- Executes implementation plans step by step
- Applies hash-checked, unique SEARCH/REPLACE edits through a workspace boundary
- Creates new files and modifies existing ones
- Stops on rejected or no-op edits and exposes a structured result

### Workflow Overview
```
User Request → Architect → Baseline Verification → Developer
             → Post Verification → Bounded Repair (max 2) → WorkflowOutcome
```

**Key Components:**
- **State Management**: Structured data flow between agents using Pydantic models
- **Tool Integration**: File system operations, code search, and structure analysis
- **Research Pipeline**: Hypothesis-driven exploration of codebases
- **File Transactions**: Sequential in-memory staging followed by one checked commit

## 🔄 Agent State Management

The system uses a hierarchical state management approach with Pydantic models for type safety and validation. Each agent maintains its own state while sharing common entities for seamless data flow.

### Main Agent State (`AgentState`)

The top-level orchestrator state that manages the overall workflow:

```python
class AgentState(BaseModel):
    implementation_research_scratchpad: Annotated[list[AnyMessage], add_messages]
    implementation_plan: Optional[ImplementationPlan] = None
    last_edit_result: Optional[EditResult] = None
    developer_status: DeveloperStatus = DeveloperStatus.PENDING
    developer_error_code: Optional[DeveloperErrorCode] = None
```

**Fields:**
- `implementation_research_scratchpad`: Message history from research and planning phase
- `implementation_plan`: Structured plan created by architect agent for developer execution
- `last_edit_result`: Final applied, rejected, or no-op result returned by the Developer
- `developer_status`: Running, completed, explicit no-change, or failed terminal state
- `developer_error_code`: Developer-level plan/state error, separate from file-edit errors

### Architect Agent State (`SoftwareArchitectState`)

Manages the research and planning phase with hypothesis-driven exploration:

```python
class SoftwareArchitectState(BaseModel):
    research_next_step: Optional[str] = None
    implementation_plan: Optional[ImplementationPlan] = None
    implementation_research_scratchpad: Annotated[list[AnyMessage], add_messages] = []
    is_valid_research_step: Optional[bool] = None
```

**Fields:**
- `research_next_step`: Current hypothesis or research direction being explored
- `implementation_plan`: Generated structured plan with atomic tasks
- `implementation_research_scratchpad`: Research conversation history and tool outputs
- `is_valid_research_step`: Validation flag for research hypothesis quality

**Workflow:**
1. Generate research hypothesis → Validate hypothesis → Conduct research → Extract implementation plan

### Developer Agent State (`SoftwareDeveloperState`)

Handles the step-by-step implementation of the architect's plan:

```python
class SoftwareDeveloperState(BaseModel):
    implementation_plan: Optional[ImplementationPlan] = None
    current_task_idx: int = 0
    current_atomic_task_idx: int = 0
    atomic_implementation_research: Annotated[list[AnyMessage], add_messages_with_clear]
    codebase_structure: Optional[str] = None
    current_file_snapshot: Optional[WorkspaceSnapshot] = None
    current_file_transaction: Optional[WorkspaceTransaction] = None
    current_file_content: Optional[str] = None
    last_edit_result: Optional[EditResult] = None
    developer_status: DeveloperStatus = DeveloperStatus.PENDING
```

**Fields:**
- `implementation_plan`: Plan received from architect agent
- `current_task_idx`: Index of current file-level task being implemented
- `current_atomic_task_idx`: Index of current atomic change within the task
- `atomic_implementation_research`: Research specific to current implementation step
- `codebase_structure`: Current snapshot of target codebase structure
- `current_file_content`: Contents of file being modified
- `current_file_snapshot`: Validated path, content, and hash captured before model generation
- `current_file_transaction`: Original file plus the current in-memory working copy and staged task IDs
- `last_edit_result`: Structured result used to decide whether the graph may advance

**Workflow:**
1. Validate plan → Begin a file transaction → Research and stage each atomic proposal in memory → Commit once → Advance only on success

### Shared Entities

These Pydantic models provide the data contracts between agents:

#### `ImplementationPlan`
```python
class ImplementationPlan(BaseModel):
    status: PlanStatus = PlanStatus.READY
    no_change_reason: str = ""
    tasks: List[ImplementationTask]
```
The top-level plan containing all file-level implementation tasks. An empty list is valid only when `status="no_changes"` and `no_change_reason` explains why no edit is required.

#### `ImplementationTask`
```python
class ImplementationTask(BaseModel):
    file_path: str                    # Target file for modifications
    logical_task: str                 # High-level description of changes
    atomic_tasks: List[AtomicTask]    # Granular modification steps
```
Represents changes needed for a specific file.

#### `AtomicTask`
```python
class AtomicTask(BaseModel):
    atomic_task: str            # Specific code modification instruction
    additional_context: str     # Research context for this change
```
The smallest unit of implementation - a single code change.

#### `EditProposal` and `EditResult` (Developer-specific)
```python
WorkspaceEditor.apply(EditProposal) -> EditResult
```
`EditProposal` carries a stable task ID, workspace-relative path, operation, baseline hash, old text, and new text. `EditResult` reports applied, rejected, or no-op with task IDs, error codes, hashes, and the actual unified diff.

### State Flow Example

```
User Request
    ↓
AgentState {research_scratchpad: [HumanMessage("Add auth")]}
    ↓
SoftwareArchitectState {
    research_next_step: "Find existing auth patterns",
    implementation_research_scratchpad: [research_messages...]
}
    ↓ (after research)
SoftwareArchitectState {
    implementation_plan: ImplementationPlan([
        ImplementationTask(
            file_path: "auth.py",
            atomic_tasks: [AtomicTask("Add User model")]
        )
    ])
}
    ↓
SoftwareDeveloperState {
    implementation_plan: <received_plan>,
    current_task_idx: 0,
    current_atomic_task_idx: 0,
    current_file_snapshot: <validated path/content/hash>,
    last_edit_result: EditResult(status="applied")
}
    ↓ (after implementation)
Final Result: Modified codebase
```

### State Management Benefits

- **Type Safety**: Pydantic validation prevents state corruption
- **Traceability**: Complete message history for debugging
- **Explicit outcomes**: Rejected edits remain visible in graph state
- **Modularity**: Each agent manages its own concerns
- **Deterministic writes**: All proposals for one file are validated before one final replace or create

## 📋 Prerequisites

- Python 3.12+
- uv (Python package manager)
- DeepSeek API key (the production default is `deepseek-v4-flash` through the
  official LangChain DeepSeek provider)

## ⚡ Quick Start

1. **Clone the repository**
```powershell
git clone https://github.com/langtalks/swe-agent.git
cd swe-agent
```

2. **Set up environment**
```powershell
# Install dependencies with uv
uv sync

# Create environment file
Copy-Item .env.example .env
# Add DEEPSEEK_API_KEY and optional LangSmith settings to .env
```

3. **Clone a repo to ./workspace_repo**
```powershell
git clone https://github.com/browser-use/browser-use ./workspace_repo
```

4. **Run the agent**
```powershell
# Start LangGraph server
uv run langgraph dev

```

4. **Example usage**
Input:
![Input](./static/input.png)
>Enable the browser-use agent to accept multi-modal instructions by supporting image inputs (e.g., step1.png, step2.png) alongside text. This will improve the agent’s ability to interpret and follow ambiguous or unclear textual commands

Output: (browsing the workspace repo git)
![Output](./static/output.png)


## 🛠️ Development

### Project Structure
```
agent/
├── architect/          # Planning and research agent
│   ├── graph.py       # Main architect workflow
│   ├── state.py       # State definitions
│   └── prompts/       # Prompt templates
├── developer/          # Implementation agent
│   ├── graph.py       # Main developer workflow
│   ├── editing.py     # Model proposal adapter
│   ├── runtime.py     # Injectable model/workspace dependencies
│   ├── state.py       # State definitions
│   └── prompts/       # Prompt templates
├── editing/            # Deterministic workspace read/write boundary
├── runtime/            # S3 run configuration and identity contracts
├── common/            # Shared entities and state
│   └── entities.py    # Pydantic models
└── tools/             # File operations and search tools
    ├── search.py      # Code search tools
    ├── codemap.py     # Code analysis tools
    └── write.py       # File operations

workspace_repo/        # Target codebase for modifications
scripts/              # Utility scripts
helpers/              # Prompt templates and utilities
static/               # Documentation images
```

### Core Components

**Entities & State Management:**
- `ImplementationPlan`: Structured task breakdown
- `AtomicTask`: Individual code modification units
- `ImplementationTask`: File-level implementation steps

**Agent Workflows:**
- Research-driven planning with hypothesis validation
- Tool-assisted code exploration and analysis
- Incremental implementation with verification

### Running Tests
```bash
# Run all tests
uv run python -m unittest discover -s tests -v

# Run specific test modules
uv run python -m unittest tests.developer.test_workflow -v
```

Run the real model compatibility smoke after setting `DEEPSEEK_API_KEY` in
`.env` or the ignored `.env.local`. It checks plain text, a forced tool call,
and structured output:

```powershell
uv run python scripts/smoke_model.py
```

## 📁 Main Directory Files

| File | Description |
|------|-------------|
| `README.md` | Project documentation (this file) |
| `pyproject.toml` | Python project configuration and dependencies |
| `langgraph.json` | LangGraph application configuration with graph definitions |
| `langgraph_debug.py` | Debug configurations for development and testing |
| `uv.lock` | Locked dependency versions for reproducible builds |
| `.env` | Environment variables (create from .env.example) |
| `.env.example` | Template for environment configuration |
| `.gitignore` | Git ignore patterns for Python and IDE files |
| `.python-version` | Python version specification for pyenv |

## 🎯 Use Cases

- **Feature Development**: Implement new features based on high-level requirements
- **Bug Fixes**: Analyze and fix issues with automated code changes
- **Code Refactoring**: Restructure code while maintaining functionality
- **Documentation**: Generate and update code documentation
- **Testing**: Create test cases and fix failing tests

## 🗺️ Roadmap - LangTalks Community Project!

We're building the future of AI-powered software development together! These are the next major features we're looking for community contributions on:

### 🔄 Core Agent Enhancements
- [ ] **Multi-step Research & Development Loop**: Iterative refinement of implementation plans with feedback cycles
- [ ] **Testing Agent**: Dedicated agent for unit testing, functional testing, and test case generation
- [ ] **Error Fixer Agent**: Specialized agent for detecting, analyzing, and fixing code errors
- [ ] **Product Manager Agent**: High-level planning and requirement analysis agent

### 🔧 Development Tools & Quality
- [ ] **Add Linters**: Integrate code quality tools (ESLint, Black, Pylint) into the workflow
- [ ] **Components Evaluation Benchmarking**: Performance metrics and quality assessment frameworks
- [ ] **Code Semantic Indexing**: Advanced code understanding and similarity detection

### 🌐 Integrations & Connectivity
- [ ] **GitHub MCP Integration**: Direct integration with GitHub repositories and workflows
- [ ] **Context7 MCP Integration**: Enhanced context management and code understanding
- [ ] **Multi-Language Support**: Extend beyond Python to JavaScript, TypeScript, Java, Go, etc.

### 📈 Advanced Features (Future)
- [ ] **Interactive Planning UI**: Web interface for plan review and modification
- [ ] **Collaborative Workflows**: Multi-developer coordination and conflict resolution
- [ ] **Performance Optimization**: Faster research and implementation cycles
- [ ] **Plugin System**: Extensible tool and agent architecture

> **Want to contribute?** Pick any feature above and join our LangTalks community! Each feature is designed to be tackled by individual contributors or small teams.

## 🤝 Contributing

We welcome contributions! This project aims to push the boundaries of AI-powered software development. Areas where we need help:

### Priority Areas
- **Agent Improvements**: Better reasoning and planning strategies
- **Tool Development**: New code analysis and modification tools
- **Testing**: Comprehensive test coverage and validation frameworks
- **Documentation**: Examples, tutorials, and use cases
- **Performance**: Optimization and benchmarking

### How to Contribute

1. **Fork the repository**
2. **Create a feature branch** (`git checkout -b feature/amazing-feature`)
3. **Make your changes** following the existing code patterns
4. **Add tests** for new functionality
5. **Ensure tests pass** (`uv run python -m unittest discover -s tests -v`)
6. **Update documentation** if needed
7. **Commit your changes** (`git commit -m 'Add amazing feature'`)
8. **Push to the branch** (`git push origin feature/amazing-feature`)
9. **Open a Pull Request** with a clear description

### Development Setup

```bash
# Clone your fork
git clone https://github.com/langtalks/swe-agent.git
cd swe-agent

# Set up development environment
uv sync

# Run tests to ensure everything works
uv run python -m unittest discover -s tests -v
```

## 📊 Technical Details

### Dependencies
- **LangGraph**: Multi-agent workflow orchestration
- **LangChain**: AI integration and tool management
- **Model API**: DeepSeek V4 Flash by default through its OpenAI-compatible
  API and dedicated LangChain provider; explicit Anthropic configuration
  remains supported
- **Tree-sitter**: Robust code parsing and analysis
- **Pydantic**: Type-safe data validation and serialization

### Performance Considerations
- File-level transactions for atomic-task reliability
- Efficient code analysis with tree-sitter
- Structured state management for scalability
- Tool-based architecture for extensibility

## 🔧 Configuration

Key configuration files:
- `langgraph.json`: Defines agent graphs and dependencies
- `.env`: API keys and environment variables
- `pyproject.toml`: Python dependencies and project metadata

At graph startup, the production `swe_agent` can load trusted verification
commands to run before and after editing. Configure a JSON list whose `argv`
values are argument arrays:

```dotenv
SWE_AGENT_VERIFICATION_CHECKS='[{"name":"tests","argv":["python","-m","unittest"],"cwd":".","timeout_seconds":120,"max_output_bytes":20000}]'
```

Commands run with `shell=False`, and `cwd` must resolve inside
`SWE_AGENT_WORKSPACE`. An unset or empty configuration produces the explicit
`UNVERIFIED` result. Shell command strings are rejected.

S3.1 adds an explicit, validated runtime configuration contract for library
callers:

```python
import os

from agent.runtime import load_run_config, semantic_config_digest

config = load_run_config(os.environ)
digest = semantic_config_digest(config)
```

`RunConfig` binds the workspace, a separate runtime directory, the existing
DeepSeek or Anthropic settings, model output limit, ordered verification
checks, run timeout, step limit, optional cost limit, and optional pricing
snapshot. The semantic digest excludes API keys and physical workspace/runtime
paths, so credentials can rotate while behavior-changing configuration remains
detectable. `build_chat_model(settings, max_output_tokens=...)` is the explicit
model assembly seam; calling `build_chat_model()` remains compatible with the
existing environment-backed graph.

The runtime directory must be disjoint from the workspace. S3.2 provides a
local synchronous SQLite runtime with strict start and resume entry points:

```powershell
python -m agent.runtime start --run-id run-1 --thread-id thread-1 --task-id task-1 --task "Fix the failing behavior"
python -m agent.runtime resume --run-id run-1 --thread-id thread-1 --task-id task-1
```

Both commands load the same validated environment configuration. The parent
graph owns `SqliteSaver`; Architect and Developer inherit it as child graphs.
Invocations use the thread id, synchronous durability, and
`max(200, 8 * max_steps + 64)` as the graph recursion guard. The separate
`langgraph.json:swe_agent` entry remains the existing non-durable Studio/dev
compatibility graph and does not create or resume the local SQLite database.

`WorkspaceIdentity` is deliberately path-only. `preflight_start` rejects an
existing thread, while `preflight_resume` compares workspace, run/task/thread,
semantic config, and known Agent revisions through an injected checkpoint
lookup. An unknown Agent revision produces a warning. The configured step and
cost limits are validated and bound into the digest. S3.2 reserves one durable
step for each model invocation and for each model-requested tool call before
dispatch, then settles captured usage afterward. A tool batch is rejected as a
whole when the remaining step capacity is insufficient. Step and absolute
deadline gates apply to both model and tool calls. Cost exhaustion or unknown
model cost blocks only the next model call: tool calls already returned by the
current model may still run when step capacity and deadline permit.

If recovery finds an in-flight external call without a durable result, it marks
the outcome unknown and stops instead of replaying the call. A durable result
that has not yet been settled resumes at deterministic settlement. Missing
model usage remains `UNKNOWN` or `PARTIAL` with `None` values. `max_cost_usd`
can therefore block later model calls but is not an absolute billing ceiling.
Configured token-price calculations use `cost_source="configured_estimate"`;
the pricing snapshot source remains bound by the semantic config digest.

The runtime checks the absolute deadline before dispatch and again after each
synchronous model or tool return. An overrun still settles the returned usage
or tool result, then reports `TIMEOUT_OVERRUN` and blocks commit, verification,
and later side effects. Baseline and post-edit checks receive
`min(check_timeout, remaining_run_time)`; when no time remains, no verification
process is started. Synchronous provider or tool calls are not forcibly
cancelled mid-call, so the overrun is detected when control returns.

S3.2 does not provide trajectory/EventSink, complete artifact or log spooling,
pending-write hash reconciliation, or exactly-once external calls. Those
remain later S3 slices; no complete security-audit claim is made here.

For explicit S3 model assembly, `ModelSettings` supplies provider, model,
endpoint, and credentials, while `max_output_tokens` is the only accepted
model option and must be supplied. Extra keyword options are rejected because
they are absent from the semantic digest. The legacy `build_chat_model()`
environment path continues to accept its existing keyword options. A persisted
`WorkspaceIdentity` can be rebuilt after its old directory disappears; only
`WorkspaceIdentity.from_root` inspects and validates a live filesystem root.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- Built with [LangGraph](https://langchain-ai.github.io/langgraph/) for reliable agent workflows
- Powered by [Anthropic Claude](https://www.anthropic.com/) for intelligent reasoning
- Uses [tree-sitter](https://tree-sitter.github.io/) for robust code parsing
- See our [deepwiki](https://deepwiki.com/langtalks/swe-agent/1-overview)

## 📞 Support & Community

- **LangTalks Homepage**: Visit [www.langtalks.ai](https://www.langtalks.ai) for community resources and support
- **Issues**: Report bugs and request features via [GitHub Issues](https://github.com/langtalks/swe-agent/issues)
- **Discussions**: Join conversations in [GitHub Discussions](https://github.com/langtalks/swe-agent/discussions)
- **Documentation**: Complete documentation is available in this README

---

**Ready to revolutionize software development with AI? Join us at [LangTalks](https://www.langtalks.ai) and help build the future of automated coding!** ⚡🤖
