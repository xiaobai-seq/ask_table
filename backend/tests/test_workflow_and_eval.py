import asyncio
import tempfile
import unittest

from text2sql.core.graph import Text2SQLWorkflow
from text2sql.core.models import EvalCase
from text2sql.core.sample_data import create_sample_database
from text2sql.eval import EvaluationRunner


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

