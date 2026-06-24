# FlowCraft

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.2-blue.svg)]()
[中文文档](README_zh.md)

**Harness-first local Agent workflow platform** for individuals, small teams, and small businesses.

FlowCraft is built from scratch — **zero dependency on LangChain, AutoGen, CrewAI, Semantic Kernel, or other Agent frameworks**.

---

## 🆕 What's New in v0.1.2

> Compared to v0.1.1, **6 major new features** and **10 significant enhancements**.

### New Features

| # | Feature | Description |
|---|---------|-------------|
| 🎯 | **Skills System** | 33+ built-in + marketplace skills. Your Agent can now call deterministic skills — code review, web scraping, data analysis, diagram generation, file batch rename, Notion/GitHub/Trello/1Password integration, and more. Skills hot-reload without restart. One-click install from community marketplace. |
| 💬 | **Vent Mode** | LLM-driven user frustration detection and emotional feedback loop. When you're frustrated, FlowCraft detects it (5 levels), guides you through structured venting, maps your complaint to specific failure types, and writes insights into persistent memory so it won't repeat the same mistakes. |
| 🔒 | **Safe Shell Execution** | Your Agent can now run local commands — but safely. Built-in allowlist with security profiles for git, python, pip, npm, curl, etc. 4-tier risk: LOW auto-approves, MEDIUM asks once, HIGH requires explicit approval, CRITICAL is blocked. Inline eval (`python -c`, `node -e`) blocked by default. File edits have auto-backup and rollback. |
| 🧠 | **DeepSeek V4 Adaptive Reasoning** | When using DeepSeek V4 Pro, FlowCraft automatically selects reasoning depth based on task type: fast mode for simple Q&A, deep reasoning for creative writing and analysis, maximum reasoning for math proofs and academic research. Saves tokens when you don't need deep thinking. |
| 🔌 | **MCP Protocol Integration** | Connect any MCP (Model Context Protocol) server to extend your Agent's capabilities. Configure via `.mcp.json` — tools are auto-discovered and registered. |
| 🌐 | **Web UI Rewrite** | Complete SPA rewrite: real-time task panel with SSE streaming, multi-turn workflow builder, dual API key settings, skill browser, tool panel, marketplace, and Chinese/English toggle. |

### Major Enhancements

- **Task Controls** — Pause, resume, cancel, or force-kill any running task. Watchdog auto-marks hung tasks as failed (5-minute timeout)
- **Workflow Builder 2.0** — Multi-turn conversational workflow creation. Describe → preview → confirm/modify → save. Extract workflows from completed tasks
- **Model Hot-Switch** — Switch between DeepSeek, Agnes, Ollama at runtime. No restart needed. Each model keeps its own API key
- **ChromaDB Semantic Search** — Memory retrieval upgraded from keyword matching to semantic search (all-MiniLM-L6-v2 embeddings). Auto-falls back to TF-IDF offline
- **Workspace Isolation** — Agent file operations are sandboxed to `workspace/` directory, never touching your source code
- **Config Import/Export** — One-click backup and restore of all settings, workflows, and API keys
- **Knowledge Base** — Ingest local files, semantic search across knowledge, extract long-term memory from completed tasks
- **30+ New API Endpoints** — File upload, workflow building sessions, task pause/resume/cancel/force-kill, marketplace publish/browse, team workspaces, enterprise policies, DAG parallel planning, knowledge base CRUD, SSE streaming, i18n

---

## Features

### Core Platform

- 🧠 **Model Gateway** — Unified abstraction over DeepSeek (V4 Pro, V4 Flash, V3, R1), Agnes AI (2.0 Flash, 1.5 Flash), and Ollama. Hot-switch models at runtime without restart. Each model keeps its own API key managed by SecretStore
- 📋 **Structured Task Lifecycle** — Create → Intent → Plan → Execute → Complete. Pause, resume, cancel, or force-kill any running task. Watchdog auto-recovers hung tasks
- 🔍 **Full Observability** — Every LLM call, tool execution, and state change is recorded in a trace timeline. SSE (Server-Sent Events) pushes real-time updates to the Web UI
- 🏠 **Fully Local** — SQLite storage, offline-capable with Ollama local models. One-click config export/import for easy migration between machines

### Agent Capabilities (🆕 v0.1.2)

- 🎯 **Skills System** — 33+ deterministic skills your Agent can invoke: code review, web scraping, data analysis, diagram generation, file batch rename, technical documentation, and 22+ community skills (GitHub, Notion, Obsidian, Trello, 1Password, weather, etc.). Hot-reload on file change. Install from marketplace
- 🔒 **Safe Shell Execution** — Agent runs local commands under strict safety: allowlisted commands with behavior profiles, 4-tier risk (LOW/MEDIUM/HIGH/CRITICAL), inline eval detection, file edit backup & rollback. Low-risk commands auto-approved
- 💬 **Vent Mode** — When you express frustration, the system detects it via LLM sentiment analysis, guides structured venting, maps complaints to specific failure types, and writes lessons into persistent memory
- 🔧 **Rich Tool System** — File operations, browser automation, code sandbox, document parsing, web search, Playwright automation, MCP protocol servers, plugin extensions
- 🔌 **MCP Protocol** — Connect external MCP servers via `.mcp.json`. Tools are auto-discovered and registered at startup

### Workflow & Automation

- 📝 **Workflow Builder 2.0** — Multi-turn conversational creation: describe your need → Agent generates a preview → you confirm or request changes → save as reusable template. Extract workflows from completed tasks
- 🧩 **Plugin Architecture** — Extend tool capabilities without modifying core. Plugins auto-discovered from configured directories
- 📊 **DAG Parallel Planning** — Complex tasks automatically broken into dependency-aware parallel execution layers

### Memory & Knowledge

- 💾 **Memory System** — Short-term context, long-term memory extraction, and knowledge base with semantic search. ChromaDB (all-MiniLM-L6-v2) as primary engine, TF-IDF as offline fallback
- 📚 **Knowledge Base** — Ingest local files, semantic search across accumulated knowledge, extract reusable insights from completed tasks

### Safety & Collaboration

- 🛡️ **Policy & Approval Engine** — Permission checks on file operations, commands, and network access. Enterprise policy rules for team governance
- 👥 **Team Workspaces** — Shared workspaces with member management. Config sync for team-wide settings
- 🏪 **Workflow Marketplace** — Publish, browse, and install workflows from the community

### Architecture

```
[Web UI (SPA)] → [API Server] → [Intent Engine] → [Planner (Direct/Linear/DAG)]
                              ↓                              ↓
[Model Gateway] ← → [Execution Engine] ← → [Tool Harness + Skills]
     ↓              pause/resume/cancel            ↓
[DeepSeek V4 /   [Vent Mode]            [File Browser Code Shell
 Agnes / Ollama]                         Skills MCP Document ...]
     ↓
[Memory (ChromaDB) → Knowledge Base → Observability (SSE) → Checkpoint & Recovery]
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
│   ├── approval/           # Permission & command vetting
│   ├── config/             # Settings, i18n, sync
│   ├── domain/             # Schema & enums
│   ├── execution/          # Task execution engine
│   ├── feedback/           # Vent Mode & sentiment
│   ├── intent/             # Intent recognition & thinking evaluator
│   ├── memory/             # Memory (ChromaDB) & knowledge base
│   ├── models/             # LLM gateway & adapters
│   ├── observability/      # Events, traces, replay, SSE
│   ├── planning/           # Direct / Linear / DAG planner
│   ├── policy/             # Policy & enterprise rules
│   ├── runtime/            # Task lifecycle orchestrator
│   ├── security/           # Secret store & encryption
│   ├── skills/             # Skill registry & execution
│   ├── storage/            # SQLite database
│   ├── tools/              # Built-in tools & registry, MCP client
│   ├── web/                # Web UI (SPA)
│   └── workflows/          # Workflow builder (multi-turn)
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
