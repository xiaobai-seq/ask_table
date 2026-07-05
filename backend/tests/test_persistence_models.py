import unittest

try:  # SQLAlchemy 为可选依赖：缺失时整组 ORM 测试优雅 skip（保持离线可跑）。
    import sqlalchemy  # noqa: F401

    _HAS_SQLALCHEMY = True
except Exception:
    _HAS_SQLALCHEMY = False


@unittest.skipUnless(_HAS_SQLALCHEMY, "SQLAlchemy not installed")
class PersistenceModelTests(unittest.TestCase):
    def _factory(self):
        from text2sql.persistence.db import create_metadata_engine, create_session_factory, init_models

        # 纯内存 SQLite：create_metadata_engine 会用 StaticPool 让多连接共享同一库。
        engine = create_metadata_engine("sqlite://")
        init_models(engine)
        return create_session_factory(engine)

    def test_create_and_read_query_history(self):
        from text2sql.persistence.models import QueryHistory, Session

        factory = self._factory()
        with factory() as session:
            session.add(Session(session_id="demo", title="按月统计"))
            session.add(
                QueryHistory(
                    session_id="demo",
                    user_query="按月份统计订单金额趋势",
                    rewritten_query="按月份统计订单金额趋势",
                    generated_sql="SELECT 1",
                    tables=["orders"],
                    summary="ok",
                    chart_type="line",
                    row_count=12,
                    elapsed_ms=34.5,
                    trace_id="trace-1",
                    status="success",
                )
            )
            session.commit()

        with factory() as session:
            rows = session.query(QueryHistory).all()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row.session_id, "demo")
            # JSON 列应原样回读为 Python list。
            self.assertEqual(row.tables, ["orders"])
            self.assertEqual(row.chart_type, "line")
            self.assertIsNotNone(row.id)

    def test_all_tables_are_registered(self):
        from text2sql.persistence.db import Base

        expected = {
            "sessions",
            "query_history",
            "few_shot_examples",
            "schema_metadata",
            "eval_runs",
            "eval_case_results",
        }
        self.assertTrue(expected.issubset(set(Base.metadata.tables)))


if __name__ == "__main__":
    unittest.main()
