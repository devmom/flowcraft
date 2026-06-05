"""Agentic RAG — RAG as an Agent loop, not a single pipeline.

Traditional RAG: query → retrieve → generate (one shot)
Agentic RAG:   query → retrieve → evaluate → (insufficient?) → re-query → ... → generate

This turns retrieval into a multi-turn Agent decision loop:
  - Agent decides WHEN to retrieve vs. answer from memory
  - Agent evaluates retrieval QUALITY and decides whether to re-search
  - Agent can use MULTIPLE tools (web search, knowledge base, database)
  - Agent dynamically adjusts query strategy based on intermediate results

Perfect fit for FlowCraft — RAG becomes just another tool in the Agent's toolkit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RetrievalDecision:
    """Agent's decision about retrieval."""
    should_retrieve: bool
    query: str = ""
    strategy: str = "vector"  # vector, bm25, hybrid, web
    reason: str = ""


@dataclass
class AgenticRAGResult:
    """Result of an Agentic RAG session."""
    answer: str
    retrieval_rounds: int
    sources: list[dict] = field(default_factory=list)
    decisions: list[RetrievalDecision] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)


class AgenticRAG:
    """RAG as an Agent loop.

    The Agent iteratively:
      1. Decides if retrieval is needed (or if it can answer from context)
      2. Chooses retrieval strategy (vector, BM25, web, hybrid)
      3. Evaluates retrieved quality
      4. Decides to answer or re-retrieve with refined query

    Usage:
        rag = AgenticRAG(model_gateway, vector_store, bm25, web_search_tool)
        result = await rag.query("What are the latest trends in AI agents?")
    """

    MAX_RETRIEVAL_ROUNDS = 5
    MIN_RELEVANCE_THRESHOLD = 0.4  # Below this, trigger re-retrieval

    def __init__(
        self,
        model_gateway: Any,
        vector_store: Any = None,
        bm25_retriever: Any = None,
        web_search_tool: Any = None,
    ):
        self.gateway = model_gateway
        self.vector_store = vector_store
        self.bm25 = bm25_retriever
        self.web_search = web_search_tool

    async def query(self, question: str, conversation_history: list[dict] | None = None) -> AgenticRAGResult:
        """Main entry point: agentic RAG query loop."""
        sources = []
        decisions = []
        tool_calls = []
        accumulated_context = ""

        for round_num in range(1, self.MAX_RETRIEVAL_ROUNDS + 1):
            # Step 1: Agent decides what to do
            decision = await self._decide(question, accumulated_context, sources, round_num)
            decisions.append(decision)

            if not decision.should_retrieve:
                # Agent thinks it has enough — generate answer
                break

            # Step 2: Execute retrieval
            results = await self._retrieve(decision)
            if results:
                sources.extend(results)
                accumulated_context += "\n\n".join(
                    f"[source {i + 1}] {r.get('content', str(r))[:500]}"
                    for i, r in enumerate(results)
                )
            tool_calls.append(decision.strategy)

            # Step 3: Evaluate quality
            if results:
                quality = await self._evaluate_quality(question, results)
                if quality >= self.MIN_RELEVANCE_THRESHOLD and round_num >= 2:
                    break  # Good enough, stop retrieving

        # Step 4: Generate final answer
        answer = await self._generate(question, accumulated_context, sources)

        return AgenticRAGResult(
            answer=answer,
            retrieval_rounds=len(decisions),
            sources=sources,
            decisions=decisions,
            tool_calls=tool_calls,
        )

    # ── Internal ──────────────────────────────────────────

    async def _decide(
        self, question: str, context: str, sources: list, round_num: int,
    ) -> RetrievalDecision:
        """Agent decides: retrieve more, or answer now?"""
        if round_num == 1 and not context:
            # First round with no context — always retrieve
            return RetrievalDecision(should_retrieve=True, query=question, strategy="hybrid", reason="Initial retrieval")

        if not self.gateway or not self.gateway.is_live():
            return RetrievalDecision(should_retrieve=False, reason="No LLM available")

        prompt = (
            f"Question: {question}\n"
            f"Retrieved context so far ({len(sources)} sources):\n{context[:1500]}\n\n"
            f"Decide:\n"
            f"1. Is the current context sufficient to answer? (yes/no)\n"
            f"2. If no, what should we search for next? (refined query)\n"
            f"3. Which strategy: 'web' (external), 'hybrid' (local), or 'done'?\n\n"
            f"Respond in JSON: {{'sufficient': bool, 'query': str, 'strategy': str}}"
        )
        try:
            raw = await self.gateway._adapter.chat(
                [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=200,
            )
            data = self._parse_json(raw)
            if data.get("sufficient") or data.get("strategy") == "done":
                return RetrievalDecision(should_retrieve=False, reason="Context sufficient")
            return RetrievalDecision(
                should_retrieve=True,
                query=data.get("query", question),
                strategy=data.get("strategy", "hybrid"),
                reason="Need more information",
            )
        except Exception:
            return RetrievalDecision(should_retrieve=False, reason="Decision failed, answering with what we have")

    async def _retrieve(self, decision: RetrievalDecision) -> list[dict]:
        """Execute retrieval based on agent's strategy choice."""
        results = []
        if decision.strategy in ("vector", "hybrid") and self.vector_store:
            results.extend(await self._vector_search(decision.query))
        if decision.strategy in ("bm25", "hybrid") and self.bm25:
            results.extend(await self._bm25_search(decision.query))
        if decision.strategy == "web" and self.web_search:
            results.extend(await self._web_search(decision.query))
        return results[:10]  # Cap results

    async def _vector_search(self, query: str) -> list[dict]:
        try:
            return await self.vector_store.search(query, top_k=5)
        except Exception:
            return []

    async def _bm25_search(self, query: str) -> list[dict]:
        try:
            ranked = self.bm25.search(query, top_k=5)
            return [{"content": self.bm25.documents[i], "score": s, "source": "bm25"} for i, s in ranked]
        except Exception:
            return []

    async def _web_search(self, query: str) -> list[dict]:
        try:
            result = await self.web_search.execute(query=query)
            return [{"content": str(result), "source": "web"}]
        except Exception:
            return []

    async def _evaluate_quality(self, question: str, results: list[dict]) -> float:
        """Quick quality check on retrieved results."""
        if not results:
            return 0.0
        # Heuristic: check if results contain question keywords
        keywords = set(question.lower().split())
        matches = sum(
            1 for r in results
            if any(kw in str(r.get("content", "")).lower() for kw in keywords)
        )
        return min(1.0, matches / max(1, len(results)))

    async def _generate(self, question: str, context: str, sources: list) -> str:
        """Generate final answer from accumulated context."""
        if not context:
            return "No relevant information found to answer this question."

        prompt = (
            f"Question: {question}\n\n"
            f"References:\n{context[:3000]}\n\n"
            f"Answer the question based on the references above. "
            f"Cite sources where applicable. "
            f"If the references are insufficient, acknowledge the gap."
        )
        if not self.gateway or not self.gateway.is_live():
            return f"Based on {len(sources)} retrieved sources: {context[:500]}"

        try:
            return await self.gateway._adapter.chat(
                [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=1024,
            )
        except Exception:
            return f"Retrieved {len(sources)} sources but failed to generate answer."

    @staticmethod
    def _parse_json(raw: str) -> dict:
        import re
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {}
