from __future__ import annotations

import asyncio as _asyncio
import logging
import re

from flowcraft_core.domain.schemas import AgentRequest, TaskBrief
from flowcraft_core.models.gateway import ModelGateway

logger = logging.getLogger(__name__)

INTENT_TIMEOUT = 20  # seconds

# ── Workflow intent pre-filter ───────────────────────────────
# Distinguish "list/view workflows" from "create/build workflow"
# to prevent wrong routing to the WorkflowBuilder.

_WORKFLOW_QUERY_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:有哪些?|列出?|查看?|显示?|展示?|看看?|查询?|搜索?|找.{0,2}看)"
               r".{0,10}(?:工作流|workflow)", re.IGNORECASE),
    re.compile(r"(?:工作流|workflow).{0,10}"
               r"(?:列表|有哪些?|在哪|多少|几个|什么)", re.IGNORECASE),
    re.compile(r"(?:list|show|display|view|find|search|get|see|what|how many)"
               r".{0,10}(?:workflow|workflows)", re.IGNORECASE),
    re.compile(r"(?:workflow|workflows).{0,10}"
               r"(?:list|available|exist|saved|created|I have)", re.IGNORECASE),
    re.compile(r"^(?:工作流|workflow)\s*(?:列表|查询|list|search)?$", re.IGNORECASE),
    re.compile(r"(?:我|我们).{0,5}(?:有|创建了|做了|定义了)"
               r".{0,5}(?:哪些?|什么|多少).{0,5}(?:工作流|workflow)", re.IGNORECASE),
    re.compile(r"(?:show|list|view)\s+(?:my\s+)?(?:workflows|workflow)", re.IGNORECASE),
    re.compile(r"(?:example|demo|sample|示例|例子|样例)"
               r".{0,10}(?:workflow|工作流)", re.IGNORECASE),
]

_WORKFLOW_CREATE_PATTERNS: list[re.Pattern] = [
    re.compile(r"(?:创建|新建|制作|构建|生成|设计|编写|写|建)"
               r".{0,10}(?:工作流|workflow)", re.IGNORECASE),
    re.compile(r"(?:工作流|workflow).{0,10}"
               r"(?:创建|新建|制作|构建|生成|设计)", re.IGNORECASE),
    re.compile(r"(?:create|build|make|generate|design|write|craft)"
               r".{0,10}(?:workflow|workflows)", re.IGNORECASE),
    re.compile(r"(?:workflow|workflows).{0,10}"
               r"(?:creator|builder|generator|creation)", re.IGNORECASE),
    re.compile(r"(?:帮我|请|来|要|想|准备|打算).{0,5}"
               r"(?:创建|新建|做|弄|搞).{0,5}(?:个|一个)"
               r".{0,5}(?:工作流|workflow|自动化|流程)", re.IGNORECASE),
    re.compile(r"(?:create|build|make)\s+(?:a\s+)?(?:new\s+)?(?:workflow|automation)", re.IGNORECASE),
]


_WORKFLOW_EXECUTE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^(?:执行|运行|启动|run|execute|start)\s*(?:工作流|workflow)[\s:：]", re.IGNORECASE),
    re.compile(r"^\s*(?:run|execute|start)\s+(?:the\s+)?(?:workflow|automation)", re.IGNORECASE),
    re.compile(r"^(?:Execute|Run|Start)\s+workflow", re.IGNORECASE),
]


def _detect_workflow_intent(text: str) -> str | None:
    """Quick pre-filter: is the user asking about workflows?

    Returns:
        "query"   — user wants to list/search/view workflows
        "create"  — user wants to create/build a workflow
        "execute" — user wants to run an existing workflow (treat as normal task)
        None      — not workflow-related
    """
    # 执行检测必须最先：避免"执行工作流"被后续 LLM 识别误判为 WORKFLOW_AUTOMATION
    for pattern in _WORKFLOW_EXECUTE_PATTERNS:
        if pattern.search(text):
            logger.info("Workflow intent pre-filter: execute → normal task")
            return "execute"
    for pattern in _WORKFLOW_QUERY_PATTERNS:
        if pattern.search(text):
            logger.info("Workflow intent pre-filter: query → QA")
            return "query"
    for pattern in _WORKFLOW_CREATE_PATTERNS:
        if pattern.search(text):
            logger.info("Workflow intent pre-filter: create → WORKFLOW_AUTOMATION")
            return "create"
    return None


def _build_workflow_query_brief(text: str, task_id: str) -> TaskBrief:
    """Build a TaskBrief for workflow query/list intent — routes to QA."""
    return TaskBrief(
        task_id=task_id, objective=text,
        task_type="KNOWLEDGE_QA",
        required_capabilities=["knowledge"],
        requires_tools=True,
        risk_level="LOW",
        success_criteria=["列出用户可用的工作流并简要说明每个工作流的用途"],
        expected_output_format="text",
    )


def _build_workflow_create_brief(text: str, task_id: str) -> TaskBrief:
    """Build a TaskBrief for workflow creation intent."""
    return TaskBrief(
        task_id=task_id, objective=text,
        task_type="WORKFLOW_AUTOMATION",
        required_capabilities=["chat"],
        risk_level="LOW",
        success_criteria=["引导用户完成工作流创建"],
        expected_output_format="text",
    )


class IntentEngine:
    def __init__(self, model_gateway: ModelGateway) -> None:
        self.model_gateway = model_gateway

    async def recognize(self, task_id: str, request: AgentRequest) -> TaskBrief:
        # ── Workflow intent pre-filter (fast, no LLM cost) ────
        wf_intent = _detect_workflow_intent(request.raw_input)
        if wf_intent == "query":
            return _build_workflow_query_brief(request.raw_input, task_id)
        if wf_intent == "create":
            return _build_workflow_create_brief(request.raw_input, task_id)
        if wf_intent == "execute":
            # 执行已有工作流 → 走正常意图识别，不路由到 Workflow Builder
            pass  # fall through to LLM-based recognition below

        # ── Normal LLM-based recognition ──────────────────────
        try:
            payload = await _asyncio.wait_for(
                self.model_gateway.generate_structured(request.raw_input, "TaskBrief"),
                timeout=INTENT_TIMEOUT,
            )
        except _asyncio.TimeoutError:
            logger.warning("Intent recognition timed out (%ds)", INTENT_TIMEOUT)
            payload = self.model_gateway._heuristic_task_brief(request.raw_input)
        except Exception as exc:
            logger.warning("Intent recognition failed: %s, using heuristic", exc)
            payload = self.model_gateway._heuristic_task_brief(request.raw_input)

        # 防御：如果 pre-filter 检测到"执行工作流"，但 LLM 误判为 WORKFLOW_AUTOMATION，强制修正
        if wf_intent == "execute" and payload.get("task_type") == "WORKFLOW_AUTOMATION":
            logger.warning(
                "LLM misclassified workflow execution as WORKFLOW_AUTOMATION, "
                "overriding to FILE_TASK"
            )
            payload["task_type"] = "FILE_TASK"
        return TaskBrief(task_id=task_id, **payload)

