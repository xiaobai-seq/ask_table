import unittest

from text2sql.persistence.repository import (
    EvalCaseResultRecord,
    InMemoryEvalRunRepository,
)

try:
    import sqlalchemy  # noqa: F401

    _HAS_SQLALCHEMY = True
except Exception:
    _HAS_SQLALCHEMY = False


def _record(run_id: int, case_id: str = "c1") -> EvalCaseResultRecord:
    record = EvalCaseResultRecord(
        run_id=run_id,
        case_id=case_id,
        query="各品类销售额",
        rewritten_query="各品类销售额",
        passed=True,
        retrieval_hits=[{"table": "order_items", "score": 0.9}],
        table_relationship=[{"source": "order_items", "target": "skus"}],
        few_shot_examples=[{"question": "q", "sql": "SELECT 1"}],
        prompt="PROMPT_X",
        generated_sql="SELECT 1",
        execution_rows=[{"dimension_value": "手机", "metric_value": 100}],
        row_count=1,
        clarification=None,
        metrics={"table_recall": 1.0, "value_set_exact": 1.0},
        errors=[],
    )
    record.metrics["sql_generation"] = {
        "quality_gate_issues": ["issue A"],
        "quality_gate_repaired": True,
    }
    return record


class EvalCaseResultRepositoryContract:
    """eval_case_results 两实现共享契约：按 run 落逐 case trace 并可回查。"""

    def make_repo(self):  # pragma: no cover - 子类实现
        raise NotImplementedError

    def _new_run_id(self, repo) -> int:
        run = repo.record_run(total=1, passed=1, pass_rate=1.0, metrics={})
        return run.id

    def test_record_and_list_case_results_by_run(self):
        repo = self.make_repo()
        run_id = self._new_run_id(repo)
        repo.record_case_result(_record(run_id, "c1"))
        repo.record_case_result(_record(run_id, "c2"))

        rows = repo.list_case_results(run_id)
        self.assertEqual(len(rows), 2)
        by_case = {row.case_id: row for row in rows}
        self.assertEqual(by_case["c1"].generated_sql, "SELECT 1")
        # JSON 结构化字段应原样回读。
        self.assertEqual(by_case["c1"].retrieval_hits[0]["table"], "order_items")
        self.assertEqual(by_case["c1"].metrics["value_set_exact"], 1.0)
        self.assertEqual(by_case["c1"].metrics["sql_generation"]["quality_gate_issues"], ["issue A"])
        self.assertTrue(by_case["c1"].metrics["sql_generation"]["quality_gate_repaired"])
        self.assertTrue(by_case["c1"].passed)
        self.assertIsNotNone(by_case["c1"].id)

    def test_list_case_results_scoped_to_run(self):
        repo = self.make_repo()
        run_a = self._new_run_id(repo)
        run_b = self._new_run_id(repo)
        repo.record_case_result(_record(run_a, "only_a"))
        repo.record_case_result(_record(run_b, "only_b"))

        self.assertEqual([r.case_id for r in repo.list_case_results(run_a)], ["only_a"])
        self.assertEqual([r.case_id for r in repo.list_case_results(run_b)], ["only_b"])


class InMemoryEvalCaseResultRepositoryTests(
    EvalCaseResultRepositoryContract, unittest.TestCase
):
    def make_repo(self):
        return InMemoryEvalRunRepository()


@unittest.skipUnless(_HAS_SQLALCHEMY, "SQLAlchemy not installed")
class SqlAlchemyEvalCaseResultRepositoryTests(
    EvalCaseResultRepositoryContract, unittest.TestCase
):
    def make_repo(self):
        from text2sql.persistence.db import (
            create_metadata_engine,
            create_session_factory,
            init_models,
        )
        from text2sql.persistence.repository import SqlAlchemyEvalRunRepository

        engine = create_metadata_engine("sqlite://")
        init_models(engine)
        return SqlAlchemyEvalRunRepository(create_session_factory(engine))


if __name__ == "__main__":
    unittest.main()
