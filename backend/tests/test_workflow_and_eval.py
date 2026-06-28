import asyncio
import tempfile
import unittest

from text2sql.core.graph import Text2SQLWorkflow
from text2sql.core.models import EvalCase, ExecutionResult, SQLPlan
from text2sql.core.sample_data import create_sample_database
from text2sql.eval import EvaluationRunner


class _SequencedExecutor:
    """按预设序列返回执行结果的执行器替身；序列耗尽后重复最后一条。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def execute(self, sql, limit_rows: int = 1000):
        self.calls += 1
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


def _make_repaired_plan(sql: str):
    async def _repair(failed_sql, error, query, hits, relationships, context_block=""):
        return SQLPlan(sql, chart_type="kpi", reasoning="repaired by stub")

    return _repair


class WorkflowAndEvalTests(unittest.TestCase):
    def test_workflow_runs_end_to_end_with_sample_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/demo.db"
            create_sample_database(db_path)
            workflow = Text2SQLWorkflow(database_url_or_path=f"sqlite:///{db_path}", cache_dir=tmpdir)

            state = asyncio.run(workflow.run("按月份统计订单金额趋势，并计算环比增长率", "s1"))

        self.assertIsNone(state.get("clarification"))
        self.assertIn("LAG", state.get("generated_sql") or "")
        self.assertIsNotNone(state.get("execution_result"))
        self.assertFalse(state["execution_result"].error)
        self.assertEqual(state.get("chart_type"), "line")

    def test_evaluation_runner_supports_regression_cases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/demo.db"
            create_sample_database(db_path)
            workflow = Text2SQLWorkflow(database_url_or_path=f"sqlite:///{db_path}", cache_dir=tmpdir)
            case = EvalCase(
                case_id="growth",
                query="按月份统计订单金额趋势，并计算环比增长率",
                expected_tables=("orders",),
                required_sql_keywords=("lag", "over", "with"),
            )

            result = asyncio.run(EvaluationRunner(workflow).run_case(case))

        self.assertTrue(result.passed, result.errors)

    def test_sql_repair_recovers_on_second_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/demo.db"
            create_sample_database(db_path)
            workflow = Text2SQLWorkflow(
                database_url_or_path=f"sqlite:///{db_path}", cache_dir=tmpdir
            )
            # 首次执行报错、第二次成功；修复节点把 SQL 换成可成功执行的语句。
            workflow.executor = _SequencedExecutor(
                [
                    ExecutionResult(error="no such column: bogus"),
                    ExecutionResult(columns=("a",), rows=({"a": 1},), row_count=1),
                ]
            )
            workflow.sql_generator.aregenerate_with_error = _make_repaired_plan("SELECT 1 AS a")

            state = asyncio.run(workflow.run("按月份统计订单金额趋势", "s_repair"))

        self.assertEqual(state.get("attempts"), 1)
        self.assertIsNotNone(state.get("execution_result"))
        self.assertIsNone(state["execution_result"].error)
        self.assertEqual(state.get("generated_sql"), "SELECT 1 AS a")

    def test_sql_repair_degrades_after_max_retries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/demo.db"
            create_sample_database(db_path)
            workflow = Text2SQLWorkflow(
                database_url_or_path=f"sqlite:///{db_path}",
                cache_dir=tmpdir,
                sql_repair_max_retries=2,
            )
            # 每次执行都失败：达到重试上限后降级返回错误体 + 最近一次已生成 SQL。
            workflow.executor = _SequencedExecutor([ExecutionResult(error="boom")])
            workflow.sql_generator.aregenerate_with_error = _make_repaired_plan("SELECT bad_sql")

            state = asyncio.run(workflow.run("按月份统计订单金额趋势", "s_degrade"))

        self.assertEqual(state.get("attempts"), 2)
        self.assertIsNotNone(state["execution_result"].error)
        self.assertEqual(state.get("generated_sql"), "SELECT bad_sql")

    def test_sql_repair_emitted_over_stream_with_contract_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/demo.db"
            create_sample_database(db_path)
            workflow = Text2SQLWorkflow(
                database_url_or_path=f"sqlite:///{db_path}", cache_dir=tmpdir
            )
            workflow.executor = _SequencedExecutor(
                [
                    ExecutionResult(error="no such column: bogus"),
                    ExecutionResult(columns=("a",), rows=({"a": 1},), row_count=1),
                ]
            )
            workflow.sql_generator.aregenerate_with_error = _make_repaired_plan("SELECT 1 AS a")

            async def collect():
                events = []
                async for node_name, partial in workflow.astream("按月份统计订单金额趋势", "s_sse"):
                    events.append((node_name, partial))
                return events

            events = asyncio.run(collect())

        repair_events = [partial for node, partial in events if node == "sql_repair"]
        self.assertEqual(len(repair_events), 1)
        repair = repair_events[0]
        # 契约要求 sql_repair 事件 data 含 attempts / generated_sql / sql_plan。
        self.assertEqual(repair.get("attempts"), 1)
        self.assertEqual(repair.get("generated_sql"), "SELECT 1 AS a")
        self.assertIsNotNone(repair.get("sql_plan"))

    def test_ambiguous_workflow_returns_clarification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/demo.db"
            create_sample_database(db_path)
            workflow = Text2SQLWorkflow(database_url_or_path=f"sqlite:///{db_path}", cache_dir=tmpdir)

            state = asyncio.run(workflow.run("看一下情况", "s2"))

        self.assertIsNotNone(state.get("clarification"))
        self.assertIsNone(state.get("generated_sql"))


if __name__ == "__main__":
    unittest.main()

