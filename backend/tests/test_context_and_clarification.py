import unittest

from text2sql.clarification import AmbiguityDetector
from text2sql.context import ConversationMemory
from text2sql.models import ColumnInfo, ConversationTurn, RetrievalHit, TableInfo


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


if __name__ == "__main__":
    unittest.main()

