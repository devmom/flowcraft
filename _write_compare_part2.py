
import os

text = """## 3. 第二部分：代码实现与设计文档不一致

以下列出代码实际实现与设计文档描述存在差异的地方。

### 3.1 数据库表数: 文档说 10 张，实际 17 张

| 项目 | 详情 |
|------|------|
| **来源文档** | 03-DatabaseDesign |
| **文档描述** | 10 张核心表 |
| **代码实际** | 17 张表，额外包含: model_calls / settings / secrets_refs / workflow_templates / workflow_runs / knowledge_sources / policy_decisions |
| **评价文档建议** | P1: 设计文档需同步更新 |

### 3.2 API 端点数: 文档说 11 个，实际 30+

| 项目 | 详情 |
|------|------|
| **来源文档** | 05-APIDesign |
| **文档描述** | 11 个 RESTful 端点 |
| **代码实际** | api/server.py (FastAPI) + simple_server.py (Stdlib HTTP) 双服务器，30+ 端点 |
| **评价文档建议** | P1: 同步设计文档与代码实现 |

### 3.3 SSE 流式端点: 评价文档判缺失，代码实际已实现

| 项目 | 详情 |
|------|------|
| **来源文档** | 05-APIDesign-Evaluation |
| **评价文档判断** | P1: 缺少 SSE 流式推送 |
| **代码实际** | api/server.py 第125-159行已实现 GET /api/tasks/{task_id}/stream，返回 text/event-stream |
| **结论** | 评价文档误判。SSE 端点已存在 |

### 3.4 测试文件数: 文档说 4 个，实际 18 个

| 项目 | 详情 |
|------|------|
| **来源文档** | 19-Testing |
| **文档描述** | 4 个测试文件、30+ 测试用例 |
| **代码实际** | core/tests/ 下有 18 个 Python 测试文件（含 conftest.py） |
| **影响** | 评价文档测试严重不足判断需重新校准 |

### 3.5 执行模式: MVP 说 LINEAR/DIRECT，代码 4 种

| 项目 | 详情 |
|------|------|
| **来源文档** | 09-TaskPlanning (Doc) |
| **文档描述** | MVP 范围: DirectPlanner + LinearPlanner |
| **代码实际** | 4 种模式: DIRECT / LINEAR / DAG / ITERATIVE |
| **差异原因** | 代码实现了超额功能 |

### 3.6 记忆系统: MVP 说 2 层，代码 4 层

| 项目 | 详情 |
|------|------|
| **来源文档** | 13-MemorySystem (Doc) |
| **文档描述** | MVP 范围: Session + Task Memory |
| **代码实际** | 完整四层: Working + Session + Long-term + Knowledge Base |

### 3.7 Policy Engine: MVP 说不做企业级，代码已实现

| 项目 | 详情 |
|------|------|
| **来源文档** | 01-ProjectOverview |
| **文档描述** | MVP 不做: 企业级策略中心 |
| **代码实际** | policy/enterprise.py 已实现 EnterprisePolicyEngine |
| **评价文档建议** | P3: Harness 层薄化评估 |

### 3.8 文档重复文件

| 项目 | 详情 |
|------|------|
| **判断** | FlowCraftExecutableTechnicalPlan.md 与 ExecutableTechnicalPlan.md 完全重复 |
| **建议** | 保留 FlowCraft-Full-Architecture.md 作为权威架构文档 |

### 3.9 model_calls 表已实现

| 项目 | 详情 |
|------|------|
| **代码实际** | model_calls 表已记录: task_id / provider / model_name / tokens_used / latency_ms / cost_estimate |
| **差距** | 缺少前端成本展示面板 |

### 3.10 workflows 表已独立建表

| 项目 | 详情 |
|------|------|
| **评价文档建议** | P1: 增加 workflows 表 |
| **代码实际** | workflow_templates 和 workflow_runs 表已存在。model_profiles 未独立建表 |

### 3.11 PAUSED -> REPLANNING 路径未实现

| 项目 | 详情 |
|------|------|
| **评价文档建议** | 增加 PAUSED -> REPLANNING 转换路径 |
| **代码实际** | 尚未实现 |


## 4. 第三部分：对评价文档建议的回应

### 4.1 P0 级建议回应

| # | 建议 | 来源 | 回应 |
|---|------|------|------|
| 1 | 删除重复文件 | 01-Eval | 接受。建议归档 |
| 2 | 建立单一权威架构文档 | 01-Eval | 接受。FlowCraft-Full-Architecture.md 作为权威文档 |
| 3 | Python 嵌入打包原型验证 | 18-Eval | 待执行。Phase 3 关键前置 |
| 4 | LICENSE 文件 | 24-Eval | 待执行。建议 MIT 或 Apache 2.0 |
| 5 | Git 历史敏感信息扫描 | 24-Eval | 待执行 |
| 6 | 补齐核心模块单元测试 | 19-Eval | 部分接受。实际有18个测试文件但核心模块仍缺 |

### 4.2 P1 级建议回应

| # | 建议 | 来源 | 回应 |
|---|------|------|------|
| 1 | MVP 范围回溯 | 01-Eval | 接受。DAG/ITERATIVE等超额功能需评估收敛 |
| 2 | Tauri 桌面端原型 | 01/15-Eval | 待执行 |
| 3 | 减少字段冗余 | 02-Eval | 部分接受。冗余是有意为之的模块解耦 |
| 4 | 明确 WAITING_TOOL vs OBSERVING | 02-Eval | 接受 |
| 5 | 增加 workflows/model_profiles 表 | 03-Eval | 部分完成。workflows已有，model_profiles未建 |
| 6 | 数据访问层抽象 | 03-Eval | 待评估 |
| 7 | 补充 macOS 路径 | 04-Eval | 待执行 |
| 8 | 增加 SSE 端点 | 05-Eval | 已完成。评价文档需更新此项判断 |
| 9 | 同步设计文档与代码实现 | 05-Eval | 接受 |
| 10 | 预留纯 asyncio 方案 | 06-Eval | 待评估 |
| 11 | 超时前触发检查点 | 06-Eval | 部分实现。建议超时前30秒紧急保存 |
| 12 | MVP 简化 ModelPolicy | 07-Eval | 已实现。当前只有一个活跃adapter |
| 13 | LLM 作为意图识别兜底 | 08-Eval | 已实现。IntentEngine 优先LLM->启发式回退 |
| 14 | 多模型交叉审查 | 09-Eval | 架构接受。纳入 MultiModelHybridStrategy 开发计划 |
| 15 | 组件交互 Sequence Diagram | 10-Eval | 待执行 |
| 16 | 低风险工具快速通道 | 11-Eval | 部分接受 |
| 17 | PolicyEngine 简化为 if-else | 12-Eval | 当前已过此阶段 |
| 18 | 聚焦核心两层记忆 | 13-Eval | 接受 |
| 19 | TraceEvent payload 标准化 | 14-Eval | 待执行 |
| 20 | Windows 打包原型提前 | 20-Eval | 待评估 |

### 4.3 P2 级建议回应

| # | 建议 | 来源 | 回应 |
|---|------|------|------|
| 1 | 添加性能基准 | 01-Eval | 接受 |
| 2 | 补齐 PAUSED 状态转换 | 02-Eval | 待执行 |
| 3 | 添加 Schema 版本字段 | 02-Eval | 待执行 |
| 4 | Lazy initialization 目录 | 04-Eval | 接受 |
| 5 | temp 目录清理策略 | 04-Eval | 待执行 |
| 6 | API 版本策略 | 05-Eval | 接受 |
| 7 | 用户可配置超时 | 06-Eval | 接受 |
| 8 | fallback prompt 策略 | 07-Eval | 待执行 |
| 9 | 1-2 模块试点多模型 | 22-Eval | 接受. 建议 Planner + RAG |
| 10 | MCP 协议调研 | 23-Eval | 待执行 |
| 11 | RAG 评估指标 | 23-Eval | 接受. 已有 Recall@K, MRR, Faithfulness |
| 12 | 模型供应商评估矩阵 | 25-Eval | 待执行 |

### 4.4 评价文档中需要修正的判断

以下评价文档的判断基于过时信息，需要更新：

| # | 误判 | 来源 | 实际 |
|---|------|------|------|
| 1 | 缺少 SSE 流式推送 | 05-Eval P1 | 已实现 GET /api/tasks/{id}/stream |
| 2 | 4 个测试文件 | 19-Testing | 实际 18 个测试文件 |
| 3 | 测试覆盖率 ~5% | 19-Eval | 需要重新评估 |
| 4 | 缺少 workflows 表 | 03-Eval P1 | 已存在 workflow_templates + workflow_runs |
| 5 | 缺少 model_calls 记录 | 14-Observability | 已存在 model_calls 表 |
| 6 | 启发式意图识别为主 | 08-Eval | 实际 LLM 优先 -> 启发式回退 |

---

> **报告版本**: v1.0
> **对比完成日期**: 2026-06-17
> **涉及文档**: 25 组设计文档 + 25 组评价文档
> **涉及代码**: core/flowcraft_core/ (66 个 Python 源文件, ~19,457 行)
"""

path = r"D:\work\FlowCraft\TechnicalArchitecture\CompareDesignAndCode-Implementation_Differences_and_Eval_Response.md"
with open(path, "w", encoding="utf-8") as f:
    f.write(text)
print(f"Written {len(text)} chars to {path}")
