import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from text2sql.core.models import EvalResult
from text2sql.eval import _build_eval_run_repository, persist_eval_run, summarize_results
from text2sql.persistence.repository import InMemoryEvalRunRepository

try:
    import sqlalchemy  # noqa: F401

    _HAS_SQLALCHEMY = True
except Exception:
    _HAS_SQLALCHEMY = False


def _results() -> list[EvalResult]:
    return [
        EvalResult("c1", True, {"table_recall": 1.0, "execution_success": 1.0}, "SELECT 1"),
        EvalResult("c2", False, {"table_recall": 0.5, "execution_success": 1.0}, "SELECT 2"),
    ]


class EvalRunRepositoryContract:
    """eval_runs 两实现共享契约：可记录多次运行并按时间倒序回看。"""

    def make_repo(self):  # pragma: no cover - 子类实现
        raise NotImplementedError

    def test_record_and_list_runs(self):
        repo = self.make_repo()
        first = repo.record_run(total=2, passed=1, pass_rate=0.5, metrics={"a": 1.0})
        self.assertIsNotNone(first.id)
        second = repo.record_run(total=3, passed=3, pass_rate=1.0, metrics={"a": 0.9})
        runs = repo.list_runs()
        self.assertEqual(len(runs), 2)
        # 最近一次运行排在最前，支持多次对比/趋势。
        self.assertEqual(runs[0].id, second.id)
        self.assertEqual(runs[0].metrics, {"a": 0.9})
        self.assertEqual(runs[1].id, first.id)


class InMemoryEvalRunRepositoryTests(EvalRunRepositoryContract, unittest.TestCase):
    def make_repo(self):
        return InMemoryEvalRunRepository()


@unittest.skipUnless(_HAS_SQLALCHEMY, "SQLAlchemy not installed")
class SqlAlchemyEvalRunRepositoryTests(EvalRunRepositoryContract, unittest.TestCase):
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


class PersistEvalRunTests(unittest.TestCase):
    def test_summarize_results(self):
        summary = summarize_results(_results())
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["passed"], 1)
        self.assertAlmostEqual(summary["pass_rate"], 0.5)
        self.assertIn("table_recall", summary["metrics"])

    def test_persist_eval_run_writes_summary(self):
        repo = InMemoryEvalRunRepository()
        record = persist_eval_run(repo, _results())
        self.assertEqual(record.total, 2)
        self.assertEqual(record.passed, 1)
        self.assertAlmostEqual(record.pass_rate, 0.5)
        self.assertIn("table_recall", record.metrics)
        self.assertEqual(repo.list_runs()[0].id, record.id)


class EvalRunRepositoryBuilderTests(unittest.TestCase):
    def test_builder_requires_metadata_database_by_default(self):
        settings = SimpleNamespace(metadata_database_url="")

        with self.assertRaisesRegex(RuntimeError, "metadata database"):
            _build_eval_run_repository(settings)

    def test_builder_allows_inmemory_only_when_explicit(self):
        settings = SimpleNamespace(metadata_database_url="")

        repo = _build_eval_run_repository(settings, allow_inmemory=True)

        self.assertIsInstance(repo, InMemoryEvalRunRepository)

    @unittest.skipUnless(_HAS_SQLALCHEMY, "SQLAlchemy not installed")
    def test_builder_uses_sqlalchemy_repository_for_metadata_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings = SimpleNamespace(
                metadata_database_url=f"sqlite:///{Path(tmpdir) / 'metadata.db'}"
            )

            repo = _build_eval_run_repository(settings)
            record = repo.record_run(total=1, passed=1, pass_rate=1.0, metrics={"ok": 1.0})

        self.assertEqual(record.passed, 1)
        self.assertEqual(record.metrics, {"ok": 1.0})


if __name__ == "__main__":
    unittest.main()
