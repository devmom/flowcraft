# FlowCraft

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.2-blue.svg)]()
[English](README.md)

**面向个人、小团队和小企业的 Harness-first 本地 Agent 工作流平台。**

FlowCraft 从零构建——**不依赖 LangChain、AutoGen、CrewAI、Semantic Kernel 或任何 Agent 框架**。

---

## 🆕 v0.1.2 新增功能

> 相较于 v0.1.1，**6 项全新功能** + **10 项重大增强**。

### 全新功能

| # | 功能 | 说明 |
|---|------|------|
| 🎯 | **技能系统** | 33+ 内置+市场技能，Agent 现在可以调用确定性技能完成任务——代码审查、网页抓取、数据分析、图表生成、文件批量重命名、Notion/GitHub/Trello/1Password 集成等。修改技能文件无需重启即可热重载。社区市场一键安装。 |
| 💬 | **Vent Mode 情绪闭环** | LLM 驱动的用户挫败感检测与情绪疏导。当你表达不满时，FlowCraft 自动识别情绪等级（5 级），引导结构化发泄，将抱怨映射为具体故障类型，并写入长期记忆——后续不会再犯同样错误。 |
| 🔒 | **安全 Shell 执行** | Agent 可以执行本地命令，但受严格安全限制。内置白名单+安全画像（git, python, pip, npm, curl 等），4 级风险分类：低风险自动批准、中风险询问一次、高风险需显式审批、危险命令直接阻止。默认禁止内联代码执行（`python -c`, `node -e`）。文件编辑自动备份可回滚。 |
| 🧠 | **DeepSeek V4 推理深度自适应** | 使用 DeepSeek V4 Pro 时，系统根据任务类型自动选择推理深度：简单问答用快速模式（省 token）、创意写作/复杂分析用深度推理、数学证明/学术研究用最大推理。不需要深度思考时自动节省成本。 |
| 🔌 | **MCP 协议集成** | 接入任意 MCP (Model Context Protocol) 服务器，扩展 Agent 能力。通过 `.mcp.json` 配置文件即可，工具自动发现并注册。 |
| 🌐 | **Web 界面全面重写** | 全新 SPA 界面：实时任务面板（SSE 流式推送）、多轮对话工作流构建器、双 API Key 设置、技能浏览器、工具面板、工作流市场、中英文切换。 |

### 重大增强

- **任务控制** — 运行中任务可暂停、恢复、取消、强制终止。看门狗自动标记超时任务为失败（5 分钟超时）
- **工作流构建器 2.0** — 多轮对话式交互：描述需求 → Agent 生成预览 → 确认或修改 → 保存为模板。可从已完成任务中提取工作流
- **模型热切换** — DeepSeek、Agnes、Ollama 之间一键切换，无需重启。每个模型独立管理 API Key
- **ChromaDB 语义搜索** — 记忆检索从关键词匹配升级为语义搜索（all-MiniLM-L6-v2 嵌入）。离线环境自动降级 TF-IDF
- **工作空间隔离** — Agent 文件操作自动沙箱限制在 `workspace/` 目录，不会触碰项目源码
- **配置导入导出** — 一键备份/恢复全部设置、工作流和 API Key，迁移机器无忧
- **知识库** — 摄入本地文件、语义搜索历史知识、从已完成任务提取长期记忆
- **30+ 新 API 端点** — 文件上传、工作流构建会话、任务暂停/恢复/取消/强杀、市场发布/浏览、团队工作空间、企业策略、DAG 并行规划、知识库 CRUD、SSE 流式推送、国际化

---

## 全部特性

### 核心平台

- 🧠 **模型网关** — 统一接入 DeepSeek（V4 Pro、V4 Flash、V3、R1）、Agnes AI（2.0 Flash、1.5 Flash）、Ollama。支持运行时热切换，无需重启。每个模型独立 SecretStore 管理 API Key
- 📋 **结构化任务生命周期** — 创建 → 意图识别 → 规划 → 执行 → 完成。可暂停、恢复、取消、强制终止运行中任务。看门狗自动恢复卡死任务
- 🔍 **全链路可观测** — 每次 LLM 调用、工具执行、状态变更均记录追踪时间线。SSE（Server-Sent Events）实时推送更新到 Web 界面
- 🏠 **完全本地化** — SQLite 存储，搭配 Ollama 可完全离线运行。一键配置导出/导入，跨机器迁移无忧

### Agent 能力 (🆕 v0.1.2)

- 🎯 **技能系统** — 33+ 个确定性技能供 Agent 调用：代码审查、网页抓取、数据分析、图表生成、文件批量重命名、技术文档写作，以及 22+ 社区技能（GitHub、Notion、Obsidian、Trello、1Password、天气查询等）。文件变更即时热重载。市场一键安装
- 🔒 **安全 Shell 执行** — Agent 在严格安全限制下执行本地命令：白名单+行为画像、4 级风险（低/中/高/严重）、内联 eval 检测、文件编辑备份回滚。低风险命令自动批准
- 💬 **Vent Mode 情绪闭环** — 当你表达挫败时，系统通过 LLM 情感分析检测情绪，引导结构化发泄，将抱怨映射为具体故障类型，并将教训写入持久记忆
- 🔧 **丰富工具系统** — 文件操作、浏览器自动化、代码沙箱、文档解析、网页搜索、Playwright 自动化、MCP 协议服务器、插件扩展
- 🔌 **MCP 协议** — 通过 `.mcp.json` 连接外部 MCP 服务器，工具启动时自动发现并注册

### 工作流与自动化

- 📝 **工作流构建器 2.0** — 多轮对话创建：描述需求 → Agent 生成预览 → 确认或修改 → 保存为可复用模板。可从已完成任务中提取工作流
- 🧩 **插件架构** — 无需修改核心即可扩展工具能力。插件从配置目录自动发现
- 📊 **DAG 并行规划** — 复杂任务自动分解为依赖感知的并行执行层

### 记忆与知识

- 💾 **记忆系统** — 短期上下文、长期记忆提取、知识库语义搜索。ChromaDB（all-MiniLM-L6-v2）为主引擎，TF-IDF 为离线回退
- 📚 **知识库** — 摄入本地文件、语义搜索积累知识、从已完成任务提取可复用洞察

### 安全与协作

- 🛡️ **策略与审批引擎** — 文件操作、命令执行、网络访问均需权限审核。企业策略规则支持团队治理
- 👥 **团队工作空间** — 共享工作空间+成员管理。配置同步实现团队级统一设置
- 🏪 **工作流市场** — 发布、浏览、安装社区工作流

### 架构

```
[Web UI (SPA)] → [API Server] → [意图引擎] → [规划器（直接/线性/DAG）]
                              ↓                            ↓
[模型网关] ← → [执行引擎] ← → [工具执行器 + 技能系统]
     ↓           暂停/恢复/取消               ↓
[DeepSeek V4 /   [情绪闭环]        [文件 浏览器 代码 Shell
 Agnes / Ollama]                        技能 MCP 文档 ...]
     ↓
[记忆 (ChromaDB) → 知识库 → 可观测性 (SSE) → 检查点与恢复]
```

## 快速开始

### Windows 免安装版

从 [Releases](https://github.com/devmom/flowcraft/releases) 下载最新的便携包，解压后双击 `FlowCraft\FlowCraft.bat` 即可使用，浏览器自动打开，无需安装任何环境。

### 源码安装前置条件

- Python 3.12+
- 一个 API Key：[DeepSeek](https://platform.deepseek.com/) 或 [Agnes AI](https://agnes-ai.com/) 或 [Ollama](https://ollama.com/) 本地模型

### 安装

```bash
git clone https://github.com/devmom/flowcraft.git
cd flowcraft/core
pip install -e ".[dev]"
```

### 配置

```bash
# 方案 A：DeepSeek（推荐）
export FLOWCRAFT_DEEPSEEK_API_KEY=sk-你的密钥

# 方案 B：Agnes AI（免费额度）
export AGNES_API_KEY=sk-你的密钥

# 方案 C：Ollama（纯本地，无需 API Key）
# 安装 Ollama 后拉取模型：ollama pull qwen3
```

### 启动

```bash
cd core
python -m flowcraft_core.simple_server
```

浏览器打开 **http://127.0.0.1:8765** 即可使用。

### API 快速测试

```bash
# 健康检查
curl http://127.0.0.1:8765/health

# 创建任务
curl -X POST http://127.0.0.1:8765/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"session_id":"default","input":"FlowCraft 是什么？"}'

# 查询任务状态
curl http://127.0.0.1:8765/api/tasks/{task_id}
```

## 支持的模型

| 提供商 | 模型 | 认证方式 | 费用 |
|--------|------|----------|------|
| **DeepSeek** | V4 Pro、V4 Flash | `FLOWCRAFT_DEEPSEEK_API_KEY` | 付费 |
| **Agnes AI** | 2.0 Flash、1.5 Flash | `AGNES_API_KEY` | 免费额度 |
| **Ollama** | Qwen3、Llama 等 | 无需（本地运行） | 免费 |

Agnes AI 还支持图像生成（`agnes-image-2.1-flash`）。

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest

# 运行单个测试文件
pytest tests/test_runtime_smoke.py -v
```

详见 [DEVELOPMENT.md](DEVELOPMENT.md)。

## 项目结构

```
flowcraft/
├── core/flowcraft_core/    # 核心运行时
│   ├── api/                # FastAPI REST 服务端
│   ├── approval/           # 权限与命令审批
│   ├── config/             # 配置、国际化、同步
│   ├── domain/             # 数据模型与枚举
│   ├── execution/          # 任务执行引擎
│   ├── feedback/           # 情绪闭环 (Vent Mode) 与感知
│   ├── intent/             # 意图识别与推理深度评估
│   ├── memory/             # 记忆 (ChromaDB) 与知识库
│   ├── models/             # LLM 网关与适配器
│   ├── observability/      # 事件、追踪、回放、SSE
│   ├── planning/           # 直接 / 线性 / DAG 规划器
│   ├── policy/             # 策略与企业规则
│   ├── runtime/            # 任务生命周期编排
│   ├── security/           # 密钥存储与加密
│   ├── skills/             # 技能注册与执行
│   ├── storage/            # SQLite 数据库
│   ├── tools/              # 内置工具与注册中心、MCP 客户端
│   ├── web/                # Web 前端界面 (SPA)
│   └── workflows/          # 工作流构建器 (多轮对话)
├── TechnicalArchitecture/  # 架构设计与调研文档
├── scripts/                # 开发与工具脚本
└── packaging/              # 安装包构建脚本
```

## 参与贡献

欢迎贡献代码！请参阅 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 开源协议

MIT — 详见 [LICENSE](LICENSE)。

## 致谢

不依赖重型框架，从零构建。受 Unix 哲学启发：小而可组合的工具，胜过庞大而僵化的抽象。
