# FlowCraft

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-MVP-orange.svg)]()
[中文文档](README_zh.md)

**Harness-first local Agent workflow platform** for individuals, small teams, and small businesses.

FlowCraft is built from scratch — **zero dependency on LangChain, AutoGen, CrewAI, Semantic Kernel, or other Agent frameworks**.

---

## Features

- 🧠 **Model Gateway** — Unified abstraction over DeepSeek, Agnes AI, Ollama (OpenAI-compatible)
- 📋 **Structured Task Lifecycle** — Create → Intent → Plan → Execute → Complete
- 🔍 **Trace Event Timeline** — Full observability: every LLM call, tool execution, state change is recorded
- 🛡️ **Policy & Approval Engine** — File operations, commands, network access require permission checks
- 🔧 **Rich Tool System** — Files, browser, code sandbox, document parsing, web search, Playwright automation, plugin support
- 📝 **Workflow Builder** — LLM-assisted visual/declarative workflow creation
- 🧩 **Plugin Architecture** — Extend tools without modifying core
- 🌍 **i18n Ready** — Built-in internationalization framework
- 💾 **Memory System** — Short-term, long-term, knowledge base with vector search
- 🏠 **Fully Local** — SQLite storage, works offline with local models (Ollama)

### Architecture

```
[Web UI] → [API Server] → [Intent Engine] → [Planner (Direct/Linear/DAG)]
                                                   ↓
[Model Gateway] ← → [Execution Engine] ← → [Tool Harness]
     ↓                                             ↓
[DeepSeek / Agnes / Ollama]              [File Browser Code Sandbox
                                           Document Network Playwright ...]
     ↓
[Memory → Knowledge Base → Observability → Checkpoint & Recovery]
```

## Quick Start

### Windows (no Python required)

Download the latest portable package from [Releases](https://github.com/devmom/flowcraft/releases), extract, and double-click `FlowCraft\FlowCraft.bat`. The browser opens automatically — no installation needed.

### Prerequisites (for source install)

- Python 3.12+
- An API key from [DeepSeek](https://platform.deepseek.com/) OR [Agnes AI](https://agnes-ai.com/) OR [Ollama](https://ollama.com/) (local)

### Install

```bash
git clone https://github.com/devmom/flowcraft.git
cd flowcraft/core
pip install -e ".[dev]"
```

### Configure

```bash
# Option A: DeepSeek (recommended)
export FLOWCRAFT_DEEPSEEK_API_KEY=sk-your-key

# Option B: Agnes AI (free tier)
export AGNES_API_KEY=sk-your-key

# Option C: Ollama (fully local, no API key)
# Install Ollama and pull a model: ollama pull qwen3
```

### Run

```bash
cd core
python -m flowcraft_core.simple_server
```

Open **http://127.0.0.1:8765** in your browser.

### API Quick Test

```bash
# Health check
curl http://127.0.0.1:8765/health

# Create a task
curl -X POST http://127.0.0.1:8765/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"session_id":"default","input":"What is FlowCraft?"}'

# Get task status
curl http://127.0.0.1:8765/api/tasks/{task_id}
```

## Supported Models

| Provider | Models | Auth | Cost |
|----------|--------|------|------|
| **DeepSeek** | V4 Pro, V4 Flash | `FLOWCRAFT_DEEPSEEK_API_KEY` | Paid |
| **Agnes AI** | 2.0 Flash, 1.5 Flash | `AGNES_API_KEY` | Free tier |
| **Ollama** | Qwen3, Llama, etc. | None (local) | Free |

Image generation available via Agnes AI (`agnes-image-2.1-flash`).

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run a single test file
pytest tests/test_runtime_smoke.py -v
```

See [DEVELOPMENT.md](DEVELOPMENT.md) for detailed setup and architecture docs.

## Project Structure

```
flowcraft/
├── core/flowcraft_core/    # Main runtime
│   ├── api/                # FastAPI REST server
│   ├── approval/           # Permission & approval
│   ├── config/             # Settings, i18n, sync
│   ├── domain/             # Schema & enums
│   ├── execution/          # Task execution engine
│   ├── intent/             # Intent recognition
│   ├── memory/             # Memory & knowledge base
│   ├── models/             # LLM gateway & adapters
│   ├── observability/      # Events, traces, replay
│   ├── planning/           # Direct / Linear / DAG planner
│   ├── policy/             # Policy & enterprise rules
│   ├── runtime/            # Task lifecycle orchestrator
│   ├── security/           # Secret store & encryption
│   ├── storage/            # SQLite database
│   ├── tools/              # Built-in tools & registry
│   ├── web/                # Web UI
│   └── workflows/          # Workflow builder
├── TechnicalArchitecture/  # Design documents & research
├── scripts/                # Dev & utility scripts
└── packaging/              # Installer build scripts
```

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT — see [LICENSE](LICENSE) for details.

## Acknowledgments

Built without heavy frameworks. Inspired by the Unix philosophy: small, composable tools over monolithic abstractions.
