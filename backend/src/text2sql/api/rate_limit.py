from __future__ import annotations

"""限流（令牌桶）。

按 session/IP 维度做令牌桶限流：
- `InMemoryRateLimiter`：进程内令牌桶，零外部依赖，作为默认与降级实现；
- `RedisRateLimiter`：用 Redis + Lua 原子脚本做跨进程限流（配置 redis_url 时启用）。

`build_rate_limiter` 依据 Settings 选择实现：配置了可用 Redis 用 Redis，否则降级内存，
保证「缺 Redis 可离线跑」。`RateLimitMiddleware` 是 FastAPI/Starlette 中间件，超阈值返回
429 + 标准错误体（code=rate_limited）；FastAPI 缺失时该类为 None。
"""

import logging
import time
from typing import Callable, Protocol

from text2sql.api.errors import build_error

logger = logging.getLogger(__name__)


class RateLimiter(Protocol):
    """限流器接口：allow 返回本次请求是否放行。"""

    def allow(self, key: str) -> bool: ...


class InMemoryRateLimiter:
    """进程内令牌桶：容量 = 每分钟配额，按时间线性补充。

    rate <= 0 视为关闭限流（始终放行），便于测试或显式禁用。
    """

    def __init__(self, rate_per_minute: int, time_func: Callable[[], float] = time.monotonic) -> None:
        self.capacity = float(rate_per_minute)
        self.refill_per_sec = rate_per_minute / 60.0
        self._time = time_func
        # key -> (剩余令牌, 上次刷新时间)
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        if self.capacity <= 0:
            return True
        now = self._time()
        tokens, last = self._buckets.get(key, (self.capacity, now))
        # 按经过时间线性补充，封顶到容量。
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_sec)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True
        self._buckets[key] = (tokens, now)
        return False


# Redis 令牌桶的原子实现：在一次 EVAL 中完成「补充 + 判定 + 扣减」，避免并发竞态。
_REDIS_TOKEN_BUCKET_LUA = """
local tokens_key = KEYS[1]
local ts_key = KEYS[2]
local capacity = tonumber(ARGV[1])
local refill_per_sec = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local tokens = tonumber(redis.call('get', tokens_key))
local last = tonumber(redis.call('get', ts_key))
if tokens == nil then tokens = capacity end
if last == nil then last = now end
local delta = math.max(0, now - last)
tokens = math.min(capacity, tokens + delta * refill_per_sec)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('set', tokens_key, tokens)
redis.call('set', ts_key, now)
return allowed
"""


class RedisRateLimiter:
    """基于 Redis 的跨进程令牌桶。

    fail_open 控制 Redis 运行期异常时的策略：True 放行（保可用性，默认），False 拒绝（保后端）。
    """

    def __init__(self, client, rate_per_minute: int, fail_open: bool = True) -> None:
        self.client = client
        self.capacity = float(rate_per_minute)
        self.refill_per_sec = rate_per_minute / 60.0
        self.fail_open = fail_open
        self._script = client.register_script(_REDIS_TOKEN_BUCKET_LUA)

    def allow(self, key: str) -> bool:
        if self.capacity <= 0:
            return True
        try:
            allowed = self._script(
                keys=[f"rl:{key}:tokens", f"rl:{key}:ts"],
                args=[self.capacity, self.refill_per_sec, time.time()],
            )
            return bool(allowed)
        except Exception as exc:
            # Redis 故障时按配置 fail-open/fail-closed，并告警便于发现底层异常。
            logger.warning(
                "redis rate limiter failed (fail_open=%s): %s", self.fail_open, exc
            )
            return self.fail_open


def build_rate_limiter(settings) -> RateLimiter:
    """按配置构建限流器：可用 Redis 优先，否则降级内存。"""

    rate = settings.rate_limit_per_minute
    fail_open = getattr(settings, "rate_limit_fail_open", True)
    redis_url = getattr(settings, "redis_url", None)
    if redis_url:
        limiter = _try_build_redis_limiter(redis_url, rate, fail_open)
        if limiter is not None:
            return limiter
    return InMemoryRateLimiter(rate)


def _try_build_redis_limiter(redis_url: str, rate: int, fail_open: bool) -> RateLimiter | None:
    """尝试连接 Redis 并构建限流器；不可用时返回 None 触发降级。"""

    try:  # pragma: no cover - 依赖外部 Redis，离线测试不覆盖
        import redis

        client = redis.Redis.from_url(redis_url)
        client.ping()
        return RedisRateLimiter(client, rate, fail_open=fail_open)
    except Exception as exc:  # pragma: no cover
        logger.warning("redis unavailable, falling back to in-memory rate limiter: %s", exc)
        return None


try:  # pragma: no cover - Starlette/FastAPI 缺失时中间件不可用
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class RateLimitMiddleware(BaseHTTPMiddleware):
        """超阈值返回 429 + 标准错误体的限流中间件。

        限流键优先取 session_id（查询参数），否则回退到客户端 IP。健康检查等路径豁免。
        """

        def __init__(self, app, limiter: RateLimiter, exempt_paths: tuple[str, ...] = ("/healthz",)) -> None:
            super().__init__(app)
            self.limiter = limiter
            self.exempt_paths = set(exempt_paths)

        async def dispatch(self, request, call_next):
            if self.limiter is None or request.url.path in self.exempt_paths:
                return await call_next(request)
            if not self.limiter.allow(self._key(request)):
                error = build_error("rate_limited", "Too many requests")
                return JSONResponse(status_code=429, content=error.to_dict())
            return await call_next(request)

        @staticmethod
        def _key(request) -> str:
            session = request.query_params.get("session_id")
            if session:
                return f"session:{session}"
            client = request.client.host if request.client else "anonymous"
            return f"ip:{client}"

except Exception:  # pragma: no cover

    RateLimitMiddleware = None  # type: ignore[assignment]
