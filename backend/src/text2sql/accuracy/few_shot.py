from __future__ import annotations

"""Few-shot 示例库。

向 SQL prompt 注入「问题 → 优质 SQL」示例能显著提升生成准确率。本模块提供：

- `FewShotStore` 接口（便于后续阶段替换为落库实现）；
- `InMemoryFewShotStore` 内存实现，按相似度检索 Top-K 示例。

相似度策略：默认离线环境用基于 `tokenize` 的关键词重叠（对中文友好、确定性强）；
若显式注入语义 embedding provider，则改用向量余弦。两条路径都不引入外部强依赖，
保证「缺依赖可降级」。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from text2sql.core.embeddings import EmbeddingProvider, cosine
from text2sql.core.tokenization import tokenize


@dataclass(frozen=True)
class FewShotExample:
    """一条「问题 → SQL」示例；chart_type 可选，便于回放推荐图表。"""

    question: str
    sql: str
    chart_type: str = "table"


class FewShotStore(Protocol):
    """few-shot 示例库接口；内存与落库实现共享同一契约。"""

    def add(self, example: FewShotExample) -> None: ...

    def search(self, query: str, top_k: int) -> list[FewShotExample]: ...


class InMemoryFewShotStore:
    """内存示例库：离线用关键词相似，注入语义向量时用余弦相似。"""

    def __init__(self, embedding_provider: EmbeddingProvider | None = None) -> None:
        # 只有显式传入「真实语义」provider 才启用向量召回；默认走关键词相似，
        # 因为本地 hashing embedding 对无空格的中文几乎退化为精确匹配。
        self.embedding_provider = embedding_provider
        self._examples: list[FewShotExample] = []
        self._question_tokens: list[set[str]] = []
        self._vectors: list[list[float]] = []

    def add(self, example: FewShotExample) -> None:
        self._examples.append(example)
        self._question_tokens.append(set(tokenize(example.question)))
        if self.embedding_provider is not None:
            self._vectors.append(self.embedding_provider.embed(example.question))

    def add_many(self, examples: list[FewShotExample]) -> None:
        for example in examples:
            self.add(example)

    def search(self, query: str, top_k: int) -> list[FewShotExample]:
        if not self._examples or top_k <= 0:
            return []
        scores = (
            self._vector_scores(query)
            if self.embedding_provider is not None
            else self._keyword_scores(query)
        )
        ranked = sorted(
            range(len(self._examples)), key=lambda index: scores[index], reverse=True
        )
        return [self._examples[index] for index in ranked[:top_k]]

    def _keyword_scores(self, query: str) -> list[float]:
        # Jaccard 相似度：交集越大、并集越小越相似，对中文分词结果稳定可解释。
        query_tokens = set(tokenize(query))
        scores: list[float] = []
        for tokens in self._question_tokens:
            union = query_tokens | tokens
            scores.append(len(query_tokens & tokens) / len(union) if union else 0.0)
        return scores

    def _vector_scores(self, query: str) -> list[float]:
        query_vector = self.embedding_provider.embed(query)
        return [cosine(query_vector, vector) for vector in self._vectors]

    @classmethod
    def from_jsonl(
        cls, path: str | Path, embedding_provider: EmbeddingProvider | None = None
    ) -> "InMemoryFewShotStore":
        """从 JSONL 种子文件加载；文件缺失或损坏时返回空库。"""

        store = cls(embedding_provider)
        file_path = Path(path)
        if not file_path.exists():
            return store
        try:
            for line in file_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                store.add(
                    FewShotExample(
                        question=payload["question"],
                        sql=payload["sql"],
                        chart_type=payload.get("chart_type", "table"),
                    )
                )
        except Exception:  # pragma: no cover - 种子损坏时降级为已加载部分
            return store
        return store


def format_examples_block(examples: list[FewShotExample]) -> str:
    """把示例渲染成可注入 prompt 的文本块。"""

    if not examples:
        return ""
    lines: list[str] = []
    for index, example in enumerate(examples, start=1):
        lines.append(f"示例{index} 问题: {example.question}")
        lines.append(f"示例{index} SQL: {example.sql}")
    return "\n".join(lines)
