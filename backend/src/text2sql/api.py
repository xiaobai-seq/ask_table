from __future__ import annotations

"""FastAPI/SSE 入口。

API 层不做 Text2SQL 业务决策，只负责创建工作流、管理取消信号，
并把 workflow.astream 产生的节点增量状态转换成 Server-Sent Events。
"""

import asyncio
import json
import os
import uuid
from typing import AsyncIterator

from text2sql.graph import Text2SQLWorkflow
from text2sql.models import to_plain

try:  # pragma: no cover - optional dependency
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel
except Exception:  # pragma: no cover
    FastAPI = None
    StreamingResponse = None
    HTTPException = Exception
    BaseModel = object


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

    @app.on_event("startup")
    async def startup() -> None:
        global workflow
        # 默认指向样例库；生产环境通过 TEXT2SQL_DATABASE_URL 接真实数据源。
        database_url = os.getenv("TEXT2SQL_DATABASE_URL", "sqlite:///./examples/demo.db")
        workflow = Text2SQLWorkflow(database_url_or_path=database_url)

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
    finally:
        registry.finish(task_id)


def sse(event: str, payload: dict) -> str:
    """按 SSE 协议格式化一条事件。"""

    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


app = create_app() if FastAPI is not None else None
