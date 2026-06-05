"""Workflow Builder — guided conversation to create executable workflows.

Architecture:
    User says "create a workflow" → Builder starts guided dialog
    → 3-5 rounds of LLM-driven Q&A → LLM generates structured workflow
    → User confirms/modifies → Saved as WorkflowTemplate

State machine: IDLE → CLARIFY_GOAL → CLARIFY_INPUTS → CLARIFY_PROCESS
              → CLARIFY_OUTPUTS → GENERATING → AWAIT_CONFIRM → COMPLETED

Key contract: INTENT ROUTING
    When the RuntimeEngine detects WORKFLOW_AUTOMATION intent, it MUST NOT
    proceed through the normal pipeline. Instead it activates builder mode:
      1. Calls WorkflowBuilder.start() to initiate the conversation
      2. The builder returns the opening message which is presented to the user
      3. The frontend enters builder-mode, routing subsequent keystrokes to
         /api/workflows/build/continue
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from flowcraft_core.models.gateway import ModelGateway
from flowcraft_core.storage.database import Database
from flowcraft_core.workflows.models import WorkflowTemplate
from flowcraft_core.tools.base import ToolDefinition
from flowcraft_core.domain.enums import RiskLevel

logger = logging.getLogger(__name__)

# ── Builder Session ──────────────────────────────────────────

BUILDER_STAGES = [
    "idle",
    "clarify_goal",          # 1: what to do (per Architecture Doc §3.4)
    "clarify_inputs",        # 2: where inputs come from
    "clarify_process",       # 3: what processing logic
    "clarify_outputs",       # 4: what outputs and format
    "generating",            # LLM is composing the workflow
    "await_confirm",         # show preview, wait for user (confirm_and_create in doc)
    "completed",
]

# ── Timeouts ──
GENERATE_TIMEOUT = 35.0   # seconds for LLM workflow generation
MODIFY_TIMEOUT = 35.0     # seconds for LLM workflow modification
CONVERSATION_TIMEOUT = 25.0  # seconds for LLM conversation turn


@dataclass
class BuilderSession:
    """Tracks state of a workflow-building conversation."""

    session_id: str
    stage: str = "clarify_goal"
    # Collected info across stages
    goal: str = ""
    inputs_info: dict[str, Any] = field(default_factory=dict)
    process_info: dict[str, Any] = field(default_factory=dict)
    output_info: dict[str, Any] = field(default_factory=dict)
    # Generated workflow preview
    workflow_preview: dict[str, Any] | None = None
    # History
    conversation: list[dict[str, str]] = field(default_factory=list)
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "stage": self.stage,
            "goal": self.goal,
            "inputs_info": self.inputs_info,
            "process_info": self.process_info,
            "output_info": self.output_info,
            "workflow_preview": self.workflow_preview,
            "created_at": self.created_at,
        }


# ── Prompt Templates ─────────────────────────────────────────

def _build_conversation_prompt(session: BuilderSession, tools_summary: str) -> str:
    """Build the prompt for the LLM to generate the next conversation turn.

    This is the core prompt that drives the guided conversation.  The LLM is
    asked to respond with (a) a natural-language message to the user and (b)
    structured extracted info that advances the builder stage machine.
    """
    conv_text = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
        for m in session.conversation[-10:]
    ) or "(beginning of conversation)"

    stage_descriptions = {
        "clarify_goal":    "Goal: understand what the user wants this workflow to accomplish.",
        "clarify_inputs":  "Inputs: where the data/content comes from — files, URLs, user input, APIs, databases, etc.",
        "clarify_process": "Process: what steps/transformations/actions the workflow should perform. "
                            "Tailor questions to the user's domain (writing, analysis, automation, etc.).",
        "clarify_outputs": "Outputs: what format and where to save — file, chat display, email, etc.",
    }

    current_stage_desc = stage_descriptions.get(session.stage, stage_descriptions["clarify_goal"])

    # Determine which stages are already done
    done_stages = []
    if session.goal:
        done_stages.append("✅ Goal clarified")
    if session.inputs_info and session.inputs_info.get("summary"):
        done_stages.append("✅ Inputs clarified")
    if session.process_info and session.process_info.get("summary"):
        done_stages.append("✅ Processing clarified")
    if session.output_info and session.output_info.get("summary"):
        done_stages.append("✅ Outputs clarified")

    return f"""You are a helpful workflow design assistant. Your job is to guide a user through
creating a reusable automation workflow by asking targeted questions TAILORED to their specific domain.

## USER'S GOAL
{session.goal or '(stated in conversation)'}

## AVAILABLE TOOLS
{tools_summary}

## CONVERSATION SO FAR
{conv_text}

## CURRENT STAGE: {session.stage}
{current_stage_desc}

## PROGRESS
{chr(10).join(done_stages) if done_stages else 'No stages completed yet.'}

## COLLECTED INFO
Goal: {session.goal or '(not yet clarified)'}
Inputs: {_json.dumps(session.inputs_info, ensure_ascii=False) if session.inputs_info else '(not yet)'}
Processing: {_json.dumps(session.process_info, ensure_ascii=False) if session.process_info else '(not yet)'}
Outputs: {_json.dumps(session.output_info, ensure_ascii=False) if session.output_info else '(not yet)'}

## INSTRUCTIONS
1. Ask ONLY 1 question at a time, SPECIFIC to the user's domain.
   For a WRITING workflow, ask about genre/characters/chapters.
   For a DATA workflow, ask about files/columns/analysis.
   For an AUTOMATION workflow, ask about triggers/schedules/actions.
   NEVER ask generic questions that don't fit the user's goal.
2. Advance to the NEXT stage after collecting just ONE or TWO pieces of information per stage.
   Complete ALL 4 stages within 4-6 messages total.
3. Users don't need perfect completeness — fill in reasonable defaults for missing details.
4. When you have ANY info about the current stage, immediately summarize and move to the NEXT stage.
5. If the user's reply contains information about FUTURE stages, capture it all and advance MULTIPLE stages.
6. If ALL four stages have sufficient info, set stage to "generating" and write a warm summary.
7. Respond in the USER'S LANGUAGE. Keep responses brief — maximum 3 sentences.

Return JSON:
{{"stage": "clarify_goal"|"clarify_inputs"|"clarify_process"|"clarify_outputs"|"generating",
 "message": "your natural-language message to the user",
 "extracted_info": {{
   "goal_updates": "updated goal description or null",
   "inputs_summary": "summary of input information or null",
   "process_summary": "summary of processing requirements or null",
   "outputs_summary": "summary of output requirements or null"
 }}}}"""


def _prompt_generate_workflow(session: BuilderSession, tools_summary: str) -> str:
    """Build prompt for LLM to generate the final workflow."""

    return (
        "## WORKFLOW GENERATION\n\n"
        "Based on the user's requirements below, generate a complete, executable workflow.\n\n"
        "## USER REQUIREMENTS\n"
        f"Goal: {session.goal}\n"
        f"Inputs: {_json.dumps(session.inputs_info, ensure_ascii=False)}\n"
        f"Processing: {_json.dumps(session.process_info, ensure_ascii=False)}\n"
        f"Outputs: {_json.dumps(session.output_info, ensure_ascii=False)}\n\n"
        f"## AVAILABLE TOOLS\n{tools_summary}\n\n"
        "## RULES\n"
        "1. Each step must use an action_type: PREPARE, TOOL, MODEL_ANSWER, OBSERVE, FINALIZE\n"
        "2. TOOL steps must specify which tool_name from the available tools list\n"
        "3. Keep steps focused and atomic (one clear action per step)\n"
        "4. Use 4-8 steps for a typical workflow\n"
        "5. Risk level: LOW (read-only), MEDIUM (writes files), HIGH (deletes/runs commands)\n"
        "6. Generate a descriptive name and a clear description\n\n"
        "Return JSON matching this schema:\n"
        + _workflow_json_schema()
    )


def _workflow_json_schema() -> str:
    return """{
  "name": "Workflow display name",
  "description": "What this workflow does in 1-2 sentences",
  "risk_summary": "LOW"|"MEDIUM"|"HIGH",
  "tags": ["reporting", "automation", ...],
  "required_permissions": ["tool:file.read", ...],
  "required_tools": ["file.read", ...],
  "input_schema": {"fields": [{"name": "...", "type": "...", "description": "...", "required": true|false}]},
  "output_schema": {"format": "markdown"|"json"|"text", "description": "..."},
  "steps": [
    {
      "index": 1,
      "title": "Step title",
      "objective": "What this step aims to achieve (for the Agent)",
      "action_type": "PREPARE"|"TOOL"|"MODEL_ANSWER"|"OBSERVE"|"FINALIZE",
      "tool_name": "file.read"|"file.list"|"http.request"|... (only for TOOL type),
      "tool_params_template": {"path": "...", "query": "..."},
      "depends_on": [],
      "expected_output": "What should result from this step",
      "error_handling": "skip"|"retry"|"abort",
      "risk_level": "LOW"|"MEDIUM"|"HIGH"
    }
  ]
}"""


# ── Workflow Builder Engine ──────────────────────────────────

class WorkflowBuilder:
    """Guided conversation engine for creating workflows.

    Uses the ModelGateway's structured-output capability to drive the
    conversation AND to generate the final workflow definition.
    """

    def __init__(self, model_gateway: ModelGateway, tool_registry=None) -> None:
        self.model_gateway = model_gateway
        self.tool_registry = tool_registry
        self._sessions: dict[str, BuilderSession] = {}

    def get_tools_summary(self) -> str:
        """Summarize available tools for the LLM prompt."""
        if not self.tool_registry:
            return "file.read, file.write, file.list, file.search, file.delete, file.meta, http.request, http.download, web.search, browser.read, browser.screenshot, command.run, code.execute, pdf.read, docx.read, excel.read, knowledge.search"
        defs = self.tool_registry.list_definitions()
        lines = []
        for d in defs:
            lines.append(f"- {d['tool_name']} ({d.get('risk_level','LOW')}): {d.get('description','')[:120]}")
        return "\n".join(lines) if lines else "(no tools registered)"

    def start(self, user_input: str, session_id: str | None = None) -> dict[str, Any]:
        """Begin a new workflow building session.

        Returns immediately with a hardcoded opening message (no LLM call)
        so the frontend gets a fast response.  The first real LLM-driven
        turn happens inside continue_dialog().
        """
        sid = session_id or f"wfbuild_{uuid4().hex[:12]}"
        # IMPORTANT: session.stage MUST match the returned stage below.
        # We skip clarify_goal because the user already stated their goal.
        session = BuilderSession(session_id=sid, stage="clarify_inputs")
        session.conversation.append({"role": "user", "content": user_input})
        session.goal = user_input[:300]
        self._sessions[sid] = session

        # Fast, hardcoded opening message — feels instant
        return {
            "session_id": sid,
            "stage": "clarify_inputs",
            "agent_message": (
                f"好的，我理解了你的目标：**{user_input[:100]}{'...' if len(user_input)>100 else ''}**\n\n"
                "接下来我需要了解一些细节来设计这个工作流。请告诉我：\n"
                "1. **输入来源**：这个工作流需要什么输入？（文件、URL、用户提供的内容等）\n"
                "2. **输入形式**：输入内容是什么格式或什么类型的信息？"
            ),
            "collected_info": {"goal": session.goal},
        }

    async def continue_dialog(
        self, session_id: str, user_reply: str
    ) -> dict[str, Any]:
        """Process user's reply and advance the conversation.

        Uses LLM to:
        1. Extract structured info from the user's reply
        2. Decide which stage we're at
        3. Generate the next natural-language question

        Falls back to hardcoded questions when LLM is unavailable.
        """
        session = self._sessions.get(session_id)
        if not session:
            return {"error": "Session not found", "session_id": session_id}

        session.conversation.append({"role": "user", "content": user_reply})

        # ── Handle confirmation stage ──
        if session.stage == "await_confirm":
            # Check if user is confirming or modifying
            if any(kw in user_reply.lower() for kw in ["确认", "没问题", "创建", "confirm", "yes", "ok", "好的", "可以", "保存", "save"]):
                session.conversation.append({"role": "assistant", "content": "工作流已确认，请到前端确认保存。"})
                return {
                    "session_id": session_id,
                    "stage": "await_confirm",
                    "agent_message": "✅ 请点击 **确认创建** 按钮来保存这个工作流。",
                    "collected_info": self._collect_info(session),
                    "workflow_preview": session.workflow_preview,
                    "ready_to_confirm": True,
                }
            else:
                # User wants to modify → go to modify_workflow
                return await self.modify_workflow(session_id, user_reply)

        # ── LLM-driven conversation ──
        if self.model_gateway.is_live():
            try:
                import asyncio as _asyncio
                tools_summary = self.get_tools_summary()
                prompt = _build_conversation_prompt(session, tools_summary)

                result = await _asyncio.wait_for(
                    self._call_llm_conversation_turn(prompt),
                    timeout=CONVERSATION_TIMEOUT,
                )

                # Update session from LLM response
                agent_msg = result.get("message", "请继续描述你的需求...")
                extracted = result.get("extracted_info", {})
                new_stage = result.get("stage", session.stage)

                # Merge extracted info
                if extracted.get("goal_updates"):
                    session.goal = extracted["goal_updates"]
                if extracted.get("inputs_summary"):
                    session.inputs_info["summary"] = extracted["inputs_summary"]
                if extracted.get("process_summary"):
                    session.process_info["summary"] = extracted["process_summary"]
                if extracted.get("outputs_summary"):
                    session.output_info["summary"] = extracted["outputs_summary"]

                # Also store raw reply
                stage_key_map = {
                    "clarify_inputs": "inputs_info",
                    "clarify_process": "process_info",
                    "clarify_outputs": "output_info",
                }
                info_key = stage_key_map.get(session.stage)
                if info_key:
                    info_dict = getattr(session, info_key)
                    info_dict["raw_reply"] = user_reply

                session.stage = new_stage
                session.conversation.append({"role": "assistant", "content": agent_msg})

                result_dict: dict[str, Any] = {
                    "session_id": session_id,
                    "stage": new_stage,
                    "agent_message": agent_msg,
                    "collected_info": self._collect_info(session),
                }

                # If ready to generate, do it now
                if new_stage == "generating":
                    gen_result = await self.generate_workflow(session_id)
                    result_dict.update(gen_result)

                return result_dict

            except _asyncio.TimeoutError:
                logger.warning("Conversation LLM call timed out (%.0fs)", CONVERSATION_TIMEOUT)
            except Exception as exc:
                logger.warning("Conversation LLM call failed: %s", exc)

        # ── Fallback: hardcoded stage progression (works without LLM) ──
        return self._fallback_continue_dialog(session, user_reply, session_id)

    def _fallback_continue_dialog(
        self, session: BuilderSession, user_reply: str, session_id: str
    ) -> dict[str, Any]:
        """Generic conversation fallback when LLM is unavailable.

        Uses neutral, domain-agnostic questions that work for any workflow type
        (writing, analysis, automation, file processing, etc.).
        """
        if session.stage == "clarify_goal":
            session.stage = "clarify_inputs"
            agent_msg = (
                "明白了。请告诉我工作流的**输入信息**：\n"
                "1. 数据/内容从哪来？（文件路径、API、用户输入等）\n"
                "2. 输入是什么格式或形式？"
            )
        elif session.stage == "clarify_inputs":
            session.inputs_info["raw_reply"] = user_reply
            session.stage = "clarify_process"
            agent_msg = (
                "好的。接下来请描述你需要的**处理步骤**：\n"
                "1. 这个工作流需要完成哪些具体操作？\n"
                "2. 有没有特定的规则、条件或约束？"
            )
        elif session.stage == "clarify_process":
            session.process_info["raw_reply"] = user_reply
            session.stage = "clarify_outputs"
            agent_msg = (
                "了解。最后关于**输出结果**：\n"
                "1. 最终产物是什么？（文件、消息、操作结果等）\n"
                "2. 输出到哪里？以什么形式保存或展示？"
            )
        elif session.stage == "clarify_outputs":
            session.output_info["raw_reply"] = user_reply
            session.stage = "generating"
            agent_msg = "好的，信息已收集完毕，正在生成工作流预览..."
        else:
            session.stage = "generating"
            agent_msg = "正在生成工作流..."

        session.conversation.append({"role": "assistant", "content": agent_msg})

        result: dict[str, Any] = {
            "session_id": session_id,
            "stage": session.stage,
            "agent_message": agent_msg,
            "collected_info": self._collect_info(session),
        }

        # If ready to generate, do it now
        if session.stage == "generating":
            import asyncio as _asyncio
            try:
                loop = _asyncio.get_event_loop()
                if loop.is_running():
                    # We're inside an async context; use synchronous fallback
                    wf = self._fallback_workflow(session)
                    session.workflow_preview = wf.to_dict()
                    session.stage = "await_confirm"
                    result.update({
                        "stage": "await_confirm",
                        "agent_message": self._format_preview_message(wf),
                        "workflow_preview": wf.to_dict(),
                    })
                else:
                    gen_result = loop.run_until_complete(self.generate_workflow(session_id))
                    result.update(gen_result)
            except RuntimeError:
                wf = self._fallback_workflow(session)
                session.workflow_preview = wf.to_dict()
                session.stage = "await_confirm"
                result.update({
                    "stage": "await_confirm",
                    "agent_message": self._format_preview_message(wf),
                    "workflow_preview": wf.to_dict(),
                })

        return result

    async def _call_llm_conversation_turn(self, prompt: str) -> dict[str, Any]:
        """Call the LLM for one conversation turn using the public API."""
        messages = [
            {"role": "system", "content": (
                "You are a friendly workflow design assistant. "
                "Guide users through creating automation workflows by asking focused questions. "
                "Extract information from their answers and advance the conversation naturally. "
                "Respond in the user's language. Keep responses warm and concise."
            )},
            {"role": "user", "content": prompt},
        ]

        schema = {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": ["clarify_goal", "clarify_inputs", "clarify_process", "clarify_outputs", "generating"],
                },
                "message": {"type": "string"},
                "extracted_info": {
                    "type": "object",
                    "properties": {
                        "goal_updates": {"type": "string"},
                        "inputs_summary": {"type": "string"},
                        "process_summary": {"type": "string"},
                        "outputs_summary": {"type": "string"},
                    },
                },
            },
            "required": ["stage", "message"],
        }

        # Use the ModelGateway's structured output method
        if hasattr(self.model_gateway, '_adapter') and self.model_gateway._adapter:
            return await self.model_gateway._adapter.structured_chat(
                messages, schema, temperature=0.4, max_tokens=1024,
            )
        return {"stage": "clarify_inputs", "message": "请继续描述你的需求...", "extracted_info": {}}

    async def generate_workflow(self, session_id: str) -> dict[str, Any]:
        """Generate the workflow from collected requirements.

        Has GENERATE_TIMEOUT — falls back to template if LLM is slow.
        """
        import asyncio as _asyncio

        session = self._sessions.get(session_id)
        if not session:
            return {"error": "Session not found"}

        session.stage = "generating"

        if not self.model_gateway.is_live():
            wf = self._fallback_workflow(session)
            session.workflow_preview = wf.to_dict()
            session.stage = "await_confirm"
            return {
                "stage": "await_confirm",
                "agent_message": self._format_preview_message(wf),
                "workflow_preview": wf.to_dict(),
            }

        try:
            tools_summary = self.get_tools_summary()
            prompt = _prompt_generate_workflow(session, tools_summary)

            schema = {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "risk_summary": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "required_permissions": {"type": "array", "items": {"type": "string"}},
                    "required_tools": {"type": "array", "items": {"type": "string"}},
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer"},
                                "title": {"type": "string"},
                                "objective": {"type": "string"},
                                "action_type": {"type": "string", "enum": ["PREPARE", "TOOL", "MODEL_ANSWER", "OBSERVE", "FINALIZE"]},
                                "tool_name": {"type": "string"},
                                "tool_params_template": {"type": "object"},
                                "depends_on": {"type": "array", "items": {"type": "integer"}},
                                "expected_output": {"type": "string"},
                                "error_handling": {"type": "string", "enum": ["skip", "retry", "abort"]},
                                "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
                            },
                            "required": ["index", "title", "objective", "action_type", "expected_output", "risk_level"],
                        },
                    },
                },
                "required": ["name", "description", "risk_summary", "steps"],
            }

            result = await _asyncio.wait_for(
                self.model_gateway._adapter.structured_chat(
                    [
                        {"role": "system", "content": (
                            "You are a workflow generation engine. "
                            "Generate executable workflows from user requirements. "
                            "Your output must be valid JSON matching the schema exactly. "
                            "Use only tools that are listed as available."
                        )},
                        {"role": "user", "content": prompt},
                    ],
                    schema,
                    temperature=0.3, max_tokens=3072,
                ),
                timeout=GENERATE_TIMEOUT,
            )

            wf = WorkflowTemplate(
                name=result.get("name", session.goal[:40] or "Untitled Workflow"),
                description=result.get("description", ""),
                risk_summary=result.get("risk_summary", "LOW"),
                required_tools=result.get("required_tools", []),
                required_permissions=result.get("required_permissions", []),
                input_schema=result.get("input_schema", {}),
                output_schema=result.get("output_schema", {}),
                steps=result.get("steps", []),
            )

            session.workflow_preview = wf.to_dict()
            session.stage = "await_confirm"

            return {
                "stage": "await_confirm",
                "agent_message": self._format_preview_message(wf),
                "workflow_preview": wf.to_dict(),
            }

        except _asyncio.TimeoutError:
            logger.warning("Workflow generation timed out (%.0fs), using fallback", GENERATE_TIMEOUT)
            wf = self._fallback_workflow(session)
            session.workflow_preview = wf.to_dict()
            session.stage = "await_confirm"
            return {
                "stage": "await_confirm",
                "agent_message": "⚠️ LLM 生成超时，已使用模板工作流。你可以在确认后手动修改步骤。\n\n" + self._format_preview_message(wf),
                "workflow_preview": wf.to_dict(),
            }
        except Exception as exc:
            logger.warning("Workflow generation failed: %s", exc)
            wf = self._fallback_workflow(session)
            session.workflow_preview = wf.to_dict()
            session.stage = "await_confirm"
            return {
                "stage": "await_confirm",
                "agent_message": self._format_preview_message(wf),
                "workflow_preview": wf.to_dict(),
            }

    async def modify_workflow(
        self, session_id: str, feedback: str
    ) -> dict[str, Any]:
        """Handle user feedback to modify the workflow preview.

        Has MODIFY_TIMEOUT — falls back gracefully on timeout.
        """
        import asyncio as _asyncio

        session = self._sessions.get(session_id)
        if not session or not session.workflow_preview:
            return {"error": "No workflow to modify"}

        session.conversation.append({"role": "user", "content": feedback})

        if not self.model_gateway.is_live():
            session.workflow_preview["_feedback"] = feedback
            return {
                "stage": "await_confirm",
                "agent_message": f"已记录修改意见。你可以回复'确认'来保存，或继续提出修改。",
                "workflow_preview": session.workflow_preview,
            }

        try:
            prompt = (
                f"## CURRENT WORKFLOW\n{_json.dumps(session.workflow_preview, ensure_ascii=False, indent=2)}\n\n"
                f"## USER FEEDBACK\n{feedback}\n\n"
                f"## INSTRUCTIONS\n"
                f"Modify the workflow according to the user's feedback. "
                f"Keep the same output schema. Return the complete modified workflow JSON."
            )

            result = await _asyncio.wait_for(
                self.model_gateway._adapter.structured_chat(
                    [
                        {"role": "system", "content": "You are a workflow editor. Modify workflows based on user feedback. Return the COMPLETE modified workflow."},
                        {"role": "user", "content": prompt},
                    ],
                    {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "risk_summary": {"type": "string"},
                            "required_tools": {"type": "array", "items": {"type": "string"}},
                            "steps": {"type": "array"},
                        },
                    },
                    temperature=0.3, max_tokens=3072,
                ),
                timeout=MODIFY_TIMEOUT,
            )

            # Update preview
            session.workflow_preview["name"] = result.get("name", session.workflow_preview.get("name"))
            session.workflow_preview["description"] = result.get("description", "")
            session.workflow_preview["steps"] = result.get("steps", session.workflow_preview.get("steps"))
            session.workflow_preview["risk_summary"] = result.get("risk_summary", "LOW")
            session.workflow_preview["required_tools"] = result.get("required_tools", [])

            wf = WorkflowTemplate(
                name=session.workflow_preview["name"],
                description=session.workflow_preview.get("description", ""),
                risk_summary=session.workflow_preview.get("risk_summary", "LOW"),
                steps=session.workflow_preview.get("steps", []),
                required_tools=session.workflow_preview.get("required_tools", []),
                required_permissions=session.workflow_preview.get("required_permissions", []),
            )

            session.conversation.append({"role": "assistant", "content": f"已修改工作流"})

            return {
                "stage": "await_confirm",
                "agent_message": f"✅ 已根据你的意见修改。\n{self._format_preview_message(wf)}\n你可以继续修改或回复 **'确认'** 保存。",
                "workflow_preview": session.workflow_preview,
            }

        except _asyncio.TimeoutError:
            logger.warning("Workflow modification timed out (%.0fs)", MODIFY_TIMEOUT)
            return {
                "stage": "await_confirm",
                "agent_message": "⚠️ 修改超时，当前预览保持不变。你可以重试或回复'确认'保存。",
                "workflow_preview": session.workflow_preview,
            }
        except Exception as exc:
            logger.warning("Workflow modification failed: %s", exc)
            return {
                "stage": "await_confirm",
                "agent_message": f"修改遇到问题: {exc}。请重试或回复'确认'保存当前版本。",
                "workflow_preview": session.workflow_preview,
            }

    def _collect_info(self, session: BuilderSession) -> dict[str, Any]:
        """Collect all gathered info for API responses."""
        return {
            "goal": session.goal,
            "inputs": session.inputs_info,
            "process": session.process_info,
            "outputs": session.output_info,
        }

    def _fallback_workflow(self, session: BuilderSession) -> WorkflowTemplate:
        """Generate a simple workflow without LLM (dev-mode fallback)."""
        steps = [
            {
                "index": 1, "title": "确认目标和输入",
                "objective": f"确认工作流目标: {session.goal[:100]}",
                "action_type": "PREPARE",
                "depends_on": [],
                "expected_output": "已确认的执行上下文",
                "error_handling": "abort",
                "risk_level": "LOW",
            },
            {
                "index": 2, "title": "处理数据/执行分析",
                "objective": "根据用户需求执行核心处理逻辑",
                "action_type": "TOOL",
                "tool_name": "file.read",
                "depends_on": [1],
                "expected_output": "处理结果",
                "error_handling": "retry",
                "risk_level": "LOW",
            },
            {
                "index": 3, "title": "生成输出",
                "objective": "生成最终结果并展示给用户",
                "action_type": "FINALIZE",
                "depends_on": [2],
                "expected_output": "最终输出",
                "error_handling": "abort",
                "risk_level": "LOW",
            },
        ]
        return WorkflowTemplate(
            name=session.goal[:50] or "自定义工作流",
            description=session.goal[:200],
            risk_summary="LOW",
            steps=steps,
            required_tools=["file.read"],
            required_permissions=["tool:file.read"],
        )

    def _format_preview_message(self, wf: WorkflowTemplate) -> str:
        """Format workflow preview as a readable message."""
        step_lines = "\n".join(
            f"  {s.get('index', i+1)}. **{s.get('title', 'Step')}** — {s.get('action_type', '')}"
            for i, s in enumerate(wf.steps[:8])
        )
        if len(wf.steps) > 8:
            step_lines += f"\n  ... 共 {len(wf.steps)} 步"

        tools_str = ", ".join(wf.required_tools[:5]) if wf.required_tools else "无"
        risk_icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(wf.risk_summary, "⚪")

        return (
            f"## 📋 工作流预览\n\n"
            f"**名称**: {wf.name}\n"
            f"**描述**: {wf.description or '(无)'}\n"
            f"**风险**: {risk_icon} {wf.risk_summary}\n"
            f"**所需工具**: {tools_str}\n\n"
            f"### 步骤 ({len(wf.steps)} 步)\n{step_lines}\n\n"
            f"---\n"
            f"回复 **'确认'** 保存此工作流，或说出你想修改的地方。"
        )

    def get_session(self, session_id: str) -> BuilderSession | None:
        return self._sessions.get(session_id)

    def complete_session(self, session_id: str) -> BuilderSession | None:
        """Mark session as completed and return it."""
        s = self._sessions.get(session_id)
        if s:
            s.stage = "completed"
        return s

    def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
