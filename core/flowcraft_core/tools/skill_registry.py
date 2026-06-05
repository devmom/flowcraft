"""Skill Registry — Reusable agent skill modules.

Skills package tool knowledge, workflows, and prompts into reusable units.
When a task matches a known skill, the Planner is bypassed — the skill's
pre-built workflow is used directly, making execution 3-5x faster and more reliable.

Concept:
  - Function Calling = atomic tool (single screwdriver)
  - MCP = tool standard (toolbox)
  - Skill = operation manual (when to use which tools, in what order, how to verify)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """A reusable agent capability — tool chain + prompt + workflow template."""
    name: str
    description: str
    trigger_keywords: list[str] = field(default_factory=list)
    tools_required: list[str] = field(default_factory=list)
    system_prompt: str = ""
    workflow_steps: list[dict] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    risk_level: str = "LOW"
    enabled: bool = True
    version: str = "1.0"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "trigger_keywords": self.trigger_keywords,
            "tools_required": self.tools_required,
            "version": self.version,
            "risk_level": self.risk_level,
        }


# ── Built-in Skills ────────────────────────────────────────

BUILTIN_SKILLS = [
    Skill(
        name="code_review",
        description="Review code for quality, security, and best practices",
        trigger_keywords=["review code", "code review", "审查代码", "检查代码", "review this code"],
        tools_required=["file_read", "file_search"],
        system_prompt="You are a senior code reviewer. Check for: correctness, security, performance, readability.",
        workflow_steps=[
            {"index": 1, "title": "Read code files", "action": "read_files"},
            {"index": 2, "title": "Static analysis", "action": "analyze", "prompt": "Identify: bugs, security issues, performance problems, style violations."},
            {"index": 3, "title": "Generate report", "action": "report", "prompt": "Summarize findings with severity levels and fix suggestions."},
        ],
        success_criteria=["All files reviewed", "Issues categorized by severity", "Specific fix suggestions provided"],
    ),
    Skill(
        name="document_summary",
        description="Summarize long documents into concise overviews",
        trigger_keywords=["summarize", "总结", "概述", "tl;dr", "summarize document"],
        tools_required=["file_read", "pdf_read", "docx_read"],
        system_prompt="You are a document summarizer. Extract key points and produce concise summaries.",
        workflow_steps=[
            {"index": 1, "title": "Read document", "action": "read_document"},
            {"index": 2, "title": "Identify key points", "action": "analyze", "prompt": "Extract: main thesis, key arguments, conclusions, important data."},
            {"index": 3, "title": "Generate summary", "action": "report", "prompt": "Produce structured summary with TL;DR, key points, and detailed sections."},
        ],
        success_criteria=["Key points captured", "Structure preserved", "Concise (20-30% of original length)"],
    ),
    Skill(
        name="web_research",
        description="Research a topic using web search and compile findings",
        trigger_keywords=["research", "调研", "搜索", "search for", "find information about", "look up"],
        tools_required=["web_search", "browser_read", "file_write"],
        system_prompt="You are a research assistant. Search broadly, verify sources, compile structured reports.",
        workflow_steps=[
            {"index": 1, "title": "Search", "action": "web_search", "prompt": "Search multiple sources for the topic."},
            {"index": 2, "title": "Read & extract", "action": "browser_read", "prompt": "Read top results and extract relevant information."},
            {"index": 3, "title": "Compile report", "action": "report", "prompt": "Compile findings with sources cited. Include: overview, key findings, sources."},
        ],
        success_criteria=["Multiple sources consulted", "Information verified", "Sources cited"],
    ),
    Skill(
        name="data_analysis",
        description="Analyze CSV/Excel data and produce insights",
        trigger_keywords=["analyze data", "数据分析", "analyze csv", "analyze excel", "plot", "chart"],
        tools_required=["file_read", "excel_read", "code_execute"],
        system_prompt="You are a data analyst. Load data, compute statistics, generate visualizations and insights.",
        workflow_steps=[
            {"index": 1, "title": "Load data", "action": "read_file"},
            {"index": 2, "title": "Explore & clean", "action": "analyze", "prompt": "Check data types, missing values, outliers. Clean if needed."},
            {"index": 3, "title": "Analyze", "action": "code_execute", "prompt": "Compute statistics, correlations, trends."},
            {"index": 4, "title": "Report", "action": "report", "prompt": "Present findings with key metrics and recommendations."},
        ],
        success_criteria=["Data loaded successfully", "Statistics computed", "Actionable insights provided"],
        risk_level="MEDIUM",
    ),
]


# ── Skill Registry ──────────────────────────────────────────

class SkillRegistry:
    """Registry of reusable agent skills.

    Skills are matched against user intent and, when applicable, bypass
    the LLM Planner for faster, more predictable execution.
    """

    def __init__(self):
        self._skills: dict[str, Skill] = {}
        # Register built-in skills
        for skill in BUILTIN_SKILLS:
            self.register(skill)

    def register(self, skill: Skill) -> None:
        """Register a new skill."""
        self._skills[skill.name] = skill
        logger.info("Skill registered: %s (keywords=%d)", skill.name, len(skill.trigger_keywords))

    def unregister(self, name: str) -> bool:
        """Remove a skill by name."""
        if name in self._skills:
            del self._skills[name]
            return True
        return False

    def match(self, user_input: str, min_keywords: int = 1) -> list[Skill]:
        """Match user input against registered skills.

        Returns skills sorted by match score (more keyword matches = higher).
        """
        scored = []
        input_lower = user_input.lower()
        for skill in self._skills.values():
            if not skill.enabled:
                continue
            matches = sum(1 for kw in skill.trigger_keywords if kw.lower() in input_lower)
            if matches >= min_keywords:
                scored.append((matches, skill))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored]

    def get_best_match(self, user_input: str) -> Skill | None:
        """Get the single best matching skill, or None."""
        matches = self.match(user_input)
        return matches[0] if matches else None

    def list_all(self) -> list[dict]:
        """List all registered skills."""
        return [s.to_dict() for s in self._skills.values() if s.enabled]

    def get(self, name: str) -> Skill | None:
        """Get a skill by name."""
        return self._skills.get(name)
