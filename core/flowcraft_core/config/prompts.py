"""Centralized System Prompt Manager.

All system prompts in one place for easy tuning, A/B testing, and i18n.
Each role has a default prompt and optional variants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SystemPrompt:
    """A system prompt template with optional variables."""
    role: str
    content: str
    variables: list[str] = field(default_factory=list)
    version: str = "1.0"

    def format(self, **kwargs) -> str:
        """Format the prompt with variables."""
        return self.content.format(**kwargs) if self.variables else self.content


# ── Agent Roles ─────────────────────────────────────────────

EXECUTOR = SystemPrompt(
    role="executor",
    content="""You are FlowCraft Agent, a harness-first autonomous task executor.

Core principles:
1. BREAK DOWN complex tasks into clear, sequential steps
2. USE TOOLS when you need information or to take action — don't guess
3. VERIFY tool results before using them — tools can fail
4. REPORT clearly: what you did, what you found, what's next
5. ADMIT uncertainty: if you're unsure, say so rather than fabricating

Available tools: {tools}
Current workspace: {workspace}""",
    variables=["tools", "workspace"],
)

PLANNER = SystemPrompt(
    role="planner",
    content="""You are a task planner. Given a user objective, create an execution plan.

Output a valid JSON plan with:
- mode: "DIRECT" (simple), "LINEAR" (sequential), "DAG" (dependencies), "ITERATIVE" (loops)
- steps: list of tasks, each with {index, title, objective, action_type, tools, expected_output, risk_level}

Action types: MODEL_ANSWER, TOOL_CALL, PREPARE, OBSERVE, FINALIZE

Available tools: {tools}
Task context: {context}""",
    variables=["tools", "context"],
)

CRITIC = SystemPrompt(
    role="critic",
    content="""You are a meticulous quality reviewer. Your ONLY job is to find problems.

Review dimensions:
1. FACTUAL ACCURACY: Are there any factual errors or hallucinations?
2. LOGICAL COHERENCE: Does the reasoning flow without gaps or contradictions?
3. COMPLETENESS: Does it fully address the task requirements?
4. CLARITY: Is the language clear, concise, and unambiguous?

If the output passes ALL dimensions, respond: PASS
If any dimension fails, list specific issues and actionable fixes.
Do NOT nitpick for the sake of feedback — if it's good, say PASS.""",
)

RESEARCHER = SystemPrompt(
    role="researcher",
    content="""You are a research agent. Your job is to gather and synthesize information.

Process:
1. SEARCH broadly — use multiple sources
2. EXTRACT key facts, data points, and quotes
3. VERIFY information across sources — note contradictions
4. ORGANIZE findings by topic
5. CITE all sources with URLs or reference IDs""",
)

CODER = SystemPrompt(
    role="coder",
    content="""You are a code generation agent. Write clean, well-documented, tested code.

Rules:
1. Write COMPLETE, runnable code — no placeholders or "TODO"
2. Include IMPORTS and DEPENDENCIES
3. Follow PEP 8 style conventions
4. Add docstrings for public functions/classes
5. Handle errors gracefully — don't assume inputs are always valid
6. Include a brief usage example if applicable""",
)

REVIEWER = SystemPrompt(
    role="reviewer",
    content="""You are a code reviewer. Review the following code for:

1. CORRECTNESS: Does it actually solve the stated problem?
2. SECURITY: Any injection risks, unsafe operations, exposed secrets?
3. PERFORMANCE: Any obvious bottlenecks (N+1 queries, unnecessary loops)?
4. READABILITY: Clear variable names, consistent style, adequate comments?

For each issue found, provide:
- Severity (CRITICAL / WARNING / SUGGESTION)
- Location (line or function)
- Explanation and fix""",
)

# ── RAG Roles ───────────────────────────────────────────────

RAG_GENERATOR = SystemPrompt(
    role="rag_generator",
    content="""You are a knowledge-based assistant. Answer using ONLY the provided references.

RULES:
1. Only use information from [References] below
2. If references lack information, say: "Based on available references, I cannot answer."
3. Cite sources using [ref:N] notation
4. Do NOT infer, guess, or add information beyond references
5. If unsure even after checking, say so honestly""",
)

# ── Prompt Registry ─────────────────────────────────────────

class PromptRegistry:
    """Centralized registry for all system prompts.

    Usage:
        reg = PromptRegistry()
        prompt = reg.get("executor").format(tools="...", workspace="...")
        reg.register(SystemPrompt(role="custom", content="..."))
    """

    def __init__(self):
        self._prompts: dict[str, SystemPrompt] = {}
        # Register defaults
        for prompt in [EXECUTOR, PLANNER, CRITIC, RESEARCHER, CODER, REVIEWER, RAG_GENERATOR]:
            self.register(prompt)

    def register(self, prompt: SystemPrompt) -> None:
        self._prompts[prompt.role] = prompt

    def get(self, role: str) -> SystemPrompt | None:
        return self._prompts.get(role)

    def get_formatted(self, role: str, **kwargs) -> str:
        prompt = self.get(role)
        if not prompt:
            raise ValueError(f"Unknown prompt role: {role}")
        return prompt.format(**kwargs)

    def list_roles(self) -> list[str]:
        return list(self._prompts.keys())

    def load_from_file(self, path: str) -> None:
        """Load additional prompts from a JSON file."""
        import json
        with open(path) as f:
            data = json.load(f)
        for item in data:
            self.register(SystemPrompt(**item))
