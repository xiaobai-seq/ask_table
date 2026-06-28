from __future__ import annotations

"""FastAPI/SSE 入口。

API 层不做 Text2SQL 业务决策，只负责创建工作流、管理取消信号，
并把 workflow.astream 产生的节点增量状态转换成 Server-Sent Events。
"""

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator

from text2sql.accuracy.few_shot import InMemoryFewShotStore
from text2sql.accuracy.schema_semantics import SchemaSemantics
from text2sql.api.errors import build_error, register_exception_handlers
from text2sql.api.rate_limit import RateLimitMiddleware, build_rate_limiter
from text2sql.config import Settings
from text2sql.core.graph import Text2SQLWorkflow
from text2sql.core.models import to_plain

try:  # pragma: no cover - optional dependency
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel
except Exception:  # pragma: no cover
    FastAPI = None
    StreamingResponse = None
    HTTPException = Exception
    BaseModel = object

logger = logging.getLogger(__name__)


class QueryRequest(BaseModel):  # type: ignore[misc]
    """前端查询请求；task_id 可由调用方指定，便于后续取消。"""

    query: str
    session_id: str = "default"
    task_id: str | None = None


class TaskRegistry:
    """保存 task_id 到取消事件的映射。"""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def create(self, task_id: str | None = None) -> tuple[str, asyncio.Event]:
        # 每个流式请求都有独立取消开关，cancel 接口只需要 set 对应 Event。
        task_id = task_id or uuid.uuid4().hex
        event = asyncio.Event()
        self._events[task_id] = event
        return task_id, event

    def cancel(self, task_id: str) -> bool:
        event = self._events.get(task_id)
        if not event:
            return False
        event.set()
        return True

    def finish(self, task_id: str) -> None:
        self._events.pop(task_id, None)


registry = TaskRegistry()
workflow: Text2SQLWorkflow | None = None


def create_app() -> "FastAPI":
    if FastAPI is None:  # pragma: no cover
        raise RuntimeError("FastAPI is not installed")
    app = FastAPI(title="Enterprise Text2SQL", version="0.1.0")
    settings = Settings()
    # 统一把未捕获异常收敛成 {code, message, trace_id} 结构化错误体。
    register_exception_handlers(app)
    # 限流中间件：阈值取 settings；配置可用 Redis 用 Redis，否则降级内存令牌桶。
    if RateLimitMiddleware is not None:
        app.add_middleware(RateLimitMiddleware, limiter=build_rate_limiter(settings))

    @app.on_event("startup")
    async def startup() -> None:
        global workflow
        # 默认指向样例库；生产环境通过 TEXT2SQL_DATABASE_URL 接真实数据源。
        settings = Settings()
        # 加载 schema 语义元数据（中文别名/枚举字典），文件缺失时自动降级为空。
        semantics = SchemaSemantics.from_yaml(settings.schema_metadata_path)
        # 加载 few-shot 种子示例库，文件缺失时自动降级为空库。
        few_shot_store = InMemoryFewShotStore.from_jsonl(settings.few_shot_seed_path)
        workflow = Text2SQLWorkflow(
            database_url_or_path=settings.database_url,
            schema_semantics=semantics,
            few_shot_store=few_shot_store,
            few_shot_top_k=settings.few_shot_top_k,
            sql_repair_max_retries=settings.sql_repair_max_retries,
        )

    @app.post("/query")
    async def query(request: QueryRequest):
        if workflow is None:
            raise HTTPException(status_code=503, detail="Workflow is not initialized")
        task_id, cancel_event = registry.create(request.task_id)
        return StreamingResponse(
            stream_query(workflow, request.query, request.session_id, task_id, cancel_event),
            media_type="text/event-stream",
            headers={"X-Task-ID": task_id},
        )

    @app.post("/cancel/{task_id}")
    async def cancel(task_id: str):
        cancelled = registry.cancel(task_id)
        if not cancelled:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"task_id": task_id, "cancelled": True}

    return app


async def stream_query(
    text2sql_workflow: Text2SQLWorkflow,
    query: str,
    session_id: str,
    task_id: str,
    cancel_event: asyncio.Event,
) -> AsyncIterator[str]:
    try:
        yield sse("task", {"task_id": task_id, "status": "started"})
        async for node_name, partial in text2sql_workflow.astream(query, session_id, cancel_event):
            # partial 是单个节点新增的状态，前端可以按 node 字段增量刷新进度。
            payload = {"task_id": task_id, "node": node_name, "data": to_plain(partial)}
            yield sse(node_name, payload)
            if node_name == "cancelled":
                return
        yield sse("task", {"task_id": task_id, "status": "finished"})
    except Exception as exc:  # noqa: BLE001 - 兜底单点异常，避免整条 SSE 流直接崩溃
        # 对外只回通用安全文案，异常细节仅记录到服务端日志（带同一 trace_id 便于关联）。
        error = build_error(code="stream_error", message="Internal server error")
        logger.error(
            "unhandled error in SSE stream", exc_info=exc, extra={"trace_id": error.trace_id}
        )
        yield sse("error", {"task_id": task_id, **error.to_dict()})
    finally:
        registry.finish(task_id)


def sse(event: str, payload: dict) -> str:
    """按 SSE 协议格式化一条事件。"""

    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


app = create_app() if FastAPI is not None else None
