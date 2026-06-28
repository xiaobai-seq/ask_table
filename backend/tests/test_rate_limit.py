import unittest

from text2sql.api.rate_limit import InMemoryRateLimiter, build_rate_limiter
from text2sql.config import Settings

try:  # FastAPI/TestClient 可能未安装，中间件集成测试据此优雅 skip。
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from text2sql.api.rate_limit import RateLimitMiddleware

    _HAS_FASTAPI = RateLimitMiddleware is not None
except Exception:
    _HAS_FASTAPI = False


class InMemoryRateLimiterTests(unittest.TestCase):
    def test_allows_up_to_capacity_then_blocks(self):
        # 固定时钟：不发生补充，capacity 个请求后即拒绝。
        limiter = InMemoryRateLimiter(3, time_func=lambda: 0.0)
        self.assertTrue(limiter.allow("k"))
        self.assertTrue(limiter.allow("k"))
        self.assertTrue(limiter.allow("k"))
        self.assertFalse(limiter.allow("k"))

    def test_non_positive_rate_disables_limiting(self):
        limiter = InMemoryRateLimiter(0)
        self.assertTrue(all(limiter.allow("k") for _ in range(100)))

    def test_keys_are_independent(self):
        limiter = InMemoryRateLimiter(1, time_func=lambda: 0.0)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))
        self.assertFalse(limiter.allow("a"))

    def test_tokens_refill_over_time(self):
        clock = {"t": 0.0}
        limiter = InMemoryRateLimiter(60, time_func=lambda: clock["t"])
        for _ in range(60):
            self.assertTrue(limiter.allow("k"))
        self.assertFalse(limiter.allow("k"))
        # 过去 1 秒应补充约 1 个令牌（60/分钟）。
        clock["t"] = 1.0
        self.assertTrue(limiter.allow("k"))

    def test_build_falls_back_to_memory_without_redis(self):
        settings = Settings()  # 默认 redis_url 为 None
        limiter = build_rate_limiter(settings)
        self.assertIsInstance(limiter, InMemoryRateLimiter)


class _ExplodingScript:
    def __call__(self, *args, **kwargs):
        raise RuntimeError("redis down")


class _FakeRedis:
    def register_script(self, lua):
        return _ExplodingScript()


class RedisRateLimiterFailPolicyTests(unittest.TestCase):
    """Redis 运行期异常时按 fail_open 配置放行/拒绝。"""

    def test_default_setting_is_fail_open(self):
        self.assertTrue(Settings().rate_limit_fail_open)

    def test_fail_open_allows_on_error(self):
        from text2sql.api.rate_limit import RedisRateLimiter

        limiter = RedisRateLimiter(_FakeRedis(), rate_per_minute=1, fail_open=True)
        self.assertTrue(limiter.allow("k"))

    def test_fail_closed_blocks_on_error(self):
        from text2sql.api.rate_limit import RedisRateLimiter

        limiter = RedisRateLimiter(_FakeRedis(), rate_per_minute=1, fail_open=False)
        self.assertFalse(limiter.allow("k"))


@unittest.skipUnless(_HAS_FASTAPI, "FastAPI/TestClient not installed")
class RateLimitMiddlewareTests(unittest.TestCase):
    def _client(self, rate: int):
        app = FastAPI()
        app.add_middleware(
            RateLimitMiddleware,
            limiter=InMemoryRateLimiter(rate, time_func=lambda: 0.0),
        )

        @app.get("/ping")
        async def ping():
            return {"ok": True}

        @app.get("/healthz")
        async def healthz():
            return {"status": "ok"}

        return TestClient(app)

    def test_returns_429_with_standard_error_body_over_limit(self):
        client = self._client(rate=2)
        self.assertEqual(client.get("/ping").status_code, 200)
        self.assertEqual(client.get("/ping").status_code, 200)
        blocked = client.get("/ping")
        self.assertEqual(blocked.status_code, 429)
        body = blocked.json()
        self.assertEqual(set(body), {"code", "message", "trace_id"})
        self.assertEqual(body["code"], "rate_limited")
        self.assertTrue(body["trace_id"])

    def test_healthz_is_exempt_from_rate_limit(self):
        client = self._client(rate=1)
        for _ in range(5):
            self.assertEqual(client.get("/healthz").status_code, 200)


@unittest.skipUnless(_HAS_FASTAPI, "FastAPI/TestClient not installed")
class QueryEndpointRateLimitTests(unittest.TestCase):
    """POST /query 必须按 session_id（body）限流，而非退化为按 IP。"""

    def _client(self, rate: int):
        import text2sql.api as api_module

        app = api_module.create_app()
        # 注入固定时钟限流器；不使用 with，避免触发 startup。
        app.state.rate_limiter = InMemoryRateLimiter(rate, time_func=lambda: 0.0)
        return TestClient(app)

    def test_query_limited_per_session_not_per_ip(self):
        client = self._client(rate=2)
        # 同一 IP、session "a"：配额内不应 429（workflow 未初始化会返回 503，但绝非 429）。
        for _ in range(2):
            response = client.post("/query", json={"query": "q", "session_id": "a"})
            self.assertNotEqual(response.status_code, 429)
        # session "a" 超过配额 → 429 + 标准错误体。
        blocked = client.post("/query", json={"query": "q", "session_id": "a"})
        self.assertEqual(blocked.status_code, 429)
        body = blocked.json()
        self.assertEqual(body["code"], "rate_limited")
        self.assertEqual(set(body), {"code", "message", "trace_id"})
        # 同一 IP、不同 session "b"：配额独立计数，不应被 a 的用量影响。
        other = client.post("/query", json={"query": "q", "session_id": "b"})
        self.assertNotEqual(other.status_code, 429)


if __name__ == "__main__":
    unittest.main()
