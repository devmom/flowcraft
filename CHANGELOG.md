# Changelog

All notable changes to FlowCraft will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-29

### Added

- **Model Gateway**: unified abstraction over DeepSeek V4 Pro/Flash, Agnes AI 2.0/1.5 Flash, Ollama
- **Intent Engine**: automatic task intent recognition
- **Planner**: Direct, Linear, and DAG planning modes
- **Execution Engine**: structured task execution with checkpoints and recovery
- **Policy Engine**: permission-based tool access control
- **Approval Manager**: interactive approval for sensitive operations
- **Tool System**: 15+ built-in tools
  - File: read, write, delete, list, search, metadata
  - Browser: Playwright automation (navigate, click, fill, screenshot)
  - Code: sandbox execution
  - Document: PDF, DOCX, Excel parsing
  - Network: HTTP requests, web search, file download
  - Knowledge: local knowledge base search
  - Meta: dynamic tool creation and management
- **Plugin Architecture**: load external tools without modifying core
- **Workflow Builder**: LLM-assisted declarative workflow creation
- **Memory System**: short-term and long-term memory with vector search
- **Observability**: full trace event recording and task replay
- **Secret Store**: encrypted API key storage
- **i18n Framework**: internationalization support
- **Web UI**: built-in browser interface
- **Windows Portable Build**: self-contained installer with embedded Python

[0.1.0]: https://github.com/devmom/flowcraft/releases/tag/v0.1.0
