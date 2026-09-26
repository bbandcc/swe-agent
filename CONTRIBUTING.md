# Contributing to SWE Agent LangGraph

Welcome to the LangTalks community! We're excited to have you contribute to the future of AI-powered software development. This guide will help you get started and make meaningful contributions.

> ⚠️ **Alpha Project**: This is an alpha-stage project. Your contributions will directly shape the foundation of AI-powered software development. Expect rapid changes and exciting new features!

## 🚀 Getting Started

### Prerequisites

- Python 3.12+
- uv (Python package manager)
- Git

The deterministic test suite uses fake models and does not require an API key.

### Development Setup

1. **Fork and Clone**
```bash
# Fork the repository on GitHub first, then:
git clone https://github.com/langtalks/swe-agent-langgraph.git
cd swe-agent-langgraph
```

2. **Environment Setup**
```bash
# Install the locked production and development/test dependencies
uv sync --locked

# Set up environment variables
cp .env.example .env
# Add your API keys to .env

# Run commands through uv; manual environment activation is optional
```

3. **Verify Setup**
```bash
# Canonical full test suite (same entry as README)
uv run --locked python -m unittest discover -s tests -v

# Additional pytest compatibility run; pytest is in the locked dev group
uv run --locked python -m pytest

# Compile production and test modules
uv run --locked python -m compileall -q agent tests
```

## 🎯 How to Contribute

### Priority Areas for LangTalks Community

We're looking for contributions in these key areas:

#### 🔄 **Core Agent Enhancements**
- **Multi-step Research & Development Loop**: Iterative refinement cycles
- **Testing Agent**: Automated test generation and execution
- **Error Fixer Agent**: Smart error detection and resolution
- **Product Manager Agent**: High-level requirement analysis

#### 🔧 **Development Tools & Quality**
- **Future tooling**: Linter integration (not currently configured)
- **Benchmarking Framework**: Performance and quality metrics
- **Code Semantic Indexing**: Advanced code understanding

#### 🌐 **Integrations & Connectivity**
- **GitHub MCP Integration**: Repository workflow automation
- **Context7 MCP Integration**: Enhanced context management
- **Multi-Language Support**: JavaScript, TypeScript, Java, Go

### Finding Issues to Work On

1. **Check the [Issues](https://github.com/langtalks/swe-agent-langgraph/issues)** tab for open tasks
2. **Look for labels**:
   - `good first issue` - Great for newcomers
   - `help wanted` - Community contributions needed
   - `enhancement` - New feature development
   - `bug` - Bug fixes needed
3. **Join [Discussions](https://github.com/langtalks/swe-agent-langgraph/discussions)** to propose new ideas

## 📝 Development Workflow

### 1. **Create a Feature Branch**
```bash
git checkout -b feature/your-feature-name
# OR
git checkout -b fix/issue-description
```

### 2. **Development Guidelines**

#### Code Style
- **Python**: Follow PEP 8 and the style of nearby code
- **Type Hints**: Required for all new code
- **Docstrings**: Use Google style for functions and classes
- **Variable Names**: Descriptive and meaningful

#### Architecture Patterns
- **State Management**: Use Pydantic models for all state
- **Agent Design**: Follow the existing architect → developer pattern
- **Tool Integration**: Implement tools following LangGraph patterns
- **Message Handling**: Use proper LangChain message types

#### Testing Requirements
- **Unit Tests**: Required for all new functionality
- **Integration Tests**: For agent workflows
- **Fixtures**: Reuse the existing unittest/pytest test patterns

### 3. **Configured Checks**
```bash
# Canonical tests, pytest compatibility run, and compile check
uv run --locked python -m unittest discover -s tests -v
uv run --locked python -m pytest
uv run --locked python -m compileall -q agent tests
```

Black, isort, mypy, flake8, and coverage reporting are not currently configured
project gates. Do not treat commands for those tools as available checks.

### 4. **Commit Guidelines**

Use conventional commits:
```bash
# Feature
git commit -m "feat: add multi-step research loop to architect agent"

# Bug fix
git commit -m "fix: resolve state persistence issue in developer agent"

# Documentation
git commit -m "docs: update API documentation for new tools"

# Tests
git commit -m "test: add integration tests for GitHub MCP"
```

### 5. **Submit Pull Request**

1. **Push your branch**
```bash
git push origin feature/your-feature-name
```

2. **Create Pull Request** with:
   - Clear description of changes
   - Reference to related issues
   - Screenshots/demos if applicable
   - Test results

3. **PR Checklist**:
   - [ ] Tests pass
   - [ ] Type hints added
   - [ ] Documentation updated
   - [ ] Changelog entry added (if applicable)

## 🧪 Testing

### Running Tests
```bash
# Canonical full suite
uv run --locked python -m unittest discover -s tests -v

# Specific test module
uv run --locked python -m unittest tests.developer.test_workflow -v

# Pytest compatibility run
uv run --locked python -m pytest
```

The project does not currently configure a coverage plugin or a separate
integration-test directory.

### Writing Tests

#### Unit Tests
```python
import pytest
from agent.architect.state import SoftwareArchitectState

def test_architect_state_initialization():
    state = SoftwareArchitectState()
    assert state.research_next_step is None
    assert state.implementation_plan is None
```

#### Integration Tests
```python
async def test_architect_to_developer_workflow():
    # Test complete workflow from architect to developer
    initial_state = {"implementation_research_scratchpad": [HumanMessage("Add feature")]}
    result = await swe_agent.ainvoke(initial_state)
    assert result["implementation_plan"] is not None
```

## 🎨 UI/UX Contributions

If you're working on user interfaces:
- Follow modern design principles
- Ensure accessibility (WCAG 2.1 AA)
- Mobile-responsive design
- Use consistent color schemes and typography

## 📖 Documentation

### Types of Documentation
- **Code Documentation**: Inline comments and docstrings
- **API Documentation**: Comprehensive function/class documentation
- **User Guides**: How-to guides and tutorials
- **Architecture Documentation**: System design and patterns

### Documentation Standards
- Clear, concise language
- Code examples for all features
- Screenshots for UI components
- Keep documentation up-to-date with code changes

## 🤝 Community Guidelines

### Code of Conduct
- Be respectful and inclusive
- Welcome newcomers and help them learn
- Provide constructive feedback
- Focus on the problem, not the person

### Getting Help
- **LangTalks Community**: Visit [www.langtalks.ai](https://www.langtalks.ai) for community resources and support
- **GitHub Discussions**: Ask questions and share ideas
- **Issues**: Report bugs and request features
- **Documentation**: Complete project documentation is in the main README

### Recognition
Contributors will be:
- Listed in the project contributors
- Mentioned in release notes for significant contributions
- Invited to join the LangTalks maintainer team for exceptional contributions
- Featured on the [LangTalks community page](https://www.langtalks.ai)

## 🚀 Advanced Contributions

### Creating New Agents
1. Define agent state using Pydantic models
2. Implement agent workflow using LangGraph
3. Add appropriate tools and integrations
4. Write comprehensive tests
5. Update documentation

### Adding New Tools
1. Follow the existing tool pattern in `agent/tools/`
2. Implement proper error handling
3. Add tool documentation
4. Test tool integration with agents

### Performance Optimizations
- Profile code using `py-spy` or similar tools
- Optimize database queries and API calls
- Implement caching where appropriate
- Benchmark performance improvements

## 📋 Release Process

1. **Feature Complete**: All planned features implemented
2. **Testing**: Full test suite passes
3. **Documentation**: All documentation updated
4. **Version Bump**: Update version numbers
5. **Changelog**: Update CHANGELOG.md
6. **Release**: Create GitHub release with notes

---

## 🙏 Thank You!

Every contribution, no matter how small, helps build the future of AI-powered software development. Thank you for being part of the LangTalks community!

**Questions?** Visit [www.langtalks.ai](https://www.langtalks.ai) or open a [discussion](https://github.com/langtalks/swe-agent-langgraph/discussions).

**Ready to contribute?** Check out our [good first issues](https://github.com/langtalks/swe-agent-langgraph/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) and get started! 🚀
