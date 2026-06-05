"""Adaptive RAG — Self-RAG and CRAG (Corrective RAG).

Self-RAG: The model decides for itself whether to retrieve, evaluates its own
  outputs, and can trigger re-retrieval when quality is low.

CRAG (Corrective RAG): When retrieval quality is poor (low relevance scores),
  automatically fall back to web search instead of forcing a low-quality answer.

These patterns make RAG resilient — the system doesn't blindly trust retrieval
results, but actively evaluates and corrects its own process.

Reference:
  - Self-RAG: Asai et al. 2023
  - CRAG: Yan et al. 2024
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Self-RAG ───────────────────────────────────────────────

@dataclass
class SelfRAGResult:
    """Self-RAG output with self-assessment."""
    answer: str
    needs_retrieval: bool
    retrieval_queries: list[str] = field(default_factory=list)
    is_supported: bool = True      # Is the answer supported by retrieved docs?
    is_complete: bool = True       # Does it fully address the question?
    confidence: float = 0.5        # 0.0-1.0 self-assessed confidence
    reflection: str = ""


class SelfRAG:
    """Self-Reflective RAG — model decides when to retrieve and evaluates own output.

    The model generates special reflection tokens that guide the process:
      - <RETRIEVE>: Need to search for information
      - <ISSUP>: Output is supported by retrieved documents
      - <ISREL>: Output is relevant to the question
      - <ISCOMP>: Output is complete
    """

    def __init__(self, model_gateway: Any, retriever: Any = None):
        self.gateway = model_gateway
        self.retriever = retriever

    async def query(self, question: str, max_retrievals: int = 3) -> SelfRAGResult:
        """Self-RAG query with automatic retrieval decisions."""
        all_docs = []
        queries_used = []

        for _ in range(max_retrievals):
            # Step 1: Decide if retrieval needed
            needs_retrieval, query = await self._retrieval_decision(question, all_docs)
            if not needs_retrieval or not query:
                break

            # Step 2: Retrieve
            docs = await self._retrieve(query)
            if docs:
                all_docs.extend(docs)
                queries_used.append(query)
            else:
                break

        # Step 3: Generate with self-assessment
        answer, reflection = await self._generate_with_reflection(question, all_docs)
        is_supported = "<ISSUP>" not in reflection or "supported" in reflection.lower()
        is_complete = "<ISCOMP>" not in reflection or "complete" in reflection.lower()

        # Extract confidence
        confidence = 0.5
        if "confidence: high" in reflection.lower():
            confidence = 0.9
        elif "confidence: low" in reflection.lower():
            confidence = 0.3
        elif "confidence: medium" in reflection.lower():
            confidence = 0.6

        return SelfRAGResult(
            answer=answer,
            needs_retrieval=len(queries_used) > 0,
            retrieval_queries=queries_used,
            is_supported=is_supported,
            is_complete=is_complete,
            confidence=confidence,
            reflection=reflection,
        )

    async def _retrieval_decision(self, question: str, existing_docs: list) -> tuple[bool, str]:
        """Model decides whether to retrieve and what to search for."""
        if not existing_docs:
            return True, question  # Initial: always retrieve

        context = "\n".join(str(d)[:300] for d in existing_docs[-3:])
        prompt = (
            f"Question: {question}\n\n"
            f"Existing knowledge:\n{context}\n\n"
            f"Is the existing knowledge sufficient to answer? (yes/no)\n"
            f"If no, provide a refined search query.\n"
            f"If yes, respond just 'SUFFICIENT'.\n\n"
            f"Response:"
        )
        if not self.gateway or not self.gateway.is_live():
            return False, ""

        try:
            raw = await self.gateway._adapter.chat(
                [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=100,
            )
            if "SUFFICIENT" in raw.upper():
                return False, ""
            return True, raw.strip()
        except Exception:
            return False, ""

    async def _retrieve(self, query: str) -> list:
        """Retrieve documents for a query."""
        if not self.retriever:
            return []
        try:
            return await self.retriever.search(query)
        except Exception:
            return []

    async def _generate_with_reflection(self, question: str, docs: list) -> tuple[str, str]:
        """Generate answer with built-in reflection and confidence."""
        doc_text = "\n\n".join(
            f"[{i + 1}] {str(d)[:400]}" for i, d in enumerate(docs)
        ) if docs else "(No documents retrieved)"

        prompt = (
            f"Question: {question}\n\n"
            f"Documents:\n{doc_text}\n\n"
            f"Answer the question based on the documents. Then, reflect on your answer:\n"
            f"- Is the answer fully supported by the documents? (ISSUP: yes/no)\n"
            f"- Is the answer complete? (ISCOMP: yes/no)\n"
            f"- Your confidence level? (CONFIDENCE: high/medium/low)\n\n"
            f"Output format:\n"
            f"ANSWER: [your answer]\n"
            f"REFLECTION: [your reflection]"
        )

        if not self.gateway or not self.gateway.is_live():
            return f"Based on {len(docs)} documents (model not available).", "CONFIDENCE: low"

        try:
            raw = await self.gateway._adapter.chat(
                [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=1024,
            )
            # Split answer and reflection
            if "REFLECTION:" in raw:
                parts = raw.split("REFLECTION:", 1)
                return parts[0].replace("ANSWER:", "").strip(), parts[1].strip()
            return raw, "CONFIDENCE: medium"
        except Exception:
            return "Generation failed.", "CONFIDENCE: low"


# ── CRAG (Corrective RAG) ───────────────────────────────────

class CorrectiveRAG:
    """Corrective RAG — auto-fallback when retrieval quality is poor.

    Core idea: When vector search returns low-quality results, automatically
    fall back to web search instead of forcing LLM to answer from bad context.

    Quality assessment:
      - Score < 0.3: Skip local, go directly to web
      - Score 0.3-0.6: Use local + annotate as "moderate confidence"
      - Score > 0.6: Use local, high confidence
    """

    FALLBACK_THRESHOLD = 0.3
    MODERATE_THRESHOLD = 0.6

    def __init__(self, local_retriever: Any = None, web_searcher: Any = None):
        self.local = local_retriever
        self.web = web_searcher

    async def retrieve(self, query: str, top_k: int = 5) -> dict[str, Any]:
        """Retrieve with automatic quality-based fallback.

        Returns:
            {
                "results": [...],
                "source": "local" | "web" | "hybrid",
                "quality_score": 0.0-1.0,
                "fallback_triggered": bool,
            }
        """
        # Try local first
        local_results = []
        local_score = 0.0

        if self.local:
            try:
                local_results = await self.local.search(query, top_k=top_k)
                local_score = self._assess_quality(query, local_results)
            except Exception as exc:
                logger.warning("Local retrieval failed: %s", exc)

        # Decision: local good enough?
        if local_score >= self.MODERATE_THRESHOLD:
            return {
                "results": local_results,
                "source": "local",
                "quality_score": local_score,
                "fallback_triggered": False,
            }

        # Moderate: use local but mark confidence
        if local_score >= self.FALLBACK_THRESHOLD:
            return {
                "results": local_results,
                "source": "local",
                "quality_score": local_score,
                "fallback_triggered": False,
                "warning": "Moderate confidence — consider refining your query.",
            }

        # Fallback: try web search
        web_results = []
        web_score = 0.0
        if self.web:
            try:
                web_results = await self.web.search(query, top_k=top_k)
                web_score = self._assess_quality(query, web_results)
            except Exception as exc:
                logger.warning("Web fallback failed: %s", exc)

        # Merge what we have
        combined = local_results + web_results
        best_score = max(local_score, web_score)
        source = "hybrid" if (local_results and web_results) else ("web" if web_results else "local")

        return {
            "results": combined[:top_k],
            "source": source,
            "quality_score": best_score,
            "fallback_triggered": len(web_results) > 0,
        }

    def _assess_quality(self, query: str, results: list) -> float:
        """Heuristic quality assessment."""
        if not results:
            return 0.0

        query_terms = set(query.lower().split())
        scores = []
        for r in results:
            content = str(r).lower()
            matches = sum(1 for t in query_terms if t in content)
            term_ratio = matches / max(1, len(query_terms))
            length_score = min(1.0, len(content) / 200)  # Prefer chunks > 200 chars
            scores.append(term_ratio * 0.7 + length_score * 0.3)

        return sum(scores) / len(scores)
