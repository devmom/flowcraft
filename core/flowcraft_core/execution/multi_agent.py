"""Multi-Agent Orchestrator — Centralized multi-agent task coordination.

Design principles (from engineering best practices):
  1. Centralized Orchestrator — NOT decentralized (too hard to debug)
  2. DAG-based dependency — leverages existing DAG Planner
  3. Isolated contexts — each Agent has its own clean context window
  4. State management — append-only shared state, no overwrites

Three collaboration modes:
  - Sequential Pipeline: Agent A → Agent B → Agent C
  - Parallel Fan-out: Orchestrator dispatches independent subtasks in parallel
  - Debate/Review: Multiple agents propose, Critic selects best

Reference: Lesson from agent interview Q10-Q16 (xiaolinnote.com)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── Data Types ──────────────────────────────────────────────

class CollaborationMode(Enum):
    SEQUENTIAL = "sequential"       # A → B → C
    FAN_OUT = "fan_out"             # Parallel dispatch
    DEBATE = "debate"               # Multiple proposals → select best


@dataclass
class AgentProfile:
    """Profile for a worker agent in the multi-agent system."""
    name: str
    role: str                    # e.g., "researcher", "coder", "reviewer"
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    model_id: str | None = None  # None = use orchestrator's model
    context_budget: int = 8000   # Max context chars for this agent


@dataclass
class AgentResult:
    """Result from a single agent execution."""
    agent_name: str
    output: str
    success: bool = True
    error: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class OrchestrationResult:
    """Complete multi-agent orchestration result."""
    mode: CollaborationMode
    results: list[AgentResult] = field(default_factory=list)
    merged_output: str = ""
    rounds: int = 1
    reflections: int = 0


# ── Shared State ────────────────────────────────────────────

class SharedState:
    """Append-only shared state for multi-agent coordination.

    Key design decisions:
      - Append-only: agents write results, never overwrite
      - Global + Local: global state (all agents) + per-agent local state
      - Error propagation: failed agent writes error state, not silently ignored
    """

    def __init__(self):
        self.global_state: dict[str, Any] = {}       # All agents can read/write
        self.local_states: dict[str, dict] = {}       # Per-agent private state
        self.task_log: list[dict] = []                # Append-only execution log

    def set_global(self, key: str, value: Any) -> None:
        self.global_state[key] = value
        self.task_log.append({"type": "global_set", "key": key, "agent": "orchestrator"})

    def get_global(self, key: str, default: Any = None) -> Any:
        return self.global_state.get(key, default)

    def set_local(self, agent: str, key: str, value: Any) -> None:
        if agent not in self.local_states:
            self.local_states[agent] = {}
        self.local_states[agent][key] = value

    def get_local(self, agent: str, key: str, default: Any = None) -> Any:
        return self.local_states.get(agent, {}).get(key, default)

    def record_result(self, agent: str, result: AgentResult) -> None:
        self.task_log.append({
            "type": "agent_result",
            "agent": agent,
            "success": result.success,
            "output_len": len(result.output),
            "error": result.error,
        })

    def summary(self) -> str:
        """Build a summary of execution progress for the Orchestrator."""
        completed = [e for e in self.task_log if e["type"] == "agent_result" and e["success"]]
        failed = [e for e in self.task_log if e["type"] == "agent_result" and not e["success"]]
        return (
            f"Progress: {len(completed)} completed, {len(failed)} failed.\n"
            + "\n".join(f"- [{e.get('agent', '?')}]: {'OK' if e.get('success') else 'FAIL'}"
                       for e in self.task_log[-5:])
        )


# ── Orchestrator ────────────────────────────────────────────

class MultiAgentOrchestrator:
    """Centralized multi-agent coordinator.

    Usage:
        orch = MultiAgentOrchestrator(model_gateway, tool_harness)
        orch.register_agent(AgentProfile(name="researcher", role="researcher", ...))
        orch.register_agent(AgentProfile(name="coder", role="coder", ...))

        result = await orch.orchestrate(
            task="Write a report on AI trends and provide code examples",
            mode=CollaborationMode.SEQUENTIAL,
            agent_chain=["researcher", "coder"],
        )
    """

    def __init__(self, model_gateway: Any, tool_harness: Any = None, events: Any = None):
        self.gateway = model_gateway
        self.tool_harness = tool_harness
        self.events = events
        self._agents: dict[str, AgentProfile] = {}
        self._state = SharedState()

    def register_agent(self, profile: AgentProfile) -> None:
        """Register a worker agent."""
        self._agents[profile.name] = profile
        logger.info("Agent registered: %s (role=%s, tools=%d)", profile.name, profile.role, len(profile.tools))

    # ── Main Orchestration ────────────────────────────────

    async def orchestrate(
        self,
        task: str,
        mode: CollaborationMode = CollaborationMode.SEQUENTIAL,
        agent_chain: list[str] | None = None,
        max_rounds: int = 1,
    ) -> OrchestrationResult:
        """Main entry point: orchestrate a task across agents.

        Args:
            task: User's task description.
            mode: Collaboration mode.
            agent_chain: Ordered list of agent names (sequential) or agent names (fan_out).
            max_rounds: Max refinement rounds (for debate mode).

        Returns:
            OrchestrationResult with all agent outputs merged.
        """
        if not agent_chain:
            agent_chain = list(self._agents.keys())

        self._state = SharedState()
        self._state.set_global("task", task)

        if mode == CollaborationMode.SEQUENTIAL:
            result = await self._run_sequential(task, agent_chain)
        elif mode == CollaborationMode.FAN_OUT:
            result = await self._run_fan_out(task, agent_chain)
        elif mode == CollaborationMode.DEBATE:
            result = await self._run_debate(task, agent_chain, max_rounds)
        else:
            raise ValueError(f"Unknown mode: {mode}")

        return result

    # ── Execution Modes ───────────────────────────────────

    async def _run_sequential(self, task: str, chain: list[str]) -> OrchestrationResult:
        """Sequential pipeline: A → B → C."""
        results = []
        context = task

        for agent_name in chain:
            agent = self._agents.get(agent_name)
            if not agent:
                logger.warning("Agent '%s' not found, skipping", agent_name)
                continue

            # Pass previous agent's output as context
            prompt = self._build_agent_prompt(agent, context)
            result = await self._execute_agent(agent, prompt)
            results.append(result)
            self._state.record_result(agent_name, result)

            if result.success:
                context = f"Task: {task}\n\nPrevious agent ({agent_name}) output:\n{result.output}"
            else:
                logger.warning("Agent %s failed: %s", agent_name, result.error)

        merged = self._merge_results(results)
        return OrchestrationResult(
            mode=CollaborationMode.SEQUENTIAL,
            results=results,
            merged_output=merged,
        )

    async def _run_fan_out(self, task: str, agents: list[str]) -> OrchestrationResult:
        """Parallel fan-out: dispatch to all agents simultaneously."""
        async def run_one(name: str):
            agent = self._agents.get(name)
            if not agent: return None
            prompt = self._build_agent_prompt(agent, task)
            return await self._execute_agent(agent, prompt)

        # Run all in parallel
        coros = [run_one(name) for name in agents]
        results_raw = await asyncio.gather(*coros)

        results = [r for r in results_raw if r is not None]
        merged = self._merge_results(results)

        return OrchestrationResult(
            mode=CollaborationMode.FAN_OUT,
            results=results,
            merged_output=merged,
        )

    async def _run_debate(self, task: str, agents: list[str], max_rounds: int) -> OrchestrationResult:
        """Debate mode: all agents propose, Critic selects best."""
        proposals = []

        # Round 1: All agents propose
        for agent_name in agents:
            agent = self._agents.get(agent_name)
            if not agent: continue
            prompt = self._build_agent_prompt(agent, f"Propose a solution for: {task}")
            result = await self._execute_agent(agent, prompt)
            proposals.append(result)
            self._state.record_result(agent_name, result)

        # Critic evaluation
        best = await self._select_best(task, proposals)

        return OrchestrationResult(
            mode=CollaborationMode.DEBATE,
            results=proposals,
            merged_output=best.output if best else self._merge_results(proposals),
            rounds=max_rounds,
        )

    # ── Internal ──────────────────────────────────────────

    def _build_agent_prompt(self, agent: AgentProfile, context: str) -> str:
        """Build a prompt for a specific agent including its role and context."""
        parts = [
            f"## Role: {agent.role}",
            f"## Instructions\n{agent.system_prompt}",
        ]
        if agent.tools:
            parts.append(f"## Available Tools\n{', '.join(agent.tools)}")
        parts.append(f"## Task/Context\n{context}")
        return "\n\n".join(parts)

    async def _execute_agent(self, agent: AgentProfile, prompt: str) -> AgentResult:
        """Execute a single agent and return its result."""
        if not self.gateway or not self.gateway.is_live():
            return AgentResult(
                agent_name=agent.name,
                output=f"[Agent {agent.name} ({agent.role})] Model not available for execution.",
                success=False,
                error="Model not configured",
            )

        try:
            messages = [
                {"role": "system", "content": agent.system_prompt},
                {"role": "user", "content": prompt},
            ]

            # If agent has specific model, switch temporarily
            if agent.model_id and agent.model_id != self.gateway.current_model_id:
                self.gateway.switch_model(agent.model_id)

            output = await self.gateway._adapter.chat(messages, temperature=0.3, max_tokens=2048)

            return AgentResult(
                agent_name=agent.name,
                output=output,
                success=True,
                metadata={"model": self.gateway.current_model_id},
            )
        except Exception as exc:
            return AgentResult(
                agent_name=agent.name,
                output="",
                success=False,
                error=str(exc),
            )

    async def _select_best(self, task: str, proposals: list[AgentResult]) -> AgentResult | None:
        """Debate: let the orchestrator model select the best proposal."""
        if not proposals:
            return None
        if not self.gateway or not self.gateway.is_live():
            return proposals[0]  # Fallback: first proposal

        options = "\n\n".join(
            f"Option {i + 1} (from {p.agent_name}):\n{p.output[:500]}"
            for i, p in enumerate(proposals)
        )
        prompt = (
            f"Task: {task}\n\n"
            f"Proposals:\n{options}\n\n"
            f"Select the best proposal. Consider: correctness, completeness, clarity. "
            f"Output: {{'best': N, 'reason': '...'}}"
        )
        try:
            raw = await self.gateway._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=200,
            )
            match = __import__('re').search(r"'best':\s*(\d+)", raw)
            if match:
                idx = int(match.group(1)) - 1
                if 0 <= idx < len(proposals):
                    return proposals[idx]
        except Exception:
            pass
        return proposals[0]

    @staticmethod
    def _merge_results(results: list[AgentResult]) -> str:
        """Merge multiple agent results into a single output."""
        parts = []
        for r in results:
            status = "OK" if r.success else f"FAIL: {r.error}"
            parts.append(f"## {r.agent_name} [{status}]\n{r.output}")
        return "\n\n".join(parts)
