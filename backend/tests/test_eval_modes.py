import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from text2sql.core.models import EvalCase, ExecutionResult, RetrievalHit, SQLPlan, TableInfo
from text2sql.eval import (
    FixedTableEvaluationRunner,
    TableRetrievalEvaluationRunner,
    checkpoint_report_path,
    load_cases,
    write_checkpoint_report,
)


class _StubRetriever:
    def __init__(self, hits):
        self.hits = hits
        self.calls = 0

    def retrieve(self, query: str, top_k: int = 6):
        self.calls += 1
        return self.hits[:top_k]


class _StubRelationshipResolver:
    def paths_for_tables(self, tables, max_depth: int = 4):
        return []


class _StubSQLGenerator:
    def __init__(self, sql: str):
        self.sql = sql
        self.prompt_hits = []

    def build_prompt(self, query, hits, relationships, context_block=""):
        self.prompt_hits = [hit.table.name for hit in hits]
        return "PROMPT"

    async def agenerate(self, query, hits, relationships, context_block="", prompt=None):
        return SQLPlan(self.sql, chart_type="kpi", reasoning="stub")


class _StubExecutor:
    async def execute(self, sql, limit_rows: int = 1000):
        return ExecutionResult(
            columns=("metric_value",),
            rows=({"metric_value": 1},),
            row_count=1,
        )


class _StubWorkflow:
    def __init__(self):
        self.tables = [TableInfo("orders"), TableInfo("customers"), TableInfo("noise")]
        self.retriever = _StubRetriever(
            [
                RetrievalHit(TableInfo("customers"), 0.9),
                RetrievalHit(TableInfo("orders"), 0.8),
                RetrievalHit(TableInfo("noise"), 0.1),
            ]
        )
        self.relationship_resolver = _StubRelationshipResolver()
        self.sql_generator = _StubSQLGenerator("SELECT 1 AS metric_value")
        self.executor = _StubExecutor()


class EvalModeTests(unittest.TestCase):
    def test_load_cases_reads_fixed_tables(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cases.jsonl"
            path.write_text(
                '{"case_id":"c1","query":"q","expected_tables":["orders"],'
                '"fixed_tables":["orders","customers"]}\n',
                encoding="utf-8",
            )

            cases = load_cases(path)

        self.assertEqual(cases[0].fixed_tables, ("orders", "customers"))

    def test_retrieval_runner_scores_table_metrics_without_sql_generation(self):
        workflow = _StubWorkflow()
        case = EvalCase(
            case_id="tables",
            query="按客户统计订单金额",
            expected_tables=("orders", "customers"),
        )

        result = asyncio.run(TableRetrievalEvaluationRunner(workflow, top_k=3).run_case(case))

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(workflow.retriever.calls, 1)
        self.assertEqual(result.metrics["table_recall"], 1.0)
        self.assertAlmostEqual(result.metrics["table_precision"], 2 / 3)
        self.assertEqual(result.metrics["table_accuracy"], 1.0)
        self.assertIsNone(result.generated_sql)
        self.assertEqual(result.trace["mode"], "retrieval")

    def test_runner_progress_reports_case_start_and_status(self):
        workflow = _StubWorkflow()
        case = EvalCase(
            case_id="tables",
            query="按客户统计订单金额",
            expected_tables=("orders", "customers"),
        )
        stream = io.StringIO()

        with redirect_stderr(stream):
            results = asyncio.run(
                TableRetrievalEvaluationRunner(workflow, top_k=3, progress=True).run([case])
            )

        self.assertTrue(results[0].passed, results[0].errors)
        output = stream.getvalue()
        self.assertIn("[eval] retrieval 1/1 tables start", output)
        self.assertIn("[eval] retrieval 1/1 tables PASS", output)

    def test_runner_after_case_callback_can_write_checkpoint(self):
        workflow = _StubWorkflow()
        cases = [
            EvalCase(
                case_id="tables_1",
                query="按客户统计订单金额",
                expected_tables=("orders", "customers"),
            ),
            EvalCase(
                case_id="tables_2",
                query="按客户统计订单金额",
                expected_tables=("orders", "customers"),
            ),
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "eval.json.checkpoint.json"

            def after_case(results, _result, completed, total):
                write_checkpoint_report(
                    path,
                    results,
                    {"mode": "retrieval"},
                    completed=completed,
                    total=total,
                )

            results = asyncio.run(
                TableRetrievalEvaluationRunner(workflow, top_k=3).run(
                    cases,
                    after_case=after_case,
                )
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(results), 2)
        self.assertEqual(payload["metadata"]["checkpoint"], True)
        self.assertEqual(payload["metadata"]["completed"], 2)
        self.assertEqual(payload["metadata"]["total"], 2)
        self.assertEqual(payload["summary"]["total"], 2)

    def test_checkpoint_report_path_defaults_next_to_report(self):
        self.assertEqual(
            checkpoint_report_path("reports/eval.json"),
            Path("reports/eval.json.checkpoint.json"),
        )

    def test_fixed_table_runner_uses_fixed_tables_for_generation_accuracy(self):
        workflow = _StubWorkflow()
        case = EvalCase(
            case_id="fixed",
            query="总订单数",
            expected_tables=("orders",),
            fixed_tables=("orders",),
            expected_result=({"metric_value": 1},),
        )

        result = asyncio.run(FixedTableEvaluationRunner(workflow).run_case(case))

        self.assertTrue(result.passed, result.errors)
        self.assertEqual(workflow.sql_generator.prompt_hits, ["orders"])
        self.assertEqual(result.metrics["execution_success"], 1.0)
        self.assertEqual(result.metrics["llm_generation_accuracy"], 1.0)
        self.assertEqual(result.trace["mode"], "fixed-tables")


if __name__ == "__main__":
    unittest.main()
