# AI新闻每日简报自动化工作流 — 完整执行流程分析

---

## 总览

```
用户点击「▶ Run」
    ↓
前端 fetch POST /api/workflows/{id}/run
    ↓
后端读库 → 构建 prompt → 创建任务 → 后台执行
    ↓
Pipeline: 意图识别 → 计划生成 → 策略检查 → 4步执行 → 完成
    ↓
产出: ai_news_briefing_YYYY-MM-DD.md (保存在 workspace 目录)
```

---

## 一、触发入口：前端 → 后端

### 1.1 前端 (index.html)
```javascript
runWorkflow('wf_641aa7099e7f4b189ec2c2fce9a43b3f')
  → fetch POST /api/workflows/wf_641aa.../run
     body: { "session_id": "session_xxx" }
```
**文件**: `core/flowcraft_core/web/index.html` — runWorkflow 函数

### 1.2 HTTP Server (simple_server.py)
```python
do_POST()
  → 匹配路径: /api/workflows/{id}/run
  → body = self._body()                          # 解析 JSON 请求体
  → 查库: SELECT * FROM workflow_templates       # 读取工作流定义
  → json.loads(data_json)                         # 解析 4 个步骤
  → 构建 prompt:
      "执行工作流: AI新闻每日简报自动化工作流
       描述: 自动获取当日AI新闻...
       步骤: [{index:1, title:'搜索当日AI新闻', tool_name:'web.search'}, ...]"
  → asyncio.run(app.runtime.create_task_async(request))
  → 返回 {"task_id": "...", "status": "started"}
```
**文件**: `core/flowcraft_core/simple_server.py` — do_POST 方法, 617-661行

### 1.3 任务创建 (runtime/engine.py)
```python
create_task_async(request)
  → 创建 Task 对象 (task_id, session_id, title, objective)
  → 写入 tasks 表 (SQLite)
  → 注册活跃任务到 _active_tasks 字典
  → 启动后台线程 _run_pipeline()
  → 立即返回 task (前端收到 'started' 响应，开始 SSE 轮询)
```
**文件**: `core/flowcraft_core/runtime/engine.py` — create_task_async 方法, 60-178行

---

## 二、Pipeline 五阶段 (后台线程)

### 阶段 1: 意图识别

```python
IntentEngine.recognize(task_id, request)
```
**文件**: `core/flowcraft_core/intent/engine.py`

```
步骤:
  1) _detect_workflow_intent("执行工作流: AI新闻...")
     → 正则匹配 ^(?:执行|运行|启动)工作流 → 返回 "execute"
     
  2) 走正常 LLM 识别路径 (不路由到 Workflow Builder)
     → model_gateway.generate_structured(raw_input, "TaskBrief")
     → 调用 DeepSeek V4 Pro API
     → 返回: {task_type: "FILE_TASK", risk_level: "LOW", ...}
     
  3) 兜底防御: 如果 LLM 误判为 WORKFLOW_AUTOMATION
     → 强制覆写为 FILE_TASK
     
  4) 写入 intent.recognized 事件 → 前端显示"已识别任务意图"
```

### 阶段 2: 计划生成

```python
Planner.create_plan(brief)
```
**文件**: `core/flowcraft_core/planning/planner.py`

```
  LLM 生成执行计划 (temperature=0.15, max_tokens=3072):
  
  输入:
    - 任务目标: "执行工作流: AI新闻每日简报..."
    - 工作流步骤描述 (4步)
    - 可用工具列表 (web.search, browser.read, file.write, file.read)
    
  输出 (JSON):
    {
      "mode": "LINEAR",
      "steps": [
        {index:1, title:"搜索当日AI新闻",
         action_type:TOOL, tool_name:web.search, risk_level:LOW},
        {index:2, title:"抓取重点新闻全文",
         action_type:TOOL, tool_name:browser.read, risk_level:LOW},
        {index:3, title:"多角度分析并生成简报",
         action_type:MODEL_ANSWER, risk_level:LOW},
        {index:4, title:"保存简报文件",
         action_type:TOOL, tool_name:file.write, risk_level:MEDIUM}
      ]
    }
    
  → PlanValidator.validate(plan)  # 校验步骤合法性
  → 写入 plan.created 事件 → 前端显示"已生成执行计划 (4个步骤)"
```

### 阶段 3: 策略检查

```python
PolicyEngine.check_plan(task_id, plan)
```
**文件**: `core/flowcraft_core/policy/engine.py`

```
  遍历 4 个步骤:
    Step 1-3: risk_level=LOW → 直接放行
    Step 4: risk_level=MEDIUM → 检查是否需要审批
  
  本工作流: 所有步骤均通过 → 不需要用户审批
  → policy.checked 事件
```

---

### 阶段 4: 步骤执行 (核心 — 4步逐一执行)

**文件**: `core/flowcraft_core/execution/engine.py` — execute_plan 方法, 209-328行

执行模式: LINEAR (顺序执行, step_outputs 累积前序输出)

#### Step 1: 搜索当日AI新闻 [TOOL — web.search]

```
1) _build_context → 注入任务目标 + 会话记忆
2) _build_step_prompt → Task Context + Available Tools + Decision 格式
3) _llm_decide_with_retry:
   → DeepSeek 返回 {action:"tool_call", tool_name:"web.search",
     tool_input:{query:"AI news today 2025...", max_results:15}}
4) ToolHarness.execute → WebSearchTool.execute()
   → Provider 1: DuckDuckGo API (httpx GET)
   → Provider 2 (fallback): Bing cn.bing.com (正则解析 HTML)
   → Provider 3 (fallback): DuckDuckGo HTML 抓取
   → 返回 ToolObservation {status:"COMPLETED", results:[...]}
5) LLM 验证 → final_answer: "已搜索到15条新闻..."
6) CompletionChecker.check_step → 质量验证 → 通过
7) _remember_step → 写入记忆系统 + 向量索引
8) CheckpointManager.save → 检查点持久化
```

**Python 脚本/模块**: `tools/network.py` (WebSearchTool + httpx), `tools/harness.py` (ToolHarness), `execution/completion_checker.py`, `memory/manager.py`, `execution/checkpoint.py`

#### Step 2: 抓取重点新闻全文 [TOOL — browser.read]

```
1) _build_context → 注入 Step 1 完整搜索结果 (prior_text)
   "## 已完成步骤的输出
    ### 步骤 1
    {15条新闻标题、URL、摘要}"

2) LLM 决策 → 选择3-5篇最重要新闻, 逐一调用 browser.read(url)
3) ToolHarness.execute → BrowserReadTool.execute()
   → httpx.AsyncClient.get(url) → HTML解析提取正文
4) LLM 完成 → final_answer: "已抓取5篇重点新闻全文..."
```

**Python 脚本/模块**: `tools/browser.py` (BrowserReadTool), `tools/network.py` (httpx client)

#### Step 3: 多角度分析并生成简报 [MODEL_ANSWER]

```
★ 此步骤无工具调用 — 完全由 LLM (DeepSeek) 完成 ★

1) _build_context → 注入 Step 1 (摘要) + Step 2 (全文) 完整输出
2) _build_step_prompt → 激活 Large Output Strategy:
   检测到"简报/Markdown/报告"关键词, 提示 LLM 分块输出
3) LLM 基于上下文完成:
   a) 多角度解读: 技术影响 / 商业价值 / 行业趋势 / 潜在风险
   b) 评估信息来源可信度
   c) 按重要性排序, 控制阅读时间约15分钟
   d) 撰写完整 Markdown 简报 (标题+摘要+逐条分析+综合点评)
4) CompletionChecker.check_step:
   → parse_expected_length() 检查字数
   → 输出过短 → needs_replan → 再给一轮
   → 通过
```

**Python 脚本/模块**: `execution/completion_checker.py` (字数验证), `models/gateway.py` (LLM调用), `models/adapters/openai_compatible.py` (API适配)

#### Step 4: 保存简报文件 [TOOL — file.write + file.read]

```
1) LLM 决策 → tool_call: file.write {
     path:"ai_news_briefing_2026-06-21.md",
     content:"{完整 Markdown 简报}"
   }
2) ToolHarness.execute → FileWriteTool.execute()
   → is_path_allowed() 安全检查
   → path.parent.mkdir() 创建目录
   → path.write_text(content, encoding="utf-8")
3) LLM 验证 → tool_call: file.read 确认完整性
4) final_answer: "简报已保存, 验证通过"
```

**Python 脚本/模块**: `tools/builtin.py` (FileWriteTool/FileReadTool), `tools/base.py` (is_path_allowed)

---

### 阶段 5: 任务完成

```
CompletionChecker.check_task → 4/4步骤完成 → is_complete=True
task.status = COMPLETED → 更新 tasks 表
task.completed 事件 → 前端 SSE 收到 → 停止轮询
活跃任务清理 (_active_tasks + SSE队列)
```

**文件**: `core/flowcraft_core/execution/completion_checker.py`

---

## 三、Python 模块完整调用链

```
HTTP 服务          simple_server.py
应用入口           app.py (FlowCraftApp)
任务编排           runtime/engine.py (RuntimeEngine)
意图识别           intent/engine.py (IntentEngine + 正则预过滤)
计划生成           planning/planner.py (Planner + PlanValidator)
策略检查           policy/engine.py (PolicyEngine)
步骤执行           execution/engine.py (ExecutionEngine)
LLM 决策           models/gateway.py (ModelGateway)
LLM 适配           models/adapters/openai_compatible.py
工具注册/调度      tools/harness.py (ToolHarness, ToolRegistry)
网页搜索           tools/network.py (WebSearchTool + DuckDuckGo/Bing)
浏览器读取         tools/browser.py (BrowserReadTool + HTML解析)
文件读写           tools/builtin.py (FileReadTool, FileWriteTool)
HTTP客户端         tools/network.py (httpx.AsyncClient)
完成判定           execution/completion_checker.py (含字数验证)
上下文压缩         execution/context_compressor.py
上下文摘要         memory/context_summarizer.py (smart_truncate)
记忆系统           memory/manager.py + vector_store.py
检查点             execution/checkpoint.py
事件/SSE           observability/events.py
数据库             storage/database.py (SQLite)
配置               config/settings.py
```

---

## 四、是否需要外部 Python 脚本?  不需要

该工作流 4 步全部使用 FlowCraft 内置工具, 无外部 .py 依赖:

| 步骤 | 工具 | 底层实现 | 外部依赖 |
|------|------|---------|---------|
| Step 1 | web.search | httpx → DuckDuckGo/Bing HTTP API | 无 |
| Step 2 | browser.read | httpx → 网页抓取 + HTML解析 | 无 |
| Step 3 | MODEL_ANSWER | DeepSeek LLM API 调用 | 需要 API Key |
| Step 4 | file.write | Python 内置 open()/write() | 无 |

与旧版 v1.0 的关键区别:
```
旧 Step 3: command.run → python fetch_expert_opinions.py  ← 不存在!
旧 Step 4-6: code.execute → 沙箱执行 (无AI能力, 模板变量未解析)
旧 Step 2-8: {{template}} 变量 → 原始字符串传入, 无人替换
→ 以上已在 v2.0.0 全部删除
```

---

## 五、上下文传递机制 (记忆系统 — 本次修复的核心)

每一步执行时, 前序步骤完整输出注入 LLM 上下文:

```
_build_context() 组装:
  session_context           ← 会话记忆 (语义检索 + 衰减)
  _build_memory_context     ← 任务目标 + 工具观察(最近6个)
  prior_text                ← ★ 前序步骤完整输出 (不再硬截断)
      "## 已完成步骤的输出
       ### 步骤 1
       {15条完整搜索结果}
       ### 步骤 2
       {3-5篇完整新闻全文}"
  compressed.context_text   ← 上下文压缩器

预算溢出时:
  smart_truncate() 智能压缩
    CRITICAL: 任务目标+当前步骤 → 全文保留
    HIGH: 最近观察 → 全文或按条目截断
    MEDIUM: 会话历史+前序步骤 → 保留标题, 压缩正文
    LOW: 旧内容 → 预算宽裕时保留
```

已修复的 bug:
- prior_text 不再硬截断为 200 字符 → 完整保留
- _add_step_summary 不再硬截断为 300 字符 → 完整保留
- CompletionChecker 新增 parse_expected_length() 字数验证

---

## 六、最终产出

```
D:\work\FlowCraft\workspace\
  └── ai_news_briefing_2026-06-21.md
      ├── # AI新闻每日简报
      ├── ## 摘要
      ├── ## 重点新闻分析 (3-5篇, 每篇含多角度解读)
      │   ├── 技术影响
      │   ├── 商业价值
      │   ├── 行业趋势
      │   └── 潜在风险
      ├── ## 来源可信度评估
      └── ## 综合点评
```

