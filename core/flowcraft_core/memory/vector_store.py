"""Vector Memory Store — Chroma-powered semantic retrieval with TF-IDF fallback.

Primary:  ChromaDB with all-MiniLM-L6-v2 embeddings (semantic search)
Fallback: TF-IDF keyword vectors (zero-dependency, deterministic)

Architecture:
    get_vector_store() → auto-detects Chroma availability
        ├── Chroma available → ChromaVectorStore (embeddings + cosine similarity)
        └── Chroma unavailable → KeywordVectorStore (TF-IDF + BM25)
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Tokenizer ──────────────────────────────────────────────────


# Common Chinese stopwords + English stopwords
_STOPWORDS: set[str] = {
    # Chinese
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些",
    "什么", "怎么", "如何", "为什么", "可以", "这个", "那个", "还是",
    "已经", "因为", "所以", "但是", "如果", "虽然", "而且", "然后",
    "之", "与", "或", "及", "并", "从", "以", "对", "把", "向", "被",
    "让", "给", "为", "关于", "通过", "根据", "按照",
    # English
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "under", "again",
    "further", "then", "once", "here", "there", "when", "where", "why",
    "how", "all", "both", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than",
    "too", "very", "just", "now", "also", "if", "or", "and", "but",
    "it", "its", "this", "that", "these", "those", "which", "who",
    "whom", "what", "my", "your", "his", "her", "our", "their",
    "me", "him", "us", "them", "we", "he", "she", "they",
}

# Regex for Chinese word segmentation (character bigrams as fallback)
_CHINESE_CHAR = re.compile(r"[一-鿿]+")
_ALPHANUM = re.compile(r"[a-zA-Z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Tokenize text into keyword list. Uses character bigrams for Chinese,
    whole words for English/alphanumeric. Filters stopwords and short tokens."""
    tokens: list[str] = []

    # Extract Chinese text segments → character bigrams
    for match in _CHINESE_CHAR.finditer(text):
        segment = match.group()
        if len(segment) >= 4:
            # Overlapping bigrams
            for i in range(len(segment) - 1):
                bigram = segment[i:i + 2]
                if bigram not in _STOPWORDS:
                    tokens.append(bigram)
        tokens.append(segment)  # Also keep the full segment

    # Extract alphanumeric tokens
    for match in _ALPHANUM.finditer(text):
        word = match.group().lower()
        if len(word) >= 2 and word not in _STOPWORDS:
            tokens.append(word)

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        if t not in seen and len(t) >= 2:
            seen.add(t)
            result.append(t)
    return result


# ── Memory Entry with Embedding ─────────────────────────────────


@dataclass
class IndexedMemory:
    """A memory entry with its keyword vector for retrieval."""
    memory_id: str
    memory_type: str
    scope_id: str
    title: str
    content: str
    created_at: str
    confidence: float = 1.0
    keywords: list[str] = field(default_factory=list)
    # Decay factor: starts at 1.0, decreases over time
    decay_factor: float = 1.0

    @property
    def effective_score(self) -> float:
        return self.confidence * self.decay_factor


# ── Vector Store ────────────────────────────────────────────────


class KeywordVectorStore:
    """TF-IDF inspired keyword vector store for memory retrieval.

    No external dependencies. Uses:
    - Inverted index: keyword → [(memory_id, tf_score)]
    - IDF: log(total_docs / doc_frequency)
    - BM25-like scoring for queries
    - Time decay for result ranking
    """

    def __init__(self) -> None:
        # Inverted index: keyword → list of (memory_id, term_frequency_in_doc)
        self._inverted_index: dict[str, list[tuple[str, float]]] = defaultdict(list)
        # All indexed memories
        self._memories: dict[str, IndexedMemory] = {}
        # Document frequency: keyword → number of docs containing it
        self._doc_freq: dict[str, int] = defaultdict(int)
        self._total_docs: int = 0
        # Decay config
        self.decay_half_life_hours: float = 24.0  # memories lose half relevance after 24h
        self.min_decay_factor: float = 0.1
        self.relevance_threshold: float = 0.05  # below this, don't return

    # ── CRUD ─────────────────────────────────────────────────

    def index(self, memory: IndexedMemory) -> None:
        """Add or update a memory in the index."""
        # Remove old entry if exists
        self.remove(memory.memory_id)

        # Tokenize
        text = f"{memory.title} {memory.content}"
        tokens = tokenize(text)
        memory.keywords = tokens

        # Calculate term frequencies for this document
        tf: dict[str, float] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0.0) + 1.0
        # Normalize by doc length
        doc_len = max(1, len(tokens))
        for t in tf:
            tf[t] /= doc_len

        # Update inverted index
        for t, score in tf.items():
            self._inverted_index[t].append((memory.memory_id, score))
            self._doc_freq[t] += 1

        self._memories[memory.memory_id] = memory
        self._total_docs += 1

        logger.debug("Indexed memory %s with %d keywords", memory.memory_id[:12], len(tokens))

    def remove(self, memory_id: str) -> None:
        """Remove a memory from the index."""
        mem = self._memories.pop(memory_id, None)
        if not mem:
            return
        for kw in mem.keywords:
            entries = self._inverted_index.get(kw, [])
            self._inverted_index[kw] = [(mid, s) for mid, s in entries if mid != memory_id]
            if not self._inverted_index[kw]:
                del self._inverted_index[kw]
            self._doc_freq[kw] = max(0, self._doc_freq.get(kw, 1) - 1)
        self._total_docs = max(0, self._total_docs - 1)

    # ── Search ───────────────────────────────────────────────

    def search(
        self,
        query: str,
        scope_id: str | None = None,
        memory_type: str | None = None,
        top_k: int = 20,
        min_score: float | None = None,
    ) -> list[IndexedMemory]:
        """Search for memories relevant to query.

        Args:
            query: natural language query
            scope_id: optional filter by scope (session_id)
            memory_type: optional filter by type
            top_k: max results
            min_score: override relevance threshold

        Returns:
            list of IndexedMemory, ranked by relevance (highest first)
        """
        threshold = min_score if min_score is not None else self.relevance_threshold

        # Tokenize query
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        # BM25-like scoring
        scores: dict[str, float] = defaultdict(float)
        avg_dl = 1.0  # simplified: assume average doc length = 1

        for qt in query_tokens:
            postings = self._inverted_index.get(qt, [])
            df = self._doc_freq.get(qt, 0)
            if df == 0:
                continue
            # IDF
            idf = math.log((self._total_docs - df + 0.5) / (df + 0.5) + 1.0)

            for mem_id, tf_score in postings:
                # BM25 term score (simplified, k1=1.2, b=0.75)
                k1, b = 1.2, 0.75
                dl = avg_dl  # simplified
                numerator = tf_score * (k1 + 1.0)
                denominator = tf_score + k1 * (1.0 - b + b * dl / avg_dl)
                term_score = idf * numerator / max(denominator, 0.001)
                scores[mem_id] += term_score

        # Normalize by query length
        if scores:
            max_score = max(scores.values())
            if max_score > 0:
                for mid in scores:
                    scores[mid] /= max_score

        # Apply decay and filters, collect results
        results: list[tuple[float, IndexedMemory]] = []
        now = datetime.now(timezone.utc)

        for mem_id, raw_score in scores.items():
            mem = self._memories.get(mem_id)
            if not mem:
                continue
            if scope_id and mem.scope_id != scope_id:
                continue
            if memory_type and mem.memory_type != memory_type:
                continue

            # Apply time decay
            self._apply_decay(mem, now)
            effective = raw_score * mem.effective_score

            if effective >= threshold:
                results.append((effective, mem))

        # Sort by score descending
        results.sort(key=lambda x: x[0], reverse=True)
        return [mem for _, mem in results[:top_k]]

    # ── Decay ────────────────────────────────────────────────

    def _apply_decay(self, memory: IndexedMemory, now: datetime | None = None) -> None:
        """Apply exponential time decay to a memory's decay_factor."""
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            created = datetime.fromisoformat(memory.created_at.replace("Z", "+00:00"))
            age_hours = (now - created).total_seconds() / 3600.0
            if age_hours <= 0:
                memory.decay_factor = 1.0
            else:
                # Exponential decay: factor = 2^(-age / half_life)
                memory.decay_factor = max(
                    self.min_decay_factor,
                    math.pow(2.0, -age_hours / self.decay_half_life_hours)
                )
        except (ValueError, TypeError):
            memory.decay_factor = 1.0

    def apply_decay_all(self) -> int:
        """Apply decay to all indexed memories. Returns count of pruned memories."""
        now = datetime.now(timezone.utc)
        to_remove: list[str] = []
        for mem_id, mem in self._memories.items():
            self._apply_decay(mem, now)
            if mem.decay_factor <= self.min_decay_factor * 0.5:
                to_remove.append(mem_id)
        for mid in to_remove:
            self.remove(mid)
        logger.debug("Decay applied: %d memories pruned", len(to_remove))
        return len(to_remove)

    # ── Stats ────────────────────────────────────────────────

    @property
    def total_indexed(self) -> int:
        return len(self._memories)

    @property
    def total_keywords(self) -> int:
        return len(self._inverted_index)


# ── Chroma Vector Store ────────────────────────────────────────


# Cache for Chroma import check
_chroma_available: bool | None = None


def _is_chroma_available() -> bool:
    """Check if chromadb is installed and importable."""
    global _chroma_available
    if _chroma_available is not None:
        return _chroma_available
    try:
        import chromadb  # noqa: F401
        _chroma_available = True
    except ImportError:
        _chroma_available = False
        logger.info("chromadb not installed — using TF-IDF keyword vector store")
    return _chroma_available


def _get_chroma_persist_dir() -> str:
    """Get the ChromaDB persistence directory under FlowCraft data dir."""
    data_dir = os.environ.get("FLOWCRAFT_DATA_DIR", "")
    if not data_dir:
        data_dir = os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~/.local/share")),
            "FlowCraft",
        )
    chroma_dir = os.path.join(data_dir, "data", "chroma")
    os.makedirs(chroma_dir, exist_ok=True)
    return chroma_dir


class ChromaVectorStore:
    """ChromaDB-powered vector store for semantic memory retrieval.

    Uses the all-MiniLM-L6-v2 embedding model (via Chroma's built-in
    sentence-transformers integration) to encode memory content into
    384-dimensional vectors.  Search is cosine-similarity based.

    Public API is intentionally identical to KeywordVectorStore so the
    two are drop-in interchangeable.
    """

    # Chroma collection name — single collection stores all memory types;
    # filtering by scope_id / memory_type is done via Chroma metadata filters.
    COLLECTION_NAME = "flowcraft_memories"

    # Decay config (mirrors KeywordVectorStore)
    decay_half_life_hours: float = 24.0
    min_decay_factor: float = 0.1
    relevance_threshold: float = 0.05

    def __init__(self, persist_directory: str | None = None) -> None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        persist_dir = persist_directory or _get_chroma_persist_dir()
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        # Use Chroma's default embedding function (all-MiniLM-L6-v2)
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        self._memories: dict[str, IndexedMemory] = {}
        logger.info(
            "ChromaVectorStore initialized at %s (%d docs)",
            persist_dir, self._collection.count(),
        )

    # ── CRUD ─────────────────────────────────────────────────

    def index(self, memory: IndexedMemory) -> None:
        """Add or update a memory in the Chroma collection."""
        doc_id = memory.memory_id
        text = f"{memory.title}\n{memory.content}"

        # Build metadata for filtering
        metadata = {
            "memory_type": memory.memory_type,
            "scope_id": memory.scope_id,
            "title": memory.title[:500],
            "created_at": memory.created_at,
            "confidence": memory.confidence,
        }

        # Chroma upsert: if doc_id exists it updates, otherwise inserts
        self._collection.upsert(
            ids=[doc_id],
            documents=[text],
            metadatas=[metadata],
        )
        self._memories[doc_id] = memory
        logger.debug("Chroma indexed memory %s", doc_id[:12])

    def remove(self, memory_id: str) -> None:
        """Remove a memory from the Chroma collection."""
        self._memories.pop(memory_id, None)
        try:
            self._collection.delete(ids=[memory_id])
        except Exception:
            logger.debug("Chroma delete failed for %s (may not exist)", memory_id[:12])

    # ── Search ───────────────────────────────────────────────

    def search(
        self,
        query: str,
        scope_id: str | None = None,
        memory_type: str | None = None,
        top_k: int = 20,
        min_score: float | None = None,
    ) -> list[IndexedMemory]:
        """Search Chroma for memories semantically similar to query.

        Args:
            query: natural language query
            scope_id: optional filter by scope (session_id)
            memory_type: optional filter by type
            top_k: max results
            min_score: override relevance threshold (cosine distance → similarity)

        Returns:
            list of IndexedMemory, ranked by relevance (highest first)
        """
        threshold = min_score if min_score is not None else self.relevance_threshold

        # Build Chroma where-filter
        where_filter: dict[str, Any] | None = None
        conditions: list[dict[str, Any]] = []
        if scope_id:
            conditions.append({"scope_id": scope_id})
        if memory_type:
            conditions.append({"memory_type": memory_type})
        if len(conditions) == 1:
            where_filter = conditions[0]
        elif len(conditions) >= 2:
            where_filter = {"$and": conditions}

        try:
            chroma_results = self._collection.query(
                query_texts=[query],
                n_results=min(top_k * 2, 200),  # fetch extra for post-filtering
                where=where_filter,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            logger.warning("Chroma query failed: %s", exc)
            return []

        if not chroma_results or not chroma_results.get("ids") or not chroma_results["ids"][0]:
            return []

        ids = chroma_results["ids"][0]
        distances = chroma_results.get("distances", [[1.0] * len(ids)])[0]

        # Convert cosine distance → similarity (1 - distance), then apply decay
        results: list[tuple[float, IndexedMemory]] = []
        now = datetime.now(timezone.utc)

        for doc_id, distance in zip(ids, distances):
            mem = self._memories.get(doc_id)
            if not mem:
                # Memory is in Chroma but not in local cache — create lightweight wrapper
                metadatas = chroma_results.get("metadatas", [[]])[0]
                idx = ids.index(doc_id)
                meta = metadatas[idx] if idx < len(metadatas) else {}
                mem = IndexedMemory(
                    memory_id=doc_id,
                    memory_type=meta.get("memory_type", "SESSION"),
                    scope_id=meta.get("scope_id", ""),
                    title=meta.get("title", ""),
                    content="",
                    created_at=meta.get("created_at", ""),
                    confidence=meta.get("confidence", 1.0),
                )

            # Cosine similarity = 1 - cosine_distance (Chroma returns distances)
            cosine_sim = max(0.0, 1.0 - distance)
            self._apply_decay(mem, now)
            effective = cosine_sim * mem.effective_score

            if effective >= threshold:
                results.append((effective, mem))

        results.sort(key=lambda x: x[0], reverse=True)
        return [mem for _, mem in results[:top_k]]

    # ── Decay ────────────────────────────────────────────────

    def _apply_decay(self, memory: IndexedMemory, now: datetime | None = None) -> None:
        """Apply exponential time decay (same logic as KeywordVectorStore)."""
        if now is None:
            now = datetime.now(timezone.utc)
        try:
            created = datetime.fromisoformat(memory.created_at.replace("Z", "+00:00"))
            age_hours = (now - created).total_seconds() / 3600.0
            if age_hours <= 0:
                memory.decay_factor = 1.0
            else:
                memory.decay_factor = max(
                    self.min_decay_factor,
                    math.pow(2.0, -age_hours / self.decay_half_life_hours),
                )
        except (ValueError, TypeError):
            memory.decay_factor = 1.0

    def apply_decay_all(self) -> int:
        """Apply decay to all cached memories. Returns count of pruned."""
        now = datetime.now(timezone.utc)
        to_remove: list[str] = []
        for mem_id, mem in self._memories.items():
            self._apply_decay(mem, now)
            if mem.decay_factor <= self.min_decay_factor * 0.5:
                to_remove.append(mem_id)
        for mid in to_remove:
            self.remove(mid)
        logger.debug("Chroma decay applied: %d memories pruned", len(to_remove))
        return len(to_remove)

    # ── Stats ────────────────────────────────────────────────

    @property
    def total_indexed(self) -> int:
        return self._collection.count()

    @property
    def total_keywords(self) -> int:
        """Chroma has no keyword count — return collection size as proxy."""
        return self._collection.count()


# ── Singleton ──────────────────────────────────────────────────

_vector_store: KeywordVectorStore | ChromaVectorStore | None = None
_store_type: str = ""


def get_vector_store() -> KeywordVectorStore | ChromaVectorStore:
    """Get the vector store singleton.

    Auto-detects Chroma availability:
    - Chroma installed → ChromaVectorStore (semantic search)
    - Chroma not installed → KeywordVectorStore (TF-IDF fallback)
    """
    global _vector_store, _store_type
    if _vector_store is not None:
        return _vector_store

    if _is_chroma_available():
        try:
            _vector_store = ChromaVectorStore()
            _store_type = "chroma"
            logger.info("Vector store: ChromaDB (semantic embeddings)")
            return _vector_store
        except Exception as exc:
            logger.warning("Chroma init failed, falling back to TF-IDF: %s", exc)

    _vector_store = KeywordVectorStore()
    _store_type = "tfidf"
    logger.info("Vector store: TF-IDF (keyword vectors)")
    return _vector_store


def get_store_type() -> str:
    """Return the active vector store type: 'chroma' or 'tfidf'."""
    global _store_type
    if not _store_type:
        get_vector_store()
    return _store_type
