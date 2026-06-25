with open(r"D:\work\FlowCraft\TechnicalArchitecture\FlowCraft-v0.1.2-Changelog.md", "w", encoding="utf-8") as f:
    f.write("""# FlowCraft v0.1.2 Version Changelog

> Previous: v0.1.1 (commit 1d687c4) | Current: v0.1.2 (unreleased)
> Stats: 28 files modified, 20 new modules, +3769/-315 lines

## 1. New Modules

### 1.1 Skills System (Phase 1-3)
- skills/models.py - SkillDefinition, SkillManifest data models
- skills/registry.py - SkillRegistry: discover, load, validate, hot-reload
- skills/skill_tool.py - SkillExecuteTool: wraps skills as FlowCraft Tools
- skills/convert_skill.py - OpenClaw format converter
- skills/dynamic_executor.py - Dynamic script executor
- 33+ skills loaded from builtin/marketplace/generated sources

### 1.2 Feedback / Vent Mode (Phase 2)
- feedback/sentiment.py - FrustrationDetector: LLM-driven frustration detection
- feedback/vent_session.py - VentSessionManager: structured venting
- feedback/phrase_library.py - PhraseLibrary: curated phrase management
- feedback/agent_response_guard.py - AgentResponseSanitizer: one-way emotional guard
- feedback/insight_mapper.py - InsightMapper: feedback -> FailureType mapping
- feedback/feedback_memory_integrator.py - FeedbackMemoryIntegrator

### 1.3 Exec Tool + Apply Patch
- approval/exec_approval.py - ExecApprovalManager: risk-based command vetting
- tools/exec_tool.py - Safe shell execution with security profiles
- tools/apply_patch.py - Structured file editing (preview/backup/rollback)

### 1.4 Anthropic Claude Adapter
- models/adapters/anthropic.py - Claude Opus 4 / Sonnet 4 / Haiku 3.5 support

### 1.5 DeepSeek V4 Thinking Evaluator
- intent/thinking_evaluator.py - Reasoning depth assessment (disabled/high/max)

## 2. Enhanced Modules

### 2.1 Web UI (+750 lines)
- Full SPA rewrite: task panel, workflow builder, settings, skill browser
- DeepSeek/Agnes dual API key configuration UI
- Model switching UI, SSE real-time event display

### 2.2 Execution Engine (+427 lines)
- Pause/Resume/Cancel/Force Kill task controls
- Watchdog for hung task detection (300s timeout)
- Workspace isolation, skill registry integration
- SSE event streaming (task lifecycle events)

### 2.3 Workflow Builder (+584 lines)
- Multi-turn dialog: start -> continue -> confirm/modify -> save
- 60s timeout handling, session state machine
- Async/event-loop safety improvements

### 2.4 Model Gateway (+147 lines)
- Multi-provider support: DeepSeek, Agnes, Anthropic, Ollama
- Per-model API key management via SecretStore
- Runtime model switching without restart
- Provider auto-detection from model name

### 2.5 MCP Client (+196 lines)
- Full MCP (Model Context Protocol) client via stdio transport
- Auto-discovery from .mcp.json config
- Tools/Resources/Prompts three-primitive support

### 2.6 Vector Store (+289 lines)
- ChromaDB semantic search (all-MiniLM-L6-v2) as primary engine
- TF-IDF keyword fallback for zero-dependency mode

### 2.7 Config/Settings (+76 lines)
- Project root auto-detection
- Workspace isolation (workspace/ dir separate from source)
- Skills directory auto-configuration
- Artifacts directory for task outputs

### 2.8 Other Enhancements
- domain/enums.py: +5 TaskTypes (SPREADSHEET_ANALYSIS, EMAIL_ASSISTANT, etc.)
- storage/database.py: insert_json() convenience method
- tools/plugin_registry.py: plugin discovery enhancements
- intent/engine.py: intent recognition improvements
- memory/context_summarizer.py: summary quality improvements
- planning/planner.py: skill-aware planning
- execution/completion_checker.py: completion logic enhancements
- execution/context_compressor.py: compression strategy optimization

## 3. New API Endpoints (30+)

### Phase 1 - Core
POST   /api/upload              File upload (multipart)
DELETE /api/upload              Upload cleanup
POST   /api/workflows/build/*   Workflow builder session API
POST   /api/workflows/{id}/run  Run workflow (enhanced error handling)
POST   /api/tasks/{id}/approve  Approve pending task
POST   /api/tasks/{id}/pause    Pause executing task
POST   /api/tasks/{id}/resume   Resume paused task
POST   /api/tasks/{id}/cancel   Cancel task
POST   /api/tasks/{id}/force-kill Force kill hung task
GET    /api/status              Active tasks status

### Phase 2 - Collaboration
GET    /api/plugins             Plugin list
GET    /api/marketplace         Workflow marketplace browse
POST   /api/workflows/{id}/publish  Publish to marketplace
GET    /api/workspaces          Team workspaces
POST   /api/policies            Enterprise policy rules
GET    /api/sync/export         Config export
POST   /api/sync/import         Config import
POST   /api/tools/dag-plan      DAG parallel planning

### Phase 4 - Knowledge/i18n
GET    /api/knowledge/*         Knowledge base CRUD + search
POST   /api/tasks/{id}/extract-memory  Extract long-term memory
GET    /api/stream/{id}/events  SSE event stream
GET    /api/i18n/locales        Available locales
POST   /api/i18n/locale         Set locale

## 4. Infrastructure Changes

### Model Support Matrix
Provider    v0.1.1    v0.1.2
DeepSeek    V3, R1    V4 Pro, V4 Flash, V3, R1
Agnes       -         Agnes 2.0 Flash
Anthropic   -         Claude Opus 4 / Sonnet 4 / Haiku 3.5
Ollama      Yes       Yes
OpenAI      Yes       Yes

### Version Bump
- pyproject.toml: 0.1.0 -> 0.1.2
- build.bat: v0.1.1 -> v0.1.2
- settings.py: 0.1.0 -> 0.1.2

## 5. File-Level Changes (Top 15 by delta)

| File | Delta |
|------|-------|
| web/index.html | +750 |
| workflows/builder.py | +584 |
| api/server.py | +519 |
| execution/engine.py | +427 |
| memory/vector_store.py | +289 |
| tools/mcp_client.py | +196 |
| models/gateway.py | +147 |
| tools/plugin_registry.py | +127 |
| intent/engine.py | +114 |
| tests/test_intent_planning.py | +117 |
| memory/context_summarizer.py | +106 |
| tests/test_model_gateway.py | +99 |
| app.py | +89 |
| execution/completion_checker.py | +83 |
| planning/planner.py | +80 |

## New Module Files (16)
skills/models.py, registry.py, skill_tool.py, convert_skill.py, dynamic_executor.py
feedback/sentiment.py, vent_session.py, phrase_library.py, agent_response_guard.py, insight_mapper.py, feedback_memory_integrator.py
approval/exec_approval.py
tools/exec_tool.py, apply_patch.py
models/adapters/anthropic.py
intent/thinking_evaluator.py

## Summary

v0.1.2 is a major architecture upgrade adding ~3700 lines of code across 20 new modules, focusing on:

1. Skills System - 33+ skills with progressive disclosure, hot-reload, marketplace
2. Emotional Loop - LLM-driven frustration detection -> venting -> actionable feedback
3. Security Sandbox - Risk-tiered command approval, safe bin profiles, inline eval detection
4. Multi-Model - Claude + Agnes providers, DeepSeek V4 reasoning depth assessment
5. Collaboration - Team workspaces, enterprise policies, config sync, marketplace
6. Engineering Robustness - Timeout control, watchdog, SSE streaming, event-loop safety
""")
print("Written successfully")
