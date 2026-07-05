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


class _StubWorkflow:
    """最小 workflow 替身：run 返回预设 state，用于验证 trace 收集。"""

    def __init__(self, state: dict):
        self._state = state

    async def run(self, query: str, session_id: str = "default") -> dict:
        return dict(self._state)


class EvalCaseTraceCollectionTests(unittest.TestCase):
    """run_case 收集全环节 trace；persist_case_results 把 trace 落库。"""

    def _state(self):
        from text2sql.core.models import ExecutionResult, RetrievalHit, TableInfo

        return {
            "rewritten_query": "各品类销售额",
            "retrieval_hits": [RetrievalHit(table=TableInfo(name="order_items"), score=0.9)],
            "table_relationship": [],
            "sql_prompt": "PROMPT_X",
            "generated_sql": "SELECT 1",
            "execution_result": ExecutionResult(
                columns=("m",), rows=({"m": 100},), row_count=1
            ),
            "clarification": None,
        }

    def test_run_case_collects_full_trace(self):
        from text2sql.core.models import EvalCase
        from text2sql.eval import EvaluationRunner

        case = EvalCase(
            case_id="c1",
            query="各品类销售额",
            expected_tables=("order_items",),
            required_sql_keywords=("select",),
        )
        result = asyncio.run(EvaluationRunner(_StubWorkflow(self._state())).run_case(case))

        trace = result.trace
        self.assertIsNotNone(trace)
        self.assertEqual(trace["query"], "各品类销售额")
        self.assertEqual(trace["rewritten_query"], "各品类销售额")
        self.assertEqual(trace["retrieval_hits"][0]["table"], "order_items")
        self.assertEqual(trace["prompt"], "PROMPT_X")
        self.assertEqual(trace["generated_sql"], "SELECT 1")
        self.assertEqual(trace["execution_rows"], [{"m": 100}])
        self.assertEqual(trace["row_count"], 1)

    def test_persist_case_results_writes_all_cases(self):
        from text2sql.eval import persist_case_results
        from text2sql.persistence.repository import InMemoryEvalRunRepository

        repo = InMemoryEvalRunRepository()
        run = repo.record_run(total=2, passed=1, pass_rate=0.5, metrics={})
        results = [
            EvalResult(
                "c1",
                True,
                {"value_set_exact": 1.0},
                "SELECT 1",
                trace={
                    "query": "q1",
                    "rewritten_query": "q1",
                    "retrieval_hits": [{"table": "orders"}],
                    "prompt": "P1",
                    "execution_rows": [{"m": 1}],
                    "row_count": 1,
                },
            ),
            EvalResult("c2", False, {}, None, (), trace={"query": "q2"}),
        ]

        persist_case_results(repo, run.id, results)

        rows = repo.list_case_results(run.id)
        self.assertEqual(len(rows), 2)
        by_case = {row.case_id: row for row in rows}
        self.assertEqual(by_case["c1"].generated_sql, "SELECT 1")
        self.assertEqual(by_case["c1"].retrieval_hits[0]["table"], "orders")
        self.assertTrue(by_case["c1"].passed)
        self.assertEqual(by_case["c1"].metrics["value_set_exact"], 1.0)


class RunCasePassGatingTests(unittest.TestCase):
    """pass 门槛只看硬指标：SQL 文本/列别名/浮点精度差异不应造成假 fail。"""

    def _runner(self, state):
        from text2sql.eval import EvaluationRunner

        return EvaluationRunner(_StubWorkflow(state))

    def _base_state(self, generated_sql, rows):
        from text2sql.core.models import ExecutionResult, RetrievalHit, TableInfo

        return {
            "rewritten_query": "各供应商销售额",
            "retrieval_hits": [
                RetrievalHit(table=TableInfo(name="order_items"), score=0.9),
                RetrievalHit(table=TableInfo(name="suppliers"), score=0.8),
            ],
            "table_relationship": [],
            "sql_prompt": "P",
            "generated_sql": generated_sql,
            "execution_result": ExecutionResult(
                columns=tuple(rows[0].keys()) if rows else (),
                rows=tuple(rows),
                row_count=len(rows),
            ),
            "clarification": None,
        }

    def test_exact_sql_and_alias_mismatch_still_passes(self):
        from text2sql.core.models import EvalCase

        case = EvalCase(
            case_id="supplier",
            query="各供应商销售额",
            expected_sql=(
                "SELECT s.name AS dimension_value, SUM(oi.subtotal) AS metric_value "
                "FROM order_items oi JOIN suppliers s ON oi.supplier_id = s.supplier_id "
                "GROUP BY s.name"
            ),
            expected_tables=("order_items", "suppliers"),
            required_sql_keywords=("join", "sum", "group by"),
            expected_result=({"dimension_value": "甲公司", "metric_value": 100.0},),
        )
        # LLM 用了不同别名（中文）、不同 SQL 文本、浮点末位不同，但业务值一致。
        state = self._base_state(
            "SELECT suppliers.name AS 供应商, SUM(order_items.subtotal) AS 销售额 "
            "FROM order_items JOIN suppliers "
            "ON order_items.supplier_id = suppliers.supplier_id GROUP BY suppliers.name",
            [{"供应商": "甲公司", "销售额": 100.0000001}],
        )
        result = asyncio.run(self._runner(state).run_case(case))
        self.assertTrue(result.passed, msg=f"errors={result.errors} metrics={result.metrics}")
        # exact_sql / column_set_match 记录为诊断但不阻断 pass。
        self.assertEqual(result.metrics["exact_sql"], 0.0)
        self.assertEqual(result.metrics["value_set_exact"], 1.0)

    def test_missing_execution_still_blocks_pass(self):
        from text2sql.core.models import EvalCase

        case = EvalCase(
            case_id="x",
            query="q",
            expected_tables=("order_items",),
            required_sql_keywords=("select",),
        )
        state = self._base_state("SELECT 1", [{"m": 1}])
        state["execution_result"] = None
        result = asyncio.run(self._runner(state).run_case(case))
        self.assertFalse(result.passed)
        self.assertEqual(result.metrics["execution_success"], 0.0)

    def test_table_recall_and_keyword_miss_do_not_block_when_result_correct(self):
        from text2sql.core.models import EvalCase

        # 端到端口径：检索漏桥接表(skus)、关键词不齐，但结果值正确 → 应 pass；
        # table_recall/keyword_recall 仅作诊断，反映检索/写法质量，不阻断准确率判定。
        case = EvalCase(
            case_id="supplier",
            query="各供应商销售额",
            expected_tables=("order_items", "suppliers", "skus"),
            required_sql_keywords=("join", "sum", "group by", "having"),
            expected_result=({"dimension_value": "甲公司", "metric_value": 100.0},),
        )
        state = self._base_state(
            "SELECT suppliers.name AS dimension_value, SUM(order_items.subtotal) AS metric_value "
            "FROM order_items JOIN suppliers "
            "ON order_items.supplier_id = suppliers.supplier_id GROUP BY suppliers.name",
            [{"dimension_value": "甲公司", "metric_value": 100.0}],
        )
        result = asyncio.run(self._runner(state).run_case(case))
        self.assertTrue(result.passed, msg=f"errors={result.errors} metrics={result.metrics}")
        self.assertLess(result.metrics["table_recall"], 1.0)  # 检索确实漏 skus（诊断保留）
        self.assertLess(result.metrics["keyword_recall"], 1.0)  # having 未用（诊断保留）


if __name__ == "__main__":
    unittest.main()
