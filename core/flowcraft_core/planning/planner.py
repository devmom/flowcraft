"""FlowCraft Planner — 智能任务规划引擎.

支持：
- 工具感知规划：根据可用工具自动匹配步骤
- DAG 自动检测：识别可并行步骤，自动启用 DAG 模式
- 迭代求精：LLM 自审查并改进计划质量
- 子任务分解：复杂任务自动拆解
- 会话上下文注入：利用历史记忆优化规划
"""

from __future__ import annotations

import asyncio as _asyncio
import json
import logging
import time as _time
from datetime import datetime, timezone

from flowcraft_core.domain.enums import PlanMode, RiskLevel, TaskStatus
from flowcraft_core.domain.schemas import ExecutionPlan, PlanStep, TaskBrief
from flowcraft_core.logging_config import get_trace_logger, TraceSpan
from flowcraft_core.models.gateway import ModelGateway

logger = logging.getLogger(__name__)
trace = get_trace_logger("planning.planner")

PLAN_TIMEOUT = 45  # 复杂规划需要更长时间
REFINE_TIMEOUT = 20


class Planner:
    """智能规划器：工具感知 + DAG自动检测 + 迭代求精."""

    def __init__(self, model_gateway: ModelGateway, tool_registry=None, memory_manager=None, skill_registry=None) -> None:
        self.model_gateway = model_gateway
        self.tool_registry = tool_registry
        self.memory_manager = memory_manager
        self.skill_registry = skill_registry  # Phase 1: skill-aware planning

    # ── Public API ──────────────────────────────────────────

    async def create_plan(self, brief: TaskBrief) -> ExecutionPlan:
        """主入口：生成执行计划."""
        tid = brief.task_id
        trace.pipeline_begin(tid, "plan.create",
                            f"开始规划: type={brief.task_type} risk={brief.risk_level} objective={brief.objective[:80]}")
        t0 = _time.monotonic()

        if not self.model_gateway.is_live():
            trace.info(tid, "plan.fallback", "模型未接入，使用启发式规划",
                       extra={"model_live": False})
            plan = self._build_plan_from_raw(
                brief, self.model_gateway._heuristic_plan(brief.objective))
            trace.pipeline_end(tid, "plan.create",
                              f"启发式规划完成: {len(plan.steps)}步 {plan.mode.value}",
                              elapsed_s=_time.monotonic() - t0)
            return plan

        # LLM 生成
        raw = None
        try:
            trace.llm_call(tid, "_generate_plan_llm",
                          f"调用LLM生成计划 (timeout={PLAN_TIMEOUT}s)", extra={"timeout": PLAN_TIMEOUT})
            raw = await _asyncio.wait_for(
                self._generate_plan_llm(brief), timeout=PLAN_TIMEOUT)
        except _asyncio.TimeoutError:
            elapsed = _time.monotonic() - t0
            trace.llm_timeout(tid, "_generate_plan_llm",
                             f"LLM规划超时 ({PLAN_TIMEOUT}s), 回退到启发式", elapsed_s=elapsed)
            logger.warning("Plan generation timed out (%ds)", PLAN_TIMEOUT)
            raw = self.model_gateway._heuristic_plan(brief.objective)
        except Exception as exc:
            elapsed = _time.monotonic() - t0
            trace.pipeline_error(tid, "_generate_plan_llm",
                                f"LLM规划失败: {exc}", exc=exc, elapsed_s=elapsed)
            logger.warning("Plan generation failed: %s", exc)
            raw = self.model_gateway._heuristic_plan(brief.objective)

        if raw:
            trace.info(tid, "plan.raw", f"LLM返回原始计划: mode={raw.get('mode')} steps={len(raw.get('steps', []))}")

        plan = self._build_plan_from_raw(brief, raw)
        trace.info(tid, "plan.built", f"构建计划: {len(plan.steps)}步 {plan.mode.value}")

        # 迭代求精：让 LLM 自审查
        if self.model_gateway.is_live() and len(plan.steps) >= 3:
            try:
                trace.llm_call(tid, "_refine_plan",
                              f"开始迭代求精 (timeout={REFINE_TIMEOUT}s)", extra={"timeout": REFINE_TIMEOUT})
                refined = await _asyncio.wait_for(
                    self._refine_plan(plan, brief), timeout=REFINE_TIMEOUT)
                if refined and refined.steps:
                    trace.info(tid, "plan.refined",
                              f"计划已求精: {len(plan.steps)} → {len(refined.steps)} 步")
                    plan = refined
                else:
                    trace.info(tid, "plan.refined", "求精未产生改进，使用原始计划")
            except _asyncio.TimeoutError:
                trace.warn(tid, "plan.refine_timeout",
                          f"计划求精超时 ({REFINE_TIMEOUT}s)，使用原始计划")
            except Exception as exc:
                trace.warn(tid, "plan.refine_error", f"计划求精失败: {exc}")

        trace.pipeline_end(tid, "plan.create",
                          f"规划完成: {len(plan.steps)}步 {plan.mode.value}",
                          elapsed_s=_time.monotonic() - t0)
        return plan

    # ── LLM Plan Generation ─────────────────────────────────

    async def _generate_plan_llm(self, brief: TaskBrief) -> dict:
        """工具感知 + 上下文注入的计划生成."""
        tid = brief.task_id
        tools_summary = self._get_tools_summary()
        context = self._get_planning_context(brief)

        # Inject current date so search queries use the right year
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%d")
        weekday = now.strftime("%A")

        prompt = f"""## 任务信息
**目标**: {brief.objective}
**类型**: {brief.task_type}
**风险等级**: {brief.risk_level}
**约束条件**: {', '.join(brief.constraints) if brief.constraints else '无'}
**成功标准**: {', '.join(brief.success_criteria) if brief.success_criteria else '满足用户目标'}
**需要网络**: {'是' if brief.requires_network else '否'}
**需要文件**: {'是' if brief.requires_local_files else '否'}
**所需能力**: {', '.join(brief.required_capabilities) if brief.required_capabilities else '通用'}
**当前日期**: {now_str} ({weekday})

## 可用工具（只能使用列表中的工具）
{tools_summary}

{context}

## 规划要求
1. **模式选择**：
   - DIRECT: 简单问答，无需工具 → 1步 MODEL_ANSWER
   - LINEAR: 顺序执行，步骤有前后依赖
   - DAG: 存在可并行的独立步骤 → 标记 depends_on 依赖关系
   - ITERATIVE: 需要反复尝试直到满足条件

2. **步骤设计原则**：
   - 每个步骤对应一个具体的、可执行的动作
   - PREPARE = 准备上下文/确认参数
   - TOOL = 调用具体工具（必须指定 tool_name）
   - MODEL_ANSWER = LLM直接回答（无需工具）
   - OBSERVE = 等待/检查结果
   - FINALIZE = 汇总输出
   - 工具步骤的 required_tools 必须从「可用工具」列表中选择
   - 每个步骤写明 expected_output（预期产出）
   - **涉及搜索/新闻/最新/当日等时间敏感任务时，必须使用当前日期 {now_str} 构建搜索词**

2.5 **执行模式选择（execution_mode）**：
   每个步骤必须指定 execution_mode 字段，选择逻辑如下：
   | execution_mode | 使用场景 | 典型 tool_name |
   |---|---|---|
   | tool | 单个工具可完成 | exec, file.read, web_search, apply_patch |
   | skill | 存在匹配的预定义技能 | skill.execute (设 skill_name) |
   | dynamic_script | 多步数据处理、循环计算、格式转换 | (自动生成脚本) |
   | model_answer | LLM直接生成文本 | (无需工具) |

   **exec 工具的使用场景**：
   - `exec`: 执行脚本文件 (python script.py)、安装包 (pip install)、git 操作、编译构建
   - `apply_patch`: 对文件做精确的结构化修改（创建/更新/删除/补丁）
   - ⚠ 禁止 python -c 内联执行 → 先用 file.write 写入脚本，再用 exec 运行

   **选择 dynamic_script 的信号**（exec 做不到的纯计算任务）：
   - 需要在沙箱中安全执行的不信任代码
   - 纯数学/统计计算，不需要文件系统和网络
   - 需要多次迭代尝试直到找到正确答案

   **选择 exec vs dynamic_script 的判断**：
   - 需要读写文件、装包、git、pip？→ exec (tool 模式)
   - 只需要纯计算、数学、数据分析？→ dynamic_script 或 skill

   **优先使用 skill**：如果上方「可用技能」列表中有匹配的技能，优先选择 skill（确定性执行，100% 可靠）。

3. **复杂度自适应**：
   - 简单任务 1-3 步
   - 中等任务 4-8 步
   - 复杂任务 9-15 步
   - 工作流/自动化任务：覆盖完整生命周期（设计→实现→测试→交付）

4. **风险标注**：
   - 文件读取 → LOW
   - 网络请求 → MEDIUM
   - 文件写入 → MEDIUM
   - 命令执行 → HIGH
   - 文件删除 → HIGH

请用 JSON 格式返回。"""

        prompt_len = len(prompt)
        trace.debug(tid, "plan.prompt", f"LLM规划prompt长度: {prompt_len}字符",
                    extra={"prompt_len": prompt_len, "tools_count": len(tools_summary.splitlines())})

        t0 = _time.monotonic()
        result = await self.model_gateway._adapter.structured_chat(
            [
                {"role": "system", "content": (
                    "You are an expert task planner. Generate executable, tool-aware plans. "
                    "Every TOOL step MUST reference tools from the Available Tools list. "
                    "Detect parallelism opportunities and use DAG mode when steps are independent. "
                    "For complex tasks, decompose into clear sub-objectives."
                )},
                {"role": "user", "content": prompt},
            ],
            self._plan_schema(),
            temperature=0.15, max_tokens=3072,
        )
        elapsed = _time.monotonic() - t0
        trace.llm_result(tid, "_generate_plan_llm",
                        f"LLM返回 ({elapsed:.2f}s): mode={result.get('mode')} steps={len(result.get('steps', []))}",
                        elapsed_s=elapsed)
        return result

    # ── Plan Refinement ─────────────────────────────────────

    async def _refine_plan(self, plan: ExecutionPlan, brief: TaskBrief) -> ExecutionPlan | None:
        """LLM 自审查计划并改进."""
        tid = brief.task_id
        plan_json = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2)
        tools_summary = self._get_tools_summary()

        trace.debug(tid, "plan.refine.begin", f"开始求精: {len(plan.steps)}步 {plan.mode.value}")

        prompt = f"""## 原始任务
目标: {brief.objective}  |  类型: {brief.task_type}  |  风险: {brief.risk_level}

## 当前计划
{plan_json[:3000]}

## 可用工具
{tools_summary}

## 审查要点
审查当前计划，找出以下问题并修正：
1. 是否有步骤引用了不存在的工具？（只能使用上方列出的工具）
2. 是否有步骤缺少明确的 expected_output？
3. 是否有可并行执行的步骤但未标记 depends_on？
4. 步骤顺序是否合理？是否遗漏关键步骤？
5. action_type 是否正确？TOOL步骤是否指定了 tool_name？
6. 是否有不必要的步骤可以合并？
7. execution_mode 是否合理？skill 模式是否指定了有效的 skill_name？

如果计划质量良好，返回相同内容。如有改进，返回修正后的完整计划。
只改进真正有问题的部分，不要无意义重排。"""

        try:
            t0 = _time.monotonic()
            result = await self.model_gateway._adapter.structured_chat(
                [
                    {"role": "system", "content": "You are a plan reviewer. Improve plan quality. Only change what needs fixing."},
                    {"role": "user", "content": prompt},
                ],
                self._plan_schema(),
                temperature=0.1, max_tokens=3072,
            )
            elapsed = _time.monotonic() - t0
            trace.llm_result(tid, "_refine_plan",
                           f"求精LLM返回 ({elapsed:.2f}s): steps={len(result.get('steps', []))}",
                           elapsed_s=elapsed)

            steps = [PlanStep(**s) for s in result.get("steps", [])]
            if not steps:
                trace.warn(tid, "plan.refine", "求精结果为空，跳过")
                return None
            # 重新编号
            for i, s in enumerate(steps):
                s.index = i + 1
            refined = ExecutionPlan(
                task_id=plan.task_id,
                mode=PlanMode(result.get("mode", plan.mode.value)),
                goal=result.get("goal", plan.goal),
                constraints=plan.constraints,
                steps=steps,
                approval_points=[s.title for s in steps if s.approval_required],
                success_criteria=plan.success_criteria,
            )
            for s in refined.steps:
                s.plan_id = refined.plan_id
            if len(refined.steps) != len(plan.steps):
                logger.info("Plan refined: %d → %d steps", len(plan.steps), len(refined.steps))
            return refined
        except Exception as exc:
            trace.warn(tid, "plan.refine", f"求精失败: {exc}")
            logger.debug("Plan refinement skipped: %s", exc)
            return None

    # ── Helpers ─────────────────────────────────────────────

    def _plan_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["DIRECT", "LINEAR", "DAG", "ITERATIVE"]},
                "goal": {"type": "string"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "title": {"type": "string"},
                            "objective": {"type": "string"},
                            "action_type": {"type": "string", "enum": ["MODEL_ANSWER", "PREPARE", "TOOL", "OBSERVE", "FINALIZE"]},
                            "required_tools": {"type": "array", "items": {"type": "string"}},
                            "tool_name": {"type": "string", "description": "Specific tool name from available tools (required for TOOL type)"},
                            "depends_on": {"type": "array", "items": {"type": "integer"}},
                            "expected_output": {"type": "string"},
                            "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                            "approval_required": {"type": "boolean"},
                            "execution_mode": {"type": "string", "enum": ["tool", "skill", "dynamic_script", "model_answer"], "description": "How this step should be executed"},
                            "skill_name": {"type": "string", "description": "Qualified skill name when execution_mode=skill"},
                            "skill_params": {"type": "object", "description": "Parameters for skill script"},
                        },
                        "required": ["index", "title", "objective", "action_type", "expected_output", "risk_level", "execution_mode"],
                    },
                },
            },
            "required": ["mode", "goal", "steps"],
        }

    def _get_tools_summary(self) -> str:
        """获取可用工具和技能的摘要."""
        parts = []
        if self.tool_registry:
            defs = self.tool_registry.list_definitions()
            lines = []
            for d in sorted(defs, key=lambda x: x.get("category", "")):
                lines.append(
                    f"- **{d['tool_name']}** [{d.get('risk_level', '?')}] "
                    f"({d.get('category', 'general')}): {d.get('description', '')[:120]}"
                )
            if lines:
                parts.append("## 可用工具\n" + "\n".join(lines))

        if self.skill_registry:
            try:
                skills_summary = self.skill_registry.get_skills_summary()
                if skills_summary and skills_summary != "(No skills available)":
                    parts.append("## 可用技能（确定性脚本）\n" + skills_summary)
            except Exception:
                pass
        return "\n\n".join(parts) if parts else "（工具注册表不可用）"

    def _get_planning_context(self, brief: TaskBrief) -> str:
        """注入会话历史上下文辅助规划."""
        if not self.memory_manager:
            return ""
        try:
            memories = self.memory_manager.get_session_memories(
                brief.task_id, max_count=5)
            if not memories:
                return ""
            lines = ["## 会话历史（辅助规划）"]
            for m in memories[:3]:
                title = m.get("title", "")[:60]
                content = m.get("content", "")[:200]
                lines.append(f"- {title}: {content}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _build_plan_from_raw(self, brief: TaskBrief, raw: dict) -> ExecutionPlan:
        steps = []
        for sd in raw.get("steps", []):
            sd.setdefault("expected_output", sd.get("objective", "完成任务步骤"))
            # 将 tool_name 合并到 required_tools
            if sd.get("tool_name") and sd["tool_name"] not in sd.get("required_tools", []):
                tools = list(sd.get("required_tools", []))
                tools.append(sd["tool_name"])
                sd["required_tools"] = tools
            # Phase 1: set defaults for new fields
            sd.setdefault("execution_mode", "tool")
            sd.setdefault("skill_name", None)
            sd.setdefault("skill_params", {})
            # Auto-detect execution_mode from action_type if not set
            if sd.get("action_type") == "MODEL_ANSWER" and sd.get("execution_mode") == "tool":
                sd["execution_mode"] = "model_answer"
            steps.append(PlanStep(**sd))
        plan = ExecutionPlan(
            task_id=brief.task_id,
            mode=PlanMode(raw.get("mode", "LINEAR")),
            goal=raw.get("goal", brief.objective),
            constraints=brief.constraints,
            steps=steps,
            risk_points=[],
            approval_points=[s.title for s in steps if s.approval_required],
            fallback_strategy={"default": "失败时停止并尝试替代方案"},
            stop_conditions=["满足成功标准", "用户取消任务", "策略阻止继续"],
            success_criteria=brief.success_criteria,
        )
        for s in plan.steps:
            s.plan_id = plan.plan_id
        return plan


class PlanValidator:
    """计划校验器：结构 + 语义检查."""

    def validate(self, plan: ExecutionPlan) -> list[str]:
        errors: list[str] = []
        if not plan.steps:
            errors.append("计划必须至少包含一个步骤。")
        if len(plan.steps) > 20:
            errors.append(f"计划步骤不能超过 20 步（当前 {len(plan.steps)} 步）。")

        seen_indices = set()
        for step in plan.steps:
            if not step.title:
                errors.append(f"步骤 {step.index} 缺少标题。")
            if step.index in seen_indices:
                errors.append(f"步骤索引重复: {step.index}")
            seen_indices.add(step.index)
            if step.risk_level.value in ("HIGH", "CRITICAL") and not step.approval_required:
                errors.append(f"高风险步骤 {step.index}（{step.title}）必须要求审批。")
            if step.action_type == "TOOL" and not step.required_tools:
                errors.append(f"步骤 {step.index}（{step.title}）是 TOOL 类型但未指定工具。")
            # 检查 depends_on 引用有效性
            for dep in step.depends_on:
                if dep not in seen_indices and dep >= step.index:
                    errors.append(f"步骤 {step.index} 依赖不存在的步骤 {dep}。")

        return errors

