from __future__ import annotations

"""电商 few-shot 示例库测试（Task 7）。

验证 examples/ecommerce/few_shot_seed.jsonl 能被 InMemoryFewShotStore 正确加载，
并对典型业务问题召回示例。仅依赖 few_shot 公共 API，不触碰其他电商资产文件。
"""

import unittest
from pathlib import Path

from text2sql.accuracy.few_shot import InMemoryFewShotStore

# 任务指定的相对路径（与运行命令 `cd backend` 后的工作目录一致）。
SEED_REL_PATH = "examples/ecommerce/few_shot_seed.jsonl"
# 本测试位于 backend/tests/，parents[1] 即 backend/ 根目录，用于 cwd 兜底。
_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _load_store() -> InMemoryFewShotStore:
    """优先按任务指定的相对路径加载；若当前工作目录不在 backend/ 导致
    文件缺失（from_jsonl 会降级为空库），则回退到相对本文件解析的绝对路径，
    保证测试在任意 cwd 下都可复现。"""
    store = InMemoryFewShotStore.from_jsonl(SEED_REL_PATH)
    if not store.search("电商", top_k=1):  # 空库说明相对路径未命中，回退绝对路径
        store = InMemoryFewShotStore.from_jsonl(_BACKEND_ROOT / SEED_REL_PATH)
    return store


class TestEcommerceFewShot(unittest.TestCase):
    def test_seed_loads_at_least_15_examples(self):
        store = _load_store()
        # top_k 取足够大值即可召回全部示例，len 即示例总数。
        loaded = store.search("电商分析示例", top_k=1000)
        self.assertGreaterEqual(len(loaded), 15)

    def test_search_returns_examples_for_mom_question(self):
        store = _load_store()
        got = store.search("按月统计销售额环比", top_k=3)
        self.assertTrue(len(got) >= 1)

    def test_every_sql_starts_with_select_or_with(self):
        store = _load_store()
        for example in store.search("全部示例", top_k=1000):
            head = example.sql.lstrip().upper()
            self.assertTrue(
                head.startswith("SELECT") or head.startswith("WITH"),
                msg=f"SQL 必须以 SELECT/WITH 开头: {example.sql[:40]}",
            )


if __name__ == "__main__":
    unittest.main()
