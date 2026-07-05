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
from text2sql.core.context import ConversationMemory
from text2sql.core.graph import Text2SQLWorkflow
from text2sql.core.models import strip_trace_only_fields, to_plain
from text2sql.persistence.repository import (
    HistoryRecord,
    InMemoryHistoryRepository,
    SessionSummary,
)

try:  # pragma: no cover - optional dependency
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse
    from pydantic import BaseModel
except Exception:  # pragma: no cover
    FastAPI = None
    StreamingResponse = None
    JSONResponse = None
    CORSMiddleware = None
    HTTPException = Exception
    Request = object
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


def build_history_repository(settings: Settings):
    """按配置构建历史 repository：可用 SQLAlchemy 则落库，否则降级内存。"""

    try:
        from text2sql.persistence.db import (
            _HAS_SQLALCHEMY,
            create_metadata_engine,
            create_session_factory,
            init_models,
        )

        if _HAS_SQLALCHEMY and settings.metadata_database_url:
            from text2sql.persistence.repository import SqlAlchemyHistoryRepository

            engine = create_metadata_engine(settings.metadata_database_url)
            # 开发便捷建表；生产由 alembic 迁移管理（create_all 幂等、安全）。
            init_models(engine)
            return SqlAlchemyHistoryRepository(create_session_factory(engine))
    except Exception as exc:  # pragma: no cover - 缺依赖/连接失败时降级
        logger.warning("falling back to in-memory history repository: %s", exc)
    return InMemoryHistoryRepository()


def _iso(value) -> str | None:
    """datetime -> ISO8601 字符串；None 透传。"""

    return value.isoformat() if value is not None else None


def _session_to_dict(summary: SessionSummary) -> dict:
    return {
        "session_id": summary.session_id,
        "title": summary.title,
        "created_at": _iso(summary.created_at),
        "updated_at": _iso(summary.updated_at),
        "turn_count": summary.turn_count,
    }


def _history_summary_dict(record: HistoryRecord) -> dict:
    # 列表项：不含体积较大的 render_spec/execution_result。
    return {
        "id": record.id,
        "user_query": record.user_query,
        "rewritten_query": record.rewritten_query,
        "generated_sql": record.generated_sql,
        "tables": record.tables,
        "summary": record.summary,
        "chart_type": record.chart_type,
        "row_count": record.row_count,
        "elapsed_ms": record.elapsed_ms,
        "trace_id": record.trace_id,
        "status": record.status,
        "created_at": _iso(record.created_at),
    }


def _history_detail_dict(record: HistoryRecord) -> dict:
    # 详情：在摘要基础上补充 session_id 与完整渲染/执行结果，供回看复现。
    detail = _history_summary_dict(record)
    detail["session_id"] = record.session_id
    detail["render_spec"] = record.render_spec
    detail["execution_result"] = record.execution_result
    return detail


def create_app() -> "FastAPI":
    if FastAPI is None:  # pragma: no cover
        raise RuntimeError("FastAPI is not installed")
    app = FastAPI(title="Enterprise Text2SQL", version="0.1.0")
    settings = Settings()
    # 统一把未捕获异常收敛成 {code, message, trace_id} 结构化错误体。
    register_exception_handlers(app)
    # 限流器只构建一次，挂到 app.state 供中间件与 /query 路由共享同一份令牌桶。
    rate_limiter = build_rate_limiter(settings)
    app.state.rate_limiter = rate_limiter
    # 限流中间件按 IP 限其它端点；/query 因 session_id 在 body，改由路由层按 session 限流，
    # 故这里豁免 /query，避免在中间件里消费 body 流（详见 /query 路由）。
    if RateLimitMiddleware is not None:
        app.add_middleware(
            RateLimitMiddleware,
            limiter=rate_limiter,
            exempt_paths=("/healthz", "/query"),
        )
    # CORS 放在最外层（在限流之后注册），确保 429 等响应也带跨域头。
    if CORSMiddleware is not None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    # 默认内存 repository，保证未触发 startup（如单元测试）时端点仍可用、可注入替换。
    app.state.history_repository = InMemoryHistoryRepository()

    @app.on_event("startup")
    async def startup() -> None:
        global workflow
        # 默认指向样例库；生产环境通过 TEXT2SQL_DATABASE_URL 接真实数据源。
        settings = Settings()
        # 加载 schema 语义元数据（中文别名/枚举字典），文件缺失时自动降级为空。
        semantics = SchemaSemantics.from_yaml(settings.schema_metadata_path)
        # 加载 few-shot 种子示例库，文件缺失时自动降级为空库。
        few_shot_store = InMemoryFewShotStore.from_jsonl(settings.few_shot_seed_path)
        # 历史 repository 同时供 workflow 落库与 REST 回看，二者共享同一实例。
        repository = build_history_repository(settings)
        app.state.history_repository = repository
        workflow = Text2SQLWorkflow(
            database_url_or_path=settings.database_url,
            memory=ConversationMemory(repository=repository),
            schema_semantics=semantics,
            few_shot_store=few_shot_store,
            few_shot_top_k=settings.few_shot_top_k,
            sql_repair_max_retries=settings.sql_repair_max_retries,
        )

    @app.post("/query")
    async def query(payload: QueryRequest, request: Request):
        # 路由层按 session_id 限流：此时 body 已被解析，无需在中间件里消费请求流。
        limiter = getattr(request.app.state, "rate_limiter", None)
        if limiter is not None and not limiter.allow(f"session:{payload.session_id}"):
            error = build_error("rate_limited", "Too many requests")
            return JSONResponse(status_code=429, content=error.to_dict())
        if workflow is None:
            raise HTTPException(status_code=503, detail="Workflow is not initialized")
        task_id, cancel_event = registry.create(payload.task_id)
        return StreamingResponse(
            stream_query(workflow, payload.query, payload.session_id, task_id, cancel_event),
            media_type="text/event-stream",
            headers={"X-Task-ID": task_id},
        )

    @app.post("/cancel/{task_id}")
    async def cancel(task_id: str):
        cancelled = registry.cancel(task_id)
        if not cancelled:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"task_id": task_id, "cancelled": True}

    @app.get("/healthz")
    async def healthz():
        # 轻量存活探针：仅表示进程可服务，故被限流豁免。
        return {"status": "ok"}

    @app.get("/sessions")
    async def list_sessions(request: Request):
        repository = request.app.state.history_repository
        return {"sessions": [_session_to_dict(s) for s in repository.list_sessions()]}

    @app.get("/sessions/{session_id}/history")
    async def session_history(session_id: str, request: Request):
        repository = request.app.state.history_repository
        history = repository.get_session_history(session_id)
        return {"session_id": session_id, "history": [_history_summary_dict(r) for r in history]}

    @app.get("/history/{history_id}")
    async def history_detail(history_id: int, request: Request):
        repository = request.app.state.history_repository
        record = repository.get_history(history_id)
        if record is None:
            raise HTTPException(status_code=404, detail="History not found")
        return _history_detail_dict(record)

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        repository = request.app.state.history_repository
        if not repository.delete_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"session_id": session_id, "deleted": True}

    @app.delete("/history/{history_id}")
    async def delete_history(history_id: int, request: Request):
        repository = request.app.state.history_repository
        if not repository.delete_history(history_id):
            raise HTTPException(status_code=404, detail="History not found")
        return {"id": history_id, "deleted": True}

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
            # 剔除仅供评测 trace 的字段（如 sql_prompt），保持线上 SSE 增量不变。
            payload = {
                "task_id": task_id,
                "node": node_name,
                "data": to_plain(strip_trace_only_fields(partial)),
            }
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
