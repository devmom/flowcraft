from __future__ import annotations

import json
import logging
from typing import Any

from flowcraft_core.models.adapters.base import ModelProfile, ProviderAdapter
from flowcraft_core.models.adapters.openai_compatible import OpenAICompatibleAdapter

logger = logging.getLogger(__name__)

# ── DeepSeek V4 Default Configuration ─────────────────────
# 参考：https://api-docs.deepseek.com/zh-cn/quick_start/pricing
DEFAULT_DEEPSEEK_PROFILE = ModelProfile(
    model_id="deepseek-v4-pro",
    provider="deepseek",
    display_name="DeepSeek V4 Pro",
    base_url="https://api.deepseek.com",   # 不带 /v1，端点: /chat/completions
    capabilities=["chat", "structured_chat"],
    context_window=1_000_000,
    supports_structured_output=True,
    supports_streaming=True,
    cost_input_per_1k=0.00042,   # 3元/M (2.5折优惠期), 约 $0.42/M input
    cost_output_per_1k=0.00084,  # 6元/M (2.5折优惠期), 约 $0.84/M output
)

# DeepSeek V4 Flash — 更经济的替代方案
DEEPSEEK_V4_FLASH_PROFILE = ModelProfile(
    model_id="deepseek-v4-flash",
    provider="deepseek",
    display_name="DeepSeek V4 Flash",
    base_url="https://api.deepseek.com",
    capabilities=["chat", "structured_chat"],
    context_window=1_000_000,
    supports_structured_output=True,
    supports_streaming=True,
    cost_input_per_1k=0.00014,   # 1元/M, 约 $0.14/M input
    cost_output_per_1k=0.00028,  # 2元/M, 约 $0.28/M output
)

# 旧版兼容
# deepseek-chat / deepseek-reasoner 将于 2026/07/24 弃用
# 分别映射到 deepseek-v4-flash 的非思考/思考模式
DEEPSEEK_CHAT_LEGACY = ModelProfile(
    model_id="deepseek-chat",
    provider="deepseek",
    display_name="DeepSeek V3 (Legacy)",
    base_url="https://api.deepseek.com",
    capabilities=["chat", "structured_chat"],
    context_window=128000,
    supports_structured_output=True,
    supports_streaming=True,
    cost_input_per_1k=0.00027,
    cost_output_per_1k=0.00110,
)


class ModelGateway:
    """Central model access point.

    Supported providers:
    - 'deepseek': DeepSeek V4 Pro / Flash (api.deepseek.com)
    - 'agnes': Agnes AI (apihub.agnes-ai.com) — free tier, OpenAI compatible
    - 'ollama': Local models via Ollama
    - 'deterministic-dev': heuristic fallback (MVP dev mode)

    Default configuration: DeepSeek V4 Pro.
    Set FLOWCRAFT_DEEPSEEK_API_KEY or AGNES_API_KEY env var or configure via settings.
    """

    def __init__(self) -> None:
        self._adapter: ProviderAdapter | None = None
        self._profile: ModelProfile = DEFAULT_DEEPSEEK_PROFILE
        self.provider_name = "deterministic-dev"
        self.model_configured = False

    # ── Configuration ───────────────────────────────────────

    def configure(
        self,
        adapter: ProviderAdapter,
        profile: ModelProfile | None = None,
    ) -> None:
        """注入外部 adapter（从 SecretStore 加载 API Key 后调用）."""
        self._adapter = adapter
        if profile:
            self._profile = profile
        self.provider_name = adapter.profile.provider
        self.model_configured = True
        logger.info("ModelGateway configured: provider=%s model=%s", self.provider_name, self._profile.model_id)

    def switch_model(self, model_id: str, api_key: str | None = None) -> bool:
        """Switch the active model at runtime.

        Supports:
        - DeepSeek:  deepseek-v4-pro, deepseek-v4-flash, deepseek-chat
        - Agnes:     agnes-2.0-flash, agnes-1.5-flash
        - Anthropic: claude-opus-4-20250514, claude-sonnet-4-20250514, claude-3-5-haiku-20241022

        Returns True if switched successfully.
        """
        from flowcraft_core.models.adapters.agnes import (
            AGNES_2_FLASH_PROFILE, AGNES_1_5_FLASH_PROFILE, AgnesTextAdapter, is_agnes_llm,
        )
        from flowcraft_core.models.adapters.anthropic import (
            ANTHROPIC_PROFILES, AnthropicAdapter, is_anthropic_model,
        )

        profiles: dict[str, ModelProfile] = {
            "deepseek-v4-pro": DEFAULT_DEEPSEEK_PROFILE,
            "deepseek-v4-flash": DEEPSEEK_V4_FLASH_PROFILE,
            "deepseek-chat": DEEPSEEK_CHAT_LEGACY,
            "agnes-2.0-flash": AGNES_2_FLASH_PROFILE,
            "agnes-1.5-flash": AGNES_1_5_FLASH_PROFILE,
            **ANTHROPIC_PROFILES,
        }
        new_profile = profiles.get(model_id)
        if not new_profile:
            logger.warning("Unknown model_id: %s", model_id)
            return False

        key = api_key
        if not key and self._adapter:
            key = getattr(self._adapter, '_api_key', None)

        if not key:
            logger.warning("No API key available for model switch")
            return False

        try:
            if is_anthropic_model(model_id):
                adapter = AnthropicAdapter(new_profile, api_key=key)
            elif is_agnes_llm(model_id):
                adapter = AgnesTextAdapter(new_profile, api_key=key)
            else:
                adapter = OpenAICompatibleAdapter(new_profile, api_key=key)
            self._adapter = adapter
            self._profile = new_profile
            self.provider_name = new_profile.provider
            logger.info("ModelGateway switched to: %s (provider=%s)", model_id, new_profile.provider)
            return True
        except Exception as exc:
            logger.warning("Model switch failed: %s", exc)
            return False

    @property
    def current_model_id(self) -> str:
        return self._profile.model_id

    def is_live(self) -> bool:
        return self._adapter is not None and self.model_configured

    # ── Failover Chain ─────────────────────────────────────

    async def call_with_fallback(
        self,
        messages: list[dict],
        fallback_chain: list | None = None,
        **kwargs,
    ) -> str:
        """Call model with automatic failover chain.

        Tries each model in the chain sequentially. If the primary fails,
        automatically falls back to the next candidate.

        Default chain: DeepSeek V4 Pro → DeepSeek V4 Flash → Agnes 2.0 Flash

        Args:
            messages: Chat messages
            fallback_chain: Optional list of ModelProfile. None = default chain.
            **kwargs: Forwarded to adapter.chat()

        Returns:
            Model response text.

        Raises:
            RuntimeError if ALL models in the chain fail.
        """
        if fallback_chain is None:
            from flowcraft_core.models.adapters.agnes import AGNES_2_FLASH_PROFILE, AgnesTextAdapter
            fallback_chain = [
                (DEFAULT_DEEPSEEK_PROFILE, None),          # DeepSeek V4 Pro
                (DEEPSEEK_V4_FLASH_PROFILE, None),          # DeepSeek V4 Flash
                (AGNES_2_FLASH_PROFILE, AgnesTextAdapter),  # Agnes 2.0 Flash (free)
            ]

        last_error = None
        for profile, adapter_cls in fallback_chain:
            try:
                # Get API key for this profile
                key = self._get_key_for_profile(profile)
                if not key:
                    logger.debug("No key for %s, skipping in failover", profile.model_id)
                    continue

                # Switch or create adapter
                if adapter_cls:
                    adapter = adapter_cls(profile, api_key=key)
                else:
                    adapter = OpenAICompatibleAdapter(profile, api_key=key)

                result = await adapter.chat(messages, **kwargs)
                logger.info("Failover: successfully used %s", profile.model_id)
                return result
            except Exception as exc:
                last_error = exc
                logger.warning("Failover: %s failed (%s), trying next...", profile.model_id, exc)

        raise RuntimeError(
            f"All {len(fallback_chain)} models in failover chain failed. "
            f"Last error: {last_error}"
        )

    def _get_key_for_profile(self, profile) -> str | None:
        """Get API key for a model profile from env or current adapter."""
        import os
        key = None
        if profile.provider == "deepseek":
            key = os.environ.get("FLOWCRAFT_DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        elif profile.provider == "agnes":
            key = os.environ.get("AGNES_API_KEY")
        # Fallback: reuse current adapter's key
        if not key and self._adapter:
            key = getattr(self._adapter, '_api_key', None)
        return key

    # ── Public API ──────────────────────────────────────────

    async def generate_text(self, prompt: str) -> str:
        if self.is_live() and self._adapter:
            return await self._adapter.chat([{"role": "user", "content": prompt}])
        # Deterministic fallback
        return self._fallback_text(prompt)

    def _identity_text(self) -> str:
        """Identity for structured output calls with intent classification guidance."""
        profile = self._profile
        return (
            f"You are FlowCraft Agent v0.1.0 running on {profile.display_name} by {profile.provider}. "
            f"Generate output matching the requested schema. "
            f"When classifying task_type: "
            f"WORKFLOW_AUTOMATION is ONLY for creating/building NEW workflows. "
            f"For listing/viewing/searching EXISTING workflows, use KNOWLEDGE_QA or QA. "
            f"If the user asks 'what workflows do I have' or 'list workflows' or 'show workflows', "
            f"that is NOT WORKFLOW_AUTOMATION — classify it as KNOWLEDGE_QA."
        )

    async def generate_structured(self, prompt: str, schema_name: str) -> dict[str, Any]:
        schema = self._get_schema(schema_name)
        if self.is_live() and self._adapter:
            try:
                messages = [
                    {"role": "system", "content": self._identity_text()},
                    {"role": "user", "content": prompt},
                ]
                return await self._adapter.structured_chat(messages, schema)
            except Exception as exc:
                logger.warning("Structured chat failed, falling back to heuristic: %s", exc)
                # Fall through to heuristic
        # Deterministic fallback
        if schema_name == "TaskBrief":
            return self._heuristic_task_brief(prompt)
        if schema_name == "ExecutionPlan":
            return self._heuristic_plan(prompt)
        return {}

    async def test_connection(self) -> dict[str, Any]:
        """测试当前配置的模型连接."""
        if not self.is_live() or not self._adapter:
            return {
                "status": "not_configured",
                "message": "Model not configured. Use settings to configure a model provider.",
            }
        return await self._adapter.test_connection()

    # ── Fallback / Heuristic ────────────────────────────────

    @staticmethod
    def _fallback_text(prompt: str) -> str:
        if "FlowCraft" in prompt or "flowcraft" in prompt.lower():
            return (
                "FlowCraft 是一个 Harness-first 的本地 Agent 工作流框架。"
                "它通过任务状态、规划、策略、审批、工具网关和审计时间线，"
                "让个人和小团队安全地构建可复用的 AI 工作流。"
            )
        return "FlowCraft 已完成该步骤（开发模式，未接入真实模型）。"

    def _heuristic_task_brief(self, text: str) -> dict[str, Any]:
        lower = text.lower()

        # Phase 2: Extended task type detection
        has_spreadsheet = any(w in text for w in ["excel", "表格", "xlsx", "xls", "csv", "spreadsheet", "工作表"])
        has_email = any(w in text for w in ["邮件", "email", "发送邮件", "send email", "收件箱", "inbox"])
        has_schedule = any(w in text for w in ["定时", "每天", "每周", "每月", "定期", "schedule", "cron", "daily", "weekly"])
        has_knowledge = any(w in text for w in ["知识库", "文档问答", "检索", "知识问答", "knowledge base", "document qa"])
        has_research = any(w in text for w in ["研究", "调研", "深度分析", "多步分析", "research", "deep analysis", "investigate"])

        # Workflow intent detection (must come before other heuristics)
        has_workflow_create = any(w in text for w in [
            "创建工作流", "新建工作流", "制作工作流", "构建工作流", "生成工作流", "设计工作流",
            "create workflow", "build workflow", "make workflow", "new workflow",
            "create a workflow", "build a workflow",
        ])
        has_workflow_query = any(w in text for w in [
            "有哪些工作流", "列出工作流", "查看工作流", "显示工作流", "工作流列表",
            "list workflow", "show workflow", "view workflow", "workflow list",
            "what workflow", "my workflow",
        ])

        requires_file = (
            any(word in text for word in ["文件", "目录", "读取", "写入", "保存", "删除", "覆盖", "移动", "复制"])
            or any(word in lower for word in ["file", "delete", "remove", "overwrite", "move", "copy"])
        )
        requires_browser = any(word in text for word in ["网页", "浏览器", "打开网站"]) or "http" in lower
        requires_command = any(word in text for word in ["命令", "运行", "执行"]) or any(word in lower for word in ["command", "run command"])
        risk = "LOW"
        if any(word in text for word in ["删除", "覆盖", "执行命令", "安装"]) or any(
            word in lower for word in ["delete", "remove", "overwrite", "run command", "install"]
        ):
            risk = "HIGH"
        elif any(word in text for word in ["写入", "修改", "保存"]):
            risk = "MEDIUM"

        # Priority-ordered type detection (Phase 2 types checked first)
        task_type = "QA"

        # Workflow routing: create → WORKFLOW_AUTOMATION, query → KNOWLEDGE_QA
        if has_workflow_create:
            task_type = "WORKFLOW_AUTOMATION"
        elif has_workflow_query:
            task_type = "KNOWLEDGE_QA"
        elif has_research:
            task_type = "MULTI_STEP_RESEARCH"
        elif has_schedule:
            task_type = "SCHEDULED_TASK"
        elif has_email:
            task_type = "EMAIL_ASSISTANT"
        elif has_spreadsheet:
            task_type = "SPREADSHEET_ANALYSIS"
        elif has_knowledge:
            task_type = "KNOWLEDGE_QA"

        # Original types: last-match-wins ordering (command > browser > file > qa)
        if requires_command and task_type not in ("WORKFLOW_AUTOMATION", "KNOWLEDGE_QA"):
            task_type = "LOCAL_OPERATION"
        if requires_browser and task_type not in ("LOCAL_OPERATION", "WORKFLOW_AUTOMATION", "KNOWLEDGE_QA"):
            task_type = "BROWSER_TASK"
        if requires_file and task_type not in ("LOCAL_OPERATION", "BROWSER_TASK", "WORKFLOW_AUTOMATION", "KNOWLEDGE_QA"):
            task_type = "FILE_TASK"

        capabilities: list[str] = []
        if requires_file or has_spreadsheet:
            capabilities.append("file")
        if requires_browser or has_research:
            capabilities.append("browser")
        if requires_command:
            capabilities.append("command")
        if has_workflow_query:
            capabilities.append("knowledge")

        success_criteria = ["满足用户目标", "遵守权限和安全要求"]
        if has_workflow_query:
            success_criteria = ["列出用户可用的工作流并简要说明每个工作流的用途"]

        return {
            "objective": text,
            "task_type": task_type,
            "target_objects": [],
            "constraints": [],
            "required_capabilities": capabilities,
            "requires_local_files": requires_file or has_spreadsheet,
            "requires_network": requires_browser or has_research,
            "requires_tools": bool(capabilities),
            "risk_level": risk,
            "clarification_required": False,
            "clarification_questions": [],
            "success_criteria": success_criteria,
            "expected_output_format": "text",
        }

    def _heuristic_plan(self, text: str) -> dict[str, Any]:
        try:
            payload = json.loads(text)
            objective = payload.get("objective", text)
            task_type = payload.get("task_type", "QA")
            risk = payload.get("risk_level", "LOW")
        except json.JSONDecodeError:
            objective = text
            task_type = "QA"
            risk = "LOW"
        if task_type == "MULTI_STEP_RESEARCH":
            return {
                "mode": "DAG",
                "goal": objective,
                "steps": [
                    {"index": 1, "title": "信息收集", "objective": "通过浏览器和知识库收集相关信息", "action_type": "TOOL", "required_tools": ["browser.read", "knowledge.search"], "expected_output": "收集到的原始信息", "risk_level": "LOW", "approval_required": False},
                    {"index": 2, "title": "信息整理", "objective": "整理和分析收集到的信息", "action_type": "PREPARE", "required_tools": [], "expected_output": "结构化的分析结果", "risk_level": "LOW", "approval_required": False},
                    {"index": 3, "title": "深度分析", "objective": "对整理后的信息进行深度分析和推理", "action_type": "MODEL_ANSWER", "required_tools": [], "expected_output": "深度分析结论", "risk_level": "LOW", "approval_required": False},
                ],
                "constraints": [], "success_criteria": ["完成多角度分析"], "risk_points": [], "approval_points": [], "fallback_strategy": {}, "stop_conditions": [],
            }
        if task_type == "SPREADSHEET_ANALYSIS":
            return {
                "mode": "LINEAR",
                "goal": objective,
                "steps": [
                    {"index": 1, "title": "读取表格", "objective": "读取Excel/CSV文件内容", "action_type": "TOOL", "required_tools": ["document.xlsx.read", "file.read"], "expected_output": "表格数据", "risk_level": "LOW", "approval_required": False},
                    {"index": 2, "title": "数据分析", "objective": "分析表格数据，提取关键指标", "action_type": "MODEL_ANSWER", "required_tools": [], "expected_output": "分析结果", "risk_level": "LOW", "approval_required": False},
                    {"index": 3, "title": "生成报告", "objective": "生成分析报告或汇总", "action_type": "FINALIZE", "required_tools": ["file.write"], "expected_output": "分析报告", "risk_level": "MEDIUM", "approval_required": True},
                ],
                "constraints": [], "success_criteria": ["完成表格分析"], "risk_points": ["写入文件"], "approval_points": ["生成报告需要确认"], "fallback_strategy": {}, "stop_conditions": [],
            }
        if task_type == "EMAIL_ASSISTANT":
            return {
                "mode": "LINEAR",
                "goal": objective,
                "steps": [
                    {"index": 1, "title": "确认邮件内容", "objective": "确认收件人、主题和正文内容", "action_type": "PREPARE", "required_tools": [], "expected_output": "邮件草稿", "risk_level": "LOW", "approval_required": False},
                    {"index": 2, "title": "撰写邮件", "objective": "根据需求撰写邮件内容", "action_type": "MODEL_ANSWER", "required_tools": [], "expected_output": "完整邮件内容", "risk_level": "LOW", "approval_required": False},
                    {"index": 3, "title": "保存邮件", "objective": "将邮件保存为文件供用户发送", "action_type": "TOOL", "required_tools": ["file.write"], "expected_output": "邮件文件", "risk_level": "MEDIUM", "approval_required": True},
                ],
                "constraints": [], "success_criteria": ["生成可发送的邮件"], "risk_points": [], "approval_points": ["保存文件需确认"], "fallback_strategy": {}, "stop_conditions": [],
            }
        if task_type == "SCHEDULED_TASK":
            return {
                "mode": "LINEAR",
                "goal": objective,
                "steps": [
                    {"index": 1, "title": "确认定时规则", "objective": "确认执行频率、时间、触发条件", "action_type": "PREPARE", "required_tools": [], "expected_output": "定时规则", "risk_level": "LOW", "approval_required": False},
                    {"index": 2, "title": "设计工作流", "objective": "根据定时规则设计自动执行的工作流", "action_type": "PREPARE", "required_tools": [], "expected_output": "工作流设计", "risk_level": "LOW", "approval_required": False},
                    {"index": 3, "title": "保存定时工作流", "objective": "保存为可定时执行的工作流模板", "action_type": "FINALIZE", "required_tools": [], "expected_output": "定时工作流", "risk_level": "LOW", "approval_required": False},
                ],
                "constraints": [], "success_criteria": ["创建可用的定时任务"], "risk_points": [], "approval_points": [], "fallback_strategy": {}, "stop_conditions": [],
            }
        if task_type == "KNOWLEDGE_QA":
            return {
                "mode": "LINEAR",
                "goal": objective,
                "steps": [
                    {"index": 1, "title": "检索知识库", "objective": "在知识库中搜索相关信息", "action_type": "TOOL", "required_tools": ["knowledge.search"], "expected_output": "相关知识条目", "risk_level": "LOW", "approval_required": False},
                    {"index": 2, "title": "综合分析", "objective": "综合分析检索到的知识给出答案", "action_type": "MODEL_ANSWER", "required_tools": [], "expected_output": "基于知识库的回答", "risk_level": "LOW", "approval_required": False},
                ],
                "constraints": [], "success_criteria": ["基于知识库准确回答"], "risk_points": [], "approval_points": [], "fallback_strategy": {}, "stop_conditions": [],
            }
        if task_type == "QA":
            return {
                "mode": "DIRECT",
                "goal": objective,
                "steps": [
                    {
                        "index": 1,
                        "title": "回答用户问题",
                        "objective": objective,
                        "action_type": "MODEL_ANSWER",
                        "required_tools": [],
                        "expected_output": "面向用户的最终回答",
                        "risk_level": "LOW",
                        "approval_required": False,
                    }
                ],
            }
        tools = []
        if task_type == "FILE_TASK":
            tools = ["file.read"]
        elif task_type == "BROWSER_TASK":
            tools = ["browser.read"]
        elif task_type == "LOCAL_OPERATION":
            tools = ["command.run"]
        return {
            "mode": "LINEAR",
            "goal": objective,
            "steps": [
                {
                    "index": 1,
                    "title": "准备执行上下文",
                    "objective": "确认任务目标、权限和可用工具",
                    "action_type": "PREPARE",
                    "required_tools": [],
                    "expected_output": "可执行上下文",
                    "risk_level": "LOW",
                    "approval_required": False,
                },
                {
                    "index": 2,
                    "title": "执行受控工具步骤",
                    "objective": objective,
                    "action_type": "TOOL",
                    "required_tools": tools,
                    "expected_output": "工具观察结果",
                    "risk_level": risk,
                    "approval_required": risk in ["HIGH", "CRITICAL"],
                },
            ],
        }

    @staticmethod
    def _get_schema(schema_name: str) -> dict[str, Any]:
        """Get JSON schema for structured output."""
        schemas = {
            "TaskBrief": {
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "task_type": {"type": "string", "enum": ["QA", "FILE_TASK", "BROWSER_TASK", "LOCAL_OPERATION", "DOCUMENT_SUMMARY", "WORKFLOW_AUTOMATION", "SPREADSHEET_ANALYSIS", "EMAIL_ASSISTANT", "SCHEDULED_TASK", "KNOWLEDGE_QA", "MULTI_STEP_RESEARCH"], "description": "WORKFLOW_AUTOMATION is ONLY for creating/building NEW workflows. For listing/viewing/searching EXISTING workflows, use KNOWLEDGE_QA."},
                    "target_objects": {"type": "array", "items": {"type": "string"}},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "required_capabilities": {"type": "array", "items": {"type": "string"}},
                    "requires_local_files": {"type": "boolean"},
                    "requires_network": {"type": "boolean"},
                    "requires_tools": {"type": "boolean"},
                    "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                    "clarification_required": {"type": "boolean"},
                    "clarification_questions": {"type": "array", "items": {"type": "string"}},
                    "success_criteria": {"type": "array", "items": {"type": "string"}},
                    "expected_output_format": {"type": "string"},
                },
                "required": ["objective", "task_type", "risk_level"],
            },
            "ExecutionPlan": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["DIRECT", "LINEAR", "DAG", "ITERATIVE"]},
                    "goal": {"type": "string"},
                    "max_iterations": {"type": "integer", "description": "ITERATIVE 模式最大迭代次数"},
                    "stop_condition": {"type": "string", "description": "ITERATIVE 模式停止条件"},
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
                                "depends_on": {"type": "array", "items": {"type": "integer"}, "description": "DAG 依赖的步骤 index 列表"},
                                "expected_output": {"type": "string"},
                                "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                                "approval_required": {"type": "boolean"},
                            },
                            "required": ["index", "title", "objective", "action_type", "expected_output", "risk_level"],
                        },
                    },
                },
                "required": ["mode", "goal", "steps"],
            },
        }
        return schemas.get(schema_name, {})
