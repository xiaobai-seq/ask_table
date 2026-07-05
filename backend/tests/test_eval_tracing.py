import asyncio
import tempfile
import unittest

from text2sql.core.graph import Text2SQLWorkflow
from text2sql.core.models import EvalResult, strip_trace_only_fields, to_plain
from text2sql.core.sample_data import create_sample_database


class EvalResultTraceTests(unittest.TestCase):
    """EvalResult 需携带逐 case trace，并可被 to_plain 序列化进报告。"""

    def test_trace_defaults_to_none_for_backward_compat(self):
        # 不传 trace 时保持既有位置参数签名不变。
        result = EvalResult("c1", True, {"table_recall": 1.0}, "SELECT 1")
        self.assertIsNone(result.trace)

    def test_trace_is_serialized_by_to_plain(self):
        result = EvalResult(
            "c1",
            True,
            {"table_recall": 1.0},
            "SELECT 1",
            trace={
                "prompt": "PROMPT_X",
                "retrieval_hits": [{"table": "orders", "score": 0.9}],
            },
        )
        plain = to_plain(result)
        self.assertEqual(plain["trace"]["prompt"], "PROMPT_X")
        self.assertEqual(plain["trace"]["retrieval_hits"][0]["table"], "orders")


class SqlPromptTracingTests(unittest.TestCase):
    """generate_sql 需把真实 prompt 落到 state 供评测；但线上 SSE 不应带它。"""

    def test_run_exposes_sql_prompt_for_tracing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/demo.db"
            create_sample_database(db_path)
            workflow = Text2SQLWorkflow(
                database_url_or_path=f"sqlite:///{db_path}", cache_dir=tmpdir
            )
            state = asyncio.run(
                workflow.run("按月份统计订单金额趋势，并计算环比增长率", "s_prompt")
            )
        prompt = state.get("sql_prompt")
        self.assertTrue(prompt)
        self.assertIn("候选表", prompt)

    def test_strip_trace_only_fields_removes_sql_prompt_for_sse(self):
        partial = {"generated_sql": "SELECT 1", "chart_type": "kpi", "sql_prompt": "BIG"}
        cleaned = strip_trace_only_fields(partial)
        self.assertNotIn("sql_prompt", cleaned)
        self.assertEqual(cleaned["generated_sql"], "SELECT 1")
        self.assertEqual(cleaned["chart_type"], "kpi")
        # 不修改入参，避免影响调用方 state。
        self.assertIn("sql_prompt", partial)


if __name__ == "__main__":
    unittest.main()
