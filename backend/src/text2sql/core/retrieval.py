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
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from text2sql.config.domain_profile import DomainProfile, get_domain_profile
from text2sql.core.embeddings import EmbeddingProvider, cosine, default_embedding_provider
from text2sql.core.models import RetrievalHit, TableInfo
from text2sql.core.rerank import Reranker, default_reranker
from text2sql.core.tokenization import overlap_ratio, tokenize_with_profile

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

    def __init__(self, documents: list[str], domain_profile: DomainProfile | None = None) -> None:
        self.documents = documents
        self.domain_profile = domain_profile
        self.tokens = [tokenize_with_profile(document, self.domain_profile) for document in documents]
        self.doc_count = len(documents)
        self.doc_lengths = [len(tokens) for tokens in self.tokens]
        self.avg_doc_length = sum(self.doc_lengths) / max(1, self.doc_count)
        self.doc_freq: Counter[str] = Counter()
        for tokens in self.tokens:
            self.doc_freq.update(set(tokens))

    def search(self, query: str, limit: int) -> list[tuple[int, float]]:
        # 返回 document index 和 BM25 分数；调用方再把 index 映射回 TableInfo。
        query_tokens = tokenize_with_profile(query, self.domain_profile)
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
        domain_profile: DomainProfile | None = None,
    ) -> None:
        self.tables = tables
        self.semantics = semantics
        self.domain_profile = domain_profile or get_domain_profile()
        # 把人工维护的中文别名/描述/枚举词拼进检索文档，让中文提问更易命中英文 schema。
        self.documents = [self._build_document(table) for table in tables]
        self.bm25 = BM25Index(self.documents, self.domain_profile)
        self.vector_index = PersistentVectorIndex(
            tables, embedding_provider, cache_dir, documents=self.documents
        )
        self.reranker = reranker or default_reranker(self.domain_profile)
        self.rrf_k = rrf_k
        self._table_index = {table.name: index for index, table in enumerate(tables)}
        self._relationship_adjacency = self._build_relationship_adjacency(tables)

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
            comment_boost = (
                overlap_ratio(query, f"{table.name} {table.comment}", self.domain_profile) * 1.5
            )
            column_boost = overlap_ratio(
                query,
                " ".join(column.document() for column in table.columns),
                self.domain_profile,
            )
            semantic_boost = self._semantic_table_boost(query, table)
            boosted[index] = (
                score
                + 0.05 * (comment_boost + column_boost)
                + semantic_boost
                + self._configured_table_boost(query, table)
            )

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
        final_indexes = self._complete_relationship_paths(final_indexes, query, top_k, reasons)

        return [
            RetrievalHit(
                table=self.tables[index],
                score=boosted.get(index, 0.0) + 0.1 * rerank_scores.get(self.tables[index].name, 0.0),
                bm25_rank=rank_map[index].get("bm25"),
                vector_rank=rank_map[index].get("vector"),
                rerank_score=rerank_scores.get(self.tables[index].name),
                reasons=tuple(reasons[index]),
            )
            for index in final_indexes
        ]

    def _semantic_table_boost(self, query: str, table: TableInfo) -> float:
        """按 profile 配置的字段标签规则做小权重修正。"""

        query_tokens = {token.lower() for token in tokenize_with_profile(query, self.domain_profile)}
        raw_query = (query or "").lower()
        table_tags = {tag for column in table.columns for tag in column.semantic_tags}
        boost = 0.0
        for rule in self.domain_profile.retrieval_tag_boost_rules:
            if not self._query_condition_matches(rule.get("match"), query_tokens, raw_query):
                continue
            required_tags = set(self._as_tuple(rule.get("column_tags_all")))
            column_tag = str(rule.get("column_tag") or "")
            if required_tags:
                matched = required_tags <= table_tags
            elif column_tag:
                matched = column_tag in table_tags
            else:
                matched = True
            boost += self._float(rule.get("boost" if matched else "penalty"), 0.0)
        return boost

    def _configured_table_boost(self, query: str, table: TableInfo) -> float:
        """按 profile 配置的表级规则做轻量修正，避免把领域知识写进检索代码。"""

        tokens = {token.lower() for token in tokenize_with_profile(query, self.domain_profile)}
        raw_query = (query or "").lower()
        name = table.name.lower()
        boost = 0.0
        for rule in self.domain_profile.retrieval_table_boost_rules:
            if not self._query_condition_matches(rule.get("match"), tokens, raw_query):
                continue
            for target in rule.get("boosts", ()):
                if self._table_target_matches(name, target):
                    boost += self._float(target.get("boost"), 0.0)
        return boost

    def _query_anchor_tables(self, query: str) -> set[str]:
        """按 profile 配置从问题里抽取关系路径端点。"""

        tokens = {token.lower() for token in tokenize_with_profile(query, self.domain_profile)}
        raw_query = (query or "").lower()
        anchors: set[str] = set()
        config = self.domain_profile.retrieval_relationship_path
        for anchor in config.get("anchors", ()):
            if self._query_condition_matches(anchor.get("match"), tokens, raw_query):
                anchors.update(self._as_tuple(anchor.get("tables")))
        return anchors

    @classmethod
    def _query_condition_matches(
        cls,
        condition: Any,
        tokens: set[str],
        raw_query: str,
    ) -> bool:
        """解释 profile 中的查询匹配条件。

        支持 all/any 嵌套，以及 terms_any/phrases_any/terms_all/phrases_all 四种叶子条件。
        """

        if not condition:
            return True
        if not isinstance(condition, dict):
            return False
        all_conditions = condition.get("all")
        if all_conditions is not None and not all(
            cls._query_condition_matches(item, tokens, raw_query)
            for item in cls._as_tuple_or_list(all_conditions)
        ):
            return False
        any_conditions = condition.get("any")
        if any_conditions is not None and not any(
            cls._query_condition_matches(item, tokens, raw_query)
            for item in cls._as_tuple_or_list(any_conditions)
        ):
            return False

        any_checks: list[bool] = []
        terms_any = {term.lower() for term in cls._as_tuple(condition.get("terms_any"))}
        if terms_any:
            any_checks.append(bool(tokens & terms_any))
        phrases_any = tuple(phrase.lower() for phrase in cls._as_tuple(condition.get("phrases_any")))
        if phrases_any:
            any_checks.append(any(phrase in raw_query for phrase in phrases_any))
        if any_checks and not any(any_checks):
            return False

        terms_all = {term.lower() for term in cls._as_tuple(condition.get("terms_all"))}
        if terms_all and not terms_all <= tokens:
            return False
        phrases_all = tuple(phrase.lower() for phrase in cls._as_tuple(condition.get("phrases_all")))
        if phrases_all and not all(phrase in raw_query for phrase in phrases_all):
            return False
        return True

    @staticmethod
    def _table_target_matches(table_name: str, target: Any) -> bool:
        if not isinstance(target, dict):
            return False
        exact_tables = {name.lower() for name in HybridTableRetriever._as_tuple(target.get("tables"))}
        table = str(target.get("table") or "").lower()
        if table:
            exact_tables.add(table)
        if exact_tables and table_name in exact_tables:
            return True
        contains = tuple(
            fragment.lower()
            for fragment in HybridTableRetriever._as_tuple(target.get("table_contains"))
        )
        if contains and any(fragment in table_name for fragment in contains):
            return True
        prefixes = tuple(
            prefix.lower()
            for prefix in HybridTableRetriever._as_tuple(target.get("table_prefixes"))
        )
        if prefixes and any(table_name.startswith(prefix) for prefix in prefixes):
            return True
        suffixes = tuple(
            suffix.lower()
            for suffix in HybridTableRetriever._as_tuple(target.get("table_suffixes"))
        )
        return bool(suffixes and any(table_name.endswith(suffix) for suffix in suffixes))

    @staticmethod
    def _as_tuple(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple, set)):
            return tuple(str(item) for item in value)
        return (str(value),)

    @staticmethod
    def _as_tuple_or_list(value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return (value,)

    @staticmethod
    def _float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _build_relationship_adjacency(tables: list[TableInfo]) -> dict[str, set[str]]:
        """把 schema 外键整理为无向表图，用于召回阶段补全 JOIN 桥接表。"""

        adjacency: dict[str, set[str]] = {table.name: set() for table in tables}
        for table in tables:
            for fk in table.foreign_keys:
                adjacency.setdefault(fk.source_table, set()).add(fk.target_table)
                adjacency.setdefault(fk.target_table, set()).add(fk.source_table)
        return adjacency

    def _complete_relationship_paths(
        self,
        final_indexes: list[int],
        query: str,
        top_k: int,
        reasons: dict[int, list[str]],
    ) -> list[int]:
        """在最终 topK 里优先展示明确多跳关系路径，补齐配置指定的桥接表。"""

        path = self._best_relationship_path(final_indexes, query)
        if not path:
            return final_indexes
        path_indexes = [self._table_index[name] for name in path if name in self._table_index]
        if len(path_indexes) <= 1 or len(path_indexes) > top_k:
            return final_indexes

        ordered: list[int] = []
        for index in path_indexes:
            if index not in ordered:
                ordered.append(index)
                reasons[index].append("relationship_path")
        for index in final_indexes:
            if index not in ordered:
                ordered.append(index)
        return ordered[:top_k]

    def _best_relationship_path(
        self,
        final_indexes: list[int],
        query: str,
    ) -> list[str] | None:
        """从最终候选中选择最值得优先展示的一条多跳关系路径。"""

        if len(final_indexes) < 2:
            return None
        config = self.domain_profile.retrieval_relationship_path
        if not config:
            return None
        # 只在 profile 判定有多表分析意图时启用，避免单表 KPI 被关系扩展冲散。
        query_tokens = {token.lower() for token in tokenize_with_profile(query, self.domain_profile)}
        raw_query = (query or "").lower()
        if not self._query_condition_matches(config.get("enable_when"), query_tokens, raw_query):
            return None

        anchor_tables = self._query_anchor_tables(query)
        explicit_path_intent = self._query_condition_matches(
            config.get("explicit_path_when"), query_tokens, raw_query
        )
        if not anchor_tables and not explicit_path_intent:
            return None
        max_depth = int(self._float(config.get("max_depth"), 4))
        min_score = self._float(config.get("min_score"), 0.04)
        scoring = config.get("scoring", {}) if isinstance(config.get("scoring"), dict) else {}
        fact_tables = set(self._as_tuple(config.get("fact_tables")))
        best_path: list[str] | None = None
        best_score = 0.0
        for left_pos, left_index in enumerate(final_indexes):
            for right_index in final_indexes[left_pos + 1 :]:
                path = self._shortest_table_path(
                    self.tables[left_index].name,
                    self.tables[right_index].name,
                    max_depth=max_depth,
                )
                if not path or len(path) <= 2:
                    continue
                path_table_objs = [self.tables[self._table_index[name]] for name in path if name in self._table_index]
                overlap = sum(
                    overlap_ratio(query, self.documents[self._table_index[name]], self.domain_profile)
                    for name in path
                    if name in self._table_index
                )
                has_metric_table = any(
                    any("metric" in column.semantic_tags for column in table.columns)
                    for table in path_table_objs
                )
                score = overlap / len(path)
                if has_metric_table:
                    score += self._float(scoring.get("metric_table_boost"), 0.0)
                if has_metric_table and len(path) >= 4:
                    score += self._float(scoring.get("long_metric_path_boost"), 0.0)
                endpoint_names = {path[0], path[-1]}
                path_names = set(path)
                if anchor_tables & endpoint_names:
                    score += self._float(scoring.get("anchor_endpoint_boost"), 0.0)
                if anchor_tables & endpoint_names and fact_tables & endpoint_names:
                    score += self._float(scoring.get("anchor_fact_endpoint_boost"), 0.0)
                if anchor_tables & path_names:
                    score += self._float(scoring.get("anchor_path_boost"), 0.0)
                for bridge_rule in config.get("bridge_rules", ()):
                    rule_anchor_tables = set(self._as_tuple(bridge_rule.get("anchor_tables")))
                    required_path_tables = set(self._as_tuple(bridge_rule.get("required_path_tables")))
                    if anchor_tables & rule_anchor_tables and required_path_tables <= path_names:
                        score += self._float(bridge_rule.get("boost"), 0.0)
                if score > best_score:
                    best_score = score
                    best_path = path
        if best_score < min_score:
            return None
        return best_path

    def _shortest_table_path(self, source: str, target: str, max_depth: int) -> list[str] | None:
        """在表图上找最短路径，返回表名序列。"""

        if source == target:
            return [source]
        queue: deque[tuple[str, list[str]]] = deque([(source, [source])])
        visited = {source}
        while queue:
            current, path = queue.popleft()
            if len(path) - 1 >= max_depth:
                continue
            for neighbor in self._relationship_adjacency.get(current, ()):
                if neighbor in visited:
                    continue
                next_path = [*path, neighbor]
                if neighbor == target:
                    return next_path
                visited.add(neighbor)
                queue.append((neighbor, next_path))
        return None
