from __future__ import annotations

"""embedding provider 测试。

重点覆盖 DashScope 单批上限（25）导致多表 schema 向量化失败的分批修复。
"""

import unittest

from text2sql.core.embeddings import DashScopeEmbeddingProvider, HashingEmbeddingProvider


class DashScopeBatchTests(unittest.TestCase):
    def test_batch_embed_splits_requests_within_api_limit(self):
        # DashScope text-embedding 单次 input 上限 25，超过必须自动分批，
        # 否则电商这类 30+ 表 schema 向量化会直接 400。
        provider = DashScopeEmbeddingProvider(api_key="dummy")
        seen_batch_sizes: list[int] = []

        def fake_chunk(texts: list[str]) -> list[list[float]]:
            seen_batch_sizes.append(len(texts))
            return [[float(len(text))] for text in texts]

        provider._embed_chunk = fake_chunk  # type: ignore[assignment]
        vectors = provider.batch_embed([f"doc-{i}" for i in range(30)])

        self.assertEqual(len(vectors), 30)
        self.assertTrue(all(size <= 25 for size in seen_batch_sizes))
        self.assertEqual(seen_batch_sizes, [25, 5])

    def test_batch_embed_handles_empty_input(self):
        provider = DashScopeEmbeddingProvider(api_key="dummy")
        provider._embed_chunk = lambda texts: []  # type: ignore[assignment]
        self.assertEqual(provider.batch_embed([]), [])


class HashingProviderTests(unittest.TestCase):
    def test_batch_embed_matches_single(self):
        provider = HashingEmbeddingProvider(dimensions=16)
        texts = ["订单 金额", "用户 地区"]
        self.assertEqual(provider.batch_embed(texts), [provider.embed(t) for t in texts])


if __name__ == "__main__":
    unittest.main()
