from __future__ import annotations

"""SQL 执行层。

执行器只接收生成好的 SQLPlan.sql，先做只读和 schema 校验，再根据连接类型
选择 SQLite 或 SQLAlchemy 执行。所有异常都会转成 ExecutionResult.error，
避免流式链路因为一次查询失败而崩掉。
"""

import asyncio
import sqlite3
import time
from pathlib import Path

from text2sql.models import ExecutionResult, TableInfo
from text2sql.sql_validator import SQLValidator


class QueryExecutor:
    """带安全校验和行数限制的查询执行器。"""

    def __init__(self, database_url_or_path: str, tables: list[TableInfo]) -> None:
        self.database_url_or_path = database_url_or_path
        self.validator = SQLValidator(tables)

    async def execute(self, sql: str | None, limit_rows: int = 1000) -> ExecutionResult:
        # 先校验，后执行；校验失败也用 ExecutionResult 返回，方便总结节点解释错误。
        try:
            self.validator.validate(sql)
        except Exception as exc:
            return ExecutionResult(error=str(exc))
        assert sql is not None
        # 数据库驱动通常是同步 API，放到线程里避免阻塞 async workflow。
        return await asyncio.to_thread(self._execute_sync, sql, limit_rows)

    def _execute_sync(self, sql: str, limit_rows: int) -> ExecutionResult:
        start = time.perf_counter()
        # SQLite 是本地开发和测试的主路径，其他连接串走 SQLAlchemy 适配。
        if self.database_url_or_path.startswith("sqlite:///"):
            return self._execute_sqlite(self.database_url_or_path.replace("sqlite:///", "", 1), sql, limit_rows, start)
        if self.database_url_or_path.endswith(".db") or self.database_url_or_path == ":memory:":
            return self._execute_sqlite(self.database_url_or_path, sql, limit_rows, start)
        return self._execute_sqlalchemy(sql, limit_rows, start)

    def _execute_sqlite(
        self, path: str, sql: str, limit_rows: int, start: float
    ) -> ExecutionResult:
        if path != ":memory:" and not Path(path).exists():
            return ExecutionResult(error=f"SQLite database not found: {path}")
        connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(sql)
            # fetchmany 限制返回量，防止一次 SSE 响应带出过大结果集。
            rows = cursor.fetchmany(limit_rows)
            columns = tuple(description[0] for description in (cursor.description or ()))
            payload = tuple(dict(row) for row in rows)
            elapsed_ms = (time.perf_counter() - start) * 1000
            return ExecutionResult(columns, payload, len(payload), elapsed_ms)
        except Exception as exc:
            return ExecutionResult(error=str(exc), elapsed_ms=(time.perf_counter() - start) * 1000)
        finally:
            connection.close()

    def _execute_sqlalchemy(self, sql: str, limit_rows: int, start: float) -> ExecutionResult:
        try:  # pragma: no cover - optional dependency
            from sqlalchemy import create_engine, text
        except Exception as exc:  # pragma: no cover
            return ExecutionResult(error=f"SQLAlchemy is not installed: {exc}")

        engine = create_engine(
            self.database_url_or_path,
            # 这些连接池参数偏生产配置；本地测试通常不会走到这里。
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=5,
            max_overflow=10,
        )
        try:
            with engine.connect() as connection:
                result = connection.execute(text(sql))
                rows = result.fetchmany(limit_rows)
                columns = tuple(result.keys())
                payload = tuple(dict(row._mapping) for row in rows)
            elapsed_ms = (time.perf_counter() - start) * 1000
            return ExecutionResult(columns, payload, len(payload), elapsed_ms)
        except Exception as exc:  # pragma: no cover
            return ExecutionResult(error=str(exc), elapsed_ms=(time.perf_counter() - start) * 1000)
