import unittest

from text2sql.persistence.repository import HistoryRecord, InMemoryHistoryRepository

try:
    import sqlalchemy  # noqa: F401

    _HAS_SQLALCHEMY = True
except Exception:
    _HAS_SQLALCHEMY = False


class HistoryRepositoryContract:
    """两种 repository 实现共享的行为契约（内存 / SQLAlchemy 行为必须一致）。"""

    def make_repo(self):  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def _seed(self, repo):
        repo.add_turn(
            HistoryRecord(
                session_id="s1",
                user_query="按月份统计订单金额趋势",
                rewritten_query="按月份统计订单金额趋势",
                generated_sql="SELECT 1",
                tables=["orders"],
                summary="一段摘要",
                chart_type="line",
                row_count=12,
                elapsed_ms=34.5,
                trace_id="trace-1",
                status="success",
                render_spec={"chart_type": "line"},
                execution_result={"columns": ["m"], "rows": []},
            )
        )
        repo.add_turn(
            HistoryRecord(
                session_id="s1",
                user_query="再看环比",
                generated_sql="SELECT 2",
                tables=["orders"],
                chart_type="line",
            )
        )
        repo.add_turn(
            HistoryRecord(
                session_id="s2",
                user_query="各地区客户分布",
                generated_sql="SELECT 3",
                tables=["customers"],
                chart_type="bar",
            )
        )

    def test_add_returns_record_with_id(self):
        repo = self.make_repo()
        saved = repo.add_turn(HistoryRecord(session_id="s1", user_query="q"))
        self.assertIsNotNone(saved.id)

    def test_get_session_history_in_time_order(self):
        repo = self.make_repo()
        self._seed(repo)
        history = repo.get_session_history("s1")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].user_query, "按月份统计订单金额趋势")
        self.assertEqual(history[1].user_query, "再看环比")
        self.assertEqual(history[0].tables, ["orders"])

    def test_list_sessions_with_counts(self):
        repo = self.make_repo()
        self._seed(repo)
        sessions = repo.list_sessions()
        by_id = {s.session_id: s for s in sessions}
        self.assertEqual(set(by_id), {"s1", "s2"})
        self.assertEqual(by_id["s1"].turn_count, 2)
        self.assertEqual(by_id["s2"].turn_count, 1)
        # title 取会话首轮问题。
        self.assertEqual(by_id["s1"].title, "按月份统计订单金额趋势")

    def test_get_history_detail_carries_full_payload(self):
        repo = self.make_repo()
        self._seed(repo)
        first = repo.get_session_history("s1")[0]
        detail = repo.get_history(first.id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail.render_spec, {"chart_type": "line"})
        self.assertEqual(detail.execution_result, {"columns": ["m"], "rows": []})

    def test_delete_session_removes_history(self):
        repo = self.make_repo()
        self._seed(repo)
        self.assertTrue(repo.delete_session("s1"))
        self.assertEqual(repo.get_session_history("s1"), [])
        self.assertEqual({s.session_id for s in repo.list_sessions()}, {"s2"})
        # 重复删除返回 False。
        self.assertFalse(repo.delete_session("s1"))

    def test_delete_single_history(self):
        repo = self.make_repo()
        self._seed(repo)
        target = repo.get_session_history("s1")[0]
        self.assertTrue(repo.delete_history(target.id))
        remaining = repo.get_session_history("s1")
        self.assertEqual(len(remaining), 1)
        self.assertFalse(repo.delete_history(999999))

    def test_delete_last_history_keeps_session(self):
        # 删光某会话的所有历史后仍保留会话元信息：turn_count 归零、title 保留。
        repo = self.make_repo()
        self._seed(repo)
        for record in repo.get_session_history("s1"):
            self.assertTrue(repo.delete_history(record.id))
        self.assertEqual(repo.get_session_history("s1"), [])
        by_id = {s.session_id: s for s in repo.list_sessions()}
        self.assertIn("s1", by_id)
        self.assertEqual(by_id["s1"].turn_count, 0)
        self.assertEqual(by_id["s1"].title, "按月份统计订单金额趋势")


class InMemoryHistoryRepositoryTests(HistoryRepositoryContract, unittest.TestCase):
    def make_repo(self):
        return InMemoryHistoryRepository()


@unittest.skipUnless(_HAS_SQLALCHEMY, "SQLAlchemy not installed")
class SqlAlchemyHistoryRepositoryTests(HistoryRepositoryContract, unittest.TestCase):
    def make_repo(self):
        from text2sql.persistence.db import create_metadata_engine, create_session_factory, init_models
        from text2sql.persistence.repository import SqlAlchemyHistoryRepository

        engine = create_metadata_engine("sqlite://")
        init_models(engine)
        return SqlAlchemyHistoryRepository(create_session_factory(engine))


class ConversationMemoryRepositoryTests(unittest.TestCase):
    def test_memory_persists_turns_and_supports_rewrite(self):
        from text2sql.core.context import ConversationMemory
        from text2sql.core.models import ConversationTurn

        repo = InMemoryHistoryRepository()
        memory = ConversationMemory(repository=repo)
        memory.add_turn(
            "s1",
            ConversationTurn(
                user_query="按月份统计订单金额",
                rewritten_query="按月份统计订单金额",
                generated_sql="SELECT month, SUM(total_amount) FROM orders GROUP BY month",
                tables=("orders",),
            ),
        )
        # 既写入 repository，又能驱动追问改写。
        self.assertEqual(len(repo.get_session_history("s1")), 1)
        rewritten = memory.rewrite_query("s1", "再看环比")
        self.assertIn("上一轮问题", rewritten)
        self.assertIn("orders", rewritten)


if __name__ == "__main__":
    unittest.main()
