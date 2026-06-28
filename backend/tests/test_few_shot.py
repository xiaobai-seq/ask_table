import tempfile
import unittest
from pathlib import Path

from text2sql.accuracy.few_shot import (
    FewShotExample,
    InMemoryFewShotStore,
    format_examples_block,
)
from text2sql.core.models import ColumnInfo, RetrievalHit, TableInfo
from text2sql.core.sql_generator import PromptedSQLGenerator

SEED_JSONL = (
    '{"question": "按月份统计订单金额趋势", "sql": "SELECT month, SUM(total_amount) FROM orders GROUP BY month", "chart_type": "line"}\n'
    '{"question": "各地区客户数量分布", "sql": "SELECT region, COUNT(*) FROM customers GROUP BY region", "chart_type": "bar"}\n'
    '{"question": "员工组织层级", "sql": "WITH RECURSIVE h AS (...) SELECT * FROM h", "chart_type": "table"}\n'
)


class FewShotStoreTests(unittest.TestCase):
    def _seeded_store(self) -> InMemoryFewShotStore:
        store = InMemoryFewShotStore()
        store.add(FewShotExample("按月份统计订单金额趋势", "SELECT month, SUM(total_amount) FROM orders GROUP BY month", "line"))
        store.add(FewShotExample("各地区客户数量分布", "SELECT region, COUNT(*) FROM customers GROUP BY region", "bar"))
        store.add(FewShotExample("员工组织层级路径", "WITH RECURSIVE h AS (...) SELECT * FROM h", "table"))
        return store

    def test_search_returns_most_similar_example_first(self):
        store = self._seeded_store()
        results = store.search("按月统计订单销售金额走势", top_k=2)
        self.assertEqual(len(results), 2)
        # 与「订单金额趋势」最相似的示例应排在首位。
        self.assertIn("SUM(total_amount)", results[0].sql)

    def test_top_k_limits_result_count(self):
        store = self._seeded_store()
        self.assertEqual(len(store.search("任意问题", top_k=1)), 1)

    def test_empty_store_returns_empty_list(self):
        self.assertEqual(InMemoryFewShotStore().search("任何问题", top_k=3), [])

    def test_from_jsonl_loads_examples(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "few_shot_seed.jsonl"
            path.write_text(SEED_JSONL, encoding="utf-8")
            store = InMemoryFewShotStore.from_jsonl(path)
        results = store.search("按月份统计订单金额", top_k=1)
        self.assertEqual(len(results), 1)

    def test_missing_jsonl_degrades_to_empty(self):
        store = InMemoryFewShotStore.from_jsonl("/nonexistent/few_shot_seed.jsonl")
        self.assertEqual(store.search("问题", top_k=3), [])

    def test_format_examples_block_contains_question_and_sql(self):
        block = format_examples_block(
            [FewShotExample("按月份统计订单金额趋势", "SELECT month, SUM(total_amount) FROM orders GROUP BY month", "line")]
        )
        self.assertIn("按月份统计订单金额趋势", block)
        self.assertIn("SUM(total_amount)", block)


class FewShotPromptInjectionTests(unittest.TestCase):
    def setUp(self):
        self.orders = TableInfo(
            "orders",
            "订单",
            columns=(
                ColumnInfo("order_id", "INTEGER", primary_key=True),
                ColumnInfo("order_date", "TEXT", semantic_tags=("time",)),
                ColumnInfo("total_amount", "REAL", semantic_tags=("metric",)),
            ),
        )
        self.store = InMemoryFewShotStore()
        self.store.add(
            FewShotExample(
                "按月份统计订单金额趋势",
                "SELECT month, SUM(total_amount) FROM orders GROUP BY month",
                "line",
            )
        )

    def test_prompt_injects_top_k_examples(self):
        generator = PromptedSQLGenerator(few_shot_store=self.store, few_shot_top_k=2)
        prompt = generator.build_prompt(
            "按月份统计订单金额趋势", [RetrievalHit(self.orders, 1.0)], []
        )
        self.assertIn("参考示例", prompt)
        self.assertIn("SUM(total_amount)", prompt)

    def test_generator_still_degrades_without_llm(self):
        # 注入 few-shot 库不应破坏「无 LLM 时规则生成器照常出 SQL」的降级行为。
        generator = PromptedSQLGenerator(few_shot_store=self.store, few_shot_top_k=2)
        plan = generator.generate(
            "按月份统计订单金额趋势，并计算环比增长率", [RetrievalHit(self.orders, 1.0)], []
        )
        self.assertIn("LAG", plan.sql)


if __name__ == "__main__":
    unittest.main()
