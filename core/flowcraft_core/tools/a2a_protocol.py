"""A2A (Agent-to-Agent) Protocol — Google's standard for Agent collaboration.

A2A is complementary to MCP:
  - MCP: Agent ↔ Tool communication (how agents use tools)
  - A2A: Agent ↔ Agent communication (how agents talk to each other)

Key concepts:
  - Agent Card: Each agent's capability manifest (JSON)
  - Task: Unit of work passed between agents
  - Message: Communication format between agents
  - Artifact: Output of a task (text, file, structured data)

Reference: https://github.com/google/A2A
Status: Implemented as a lightweight subset for FlowCraft multi-agent scenarios.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────

class TaskState(Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ── Data Types ─────────────────────────────────────────────

@dataclass
class AgentCard:
    """A2A Agent Card — agent capability manifest.

    Published by each agent so others can discover what it can do.
    """
    name: str
    description: str
    url: str = ""  # Agent's endpoint URL
    version: str = "1.0"
    capabilities: list[str] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)
    input_modes: list[str] = field(default_factory=lambda: ["text"])
    output_modes: list[str] = field(default_factory=lambda: ["text"])

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "capabilities": self.capabilities,
            "skills": self.skills,
            "inputModes": self.input_modes,
            "outputModes": self.output_modes,
        }


@dataclass
class A2ATask:
    """A task passed between agents via A2A."""
    id: str
    session_id: str = ""
    description: str = ""
    state: TaskState = TaskState.SUBMITTED
    input_artifacts: list[dict] = field(default_factory=list)
    output_artifacts: list[dict] = field(default_factory=list)
    assigned_agent: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sessionId": self.session_id,
            "description": self.description,
            "state": self.state.value,
            "artifacts": self.output_artifacts,
            "metadata": self.metadata,
        }


@dataclass
class A2AMessage:
    """Message passed between agents."""
    id: str
    from_agent: str
    to_agent: str
    content: str
    message_type: str = "text"  # text, task, artifact, error
    task_ref: str | None = None
    artifacts: list[dict] = field(default_factory=list)
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from": self.from_agent,
            "to": self.to_agent,
            "content": self.content,
            "type": self.message_type,
            "taskRef": self.task_ref,
            "artifacts": self.artifacts,
        }


# ── A2A Bus ────────────────────────────────────────────────

class A2ABus:
    """Lightweight A2A message bus for in-process agent communication.

    For FlowCraft's internal multi-agent orchestration, all agents
    run in the same process. This bus provides structured message passing
    without requiring a network layer.

    Usage:
        bus = A2ABus()
        bus.register_agent(AgentCard(name="researcher", ...))
        bus.register_agent(AgentCard(name="coder", ...))

        task = bus.create_task("Write a report", assigned_to="researcher")
        bus.send_message(A2AMessage(from_agent="researcher", to_agent="coder", ...))

        messages = bus.get_messages(for_agent="coder")
    """

    def __init__(self):
        self._agents: dict[str, AgentCard] = {}
        self._tasks: dict[str, A2ATask] = {}
        self._messages: list[A2AMessage] = []
        self._task_counter = 0
        self._msg_counter = 0

    # ── Agent Registry ──────────────────────────────────

    def register_agent(self, card: AgentCard) -> None:
        self._agents[card.name] = card
        logger.info("A2A agent registered: %s (capabilities=%d)", card.name, len(card.capabilities))

    def unregister_agent(self, name: str) -> None:
        self._agents.pop(name, None)

    def get_agent(self, name: str) -> AgentCard | None:
        return self._agents.get(name)

    def find_agents_by_capability(self, capability: str) -> list[AgentCard]:
        """Find agents that have a specific capability."""
        return [
            agent for agent in self._agents.values()
            if capability in agent.capabilities
        ]

    # ── Task Management ────────────────────────────────

    def create_task(self, description: str, assigned_to: str = "", session_id: str = "") -> A2ATask:
        self._task_counter += 1
        task = A2ATask(
            id=f"a2a-task-{self._task_counter}",
            session_id=session_id,
            description=description,
            assigned_agent=assigned_to,
        )
        self._tasks[task.id] = task
        logger.info("A2A task created: %s → %s", task.id, assigned_to)
        return task

    def get_task(self, task_id: str) -> A2ATask | None:
        return self._tasks.get(task_id)

    def update_task_state(self, task_id: str, state: TaskState, artifacts: list[dict] | None = None) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.state = state
            if artifacts:
                task.output_artifacts = artifacts

    # ── Messaging ──────────────────────────────────────

    def send_message(self, msg: A2AMessage) -> None:
        self._msg_counter += 1
        msg.id = f"a2a-msg-{self._msg_counter}"
        self._messages.append(msg)

    def get_messages(self, for_agent: str, since_id: str | None = None) -> list[A2AMessage]:
        """Get messages addressed to a specific agent."""
        result = [m for m in self._messages if m.to_agent == for_agent]
        if since_id:
            result = [m for m in result if m.id > since_id]
        return result

    def get_task_messages(self, task_id: str) -> list[A2AMessage]:
        """Get all messages related to a task."""
        return [m for m in self._messages if m.task_ref == task_id]

    # ── Serialization ───────────────────────────────────

    def export_state(self) -> dict:
        """Export A2A bus state for debugging/audit."""
        return {
            "agents": len(self._agents),
            "tasks": {tid: t.to_dict() for tid, t in self._tasks.items()},
            "messages": len(self._messages),
        }
