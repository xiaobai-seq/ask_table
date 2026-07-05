import unittest

from text2sql.persistence.repository import HistoryRecord, InMemoryHistoryRepository

try:  # FastAPI/TestClient 缺失时整组端点测试优雅 skip。
    from fastapi.testclient import TestClient

    import text2sql.api as api_module

    _HAS_FASTAPI = api_module.FastAPI is not None
except Exception:
    _HAS_FASTAPI = False


@unittest.skipUnless(_HAS_FASTAPI, "FastAPI/TestClient not installed")
class ApiEndpointsTest(unittest.TestCase):
    def _client(self):
        # 不使用 with 上下文：避免触发 startup 覆盖我们注入的内存 repository。
        app = api_module.create_app()
        repo = InMemoryHistoryRepository()
        repo.add_turn(
            HistoryRecord(
                session_id="s1",
                user_query="按月份统计订单金额趋势",
                rewritten_query="按月份统计订单金额趋势",
                generated_sql="SELECT 1",
                tables=["orders"],
                summary="摘要",
                chart_type="line",
                row_count=12,
                elapsed_ms=34.5,
                trace_id="trace-1",
                status="success",
                render_spec={"chart_type": "line", "x": "period", "y": ["metric_value"]},
                execution_result={"columns": ["period"], "rows": [], "row_count": 0, "elapsed_ms": 1.0, "error": None},
            )
        )
        repo.add_turn(HistoryRecord(session_id="s2", user_query="各地区分布", generated_sql="SELECT 2", tables=["customers"]))
        app.state.history_repository = repo
        return TestClient(app), repo

    def test_healthz(self):
        client, _ = self._client()
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_config_shape(self):
        client, _ = self._client()
        response = client.get("/config")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            set(payload),
            {"domain_profile", "description", "example_queries", "clarification_options"},
        )
        self.assertIn("按月份统计订单金额趋势", payload["example_queries"])

    def test_list_sessions_shape(self):
        client, _ = self._client()
        response = client.get("/sessions")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("sessions", payload)
        sessions = {s["session_id"]: s for s in payload["sessions"]}
        self.assertEqual(set(sessions), {"s1", "s2"})
        s1 = sessions["s1"]
        self.assertEqual(set(s1), {"session_id", "title", "created_at", "updated_at", "turn_count"})
        self.assertEqual(s1["title"], "按月份统计订单金额趋势")
        self.assertEqual(s1["turn_count"], 1)
        # created_at 应为 ISO8601 字符串。
        self.assertIsInstance(s1["created_at"], str)
        self.assertIn("T", s1["created_at"])

    def test_session_history_shape(self):
        client, _ = self._client()
        response = client.get("/sessions/s1/history")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["session_id"], "s1")
        self.assertEqual(len(payload["history"]), 1)
        item = payload["history"][0]
        for key in (
            "id", "user_query", "rewritten_query", "generated_sql", "tables",
            "summary", "chart_type", "row_count", "elapsed_ms", "trace_id", "status", "created_at",
        ):
            self.assertIn(key, item)
        self.assertEqual(item["tables"], ["orders"])
        self.assertEqual(item["chart_type"], "line")

    def test_history_detail_includes_render_and_execution(self):
        client, repo = self._client()
        history_id = repo.get_session_history("s1")[0].id
        response = client.get(f"/history/{history_id}")
        self.assertEqual(response.status_code, 200)
        detail = response.json()
        self.assertEqual(detail["id"], history_id)
        self.assertEqual(detail["session_id"], "s1")
        self.assertEqual(detail["render_spec"]["chart_type"], "line")
        self.assertIn("execution_result", detail)

    def test_history_detail_404(self):
        client, _ = self._client()
        response = client.get("/history/999999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(set(response.json()), {"code", "message", "trace_id"})

    def test_delete_session(self):
        client, _ = self._client()
        response = client.delete("/sessions/s1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"session_id": "s1", "deleted": True})
        # 再删返回 404。
        self.assertEqual(client.delete("/sessions/s1").status_code, 404)

    def test_delete_history(self):
        client, repo = self._client()
        history_id = repo.get_session_history("s1")[0].id
        response = client.delete(f"/history/{history_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": history_id, "deleted": True})
        self.assertEqual(client.delete(f"/history/{history_id}").status_code, 404)

    def test_cors_header_present(self):
        client, _ = self._client()
        response = client.get("/healthz", headers={"Origin": "http://example.com"})
        self.assertIn("access-control-allow-origin", {k.lower() for k in response.headers})


if __name__ == "__main__":
    unittest.main()
