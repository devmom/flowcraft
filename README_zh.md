# FlowCraft

[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-MVP-orange.svg)]()
[English](README.md)

**面向个人、小团队和小企业的 Harness-first 本地 Agent 工作流平台。**

FlowCraft 从零构建——**不依赖 LangChain、AutoGen、CrewAI、Semantic Kernel 或任何 Agent 框架**。

---

## 特性

- 🧠 **模型网关** — 统一接入 DeepSeek、Agnes AI、Ollama（均兼容 OpenAI 接口）
- 📋 **结构化任务生命周期** — 创建 → 意图识别 → 规划 → 执行 → 完成
- 🔍 **全链路追踪** — 每次 LLM 调用、工具执行、状态变更均记录时间线
- 🛡️ **策略与审批引擎** — 文件操作、命令执行、网络访问均需权限审核
- 🔧 **丰富的工具系统** — 文件、浏览器、代码沙箱、文档解析、网页搜索、Playwright 自动化、插件扩展
- 📝 **工作流构建器** — LLM 辅助声明式创建可复用工作流
- 🧩 **插件架构** — 无需修改核心即可扩展工具能力
- 🌍 **国际化就绪** — 内置 i18n 框架
- 💾 **记忆系统** — 短期记忆、长期记忆、知识库 + 向量检索
- 🏠 **完全本地化** — SQLite 存储，搭配 Ollama 可离线运行

### 架构

```
[Web UI] → [API Server] → [意图引擎] → [规划器（直接/线性/DAG）]
                                                 ↓
[模型网关] ← → [执行引擎] ← → [工具执行器]
     ↓                                          ↓
[DeepSeek / Agnes / Ollama]        [文件 浏览器 代码沙箱
                                     文档 网络 Playwright ...]
     ↓
[记忆 → 知识库 → 可观测性 → 检查点与恢复]
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
│   ├── approval/           # 权限与审批
│   ├── config/             # 配置、国际化、同步
│   ├── domain/             # 数据模型与枚举
│   ├── execution/          # 任务执行引擎
│   ├── intent/             # 意图识别
│   ├── memory/             # 记忆与知识库
│   ├── models/             # LLM 网关与适配器
│   ├── observability/      # 事件、追踪、回放
│   ├── planning/           # 直接 / 线性 / DAG 规划器
│   ├── policy/             # 策略与企业规则
│   ├── runtime/            # 任务生命周期编排
│   ├── security/           # 密钥存储与加密
│   ├── storage/            # SQLite 数据库
│   ├── tools/              # 内置工具与注册中心
│   ├── web/                # Web 前端界面
│   └── workflows/          # 工作流构建器
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
