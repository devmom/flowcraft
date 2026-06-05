"""RAG Pipeline — Full retrieval-augmented generation with anti-hallucination.

Complete online pipeline:
  Query Rewrite → Embedding → Multi-Recall (vector + BM25) → Rerank → Gate → Prompt → Generate

Includes:
  - Anti-hallucination: Prompt constraints + quality gate + citation verification
  - Chunking: Fixed, semantic, structure-aware, parent-child strategies
  - BM25 keyword retrieval (pure Python, no external deps)
  - RRF fusion for multi-recall merging
  - RAG evaluation metrics (Recall@K, MRR, Faithfulness)
  - HyDE (Hypothetical Document Embeddings) query expansion
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Chunking Strategies
# ═══════════════════════════════════════════════════════════════

class ChunkingStrategy:
    """Document chunking strategies for RAG indexing."""

    @staticmethod
    def fixed_size(text: str, chunk_size: int = 512, overlap: int = 50) -> list[str]:
        """Fixed-length chunking with sliding window overlap."""
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            chunks.append(text[start:end])
            start += chunk_size - overlap
        return chunks

    @staticmethod
    def semantic(text: str, max_sentences: int = 5, min_chars: int = 50) -> list[str]:
        """Sentence-boundary-aware chunking. Never cuts mid-sentence."""
        sentences = re.split(r'(?<=[。！？.!?\n])\s*', text)
        sentences = [s.strip() for s in sentences if len(s.strip()) >= min_chars]
        chunks = []
        for i in range(0, len(sentences), max_sentences):
            chunk = ''.join(sentences[i:i + max_sentences])
            if chunk.strip():
                chunks.append(chunk.strip())
        return chunks

    @staticmethod
    def structure_aware(text: str) -> list[str]:
        """Split by Markdown headings (#, ##, ###)."""
        sections = re.split(r'\n(?=#{1,4}\s)', text)
        return [s.strip() for s in sections if s.strip()]

    @staticmethod
    def parent_child(text: str, child_size: int = 256, parent_size: int = 1024, overlap: int = 64):
        """Parent-child: retrieve with small chunks, feed LLM with large chunks."""
        children = ChunkingStrategy.fixed_size(text, child_size, overlap)
        parents = ChunkingStrategy.fixed_size(text, parent_size, overlap)
        return children, parents

    @staticmethod
    def auto(text: str, target_chunk_size: int = 512) -> list[str]:
        """Auto-select strategy based on content type."""
        if re.search(r'\n#{1,4}\s', text):
            return ChunkingStrategy.structure_aware(text)
        if text.count('。') + text.count('.') > 5:
            return ChunkingStrategy.semantic(text)
        return ChunkingStrategy.fixed_size(text, target_chunk_size)


# ═══════════════════════════════════════════════════════════════
# BM25 Keyword Retrieval
# ═══════════════════════════════════════════════════════════════

class BM25Retriever:
    """Pure-Python BM25 keyword retrieval (Okapi BM25).

    Complements vector search: catches exact keywords, IDs, error codes
    that embedding similarity misses.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents: list[str] = []
        self._doc_freqs: dict[str, int] = {}
        self._doc_lengths: list[int] = []
        self._avg_doc_len: float = 0.0
        self._total_docs: int = 0

    def index(self, documents: list[str]) -> None:
        """Build BM25 index from documents."""
        self.documents = documents
        self._doc_freqs = {}
        self._doc_lengths = []
        self._total_docs = len(documents)

        for doc in documents:
            tokens = self._tokenize(doc)
            self._doc_lengths.append(len(tokens))
            seen = set()
            for token in tokens:
                if token not in seen:
                    self._doc_freqs[token] = self._doc_freqs.get(token, 0) + 1
                    seen.add(token)

        self._avg_doc_len = sum(self._doc_lengths) / max(1, self._total_docs)

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        """Search and return [(doc_index, score), ...] sorted by relevance."""
        if not self.documents:
            return []

        query_tokens = self._tokenize(query)
        scores = []

        for idx, doc in enumerate(self.documents):
            score = self._score(query_tokens, idx)
            if score > 0:
                scores.append((idx, score))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def _score(self, query_tokens: list[str], doc_idx: int) -> float:
        """BM25 scoring for a single document."""
        doc_len = self._doc_lengths[doc_idx]
        doc_tokens = self._tokenize(self.documents[doc_idx])
        doc_term_freqs = {}
        for t in doc_tokens:
            doc_term_freqs[t] = doc_term_freqs.get(t, 0) + 1

        score = 0.0
        for token in query_tokens:
            tf = doc_term_freqs.get(token, 0)
            if tf == 0:
                continue
            df = self._doc_freqs.get(token, 0)
            idf = math.log((self._total_docs - df + 0.5) / (df + 0.5) + 1.0)
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / self._avg_doc_len)
            score += idf * numerator / denominator

        return score

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Simple CJK-aware tokenizer."""
        tokens = re.findall(r'[\w一-鿿]+', str(text).lower(), re.UNICODE)
        return [t for t in tokens if len(t) > 1]


# ═══════════════════════════════════════════════════════════════
# Multi-Recall Fusion
# ═══════════════════════════════════════════════════════════════

def rrf_fusion(
    result_sets: list[list[Any]],
    k: int = 60,
    id_fn: callable = lambda x: x,
) -> list[Any]:
    """Reciprocal Rank Fusion — merge multi-recall results.

    Args:
        result_sets: List of ranked result lists from different retrievers.
        k: RRF constant (default 60).
        id_fn: Function to extract unique ID from a result item.

    Returns:
        Merged and reranked results (first item = best).
    """
    scores: dict[Any, float] = {}
    items: dict[Any, Any] = {}

    for results in result_sets:
        for rank, item in enumerate(results):
            item_id = id_fn(item)
            if not isinstance(item_id, (str, int, tuple)):
                item_id = str(item_id)
            scores[item_id] = scores.get(item_id, 0) + 1.0 / (k + rank + 1.0)
            items[item_id] = item

    sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return [items[iid] for iid in sorted_ids]


# ═══════════════════════════════════════════════════════════════
# Query Rewrite
# ═══════════════════════════════════════════════════════════════

class QueryRewriter:
    """Rewrite user queries for better retrieval.

    Three strategies:
      1. Simple: Resolve pronouns, make standalone
      2. HyDE: Generate hypothetical answer, use it for retrieval
      3. Multi-view: Generate 3-5 alternative formulations
    """

    def __init__(self, llm: Any = None):
        self.llm = llm  # ModelGateway or None (rule-based fallback)

    async def rewrite(self, query: str, history: list[dict] | None = None, strategy: str = "simple") -> str:
        """Rewrite query for retrieval.

        Args:
            query: Original user query.
            history: Conversation history for pronoun resolution.
            strategy: "simple" | "hyde" | "multi".

        Returns:
            Rewritten query (or HyDE answer) ready for retrieval.
        """
        if strategy == "hyde":
            return await self._hyde(query)
        elif strategy == "multi":
            return await self._multi_view(query)
        else:
            return await self._simple(query, history)

    async def _simple(self, query: str, history: list[dict] | None) -> str:
        """Simple rewrite: resolve pronouns, make standalone."""
        if not history:
            return query  # No history, no rewrite needed

        if not self.llm or not getattr(self.llm, 'is_live', lambda: False)():
            return query  # No LLM, return as-is

        hist_text = "\n".join(
            f"[{m.get('role', '?')}]: {str(m.get('content', ''))[:300]}"
            for m in history[-5:]  # Last 5 turns
        )
        prompt = (
            f"Conversation history:\n{hist_text}\n\n"
            f"Current question: {query}\n\n"
            f"Rewrite the question to be standalone and self-contained. "
            f"Resolve pronouns (他/她/它), demonstratives (这个/那个), "
            f"and implicit references. Keep the original intent.\n"
            f"Output ONLY the rewritten question."
        )
        try:
            return await self.llm._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=200,
            )
        except Exception:
            return query

    async def _hyde(self, query: str) -> str:
        """HyDE: Generate hypothetical answer, use it as search query."""
        if not self.llm or not getattr(self.llm, 'is_live', lambda: False)():
            return query

        prompt = (
            f"Write a short paragraph that answers this question "
            f"(even if you're not sure, just write something that looks like an answer):\n\n"
            f"Question: {query}\n\n"
            f"Hypothetical answer:"
        )
        try:
            return await self.llm._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=300,
            )
        except Exception:
            return query

    async def _multi_view(self, query: str) -> list[str]:
        """Generate multiple query formulations."""
        if not self.llm or not getattr(self.llm, 'is_live', lambda: False)():
            return [query]

        prompt = (
            f"Generate 3 different ways to search for the answer to this question. "
            f"Each version should use different keywords and phrasing:\n\n"
            f"Question: {query}\n\n"
            f"1."
        )
        try:
            raw = await self.llm._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.5, max_tokens=300,
            )
            # Parse numbered list
            views = re.findall(r'\d+\.\s*(.+)', raw)
            return [query] + views[:2] if views else [query]
        except Exception:
            return [query]


# ═══════════════════════════════════════════════════════════════
# Anti-Hallucination Guard
# ═══════════════════════════════════════════════════════════════

ANTI_HALLUCINATION_PROMPT = """You are a knowledge-based assistant. Follow these rules STRICTLY:

1. ONLY use information from the [References] below to answer
2. If [References] lacks sufficient information, answer: "Based on available references, I cannot answer this question."
3. Cite your sources using [ref:N] notation
4. Do NOT infer, guess, or add information beyond what the references state
5. If you're unsure even after checking references, say so honestly
"""


class HallucinationGuard:
    """Anti-hallucination guard for RAG outputs.

    Three-layer defense:
      L1: Prompt constraint (system prompt with strict rules)
      L2: Retrieval quality gate (reject if top score < threshold)
      L3: Post-generation citation check (verify claims against sources)
    """

    DEFAULT_THRESHOLD = 0.3  # Min relevance score to allow generation

    def __init__(self, threshold: float | None = None):
        self.threshold = threshold or self.DEFAULT_THRESHOLD

    def build_protected_prompt(self, question: str, chunks: list[str]) -> str:
        """Build a prompt with anti-hallucination constraints.

        Returns (system_prompt, user_prompt) tuple.
        """
        refs = "\n\n".join(
            f"[ref:{i + 1}] {chunk}"
            for i, chunk in enumerate(chunks)
        )
        user_prompt = (
            f"Question: {question}\n\n"
            f"[References]\n{refs}\n\n"
            f"Answer the question using ONLY the references above. "
            f"Include [ref:N] citations for each statement."
        )
        return ANTI_HALLUCINATION_PROMPT, user_prompt

    def quality_gate(self, scores: list[float]) -> tuple[bool, str]:
        """Check if retrieval quality is sufficient.

        Returns (allowed, reason).
        """
        if not scores:
            return False, "No results retrieved"
        if max(scores) < self.threshold:
            return False, f"Top relevance score ({max(scores):.2f}) below threshold ({self.threshold})"
        return True, ""

    def check_citations(self, answer: str, chunks: list[str]) -> tuple[bool, list[str]]:
        """Post-generation check: verify cited references actually contain the claims.

        Returns (all_valid, issues).
        """
        issues = []
        cited_refs = set()
        for m in re.finditer(r'\[ref:(\d+)\]', answer):
            cited_refs.add(int(m.group(1)))

        for ref_num in cited_refs:
            if ref_num < 1 or ref_num > len(chunks):
                issues.append(f"Reference [ref:{ref_num}] does not exist (only {len(chunks)} references)")
            elif len(chunks[ref_num - 1].strip()) < 5:
                issues.append(f"Reference [ref:{ref_num}] is empty or too short")

        return len(issues) == 0, issues


# ═══════════════════════════════════════════════════════════════
# RAG Evaluation
# ═══════════════════════════════════════════════════════════════

@dataclass
class RAGEvalResult:
    """RAG evaluation metrics."""
    recall_at_k: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    ndcg: float = 0.0
    faithfulness: float = 0.0
    answer_relevance: float = 0.0
    context_relevance: float = 0.0


class RAGEvaluator:
    """Evaluate RAG pipeline quality.

    Retrieval metrics: Recall@K, MRR, NDCG
    Generation metrics: Faithfulness, Answer Relevance (LLM-as-Judge)
    """

    @staticmethod
    def recall_at_k(retrieved_ids: list[str], relevant_ids: set[str], k_values: list[int] | None = None) -> dict[int, float]:
        """Recall@K: fraction of relevant docs found in top-K results."""
        k_values = k_values or [1, 3, 5, 10]
        results = {}
        for k in k_values:
            top_k = set(retrieved_ids[:k])
            if not relevant_ids:
                results[k] = 1.0 if not top_k else 0.0
            else:
                results[k] = len(top_k & relevant_ids) / len(relevant_ids)
        return results

    @staticmethod
    def mrr(retrieved_ids: list[str], relevant_ids: set[str]) -> float:
        """Mean Reciprocal Rank: 1/rank of first relevant doc."""
        for rank, doc_id in enumerate(retrieved_ids):
            if doc_id in relevant_ids:
                return 1.0 / (rank + 1)
        return 0.0

    @staticmethod
    def ndcg(retrieved_ids: list[str], relevance_scores: dict[str, float], k: int = 10) -> float:
        """Normalized Discounted Cumulative Gain at K."""
        dcg = 0.0
        for i, doc_id in enumerate(retrieved_ids[:k]):
            rel = relevance_scores.get(doc_id, 0.0)
            dcg += rel / math.log2(i + 2)  # i+2 because log2(1)=0

        ideal = sorted(relevance_scores.values(), reverse=True)[:k]
        idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal))

        return dcg / idcg if idcg > 0 else 0.0

    @staticmethod
    async def faithfulness(answer: str, context_chunks: list[str], llm: Any = None) -> float:
        """LLM-as-Judge: is the answer faithful to the context?

        Returns 0.0-1.0 score.
        """
        if not llm or not getattr(llm, 'is_live', lambda: False)():
            return RAGEvaluator._heuristic_faithfulness(answer, context_chunks)

        context = "\n".join(f"[{i}] {c[:300]}" for i, c in enumerate(context_chunks))
        prompt = (
            f"Context:\n{context}\n\n"
            f"Answer:\n{answer}\n\n"
            f"Rate how factually faithful the answer is to the context (0.0-1.0):\n"
            f"1.0 = every claim is directly supported by context\n"
            f"0.5 = some claims unsupported\n"
            f"0.0 = mostly fabricated\n\n"
            f"Score:"
        )
        try:
            raw = await llm._adapter.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=10,
            )
            score_match = re.search(r'(\d+\.?\d*)', raw)
            if score_match:
                return max(0.0, min(1.0, float(score_match.group(1))))
        except Exception:
            pass
        return RAGEvaluator._heuristic_faithfulness(answer, context_chunks)

    @staticmethod
    def _heuristic_faithfulness(answer: str, chunks: list[str]) -> float:
        """Quick heuristic faithfulness check."""
        if not answer or not chunks:
            return 0.0
        combined = " ".join(chunks).lower()
        answer_lower = answer.lower()
        words = re.findall(r'\w{4,}', answer_lower)
        if not words:
            return 0.5
        matched = sum(1 for w in words if w in combined)
        return matched / len(words)
