from __future__ import annotations

"""统一结构化错误骨架。

对外错误一律收敛成 {code, message, trace_id} 三段式：code 便于前端分支处理，
message 给人读，trace_id 用于跨日志/链路关联定位。错误体构造不依赖 FastAPI，
仅在注册全局异常处理器时才需要 FastAPI，方便缺依赖环境照常导入与测试。
"""

import uuid
from dataclasses import dataclass


@dataclass
class ErrorBody:
    """结构化错误体。"""

    code: str
    message: str
    trace_id: str

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "trace_id": self.trace_id}


def build_error(code: str, message: str, trace_id: str | None = None) -> ErrorBody:
    """构造错误体；未显式传入 trace_id 时自动生成，保证每条错误可追踪。"""

    return ErrorBody(code=code, message=message, trace_id=trace_id or uuid.uuid4().hex)


def register_exception_handlers(app) -> None:
    """给 FastAPI app 注册全局异常处理器，把异常统一转成结构化错误体。

    缺少 FastAPI 时静默跳过，使核心逻辑在最小依赖环境仍可运行。
    """

    try:  # pragma: no cover - optional dependency
        from fastapi import HTTPException
        from fastapi.responses import JSONResponse
    except Exception:  # pragma: no cover
        return

    @app.exception_handler(HTTPException)
    async def handle_http_exception(_request, exc: HTTPException):
        # 已知的 HTTP 异常：沿用其 status_code，detail 作为可读信息。
        error = build_error(code=f"http_{exc.status_code}", message=str(exc.detail))
        return JSONResponse(status_code=exc.status_code, content=error.to_dict())

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(_request, _exc: Exception):
        # 兜底未预期异常：对外只暴露通用信息，靠 trace_id 关联内部日志定位。
        error = build_error(code="internal_error", message="Internal server error")
        return JSONResponse(status_code=500, content=error.to_dict())
