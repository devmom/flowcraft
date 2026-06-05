"""ReAct (Reasoning + Acting) Executor.

Standard ReAct loop: Thought → Action → Observation → Thought → ... → Final Answer.

Key improvements over the existing step-based execution:
  1. Explicit Thought/Action/Observation cycle (not just tool_call/final_answer)
  2. Built-in max steps guard (prevent infinite loops)
  3. Observability: each cycle is a trace event
  4. Works with or without the existing Planner — can be used standalone
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from flowcraft_core.domain.schemas import TraceEvent
from flowcraft_core.execution.reflection import ReflectionLoop

logger = logging.getLogger(__name__)

# ── ReAct System Prompt ────────────────────────────────────

REACT_SYSTEM_PROMPT = """You are an AI assistant that solves tasks through a Reasoning + Acting (ReAct) loop.

For each step, respond in this exact JSON format:

{
  "thought": "Your reasoning about what to do next and why",
  "action": "tool_name" or "final_answer",
  "action_input": {"param": "value"} or null for final_answer,
  "final_answer": "Your answer to the user" (only when action=final_answer)
}

Rules:
1. ALWAYS start with a "thought" — explain your reasoning before acting
2. After each tool call, you'll receive an "observation" — use it to decide the next step
3. If you have enough information to answer, set action="final_answer"
4. If you need more information, call a tool with the appropriate action_input
5. Do NOT make up information — only use what tools return
6. Be concise — do not repeat yourself across thoughts
"""

# ── Executor ────────────────────────────────────────────────

class ReActExecutor:
    """Standard ReAct (Reasoning + Acting) execution loop.

    Integrates with the existing ExecutionEngine's tool harness and event recorder.
    Can be used as an alternative to the step-based DAG executor for simpler tasks.
    """

    MAX_REACT_STEPS = 12  # Safety limit

    def __init__(
        self,
        model_gateway: Any,
        tool_harness: Any,
        events: Any = None,
        enable_reflection: bool = False,
    ):
        """
        Args:
            model_gateway: ModelGateway for LLM calls
            tool_harness: ToolHarness for executing tools
            events: EventRecorder for trace events (optional)
            enable_reflection: Enable step-level quality reflection
        """
        self.gateway = model_gateway
        self.tool_harness = tool_harness
        self.events = events
        self.reflection = ReflectionLoop(model_gateway) if enable_reflection else None
        self._tool_schemas: list[dict] = []

    def set_tools(self, tools: list[Any]) -> None:
        """Register available tools for the executor.

        Each tool should have: name, description, parameters (JSON Schema).
        """
        self._tool_schemas = []
        for tool in tools:
            schema = {
                "name": tool.name,
                "description": getattr(tool, "description", ""),
                "parameters": getattr(tool, "parameters", {}),
            }
            self._tool_schemas.append(schema)

    async def execute(
        self,
        task: str,
        task_id: str = "",
        additional_context: str = "",
    ) -> dict[str, Any]:
        """Execute a task using the ReAct loop.

        Args:
            task: User's task description
            task_id: Optional task ID for event tracking
            additional_context: Extra context to include in system prompt

        Returns:
            {
                "answer": str,
                "steps": int,
                "tools_called": list[str],
                "thoughts": list[str],
                "reflection_rounds": int,
            }
        """
        tools_desc = self._format_tools()
        system_prompt = REACT_SYSTEM_PROMPT
        if tools_desc:
            system_prompt += f"\n\nAvailable Tools:\n{tools_desc}"
        if additional_context:
            system_prompt += f"\n\nContext:\n{additional_context}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        thoughts: list[str] = []
        tools_called: list[str] = []
        total_reflections = 0

        for step in range(1, self.MAX_REACT_STEPS + 1):
            # ── Thought + Action ──
            t0 = time.monotonic()
            response = await self._llm_react(messages)
            elapsed = time.monotonic() - t0

            thought = response.get("thought", "")
            thoughts.append(thought)

            if self.events and task_id:
                self.events.record(TraceEvent(
                    task_id=task_id,
                    event_type="step.reasoning",
                    title=f"ReAct Step {step}: Thought",
                    message=thought[:500],
                    metadata={"step": step, "elapsed_ms": int(elapsed * 1000)},
                ))

            # ── Check for final answer ──
            action = response.get("action", "")
            if action == "final_answer" or not action:
                answer = response.get("final_answer", thought)
                # Optional: reflection
                if self.reflection and answer:
                    answer = await self.reflection.step_level_reflect(
                        step_output=answer,
                        step_objective=task,
                    )
                    total_reflections += 1

                if self.events and task_id:
                    self.events.record(TraceEvent(
                        task_id=task_id,
                        event_type="step.answer",
                        title=f"ReAct Final Answer (step {step})",
                        message=answer[:500],
                    ))
                return {
                    "answer": answer,
                    "steps": step,
                    "tools_called": tools_called,
                    "thoughts": thoughts,
                    "reflection_rounds": total_reflections,
                }

            # ── Tool call ──
            tool_name = action
            tool_input = response.get("action_input", {})
            tools_called.append(tool_name)

            observation = await self._execute_tool(tool_name, tool_input)

            if self.events and task_id:
                self.events.record(TraceEvent(
                    task_id=task_id,
                    event_type="step.completed",
                    title=f"ReAct Step {step}: {tool_name}",
                    message=observation[:500] if observation else "(no output)",
                    metadata={"tool": tool_name, "step": step},
                ))

            # ── Observation feedback ──
            messages.append({"role": "assistant", "content": json.dumps(response, ensure_ascii=False)})
            messages.append({"role": "user", "content": f"Observation: {observation}"})

        # Max steps reached — force final answer
        force_prompt = (
            "You have reached the maximum number of steps. "
            "Based on all observations so far, provide your best final answer. "
            "If you don't have enough information, explain what's missing."
        )
        messages.append({"role": "user", "content": force_prompt})
        final = await self._llm_react(messages)
        return {
            "answer": final.get("final_answer", "Unable to complete task within step limit."),
            "steps": self.MAX_REACT_STEPS,
            "tools_called": tools_called,
            "thoughts": thoughts,
            "reflection_rounds": total_reflections,
            "truncated": True,
        }

    # ── Internal ──────────────────────────────────────────

    async def _llm_react(self, messages: list[dict]) -> dict[str, Any]:
        """Call LLM and parse ReAct JSON response."""
        if not self.gateway or not self.gateway.is_live():
            return {"thought": "No model available", "action": "final_answer",
                    "final_answer": "Model not configured. Set an API key to enable ReAct execution."}

        raw = await self.gateway._adapter.chat(messages, temperature=0.2, max_tokens=2048)

        # Parse JSON from response
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # Try extracting JSON from code fence
        import re
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        # Fallback: treat entire response as final answer
        return {"thought": raw[:200], "action": "final_answer", "final_answer": raw}

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """Execute a tool via ToolHarness and return observation text."""
        try:
            result = await self.tool_harness.execute(tool_name, **(tool_input or {}))
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False, indent=2)
            return str(result)[:2000]  # Truncate very long outputs
        except Exception as exc:
            return f"Tool '{tool_name}' failed: {exc}"

    def _format_tools(self) -> str:
        """Format available tools as a readable description."""
        if not self._tool_schemas:
            return "No tools available."

        lines = []
        for tool in self._tool_schemas:
            params = json.dumps(tool.get("parameters", {}), ensure_ascii=False)
            lines.append(
                f"- {tool['name']}: {tool.get('description', 'No description')}\n"
                f"  Parameters: {params}"
            )
        return "\n".join(lines)
