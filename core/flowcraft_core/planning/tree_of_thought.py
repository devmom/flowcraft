"""Tree of Thoughts (ToT) Planner.

ToT extends Chain of Thought by exploring MULTIPLE reasoning paths simultaneously:
  1. Generate N candidate solutions
  2. Evaluate each candidate
  3. Prune low-scoring paths
  4. Expand surviving paths deeper
  5. Repeat until depth limit or best solution found

Cost: 3-5x CoT (due to multiple LLM calls per level).
Best for: High-accuracy tasks where CoT's single-path approach is insufficient.

Reference: Yao et al. 2023, "Tree of Thoughts: Deliberate Problem Solving with LLMs"
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ThoughtNode:
    """A single node in the thought tree."""
    id: str
    content: str
    score: float = 0.0
    depth: int = 0
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)


@dataclass
class ToTResult:
    """Result of a Tree of Thoughts search."""
    best_path: list[str]       # Best reasoning chain
    best_score: float
    nodes_explored: int
    nodes_pruned: int
    llm_calls: int
    final_answer: str


class TreeOfThoughtPlanner:
    """Tree of Thoughts planner for high-accuracy reasoning.

    Usage:
        tot = TreeOfThoughtPlanner(model_gateway)
        result = await tot.solve(
            problem="Find the optimal architecture for a real-time analytics pipeline",
            branching=3,   # Generate 3 candidates per level
            depth=3,        # Explore 3 levels deep
            beam_width=2,   # Keep top 2 at each level
        )
    """

    def __init__(self, model_gateway: Any):
        self.gateway = model_gateway
        self._nodes: dict[str, ThoughtNode] = {}
        self._node_counter = 0
        self._llm_calls = 0
        self._pruned = 0

    async def solve(
        self,
        problem: str,
        branching: int = 3,
        depth: int = 3,
        beam_width: int = 2,
        temperature: float = 0.4,
    ) -> ToTResult:
        """Solve a problem using Tree of Thoughts.

        Args:
            problem: Problem description.
            branching: Number of candidates to generate at each node.
            depth: Maximum search depth.
            beam_width: Number of top nodes to keep at each level.
            temperature: Creativity for candidate generation (higher = more diverse).

        Returns:
            ToTResult with best path and metadata.
        """
        self._reset()

        # Root node
        root_id = self._make_node("ROOT: " + problem, score=1.0, depth=0)
        current_level = [root_id]

        for d in range(1, depth + 1):
            # Step 1: Generate candidates for each node in current level
            all_candidates = []
            for node_id in current_level:
                node = self._nodes[node_id]
                candidates = await self._generate_candidates(
                    problem, node.content, branching, temperature,
                )
                for cand_content in candidates:
                    cand_id = self._make_node(cand_content, parent_id=node_id, depth=d)
                    node.children.append(cand_id)
                    all_candidates.append(cand_id)

            if not all_candidates:
                break

            # Step 2: Evaluate all candidates
            scores = await self._evaluate_candidates(problem, all_candidates)
            for nid, score in scores.items():
                if nid in self._nodes:
                    self._nodes[nid].score = score

            # Step 3: Prune — keep only top beam_width
            scored = sorted(
                [(nid, scores.get(nid, 0)) for nid in all_candidates],
                key=lambda x: x[1], reverse=True,
            )
            keep = scored[:beam_width]
            self._pruned += len(scored) - len(keep)
            current_level = [nid for nid, _ in keep]

            logger.info(
                "ToT depth %d: generated=%d, kept=%d, pruned=%d, best_score=%.2f",
                d, len(all_candidates), len(keep), len(scored) - len(keep),
                keep[0][1] if keep else 0,
            )

        # Find best path
        best_leaf = max(
            [n for n in self._nodes.values() if n.depth == depth],
            key=lambda n: n.score,
            default=None,
        ) if current_level else None

        if not best_leaf and self._nodes:
            best_leaf = max(self._nodes.values(), key=lambda n: n.score)

        path = self._trace_path(best_leaf.id) if best_leaf else []
        answer = await self._synthesize_answer(problem, path) if path else "No solution found"

        return ToTResult(
            best_path=path,
            best_score=best_leaf.score if best_leaf else 0,
            nodes_explored=self._node_counter,
            nodes_pruned=self._pruned,
            llm_calls=self._llm_calls,
            final_answer=answer,
        )

    # ── Internal ──────────────────────────────────────────

    def _reset(self) -> None:
        self._nodes.clear()
        self._node_counter = 0
        self._llm_calls = 0
        self._pruned = 0

    def _make_node(self, content: str, score: float = 0, depth: int = 0, parent_id: str | None = None) -> str:
        self._node_counter += 1
        nid = f"node_{self._node_counter}"
        self._nodes[nid] = ThoughtNode(id=nid, content=content, score=score, depth=depth, parent_id=parent_id)
        return nid

    async def _generate_candidates(
        self, problem: str, context: str, n: int, temperature: float,
    ) -> list[str]:
        """Generate N diverse candidate thoughts."""
        prompt = (
            f"Problem: {problem}\n\n"
            f"Current reasoning state:\n{context}\n\n"
            f"Generate {n} DIFFERENT possible next steps. "
            f"Each should represent a distinct approach or angle. "
            f"Be creative — explore diverse solutions, not minor variations.\n\n"
            f"Output format:\n"
            + "\n".join(f"{i}. [your thought]" for i in range(1, n + 1))
        )

        if not self.gateway or not self.gateway.is_live():
            return [context]  # Fallback: single path

        self._llm_calls += 1
        try:
            raw = await self.gateway._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=temperature, max_tokens=1024,
            )
            # Parse numbered list
            import re
            candidates = re.findall(r'\d+\.\s*(.+)', raw)
            return candidates[:n] if candidates else [raw]
        except Exception:
            return [context]

    async def _evaluate_candidates(self, problem: str, node_ids: list[str]) -> dict[str, float]:
        """Evaluate candidate quality (batch for efficiency)."""
        if not self._nodes:
            return {}

        candidates_text = "\n\n".join(
            f"Candidate {i + 1}:\n{self._nodes[nid].content[:300]}"
            for i, nid in enumerate(node_ids) if nid in self._nodes
        )

        prompt = (
            f"Problem: {problem}\n\n"
            f"{candidates_text}\n\n"
            f"Rate each candidate 0.0-1.0 for: correctness, feasibility, and completeness. "
            f"Output JSON: {{'scores': [score1, score2, ...]}}"
        )

        if not self.gateway or not self.gateway.is_live():
            return {nid: 0.5 for nid in node_ids}

        self._llm_calls += 1
        try:
            raw = await self.gateway._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=200,
            )
            data = json.loads(raw) if raw.strip().startswith("{") else {"scores": [0.5] * len(node_ids)}
            scores = data.get("scores", [0.5] * len(node_ids))
            result = {}
            for i, nid in enumerate(node_ids):
                result[nid] = float(scores[i]) if i < len(scores) else 0.5
            return result
        except Exception:
            return {nid: 0.5 for nid in node_ids}

    def _trace_path(self, leaf_id: str) -> list[str]:
        """Trace from leaf to root."""
        path = []
        current = leaf_id
        while current and current in self._nodes:
            node = self._nodes[current]
            path.append(node.content)
            current = node.parent_id
        return list(reversed(path))

    async def _synthesize_answer(self, problem: str, path: list[str]) -> str:
        """Synthesize the best path into a final answer."""
        reasoning = "\n".join(f"Step {i + 1}: {step}" for i, step in enumerate(path))
        prompt = (
            f"Problem: {problem}\n\n"
            f"Reasoning chain:\n{reasoning}\n\n"
            f"Synthesize the above reasoning into a clear, concise final answer."
        )

        if not self.gateway or not self.gateway.is_live():
            return "\n".join(path)

        self._llm_calls += 1
        try:
            return await self.gateway._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1, max_tokens=1024,
            )
        except Exception:
            return "\n".join(path)
