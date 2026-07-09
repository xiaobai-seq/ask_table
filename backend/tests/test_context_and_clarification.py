import unittest

from text2sql.core.clarification import AmbiguityDetector
from text2sql.core.context import ConversationMemory
from text2sql.core.models import ColumnInfo, ConversationTurn, RetrievalHit, TableInfo


class ContextAndClarificationTests(unittest.TestCase):
    def test_contextual_followup_is_rewritten_with_previous_sql_and_tables(self):
        memory = ConversationMemory(max_turns=3)
        memory.add_turn(
            "s1",
            ConversationTurn(
                user_query="按月份统计订单金额",
                rewritten_query="按月份统计订单金额",
                generated_sql="SELECT month, SUM(total_amount) FROM orders GROUP BY month",
                tables=("orders",),
            ),
        )

        rewritten = memory.rewrite_query("s1", "再看环比")

        self.assertIn("上一轮问题", rewritten)
        self.assertIn("orders", rewritten)
        self.assertIn("当前追问: 再看环比", rewritten)

    def test_vague_query_requires_clarification_without_context(self):
        table = TableInfo(
            "orders",
            "订单",
            columns=(ColumnInfo("order_id", "INTEGER"), ColumnInfo("total_amount", "REAL"),),
        )
        hit = RetrievalHit(table=table, score=1.0)

        clarification = AmbiguityDetector().detect("看一下情况", [hit], has_context=False)

        self.assertIsNotNone(clarification)
        self.assertEqual(clarification.reason, "vague_metric")

    @staticmethod
    def _hit(name: str, score: float) -> RetrievalHit:
        return RetrievalHit(
            table=TableInfo(name, name, columns=(ColumnInfo("id", "INTEGER"),)),
            score=score,
        )

    def test_default_detector_flags_close_schema_candidates(self):
        # 多张事实表得分接近（电商 orders/order_items/return_orders 场景）：
        # 线上默认门槛必须保持保守——触发数据域澄清，避免选错事实表。
        hits = [self._hit("orders", 1.0), self._hit("order_items", 0.97), self._hit("return_orders", 0.96)]

        clarification = AmbiguityDetector().detect(
            "各支付方式的成功交易金额分布", hits, has_context=False
        )

        self.assertIsNotNone(clarification)
        self.assertEqual(clarification.reason, "close_schema_candidates")

    def test_evaluation_detector_relaxes_close_schema_candidates(self):
        # 评测专用触发条件：收紧 margin 且要求更多并列候选，
        # 明确问题不再因多相近表被澄清拦截；线上门槛不受影响。
        hits = [self._hit("orders", 1.0), self._hit("order_items", 0.97), self._hit("return_orders", 0.96)]

        clarification = AmbiguityDetector.for_evaluation().detect(
            "各支付方式的成功交易金额分布", hits, has_context=False
        )

        self.assertIsNone(clarification)

    def test_same_sentence_cohort_reference_does_not_require_context(self):
        hit = self._hit("user_events", 1.0)

        clarification = AmbiguityDetector.for_evaluation().detect(
            "统计每月活跃用户数，以及这些用户下月仍有行为事件的留存率。",
            [hit],
            has_context=False,
        )

        self.assertIsNone(clarification)


if __name__ == "__main__":
    unittest.main()
