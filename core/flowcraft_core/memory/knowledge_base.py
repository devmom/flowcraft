"""Knowledge Base - Document indexing and semantic search.

Features:
    - Ingest documents (PDF, docx, xlsx, txt, md) into knowledge base
    - Simple keyword + TF-IDF indexing (no heavy ML deps)
    - Semantic search against knowledge sources
    - Source management (add, remove, refresh)
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flowcraft_core.storage.database import Database

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """Local knowledge base with document ingestion and keyword search.

    Uses lightweight TF-IDF style indexing without heavy ML dependencies.
    """

    def __init__(self, db: Database, storage_dir: Path) -> None:
        self.db = db
        self.storage_dir = Path(storage_dir)
        self.sources_dir = self.storage_dir / "sources"
        self.indexes_dir = self.storage_dir / "indexes"
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.indexes_dir.mkdir(parents=True, exist_ok=True)
        self._index_cache: dict[str, dict] = {}
        self._stopwords = set(
            "the a an is are was were be been being have has had do does did "
            "will would shall should may might must can could and or not but "
            "if then else when where why how who whom which what this that "
            "these those it its we they them he she his her their our my your "
            "in on at to for of with by from about as into through during "
            "before after above below between up down out off over under "
            "的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 "
            "会 着 没有 看 好 自己 这".split()
        )

    def ingest_file(self, path: Path, name: str | None = None) -> dict:
        """Ingest a document file into the knowledge base."""
        if not path.exists():
            return {"status": "error", "message": f"File not found: {path}"}

        source_name = name or path.stem
        content = self._read_file(path)

        if not content.strip():
            return {"status": "error", "message": "Empty or unreadable file"}

        # Copy source
        dest = self.sources_dir / f"{source_name}{path.suffix}"
        dest.write_bytes(path.read_bytes())

        # Build index
        tokens = self._tokenize(content)
        tf = Counter(tokens)
        total_terms = len(tokens)
        tfidf = {word: count / max(total_terms, 1) for word, count in tf.most_common(200)}

        index = {
            "source_name": source_name,
            "source_path": str(dest),
            "original_path": str(path),
            "file_type": path.suffix,
            "total_terms": total_terms,
            "unique_terms": len(tf),
            "tfidf": tfidf,
            "content_preview": content[:500],
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

        # Store index
        index_file = self.indexes_dir / f"{source_name}.json"
        index_file.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        self._index_cache[source_name] = index

        # Record in DB
        existing = self.db.fetch_one(
            "SELECT id FROM knowledge_sources WHERE name = ?", (source_name,))
        if existing:
            self.db.update("knowledge_sources", "id", dict(existing)["id"], {
                "status": "active", "indexed_at": index["ingested_at"],
            })
        else:
            self.db.insert_json("knowledge_sources", {
                "id": f"ks_{Path(source_name).stem}",
                "name": source_name,
                "source_type": path.suffix.lstrip("."),
                "path": str(dest),
                "status": "active",
                "indexed_at": index["ingested_at"],
                "created_at": index["ingested_at"],
            })

        logger.info("Ingested knowledge source: %s (%d terms)", source_name, total_terms)
        return {"status": "ok", "source": source_name, "terms": total_terms}

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Search knowledge base by keyword relevance."""
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        results = []
        for idx_file in self.indexes_dir.glob("*.json"):
            try:
                index = json.loads(idx_file.read_text(encoding="utf-8"))
                tfidf = index.get("tfidf", {})
                score = sum(tfidf.get(t, 0) for t in query_tokens)
                if score > 0:
                    results.append({
                        "source": index["source_name"],
                        "file_type": index["file_type"],
                        "score": round(score, 4),
                        "preview": index.get("content_preview", "")[:200],
                        "terms": index.get("total_terms", 0),
                    })
            except Exception:
                continue

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def get_context(self, query: str, max_chars: int = 2000) -> str:
        """Get relevant knowledge context as text for LLM injection."""
        results = self.search(query, top_k=3)
        if not results:
            return ""

        parts = ["## Knowledge Base Results"]
        for i, r in enumerate(results):
            parts.append(f"### Source {i+1}: {r['source']} (relevance: {r['score']:.2f})")
            parts.append(r["preview"])
        return "\n\n".join(parts)[:max_chars]

    def list_sources(self) -> list[dict]:
        rows = self.db.fetch_all(
            "SELECT * FROM knowledge_sources WHERE status='active' ORDER BY indexed_at DESC")
        return [dict(r) for r in rows]

    def remove_source(self, name: str) -> bool:
        row = self.db.fetch_one(
            "SELECT id FROM knowledge_sources WHERE name = ?", (name,))
        if not row:
            return False
        self.db.update("knowledge_sources", "id", dict(row)["id"],
                      {"status": "removed"})
        idx_file = self.indexes_dir / f"{name}.json"
        if idx_file.exists():
            idx_file.unlink()
        src_file = self.sources_dir / name
        if src_file.exists():
            src_file.unlink(missing_ok=True)
        self._index_cache.pop(name, None)
        return True

    def _read_file(self, path: Path) -> str:
        """Read file content with format detection."""
        suffix = path.suffix.lower()
        try:
            if suffix in (".txt", ".md", ".py", ".js", ".html", ".css", ".json", ".xml"):
                return path.read_text(encoding="utf-8", errors="replace")
            elif suffix == ".pdf":
                try:
                    import fitz
                    doc = fitz.open(str(path))
                    text = "\n".join(page.get_text() for page in doc)
                    doc.close()
                    return text
                except ImportError:
                    return f"[PDF requires PyMuPDF: {path.name}]"
            elif suffix == ".docx":
                try:
                    from docx import Document
                    doc = Document(str(path))
                    return "\n".join(p.text for p in doc.paragraphs)
                except ImportError:
                    return f"[DOCX requires python-docx: {path.name}]"
            else:
                return path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Failed to read %s: %s", path, exc)
            return ""

    def _tokenize(self, text: str) -> list[str]:
        """Simple tokenization with stopword removal."""
        text = text.lower()
        tokens = re.findall(r'[\w一-鿿]+', text, re.UNICODE)
        return [
            t for t in tokens
            if len(t) > 1 and t not in self._stopwords and not t.isdigit()
        ]

