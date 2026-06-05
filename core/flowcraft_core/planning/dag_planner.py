"""DAG Planner + Multi-Agent Collaboration.

DAG Planner: Generate parallel execution plans with dependency resolution.
Multi-Agent: Delegate sub-tasks to specialized agent roles.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from flowcraft_core.domain.enums import PlanMode, RiskLevel, StepStatus, TaskStatus
from flowcraft_core.domain.schemas import (
    ExecutionPlan, PlanStep, Task, TaskBrief, TraceEvent,
)
from flowcraft_core.models.gateway import ModelGateway
from flowcraft_core.observability.events import EventRecorder

logger = logging.getLogger(__name__)

# Agent role definitions for multi-agent collaboration
AGENT_ROLES = {
    "researcher": {
        "name": "研究员", "description": "搜索、阅读、分析信息",
        "tools": ["file.read", "knowledge.search", "browser.read"],
        "system_prompt": "You are a research agent. Gather and analyze information thoroughly.",
    },
    "coder": {
        "name": "程序员", "description": "编写代码、调试、分析",
        "tools": ["file.read", "file.write", "command.run"],
        "system_prompt": "You are a coding agent. Write clean, well-documented code.",
    },
    "writer": {
        "name": "写作者", "description": "撰写文档、总结、报告",
        "tools": ["file.read", "file.write"],
        "system_prompt": "You are a writing agent. Produce clear, well-structured documents.",
    },
    "analyst": {
        "name": "分析师", "description": "数据分析、可视化、建模",
        "tools": ["file.read", "document.xlsx.read", "command.run"],
        "system_prompt": "You are an analyst agent. Analyze data and provide insights.",
    },
    "executor": {
        "name": "执行者", "description": "执行命令、文件操作、部署",
        "tools": ["command.run", "file.read", "file.write"],
        "system_prompt": "You are an executor agent. Execute operations safely and report results.",
    },
}


class DagPlanner:
    """DAG-aware planner that generates parallel execution plans.

    Unlike the LINEAR planner, this can:
    - Identify independent steps that can run in parallel
    - Specify step dependencies (depends_on)
    - Generate optimal execution order via topological sort
    """

    def __init__(self, model_gateway: ModelGateway) -> None:
        self.model_gateway = model_gateway

    async def create_dag_plan(self, brief: TaskBrief) -> ExecutionPlan:
        """Generate a DAG execution plan with parallel steps and dependencies."""
        schema = {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer"},
                            "title": {"type": "string"},
                            "objective": {"type": "string"},
                            "action_type": {"type": "string", "enum": ["PREPARE", "TOOL", "MODEL_ANSWER", "FINALIZE"]},
                            "required_tools": {"type": "array", "items": {"type": "string"}},
                            "depends_on": {"type": "array", "items": {"type": "integer"}, "description": "IDs of steps this step depends on"},
                            "expected_output": {"type": "string"},
                            "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
                            "approval_required": {"type": "boolean"},
                            "agent_role": {"type": "string", "description": "Optional: assign to a specialized agent role (researcher, coder, writer, analyst, executor)"},
                        },
                        "required": ["index", "title", "objective", "action_type", "expected_output", "risk_level"],
                    },
                },
            },
            "required": ["goal", "steps"],
        }

        prompt = (
            f"Create a DAG (Directed Acyclic Graph) execution plan for this task.\n"
            f"Objective: {brief.objective}\n"
            f"Type: {brief.task_type}\n"
            f"Risk: {brief.risk_level}\n\n"
            f"Rules:\n"
            f"1. Identify steps that can run IN PARALLEL (no data dependency)\n"
            f"2. Use 'depends_on' to specify which steps must complete first\n"
            f"3. At least 2 steps should be parallelizable if possible\n"
            f"4. Assign an 'agent_role' to each step: researcher, coder, writer, analyst, executor\n"
            f"5. Steps with no dependencies run concurrently; dependent steps wait"
        )

        try:
            result = await self.model_gateway._adapter.structured_chat(
                [
                    {"role": "system", "content": "You are a workflow planner. Generate DAG execution plans."},
                    {"role": "user", "content": prompt},
                ],
                schema, temperature=0.2, max_tokens=2048,
            )
            steps = [PlanStep(**step) for step in result.get("steps", [])]
            return ExecutionPlan(
                task_id=brief.task_id,
                mode=PlanMode.DAG,
                goal=result.get("goal", brief.objective),
                steps=steps,
                constraints=brief.constraints,
                success_criteria=brief.success_criteria,
            )
        except Exception:
            # Fallback: generate linear plan with parallel hints
            return await self._fallback_dag(brief)

    async def _fallback_dag(self, brief: TaskBrief) -> ExecutionPlan:
        """Fallback DAG: create independent steps that can theoretically run in parallel."""
        return ExecutionPlan(
            task_id=brief.task_id,
            mode=PlanMode.DAG,
            goal=brief.objective,
            constraints=brief.constraints,
            steps=[
                PlanStep(
                    index=1, title="信息收集", objective=f"收集与 {brief.objective} 相关的信息",
                    action_type="PREPARE", required_tools=["knowledge.search"],
                    expected_output="信息汇总", risk_level=RiskLevel.LOW,
                ),
                PlanStep(
                    index=2, title="任务执行", objective=brief.objective,
                    action_type="TOOL", depends_on=[1],
                    required_tools=["file.read", "command.run"],
                    expected_output="执行结果", risk_level=brief.risk_level,
                ),
                PlanStep(
                    index=3, title="结果整理", objective="整理和格式化执行结果",
                    action_type="FINALIZE", depends_on=[2],
                    expected_output="最终输出", risk_level=RiskLevel.LOW,
                ),
            ],
        )

    def topological_sort(self, steps: list[PlanStep]) -> list[list[PlanStep]]:
        """Group steps into execution layers by dependency resolution.

        Returns layers where each layer's steps can run in parallel.
        """
        step_map = {s.index: s for s in steps}
        indegrees = {s.index: len(s.depends_on) for s in steps}
        reverse_deps: dict[int, list[int]] = {s.index: [] for s in steps}
        for s in steps:
            for dep in s.depends_on:
                if dep in reverse_deps:
                    reverse_deps[dep].append(s.index)

        layers: list[list[PlanStep]] = []
        processed: set[int] = set()

        while len(processed) < len(steps):
            ready = [idx for idx, deg in indegrees.items()
                     if deg == 0 and idx not in processed]
            if not ready:
                break  # cycle or all processed
            current_layer = [step_map[idx] for idx in ready if idx in step_map]
            layers.append(current_layer)
            for idx in ready:
                processed.add(idx)
                for dependent in reverse_deps.get(idx, []):
                    indegrees[dependent] -= 1

        return layers


class MultiAgentOrchestrator:
    """Orchestrate multi-agent collaboration.

    When a task is complex, decompose it into sub-tasks and assign
    to specialized agents based on their roles.
    """

    def __init__(self, model_gateway: ModelGateway, events: EventRecorder | None = None) -> None:
        self.model_gateway = model_gateway
        self.events = events

    def select_agents(self, brief: TaskBrief) -> list[str]:
        """Auto-select agent roles based on task type and requirements."""
        role_map = {
            "QA": ["researcher"],
            "FILE_TASK": ["executor"],
            "BROWSER_TASK": ["researcher"],
            "LOCAL_OPERATION": ["executor"],
            "DOCUMENT_SUMMARY": ["researcher", "writer"],
            "CODE_TASK": ["coder"],
            "DATA_ANALYSIS": ["analyst"],
            "WORKFLOW_AUTOMATION": ["researcher", "executor", "writer"],
        }
        roles = role_map.get(brief.task_type, ["executor"])
        # Ensure unique
        return list(dict.fromkeys(roles))

    async def decompose_task(
        self, task: Task, brief: TaskBrief, agent_count: int = 2
    ) -> list[dict[str, Any]]:
        """Decompose a task into sub-tasks for agent delegation."""
        agents = self.select_agents(brief)[:agent_count]

        schema = {
            "type": "object",
            "properties": {
                "sub_tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "objective": {"type": "string"},
                            "assigned_role": {"type": "string"},
                            "inputs": {"type": "array", "items": {"type": "string"}},
                            "expected_output": {"type": "string"},
                            "priority": {"type": "integer"},
                        },
                        "required": ["title", "objective", "assigned_role", "expected_output"],
                    },
                },
            },
            "required": ["sub_tasks"],
        }

        prompt = (
            f"Decompose the following task for {agent_count} specialized agents.\n"
            f"Available agents: {', '.join(agents)}\n\n"
            f"Task: {task.objective}\n"
            f"Type: {brief.task_type}\n\n"
            f"Create {agent_count} sub-tasks with clear inputs/outputs and assign each to the best agent."
        )

        try:
            result = await self.model_gateway._adapter.structured_chat(
                [
                    {"role": "system", "content": "You are a task decomposition specialist."},
                    {"role": "user", "content": prompt},
                ],
                schema, temperature=0.2, max_tokens=2048,
            )
            return result.get("sub_tasks", [])
        except Exception:
            return self._fallback_decompose(task, agents)

    def _fallback_decompose(self, task: Task, agents: list[str]) -> list[dict[str, Any]]:
        """Simple fallback decomposition."""
        return [
            {
                "title": f"分析: {task.objective[:30]}",
                "objective": f"分析和理解: {task.objective}",
                "assigned_role": agents[0] if agents else "researcher",
                "inputs": [task.objective],
                "expected_output": "分析结果",
                "priority": 1,
            },
            {
                "title": f"执行: {task.objective[:30]}",
                "objective": task.objective,
                "assigned_role": agents[-1] if agents else "executor",
                "inputs": ["分析结果"],
                "expected_output": "执行结果",
                "priority": 2,
            },
        ]

    def agent_context(self, role: str) -> dict:
        """Get agent role context (system prompt, tools, etc)."""
        return AGENT_ROLES.get(role, AGENT_ROLES["executor"])


class AgentDelegation:
    """Track an agent delegation for a sub-task."""

    def __init__(self, delegation_id: str, parent_task_id: str, role: str,
                 objective: str, inputs: list[str]) -> None:
        self.delegation_id = delegation_id
        self.parent_task_id = parent_task_id
        self.role = role
        self.objective = objective
        self.inputs = inputs
        self.status: str = "pending"
        self.result: str = ""
