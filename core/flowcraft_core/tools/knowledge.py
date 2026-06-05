"""Knowledge Base Tool — 本地文档搜索（基于关键词匹配）。"""

from __future__ import annotations

import re
from pathlib import Path

from flowcraft_core.domain.enums import RiskLevel
from flowcraft_core.domain.schemas import ToolIntent
from flowcraft_core.tools.base import Tool, ToolDefinition, observation_from_output


class KnowledgeSearchTool(Tool):
    """搜索本地知识库中的文档。"""

    def __init__(self, knowledge_dir: Path) -> None:
        self.knowledge_dir = knowledge_dir
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.definition = ToolDefinition(
            tool_name="knowledge.search",
            display_name="搜索知识库",
            description="在本地知识库中搜索相关文档内容。支持关键词和短语搜索。",
            category="knowledge",
            risk_level=RiskLevel.LOW,
            permissions=["tool:knowledge.search"],
            timeout_seconds=15,
        )

    async def execute(self, intent: ToolIntent):
        query = str(intent.input_payload.get("query", ""))
        max_results = int(intent.input_payload.get("max_results", 5))

        if not query:
            return observation_from_output(intent, "FAILED", "缺少查询参数", error="Missing query.")

        results = []
        keywords = query.lower().split()

        # 遍历知识库目录下的所有文本文件
        text_extensions = {".txt", ".md", ".py", ".json", ".yaml", ".yml", ".csv", ".log"}
        for file_path in self.knowledge_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() not in text_extensions:
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if not content.strip():
                continue

            # 计算匹配分数
            lower_content = content.lower()
            score = 0
            for kw in keywords:
                score += lower_content.count(kw)

            if score > 0:
                # 提取匹配片段
                snippets = []
                for kw in keywords:
                    for m in re.finditer(re.escape(kw), lower_content):
                        start = max(0, m.start() - 60)
                        end = min(len(content), m.end() + 60)
                        snippet = content[start:end].replace("\n", " ").strip()
                        if snippet not in snippets:
                            snippets.append(snippet)
                        if len(snippets) >= 3:
                            break

                results.append({
                    "file": str(file_path.relative_to(self.knowledge_dir)),
                    "path": str(file_path),
                    "score": score,
                    "snippets": snippets[:3],
                })

        # 按分数排序
        results.sort(key=lambda r: r["score"], reverse=True)
        top = results[:max_results]

        if not top:
            return observation_from_output(
                intent, "COMPLETED",
                f"未找到与 '{query}' 相关的结果。（知识库目录: {self.knowledge_dir}）",
                {"query": query, "results": [], "hint": "将文档放入知识库目录后重试"},
            )

        summary = f"找到 {len(top)} 个相关文档:"
        for r in top:
            summary += f"\n- {r['file']} (匹配度: {r['score']})"

        return observation_from_output(
            intent, "COMPLETED", summary,
            {"query": query, "results": top},
        )
