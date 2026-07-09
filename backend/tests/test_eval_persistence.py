import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from text2sql.core.models import EvalResult
from text2sql.eval import (
    _external_ai_eval_services,
    _build_eval_run_repository,
    persist_eval_run,
    summarize_results,
    validate_external_ai_eval_consent,
    validate_external_llm_eval_consent,
)
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


class ExternalAIEvalConsentTests(unittest.TestCase):
    def _settings(self, **overrides):
        values = {
            "use_llm": True,
            "dashscope_api_key": "test-key",
            "dashscope_http_base_url": "https://example.invalid/api/v1",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_external_ai_eval_requires_explicit_consent(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "external AI services"):
                validate_external_ai_eval_consent(self._settings(), mode="e2e")

    def test_external_ai_eval_allows_explicit_flag(self):
        with patch.dict("os.environ", {}, clear=True):
            validate_external_ai_eval_consent(
                self._settings(),
                mode="e2e",
                allow_external_ai_eval=True,
            )

    def test_external_ai_eval_allows_env_flag(self):
        with patch.dict("os.environ", {"TEXT2SQL_ALLOW_EXTERNAL_AI_EVAL": "1"}, clear=True):
            validate_external_ai_eval_consent(self._settings(), mode="fixed-tables")

    def test_external_ai_eval_keeps_legacy_llm_flag_alias(self):
        with patch.dict("os.environ", {}, clear=True):
            validate_external_llm_eval_consent(
                self._settings(),
                mode="e2e",
                allow_external_llm_eval=True,
            )

    def test_retrieval_mode_requires_consent_for_embedding_and_rerank(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "embedding"):
                validate_external_ai_eval_consent(self._settings(), mode="retrieval")

    def test_disabled_llm_still_requires_consent_for_embedding_and_rerank(self):
        with patch.dict("os.environ", {}, clear=True):
            services = _external_ai_eval_services(self._settings(use_llm=False), mode="e2e")
            self.assertEqual({service["name"] for service in services}, {"embedding", "rerank"})
            with self.assertRaisesRegex(RuntimeError, "rerank"):
                validate_external_ai_eval_consent(self._settings(use_llm=False), mode="e2e")

    def test_external_ai_eval_allows_no_api_key_and_local_llm_endpoint(self):
        with patch.dict("os.environ", {}, clear=True):
            validate_external_ai_eval_consent(
                self._settings(
                    dashscope_api_key=None,
                    dashscope_http_base_url="http://127.0.0.1:8000/v1",
                ),
                mode="e2e",
            )


if __name__ == "__main__":
    unittest.main()
