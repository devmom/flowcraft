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

# ── Sufficiency threshold for early exit ──────────────────────
# If the user has provided info covering all these areas, stop asking
# and generate the workflow immediately.  Thresholds are now
# complexity-dependent; these are the defaults used when complexity
# hasn't been assessed yet or LLM is unavailable.
DEFAULT_REQUIRED_AREAS = 3      # for MODERATE workflows
SIMPLE_REQUIRED_AREAS = 2       # for SIMPLE workflows (just goal + one other)
COMPLEX_REQUIRED_AREAS = 4      # for COMPLEX workflows (need all 4 areas)
MAX_TURNS_SIMPLE = 4
MAX_TURNS_MODERATE = 6          # never exceed this many Q&A turns
MAX_TURNS_COMPLEX = 8
# Legacy alias (used as fallback when complexity is unknown)
MIN_REQUIRED_AREAS = DEFAULT_REQUIRED_AREAS
MAX_TURNS_BEFORE_FORCE_GENERATE = MAX_TURNS_MODERATE

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
START_TIMEOUT = 10.0      # seconds for LLM first-turn message


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
    # Complexity assessment (set by LLM on first turn)
    complexity: str = "MODERATE"       # SIMPLE | MODERATE | COMPLEX
    max_questions_needed: int = 2      # number of clarifying questions expected
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

    Uses Socratic questioning: instead of rushing through a data-collection
    checklist, the assistant probes the user's thinking — challenging
    assumptions, exploring edge cases, surfacing hidden requirements, and
    helping the user discover what they *really* need.
    """
    conv_text = "\n".join(
        f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
        for m in session.conversation[-10:]
    ) or "(beginning of conversation)"

    # How many turns have we had?  Used to decide depth vs pace.
    turn_count = len([m for m in session.conversation if m['role'] == 'assistant'])

    # ── Complexity-aware pacing ──
    # Determine max turns and required areas based on assessed complexity
    complexity = getattr(session, 'complexity', 'MODERATE')
    if complexity == 'COMPLEX':
        max_turns = MAX_TURNS_COMPLEX
        required_areas = COMPLEX_REQUIRED_AREAS
    elif complexity == 'SIMPLE':
        max_turns = MAX_TURNS_SIMPLE
        required_areas = SIMPLE_REQUIRED_AREAS
    else:
        max_turns = MAX_TURNS_MODERATE
        required_areas = DEFAULT_REQUIRED_AREAS

    urgency_level = turn_count / max(max_turns, 1)  # 0.0 → 1.0

    pacing_hint = ""
    if turn_count >= max_turns:
        pacing_hint = (
            f"You have had {turn_count} turns — EXCEEDED the {max_turns}-turn budget for {complexity} workflows. "
            "You MUST set stage to 'generating' NOW. "
            "If any area is still unclear, fill in reasonable defaults."
        )
    elif urgency_level >= 0.75:
        pacing_hint = (
            f"Turn {turn_count}/{max_turns}. Most of the question budget is used. "
            "Unless there is a CRITICAL ambiguity, move to 'generating'."
        )
    elif urgency_level >= 0.5:
        pacing_hint = (
            f"Turn {turn_count}/{max_turns}. "
            "Ask ONLY if there's an important gap. Prefer to move to 'generating'."
        )

    # Collect whatever we know so the LLM can see the full picture
    gathered_context = []
    if session.goal:
        gathered_context.append(f"Goal: {session.goal[:500]}")
    if session.inputs_info and session.inputs_info.get("summary"):
        gathered_context.append(f"Inputs: {session.inputs_info['summary'][:500]}")
    if session.process_info and session.process_info.get("summary"):
        gathered_context.append(f"Process: {session.process_info['summary'][:500]}")
    if session.output_info and session.output_info.get("summary"):
        gathered_context.append(f"Outputs: {session.output_info['summary'][:500]}")

    # Calculate how many areas are filled
    filled_areas = sum(1 for item in gathered_context)
    sufficiency_signal = ""
    if filled_areas >= required_areas:
        sufficiency_signal = (
            f"\n## ⚠️ SUFFICIENCY: {filled_areas}/{required_areas} required areas are filled "
            f"(complexity={complexity}). "
            "You DO NOT need to ask more questions. Move directly to 'generating' "
            "unless there's a genuine showstopper ambiguity. "
            "For any minor gaps, fill in sensible defaults — the user can modify the preview later."
        )

    return f"""You are a **workflow design partner**. Your PRIMARY goal is to help the user
create a workflow as efficiently as possible, NOT to have a long conversation.

## WORKFLOW COMPLEXITY: {complexity} (question budget: {max_turns} turns)

## USER'S STATED GOAL
{session.goal or '(stated in conversation)'}

## AVAILABLE TOOLS
{tools_summary}

## CONVERSATION SO FAR
{conv_text}

## WHAT WE KNOW SO FAR
{chr(10).join(gathered_context) if gathered_context else '(nothing yet — start by understanding the goal)'}
{sufficiency_signal}

## GUIDING PRINCIPLES (prioritise based on complexity)
For COMPLEX workflows, spend more time on:
- Environment setup & external dependencies
- Multi-stage processing logic
- Error handling and edge cases
For SIMPLE workflows, be brief — 1-2 questions max.

## PACING
{pacing_hint if pacing_hint else f'This is turn 0 or 1 ({complexity} workflow, budget {max_turns} turns). Ask according to complexity.'}

## CRITICAL RULES
1. Ask at most ONE question per turn (or 2-3 for COMPLEX workflows in early turns).
   If you have enough to design the workflow, ask ZERO questions and set stage to "generating".
2. For COMPLEX workflows, you ARE allowed to ask 2-3 questions in one message when it makes sense.
3. If {required_areas}+ areas (goal/inputs/process/outputs) are filled, prefer to generate immediately.
4. For vague answers like "whatever works", do NOT probe again — just pick a reasonable default.
5. **Always provide 2-4 concrete options** when asking a question.
6. Respond in the USER'S LANGUAGE. Be warm and efficient.

Return JSON:
{{"stage": "clarify_goal"|"clarify_inputs"|"clarify_process"|"clarify_outputs"|"generating",
 "message": "your question (or confirmation before generating)",
 "extracted_info": {{
   "goal_updates": "updated goal or null",
   "inputs_summary": "summary of input info or null",
   "process_summary": "summary of processing info or null",
   "outputs_summary": "summary of output info or null"
 }}}}"""


def _build_first_turn_prompt(user_goal: str, tools_summary: str) -> str:
    """Build the prompt for the LLM to assess complexity AND ask the first question.

    Two-step process:
    1. **Assess complexity**: how many stages / external dependencies does this need?
    2. **Ask the right number of questions**: SIMPLE → 0-1, MODERATE → 2, COMPLEX → 3-5.

    For COMPLEX workflows, the assistant may present 2-3 questions in ONE message
    as a checklist, so the user can answer all at once and the builder can proceed.
    """

    return f"""You are a **workflow design partner** helping a user create an automation workflow.

## USER'S STATED GOAL
{user_goal[:500]}

## AVAILABLE TOOLS
{tools_summary}

## STEP 1 — ASSESS COMPLEXITY
First, evaluate how complex this workflow is:

**SIMPLE** (1-2 tools, linear flow):
  Examples: "read a file and summarize it", "translate this text", "search the web for X"
  → Ask 0-1 question, can often go straight to "generating"

**MODERATE** (3-4 tools, some branching):
  Examples: "scrape a website, clean data, output a report", "monitor a folder and alert on changes"
  → Ask 2 focused questions

**COMPLEX** (5+ tools, multi-stage pipeline, external dependencies):
  Examples: "download video, run ASR, proofread transcript, analyze content, generate report",
            "ETL pipeline with validation, transformation, and multiple output formats"
  → Ask 3-5 questions to cover: environment setup, processing rules, edge cases, output format

## STEP 2 — ASK QUESTIONS
Based on complexity, ask the right questions:

**For SIMPLE**: ask 0-1 question, or go straight to "generating"

**For MODERATE**: ask 1-2 questions. Pick the most impactful ones:
  - Input source/data format
  - Output format/preferences
  - Key processing logic

**For COMPLEX**: present 2-3 questions in ONE well-organized message as a checklist:
  - Environment/runtime needs (what tools/libraries must be installed? e.g., yt-dlp, whisper)
  - Processing logic (what exactly should happen at each stage?)
  - Output format and structure
  - Edge cases and error handling
  Format as a clear, scannable checklist so the user can answer concisely.

## CRITICAL RULES
1. Always provide 2-4 CONCRETE OPTIONS (A/B/C style) wherever possible.
2. If the goal mentions tools not in the available list (e.g., yt-dlp, whisper, ffmpeg),
   note them as "external dependencies" and ask the user to confirm they have them installed.
3. Respond in the USER'S LANGUAGE.
4. Be warm and efficient.
5. For COMPLEX workflows, ask multiple questions in ONE message — don't drip-feed one at a time.

Return JSON:
{{"complexity": "SIMPLE"|"MODERATE"|"COMPLEX",
 "max_questions_needed": 0-5,
 "stage": "clarify_goal"|"clarify_inputs"|"clarify_process"|"clarify_outputs"|"generating",
 "message": "your question(s) or generation announcement",
 "extracted_info": {{
   "goal_updates": "refined goal or null",
   "inputs_summary": "any input info implied by goal, or null",
   "process_summary": "any process info implied by goal, or null",
   "outputs_summary": "any output info implied by goal, or null"
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
        "## TOOL CAPABILITY GUIDE (IMPORTANT)\n"
        "Choose the right tool for each task:\n"
        "- **code.execute**: Python sandbox. SAFE but RESTRICTED — NO network, NO file system access.\n"
        "  Use only for pure computation (math, data processing, text analysis).\n"
        "  ❌ Cannot: download files, call APIs, read local files, use external libraries (whisper, etc.)\n"
        "  ✅ Can: process in-memory data, compute statistics, format text\n\n"
        "- **command.run**: Shell command execution. Needs user approval. Can access network & file system.\n"
        "  Use for: running CLI tools (yt-dlp, ffmpeg, whisper, pip install), file operations.\n"
        "  ⚠️ Requires HIGH risk approval from user.\n"
        "  ✅ Can: download, call APIs, run any installed CLI tool, access filesystem\n\n"
        "- **file.read / file.write**: Read/write local files.\n"
        "- **http.request / http.download**: Make HTTP requests, download files.\n"
        "- **web.search**: Search the web for information.\n\n"
        "## SCRIPT GENERATION RULES\n"
        "For steps that need custom logic beyond simple tool calls, generate an INLINE SCRIPT:\n"
        "1. Use the `script` field to embed the Python or shell script directly in the step.\n"
        "2. Set `script_type` to \"python\" or \"shell\".\n"
        "3. For Python scripts that need external packages, list them in `requires_packages`.\n"
        "4. For shell scripts that call CLI tools, list the tools in `requires_packages` as well.\n"
        "5. Add an early PREPARE step that installs dependencies via `command.run` if needed.\n\n"
        "## RULES\n"
        "1. Each step must use an action_type: PREPARE, TOOL, MODEL_ANSWER, OBSERVE, FINALIZE\n"
        "2. TOOL steps must specify which tool_name from the available tools list\n"
        "3. Keep steps focused and atomic (one clear action per step)\n"
        "4. Use 4-8 steps for SIMPLE workflows, 6-12 steps for COMPLEX workflows\n"
        "5. Risk level: LOW (read-only), MEDIUM (writes files), HIGH (deletes/runs commands)\n"
        "6. For tasks requiring external tools (yt-dlp, whisper, ffmpeg, etc.), use `command.run`\n"
        "7. Include an environment check/setup step at the beginning for COMPLEX workflows\n"
        "8. Generate a descriptive name and a clear description\n"
        "9. Use the `environment_setup` field to declare all external dependencies\n\n"
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
  "required_tools": ["file.read", "command.run", "code.execute", ...],
  "environment_setup": {
    "requires_network": true|false,
    "requires_filesystem": true|false,
    "external_dependencies": ["yt-dlp", "ffmpeg", "openai-whisper", ...],
    "setup_notes": "Instructions for installing dependencies before running"
  },
  "input_schema": {"fields": [{"name": "...", "type": "...", "description": "...", "required": true|false}]},
  "output_schema": {"format": "markdown"|"json"|"text", "description": "..."},
  "steps": [
    {
      "index": 1,
      "title": "Step title",
      "objective": "What this step aims to achieve (for the Agent)",
      "action_type": "PREPARE"|"TOOL"|"MODEL_ANSWER"|"OBSERVE"|"FINALIZE",
      "tool_name": "file.read"|"command.run"|"code.execute"|... (only for TOOL type),
      "tool_params_template": {"path": "...", "query": "...", "command": "...", "code": "..."},
      "script": "Inline Python or shell script to execute (optional — use for complex logic)",
      "script_type": "python"|"shell"|null,
      "requires_packages": ["package names needed by this step's script"],
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

    async def start(self, user_input: str, session_id: str | None = None) -> dict[str, Any]:
        """Begin a new workflow building session.

        Calls the LLM to generate a dynamic first question tailored to the
        user's specific goal.  Falls back to a hardcoded opening message if
        the LLM is unavailable or times out.
        """
        import asyncio as _asyncio

        sid = session_id or f"wfbuild_{uuid4().hex[:12]}"
        # Start in clarify_goal — the LLM will decide the actual stage
        session = BuilderSession(session_id=sid, stage="clarify_goal")
        session.conversation.append({"role": "user", "content": user_input})
        session.goal = user_input[:300]
        self._sessions[sid] = session

        # ── Try LLM-driven first turn ──
        if self.model_gateway.is_live():
            try:
                tools_summary = self.get_tools_summary()
                prompt = _build_first_turn_prompt(user_input, tools_summary)

                result = await _asyncio.wait_for(
                    self._call_llm_conversation_turn(prompt),
                    timeout=START_TIMEOUT,
                )

                agent_msg = result.get("message", "请描述一下你想要的输出结果...")
                extracted = result.get("extracted_info", {})
                new_stage = result.get("stage", "clarify_inputs")

                # ── Capture complexity assessment ──
                complexity = result.get("complexity", "MODERATE")
                max_q = result.get("max_questions_needed", 2)
                if complexity in ("SIMPLE", "MODERATE", "COMPLEX"):
                    session.complexity = complexity
                session.max_questions_needed = max_q

                # Merge extracted info into session
                if extracted.get("goal_updates"):
                    session.goal = extracted["goal_updates"]
                if extracted.get("inputs_summary"):
                    session.inputs_info["summary"] = extracted["inputs_summary"]
                if extracted.get("process_summary"):
                    session.process_info["summary"] = extracted["process_summary"]
                if extracted.get("outputs_summary"):
                    session.output_info["summary"] = extracted["outputs_summary"]

                session.stage = new_stage
                session.conversation.append({"role": "assistant", "content": agent_msg})

                return {
                    "session_id": sid,
                    "stage": new_stage,
                    "agent_message": agent_msg,
                    "collected_info": self._collect_info(session),
                    "complexity": session.complexity,
                }

            except _asyncio.TimeoutError:
                logger.warning("First-turn LLM call timed out (%.0fs), using fallback", START_TIMEOUT)
            except Exception as exc:
                logger.warning("First-turn LLM call failed: %s, using fallback", exc)

        # ── Fallback: hardcoded opening (original behaviour) ──
        session.stage = "clarify_inputs"
        fallback_msg = (
            f"我理解你想创建一个工作流：**{user_input[:120]}{'...' if len(user_input)>120 else ''}**\n\n"
            "为了设计得更准确，我先确认一下：**这个工作流完成后，你期望的结果是什么形式的？**\n\n"
            "比如：\n"
            "• 🟢 在聊天窗口直接看到分析结论\n"
            "• 📄 生成一个文件（Markdown / Excel / PDF）保存到本地\n"
            "• 📊 输出一个结构化的数据表格\n"
            "• 📧 把结果通过邮件或通知发送出去\n"
            "• 🔄 触发下一个工作流继续处理\n\n"
            "你可以直接选一个，或者说你自己的需求 😊"
        )
        session.conversation.append({"role": "assistant", "content": fallback_msg})
        return {
            "session_id": sid,
            "stage": "clarify_inputs",
            "agent_message": fallback_msg,
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

        **Key behavior**: If enough information has been collected (3+ areas filled),
        skips further questioning and generates the workflow directly.
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

        # ── Pre-LLM sufficiency check (complexity-aware) ──
        # If we already have enough info, skip further questioning and generate directly.
        filled_areas = (
            (1 if session.goal else 0) +
            (1 if session.inputs_info.get("summary") else 0) +
            (1 if session.process_info.get("summary") else 0) +
            (1 if session.output_info.get("summary") else 0)
        )
        turn_count = len([m for m in session.conversation if m["role"] == "assistant"])

        # Determine thresholds from assessed complexity
        complexity = getattr(session, 'complexity', 'MODERATE')
        if complexity == 'COMPLEX':
            required = COMPLEX_REQUIRED_AREAS
            max_t = MAX_TURNS_COMPLEX
        elif complexity == 'SIMPLE':
            required = SIMPLE_REQUIRED_AREAS
            max_t = MAX_TURNS_SIMPLE
        else:
            required = DEFAULT_REQUIRED_AREAS
            max_t = MAX_TURNS_MODERATE

        if turn_count >= max_t or filled_areas >= required:
            logger.info(
                "Sufficiency reached (areas=%d/%d, turns=%d/%d, complexity=%s) — generating workflow directly",
                filled_areas, required, turn_count, max_t, complexity
            )
            session.stage = "generating"
            gen_result = await self.generate_workflow(session_id)
            return {
                "session_id": session_id,
                **gen_result,
            }

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
        """Minimal fallback when LLM is unavailable.

        Advances through stages quickly — only 2-3 turns total.
        Each turn gives concrete options, not open-ended philosophy.
        """
        turn_count = len([m for m in session.conversation if m["role"] == "assistant"])

        if session.stage == "clarify_goal":
            session.inputs_info["raw_reply"] = user_reply
            session.inputs_info["summary"] = user_reply[:200]
            session.stage = "clarify_inputs"
            agent_msg = (
                f"明白了。接下来确认一下**输入**：\n\n"
                f"你的数据从哪里来？\n"
                f"🅰️ 本地文件（Excel / CSV / JSON / TXT）\n"
                f"🅱️ 网页链接 / API 接口\n"
                f"🅲️ 数据库查询\n"
                f"🅳️ 我直接在对话里提供内容\n\n"
                f"你选哪个？（可以多选，也可以说自己的情况）"
            )
        elif session.stage == "clarify_inputs":
            session.inputs_info["raw_reply"] = user_reply
            session.inputs_info["summary"] = user_reply[:200]
            session.stage = "clarify_outputs"
            agent_msg = (
                "好的。最后确认一下**输出格式**：\n\n"
                "你希望结果以什么形式呈现？\n"
                "🅰️ 直接在对话窗口展示分析结论\n"
                "🅱️ 生成一个文件（Markdown / Excel / PDF）\n"
                "🅲️ 输出结构化的 JSON 数据\n"
                "🅳️ 把结果发到其他地方（邮件、通知等）\n\n"
                "选一个就够，我马上为你生成工作流 😊"
            )
        elif session.stage == "clarify_process":
            session.process_info["raw_reply"] = user_reply
            session.process_info["summary"] = user_reply[:200]
            session.stage = "clarify_outputs"
            agent_msg = (
                "好的。最后确认**输出格式**：\n"
                "🅰️ 聊天窗口直接展示  🅱️ 生成文件  🅲️ JSON 数据  🅳️ 其他"
            )
        elif session.stage == "clarify_outputs":
            session.output_info["raw_reply"] = user_reply
            session.output_info["summary"] = user_reply[:200]
            session.stage = "generating"
            agent_msg = (
                "好的，信息足够了。让我为你生成工作流预览..."
            )
        else:
            session.stage = "generating"
            agent_msg = "让我根据我们讨论的内容来生成工作流..."

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
                "complexity": {
                    "type": "string",
                    "enum": ["SIMPLE", "MODERATE", "COMPLEX"],
                    "description": "Assessed workflow complexity"
                },
                "max_questions_needed": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 8,
                    "description": "Estimated number of Q&A rounds needed"
                },
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
                    "environment_setup": {
                        "type": "object",
                        "properties": {
                            "requires_network": {"type": "boolean"},
                            "requires_filesystem": {"type": "boolean"},
                            "external_dependencies": {"type": "array", "items": {"type": "string"}},
                            "setup_notes": {"type": "string"},
                        },
                    },
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
                                "script": {"type": "string", "description": "Inline Python or shell script"},
                                "script_type": {"type": "string", "enum": ["python", "shell"]},
                                "requires_packages": {"type": "array", "items": {"type": "string"}},
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
                environment_setup=result.get("environment_setup", {}),
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
        """Generate a template workflow without LLM (dev-mode fallback).

        Uses complexity-appropriate number of steps.  Contains placeholder
        steps that the user can refine manually.
        """
        complexity = getattr(session, 'complexity', 'MODERATE')

        if complexity == 'SIMPLE':
            steps = [
                {"index": 1, "title": "确认输入", "objective": f"确认输入: {session.goal[:80]}",
                 "action_type": "PREPARE", "depends_on": [], "expected_output": "已确认的输入",
                 "error_handling": "abort", "risk_level": "LOW"},
                {"index": 2, "title": "执行核心任务", "objective": "根据用户需求执行核心处理",
                 "action_type": "TOOL", "tool_name": "file.read", "depends_on": [1],
                 "expected_output": "处理结果", "error_handling": "retry", "risk_level": "LOW"},
                {"index": 3, "title": "输出结果", "objective": "展示最终结果",
                 "action_type": "FINALIZE", "depends_on": [2], "expected_output": "最终输出",
                 "error_handling": "abort", "risk_level": "LOW"},
            ]
            tools = ["file.read"]
            env_setup = {}
        elif complexity == 'COMPLEX':
            steps = [
                {"index": 1, "title": "环境准备", "objective": "检查并安装所需的依赖工具和库",
                 "action_type": "PREPARE", "depends_on": [],
                 "expected_output": "环境就绪确认", "error_handling": "abort", "risk_level": "MEDIUM"},
                {"index": 2, "title": "获取输入数据", "objective": f"获取输入: {session.goal[:80]}",
                 "action_type": "TOOL", "tool_name": "http.download", "depends_on": [1],
                 "expected_output": "输入数据", "error_handling": "retry", "risk_level": "MEDIUM"},
                {"index": 3, "title": "第一阶段处理", "objective": "执行管道第一阶段",
                 "action_type": "TOOL", "tool_name": "command.run", "depends_on": [2],
                 "expected_output": "阶段一结果", "error_handling": "retry", "risk_level": "HIGH"},
                {"index": 4, "title": "第二阶段处理", "objective": "执行管道第二阶段",
                 "action_type": "TOOL", "tool_name": "command.run", "depends_on": [3],
                 "expected_output": "阶段二结果", "error_handling": "retry", "risk_level": "HIGH"},
                {"index": 5, "title": "分析/校对", "objective": "对处理结果进行分析和校对",
                 "action_type": "MODEL_ANSWER", "depends_on": [4],
                 "expected_output": "分析报告", "error_handling": "retry", "risk_level": "LOW"},
                {"index": 6, "title": "生成最终输出", "objective": "将分析结果格式化为最终输出",
                 "action_type": "FINALIZE", "depends_on": [5], "expected_output": "最终输出",
                 "error_handling": "abort", "risk_level": "LOW"},
            ]
            tools = ["http.download", "command.run", "file.read", "file.write"]
            env_setup = {
                "requires_network": True,
                "requires_filesystem": True,
                "external_dependencies": [],
                "setup_notes": "请确认所需的命令行工具和 Python 库已安装。"
            }
        else:  # MODERATE
            steps = [
                {"index": 1, "title": "确认目标和输入", "objective": f"确认工作流目标: {session.goal[:100]}",
                 "action_type": "PREPARE", "depends_on": [], "expected_output": "已确认的执行上下文",
                 "error_handling": "abort", "risk_level": "LOW"},
                {"index": 2, "title": "处理数据/执行分析", "objective": "根据用户需求执行核心处理逻辑",
                 "action_type": "TOOL", "tool_name": "file.read", "depends_on": [1],
                 "expected_output": "处理结果", "error_handling": "retry", "risk_level": "LOW"},
                {"index": 3, "title": "生成输出", "objective": "生成最终结果并展示给用户",
                 "action_type": "FINALIZE", "depends_on": [2], "expected_output": "最终输出",
                 "error_handling": "abort", "risk_level": "LOW"},
            ]
            tools = ["file.read"]
            env_setup = {}

        return WorkflowTemplate(
            name=session.goal[:50] or "自定义工作流",
            description=session.goal[:200],
            risk_summary="MEDIUM" if complexity == "COMPLEX" else "LOW",
            steps=steps,
            required_tools=tools,
            required_permissions=[f"tool:{t}" for t in tools],
            environment_setup=env_setup,
        )

    def _format_preview_message(self, wf: WorkflowTemplate) -> str:
        """Format workflow preview as a readable message."""
        step_lines = "\n".join(
            f"  {s.get('index', i+1)}. **{s.get('title', 'Step')}** — {s.get('action_type', '')}"
            + (f" 📜 含脚本" if s.get('script') else "")
            + (f" ({s.get('script_type', '')})" if s.get('script_type') else "")
            for i, s in enumerate(wf.steps[:8])
        )
        if len(wf.steps) > 8:
            step_lines += f"\n  ... 共 {len(wf.steps)} 步"

        tools_str = ", ".join(wf.required_tools[:5]) if wf.required_tools else "无"
        risk_icon = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}.get(wf.risk_summary, "⚪")

        # Environment setup section
        env_section = ""
        env = wf.environment_setup
        if env:
            deps = env.get("external_dependencies", [])
            if deps:
                env_section += f"\n**外部依赖**: {', '.join(deps)}"
            if env.get("setup_notes"):
                env_section += f"\n**环境说明**: {env['setup_notes']}"
            if env_section:
                env_section = "\n### 🔧 环境要求" + env_section + "\n"

        return (
            f"## 📋 工作流预览\n\n"
            f"**名称**: {wf.name}\n"
            f"**描述**: {wf.description or '(无)'}\n"
            f"**风险**: {risk_icon} {wf.risk_summary}\n"
            f"**所需工具**: {tools_str}\n"
            f"{env_section}"
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
