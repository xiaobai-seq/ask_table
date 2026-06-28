from __future__ import annotations

"""embedding provider 抽象。

检索层只依赖 EmbeddingProvider 接口。默认用本地 hashing embedding 保持测试可重复；
配置 DASHSCOPE_API_KEY 后可切到真实语义向量。
"""

import hashlib
import math
import os
from typing import Protocol


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]:
        ...

    def batch_embed(self, texts: list[str]) -> list[list[float]]:
        ...


class HashingEmbeddingProvider:
    """确定性的本地向量 fallback，用于测试和离线开发。"""

    def __init__(self, dimensions: int = 128) -> None:
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        # 这是 feature hashing，不追求真实语义，只保证相同 token 产生相同方向。
        vector = [0.0] * self.dimensions
        tokens = text.lower().split()
        if not tokens:
            tokens = [text.lower()]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        return normalize(vector)

    def batch_embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


class DashScopeEmbeddingProvider:
    """DashScope text embedding 适配器，延迟 import 以保持本地测试免依赖。"""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.getenv("DASHSCOPE_EMBEDDING_MODEL", "text-embedding-v2")
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")

    def embed(self, text: str) -> list[float]:
        return self.batch_embed([text])[0]

    def batch_embed(self, texts: list[str]) -> list[list[float]]:
        try:  # pragma: no cover - optional network dependency
            import dashscope
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("dashscope is not installed") from exc

        if self.api_key:
            dashscope.api_key = self.api_key
        response = dashscope.TextEmbedding.call(model=self.model, input=texts)
        if getattr(response, "status_code", 200) != 200:
            raise RuntimeError(f"DashScope embedding failed: {response}")
        output = response.get("output", response)
        embeddings = output.get("embeddings", [])
        return [item["embedding"] for item in embeddings]


def normalize(vector: list[float]) -> list[float]:
    """把向量归一化，后续点积即可近似余弦相似度。"""

    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    """计算两向量余弦相似度；长度不一致时按最短长度对齐。"""

    if not left or not right:
        return 0.0
    length = min(len(left), len(right))
    return sum(left[i] * right[i] for i in range(length))


def default_embedding_provider() -> EmbeddingProvider:
    """按环境变量选择生产 provider 或本地 fallback。"""

    if os.getenv("DASHSCOPE_API_KEY"):
        return DashScopeEmbeddingProvider()
    return HashingEmbeddingProvider()
