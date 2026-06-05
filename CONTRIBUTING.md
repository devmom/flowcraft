# Contributing to FlowCraft

Thanks for your interest in contributing! FlowCraft is a harness-first local Agent workflow platform, and we welcome improvements of all sizes.

## Getting Started

### Prerequisites

- Python 3.12+
- Git
- (Optional) An API key for [DeepSeek](https://platform.deepseek.com/) or [Agnes AI](https://agnes-ai.com/), or [Ollama](https://ollama.com/) for local models

### Development Setup

```bash
git clone https://github.com/devmom/flowcraft.git
cd flowcraft/core
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

### Running Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=flowcraft_core --cov-report=html

# Single test file
pytest tests/test_runtime_smoke.py -v
```

## How to Contribute

### Reporting Bugs

1. Check [existing issues](https://github.com/devmom/flowcraft/issues) first
2. Use the Bug Report template
3. Include: Python version, OS, steps to reproduce, expected vs actual behavior

### Suggesting Features

1. Open a Feature Request issue
2. Describe the use case and why it benefits FlowCraft users
3. Discuss before implementing — some features may not align with the project's scope

### Pull Requests

1. Fork the repo and create a branch: `git checkout -b feature/my-feature`
2. Make your changes, add tests if applicable
3. Run `ruff check .` and `pytest` to ensure quality
4. Commit using [Conventional Commits](https://www.conventionalcommits.org/):
   - `feat: add X`
   - `fix: resolve Y`
   - `docs: update README`
   - `test: add tests for Z`
5. Open a PR against the `main` branch
6. Describe what changed and link to any related issues

## Code Style

- **Formatter**: Ruff (auto-fix: `ruff check --fix .`)
- **Type hints**: Encouraged for public APIs
- **Line length**: 120 characters
- **Docstrings**: Public classes and functions should have docstrings
- **Language**: Code and comments may use English or Chinese; public API docs should be in English

## Project Philosophy

- **Harness-first**: The tool system is the foundation; LLM is a user of tools, not the platform itself
- **Local-first**: Everything runs locally. No cloud dependency required.
- **Zero heavy frameworks**: No LangChain, AutoGen, CrewAI, or Semantic Kernel in core dependencies
- **Composable**: Small, focused modules over monolithic abstractions

## Questions?

Open a [GitHub Discussion](https://github.com/devmom/flowcraft/discussions) or file an issue.
