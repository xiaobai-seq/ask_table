from __future__ import annotations

"""schema rerank 层。

HybridTableRetriever 会先用 BM25/向量拿到候选池，再调用这里的 Reranker
对候选表重排。默认启发式实现可离线运行，DashScope 实现可在生产环境增强相关性。
"""

import os
from typing import Protocol

from text2sql.config.domain_profile import DomainProfile
from text2sql.core.models import TableInfo
from text2sql.core.tokenization import overlap_ratio


class Reranker(Protocol):
    def rerank(self, query: str, tables: list[TableInfo]) -> list[tuple[TableInfo, float]]:
        ...


class HeuristicReranker:
    """基于 query 与表/字段文本重叠度的本地重排器。"""

    def __init__(self, domain_profile: DomainProfile | None = None) -> None:
        self.domain_profile = domain_profile

    def rerank(self, query: str, tables: list[TableInfo]) -> list[tuple[TableInfo, float]]:
        scored: list[tuple[TableInfo, float]] = []
        for table in tables:
            comment_score = (
                overlap_ratio(query, f"{table.name} {table.comment}", self.domain_profile) * 1.5
            )
            column_score = overlap_ratio(
                query,
                " ".join(column.document() for column in table.columns),
                self.domain_profile,
            )
            tag_score = overlap_ratio(query, " ".join(table.semantic_tags), self.domain_profile)
            scored.append((table, comment_score + column_score + tag_score))
        return sorted(scored, key=lambda item: item[1], reverse=True)


class DashScopeGTEReranker:
    """DashScope GTE rerank 适配器。"""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.getenv("DASHSCOPE_RERANK_MODEL", "gte-rerank-v2")
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")

    def rerank(self, query: str, tables: list[TableInfo]) -> list[tuple[TableInfo, float]]:
        try:  # pragma: no cover - optional network dependency
            import dashscope
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("dashscope is not installed") from exc

        if self.api_key:
            dashscope.api_key = self.api_key
        documents = [table.document() for table in tables]
        response = dashscope.TextReRank.call(model=self.model, query=query, documents=documents)
        if getattr(response, "status_code", 200) != 200:
            raise RuntimeError(f"DashScope rerank failed: {response}")
        results = response.get("output", {}).get("results", [])
        scored = [(tables[item["index"]], float(item["relevance_score"])) for item in results]
        return sorted(scored, key=lambda item: item[1], reverse=True)


def default_reranker(domain_profile: DomainProfile | None = None) -> Reranker:
    """有 API Key 时使用模型重排，否则使用启发式重排。"""

    if os.getenv("DASHSCOPE_API_KEY"):
        return DashScopeGTEReranker()
    return HeuristicReranker(domain_profile)
