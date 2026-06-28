from __future__ import annotations

"""schema 检索层。

用户自然语言不会直接进入 SQL 生成，而是先在表 schema 上做混合检索：
BM25 负责关键词精确匹配，向量召回负责语义相似，RRF 合并两路排序，
再叠加业务字段启发式和 reranker 得到最终候选表。
"""

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from text2sql.core.embeddings import EmbeddingProvider, cosine, default_embedding_provider
from text2sql.core.models import RetrievalHit, TableInfo
from text2sql.core.rerank import Reranker, default_reranker
from text2sql.core.tokenization import overlap_ratio, tokenize

if TYPE_CHECKING:  # pragma: no cover - 仅类型注解使用，避免运行期可选依赖耦合
    from text2sql.accuracy.schema_semantics import SchemaSemantics


def schema_fingerprint(tables: list[TableInfo]) -> str:
    """根据 schema 内容生成缓存指纹，表结构变化时自动重建向量索引。"""

    payload = [
        {
            "name": table.name,
            "comment": table.comment,
            "columns": [
                {
                    "name": column.name,
                    "type": column.data_type,
                    "comment": column.comment,
                    "pk": column.primary_key,
                }
                for column in table.columns
            ],
            "foreign_keys": [fk.label() for fk in table.foreign_keys],
        }
        for table in sorted(tables, key=lambda item: item.name)
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.md5(encoded).hexdigest()


class BM25Index:
    """轻量 BM25 实现，用于没有外部搜索引擎时的 schema 关键词召回。"""

    def __init__(self, documents: list[str]) -> None:
        self.documents = documents
        self.tokens = [tokenize(document) for document in documents]
        self.doc_count = len(documents)
        self.doc_lengths = [len(tokens) for tokens in self.tokens]
        self.avg_doc_length = sum(self.doc_lengths) / max(1, self.doc_count)
        self.doc_freq: Counter[str] = Counter()
        for tokens in self.tokens:
            self.doc_freq.update(set(tokens))

    def search(self, query: str, limit: int) -> list[tuple[int, float]]:
        # 返回 document index 和 BM25 分数；调用方再把 index 映射回 TableInfo。
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        scores: list[tuple[int, float]] = []
        query_counts = Counter(query_tokens)
        k1 = 1.5
        b = 0.75
        for index, doc_tokens in enumerate(self.tokens):
            if not doc_tokens:
                continue
            tf = Counter(doc_tokens)
            score = 0.0
            for token, qf in query_counts.items():
                if token not in tf:
                    continue
                df = self.doc_freq[token]
                idf = math.log(1 + (self.doc_count - df + 0.5) / (df + 0.5))
                denom = tf[token] + k1 * (
                    1 - b + b * self.doc_lengths[index] / max(1e-9, self.avg_doc_length)
                )
                score += idf * ((tf[token] * (k1 + 1)) / denom) * qf
            if score > 0:
                scores.append((index, score))
        return sorted(scores, key=lambda item: item[1], reverse=True)[:limit]


class PersistentVectorIndex:
    """持久化 schema 向量，避免每次启动都重新 embedding 全库 schema。"""

    def __init__(
        self,
        tables: list[TableInfo],
        embedding_provider: EmbeddingProvider | None = None,
        cache_dir: str | Path = ".text2sql_cache",
        documents: list[str] | None = None,
    ) -> None:
        self.tables = tables
        # documents 允许调用方注入「已叠加语义增强」的检索文档；缺省回退到表自身文档。
        self.documents = documents if documents is not None else [table.document() for table in tables]
        self.embedding_provider = embedding_provider or default_embedding_provider()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.cache_dir / "schema_vectors.json"
        # fingerprint 同时绑定结构与文档内容：语义元数据变化也会触发向量重建，避免命中陈旧缓存。
        self.fingerprint = self._compute_fingerprint(tables, self.documents)
        self.vectors: list[list[float]] = []
        self._load_or_rebuild()

    @staticmethod
    def _compute_fingerprint(tables: list[TableInfo], documents: list[str]) -> str:
        structural = schema_fingerprint(tables)
        doc_hash = hashlib.md5("\u0001".join(documents).encode("utf-8")).hexdigest()
        return f"{structural}:{doc_hash}"

    def _load_or_rebuild(self) -> None:
        # 缓存只在 fingerprint 一致时复用；任何表/字段/外键变化都会触发重建。
        if self.cache_path.exists():
            try:
                payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if payload.get("fingerprint") == self.fingerprint:
                    self.vectors = payload["vectors"]
                    return
            except Exception:
                pass

        self.vectors = self.embedding_provider.batch_embed(self.documents)
        payload = {
            "fingerprint": self.fingerprint,
            "tables": [table.name for table in self.tables],
            "vectors": self.vectors,
        }
        self.cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def search(self, query: str, limit: int) -> list[tuple[int, float]]:
        # 当前默认 provider 是本地 hashing embedding；配置 DashScope 后会自动换成真实向量。
        query_vector = self.embedding_provider.embed(query)
        scores = [
            (index, cosine(query_vector, vector))
            for index, vector in enumerate(self.vectors)
            if vector
        ]
        return sorted(scores, key=lambda item: item[1], reverse=True)[:limit]


class HybridTableRetriever:
    """Text2SQL 的第一道“选表”关口。"""

    def __init__(
        self,
        tables: list[TableInfo],
        embedding_provider: EmbeddingProvider | None = None,
        reranker: Reranker | None = None,
        cache_dir: str | Path = ".text2sql_cache",
        rrf_k: int = 60,
        semantics: "SchemaSemantics | None" = None,
    ) -> None:
        self.tables = tables
        self.semantics = semantics
        # 把人工维护的中文别名/描述/枚举词拼进检索文档，让中文提问更易命中英文 schema。
        self.documents = [self._build_document(table) for table in tables]
        self.bm25 = BM25Index(self.documents)
        self.vector_index = PersistentVectorIndex(
            tables, embedding_provider, cache_dir, documents=self.documents
        )
        self.reranker = reranker or default_reranker()
        self.rrf_k = rrf_k

    def _build_document(self, table: TableInfo) -> str:
        base = table.document()
        if not self.semantics:
            return base
        enrichment = self.semantics.enrich_corpus(table.name)
        return f"{base} {enrichment}".strip() if enrichment else base

    def retrieve(self, query: str, top_k: int = 6, pool_size: int | None = None) -> list[RetrievalHit]:
        if not self.tables:
            return []
        # pool_size 先取一个比 top_k 大得多的候选池，给后续 rerank 留空间。
        pool_size = pool_size or min(max(top_k * 6, 24), max(24, len(self.tables)))
        bm25_hits = self.bm25.search(query, pool_size)
        vector_hits = self.vector_index.search(query, pool_size)

        # Reciprocal Rank Fusion：只看排序名次，不强行比较 BM25 分数和向量余弦分数的量纲。
        rank_map: dict[int, dict[str, int]] = defaultdict(dict)
        scores: dict[int, float] = defaultdict(float)
        reasons: dict[int, list[str]] = defaultdict(list)

        for rank, (index, _score) in enumerate(bm25_hits, start=1):
            rank_map[index]["bm25"] = rank
            scores[index] += 1.0 / (self.rrf_k + rank)
            reasons[index].append("bm25")

        for rank, (index, _score) in enumerate(vector_hits, start=1):
            rank_map[index]["vector"] = rank
            scores[index] += 1.0 / (self.rrf_k + rank)
            reasons[index].append("vector")

        if not scores:
            for rank, (index, score) in enumerate(self.bm25.search(query, max(top_k, 4)), start=1):
                rank_map[index]["bm25"] = rank
                scores[index] = score
                reasons[index].append("bm25_fallback")

        # 在通用召回之外补一点业务启发：时间/指标意图要优先命中含 time/metric 标签的表。
        boosted: dict[int, float] = {}
        for index, score in scores.items():
            table = self.tables[index]
            comment_boost = overlap_ratio(query, f"{table.name} {table.comment}") * 1.5
            column_boost = overlap_ratio(query, " ".join(column.document() for column in table.columns))
            semantic_boost = self._semantic_table_boost(query, table)
            boosted[index] = score + 0.05 * (comment_boost + column_boost) + semantic_boost

        candidate_indexes = [
            index for index, _score in sorted(boosted.items(), key=lambda item: item[1], reverse=True)
        ][:pool_size]
        candidate_tables = [self.tables[index] for index in candidate_indexes]

        # reranker 可以是本地启发式，也可以是 DashScope GTE；这里只消费统一接口。
        reranked = self.reranker.rerank(query, candidate_tables)
        rerank_scores = {table.name: score for table, score in reranked}
        rerank_order = {table.name: rank for rank, (table, _score) in enumerate(reranked, start=1)}

        # 最终分数保留 boosted 主排序，同时小幅吸收 rerank 分，避免单一模型过度主导。
        final_indexes = sorted(
            candidate_indexes,
            key=lambda index: (
                boosted[index] + 0.1 * rerank_scores.get(self.tables[index].name, 0.0),
                -rerank_order.get(self.tables[index].name, pool_size + 1),
            ),
            reverse=True,
        )[:top_k]

        return [
            RetrievalHit(
                table=self.tables[index],
                score=boosted[index] + 0.1 * rerank_scores.get(self.tables[index].name, 0.0),
                bm25_rank=rank_map[index].get("bm25"),
                vector_rank=rank_map[index].get("vector"),
                rerank_score=rerank_scores.get(self.tables[index].name),
                reasons=tuple(reasons[index]),
            )
            for index in final_indexes
        ]

    def _semantic_table_boost(self, query: str, table: TableInfo) -> float:
        """根据常见取数意图做小权重修正，帮助“按月金额趋势”类问题稳定命中事实表。"""

        query_tokens = set(tokenize(query))
        has_time_intent = bool(
            query_tokens
            & {"month", "date", "time", "period", "trend", "lag", "环比", "同比", "月份", "趋势"}
        )
        has_metric_intent = bool(
            query_tokens
            & {"amount", "price", "gmv", "revenue", "metric", "金额", "销售", "收入"}
        )
        table_tags = {tag for column in table.columns for tag in column.semantic_tags}
        boost = 0.0
        if has_time_intent:
            boost += 0.08 if "time" in table_tags else -0.03
        if has_metric_intent:
            boost += 0.04 if "metric" in table_tags else -0.02
        if has_time_intent and has_metric_intent and {"time", "metric"} <= table_tags:
            boost += 0.04
        return boost
