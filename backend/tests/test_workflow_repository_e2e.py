import asyncio
import tempfile
import unittest

from text2sql.core.context import ConversationMemory
from text2sql.core.graph import Text2SQLWorkflow
from text2sql.core.sample_data import create_sample_database
from text2sql.persistence.repository import InMemoryHistoryRepository


class WorkflowRepositoryE2ETest(unittest.TestCase):
    def test_completed_turn_is_persisted_and_queryable(self):
        repo = InMemoryHistoryRepository()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/demo.db"
            create_sample_database(db_path)
            workflow = Text2SQLWorkflow(
                database_url_or_path=f"sqlite:///{db_path}",
                cache_dir=tmpdir,
                memory=ConversationMemory(repository=repo),
            )
            asyncio.run(workflow.run("按月份统计订单金额趋势", "sess-e2e"))

        # 跑完一轮后，repository 应能查到该会话与该轮历史（workflow 已落库）。
        sessions = {s.session_id: s for s in repo.list_sessions()}
        self.assertIn("sess-e2e", sessions)
        self.assertEqual(sessions["sess-e2e"].turn_count, 1)

        history = repo.get_session_history("sess-e2e")
        self.assertEqual(len(history), 1)
        turn = history[0]
        self.assertEqual(turn.user_query, "按月份统计订单金额趋势")
        self.assertIn("orders", turn.tables)
        self.assertTrue(turn.generated_sql)
        self.assertTrue(turn.trace_id)
        # 详情应保留可回看的渲染建议与执行结果。
        detail = repo.get_history(turn.id)
        self.assertIsNotNone(detail.render_spec)
        self.assertIsNotNone(detail.execution_result)


if __name__ == "__main__":
    unittest.main()
