from __future__ import annotations

"""会话/历史 repository。

对外提供统一的 `HistoryRepository` 接口，两套实现行为一致：
- `InMemoryHistoryRepository`：纯内存，供测试与「缺 MySQL 时降级」使用；
- `SqlAlchemyHistoryRepository`：基于 ORM 落库（生产 MySQL，测试可用 SQLite）。

`HistoryRecord` / `SessionSummary` 是与 ORM 解耦的纯数据载体，避免上层（API、
ConversationMemory）直接依赖 SQLAlchemy，从而保证缺依赖时仍可运行。
"""

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class HistoryRecord:
    """一轮问答的可回看快照；字段与 query_history 表、API 契约对齐。"""

    session_id: str
    user_query: str
    rewritten_query: str = ""
    generated_sql: str | None = None
    tables: list[str] = field(default_factory=list)
    summary: str = ""
    chart_type: str = "table"
    row_count: int | None = None
    elapsed_ms: float | None = None
    trace_id: str | None = None
    status: str = "success"
    render_spec: dict | None = None
    execution_result: dict | None = None
    id: int | None = None
    created_at: datetime | None = None


@dataclass
class SessionSummary:
    """会话列表项；与 GET /sessions 响应一一对应。"""

    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    turn_count: int


@dataclass
class EvalRunRecord:
    """一次评测运行的聚合结果；多条记录支持横向/趋势对比。"""

    total: int
    passed: int
    pass_rate: float
    metrics: dict = field(default_factory=dict)
    id: int | None = None
    run_at: datetime | None = None


@dataclass
class EvalCaseResultRecord:
    """一个评测用例的逐环节 trace 记录；与 ORM 解耦，便于报告/回溯复用。"""

    run_id: int
    case_id: str
    query: str
    rewritten_query: str = ""
    passed: bool = False
    retrieval_hits: list = field(default_factory=list)
    table_relationship: list = field(default_factory=list)
    few_shot_examples: list = field(default_factory=list)
    prompt: str | None = None
    generated_sql: str | None = None
    execution_rows: list = field(default_factory=list)
    row_count: int | None = None
    clarification: dict | None = None
    metrics: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    id: int | None = None
    created_at: datetime | None = None


class HistoryRepository(Protocol):
    """会话与历史的持久化契约。"""

    def add_turn(self, record: HistoryRecord) -> HistoryRecord: ...

    def list_sessions(self) -> list[SessionSummary]: ...

    def get_session_history(self, session_id: str) -> list[HistoryRecord]: ...

    def get_history(self, history_id: int) -> HistoryRecord | None: ...

    def delete_session(self, session_id: str) -> bool: ...

    def delete_history(self, history_id: int) -> bool: ...


class InMemoryHistoryRepository:
    """内存实现：用自增 id 与有序列表模拟一张 query_history 表。"""

    def __init__(self) -> None:
        self._records: list[HistoryRecord] = []
        # 会话元信息单独维护，保留首轮标题与创建时间，便于列表展示。
        self._sessions: dict[str, dict] = {}
        self._id_seq = itertools.count(1)

    def add_turn(self, record: HistoryRecord) -> HistoryRecord:
        record.id = next(self._id_seq)
        record.created_at = record.created_at or datetime.utcnow()
        self._records.append(record)
        meta = self._sessions.get(record.session_id)
        if meta is None:
            # 新会话：标题取首轮用户问题。
            self._sessions[record.session_id] = {
                "title": record.user_query,
                "created_at": record.created_at,
                "updated_at": record.created_at,
            }
        else:
            meta["updated_at"] = record.created_at
        return record

    def list_sessions(self) -> list[SessionSummary]:
        summaries = [
            SessionSummary(
                session_id=session_id,
                title=meta["title"],
                created_at=meta["created_at"],
                updated_at=meta["updated_at"],
                turn_count=sum(1 for r in self._records if r.session_id == session_id),
            )
            for session_id, meta in self._sessions.items()
        ]
        # 按更新时间倒序，与契约「最近会话靠前」一致。
        return sorted(summaries, key=lambda s: s.updated_at, reverse=True)

    def get_session_history(self, session_id: str) -> list[HistoryRecord]:
        records = [r for r in self._records if r.session_id == session_id]
        return sorted(records, key=lambda r: (r.created_at, r.id))

    def get_history(self, history_id: int) -> HistoryRecord | None:
        return next((r for r in self._records if r.id == history_id), None)

    def delete_session(self, session_id: str) -> bool:
        if session_id not in self._sessions:
            return False
        self._sessions.pop(session_id, None)
        self._records = [r for r in self._records if r.session_id != session_id]
        return True

    def delete_history(self, history_id: int) -> bool:
        target = self.get_history(history_id)
        if target is None:
            return False
        self._records = [r for r in self._records if r.id != history_id]
        # 只删该条历史；即使会话已清空也保留会话元信息（turn_count 由 list_sessions 实时计为 0，
        # title 仍保留）。如需移除整个会话请用 delete_session。
        return True


class SqlAlchemyHistoryRepository:
    """ORM 实现：把 HistoryRecord 落到 sessions / query_history 表。"""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def add_turn(self, record: HistoryRecord) -> HistoryRecord:
        from text2sql.persistence.models import QueryHistory, Session

        with self._session_factory() as session:
            now = record.created_at or datetime.utcnow()
            existing = session.get(Session, record.session_id)
            if existing is None:
                # 新会话：标题取首轮问题，turn_count 从 1 起。
                session.add(
                    Session(
                        session_id=record.session_id,
                        title=record.user_query,
                        created_at=now,
                        updated_at=now,
                        turn_count=1,
                    )
                )
                # 没有 ORM relationship 时，显式 flush 父行，避免 MySQL 等强 FK 数据库
                # 在同一事务内先插入 query_history 导致外键失败。
                session.flush()
            else:
                existing.updated_at = now
                existing.turn_count = (existing.turn_count or 0) + 1
            row = QueryHistory(
                session_id=record.session_id,
                user_query=record.user_query,
                rewritten_query=record.rewritten_query,
                generated_sql=record.generated_sql,
                tables=list(record.tables),
                summary=record.summary,
                chart_type=record.chart_type,
                row_count=record.row_count,
                elapsed_ms=record.elapsed_ms,
                trace_id=record.trace_id,
                status=record.status,
                render_spec=record.render_spec,
                execution_result=record.execution_result,
                created_at=now,
            )
            session.add(row)
            session.commit()
            record.id = row.id
            record.created_at = row.created_at
        return record

    def list_sessions(self) -> list[SessionSummary]:
        from sqlalchemy import select

        from text2sql.persistence.models import Session

        with self._session_factory() as session:
            rows = session.execute(
                select(Session).order_by(Session.updated_at.desc())
            ).scalars().all()
            return [
                SessionSummary(
                    session_id=row.session_id,
                    title=row.title,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    turn_count=row.turn_count,
                )
                for row in rows
            ]

    def get_session_history(self, session_id: str) -> list[HistoryRecord]:
        from sqlalchemy import select

        from text2sql.persistence.models import QueryHistory

        with self._session_factory() as session:
            rows = session.execute(
                select(QueryHistory)
                .where(QueryHistory.session_id == session_id)
                .order_by(QueryHistory.created_at.asc(), QueryHistory.id.asc())
            ).scalars().all()
            return [self._to_record(row) for row in rows]

    def get_history(self, history_id: int) -> HistoryRecord | None:
        from text2sql.persistence.models import QueryHistory

        with self._session_factory() as session:
            row = session.get(QueryHistory, history_id)
            return self._to_record(row) if row else None

    def delete_session(self, session_id: str) -> bool:
        from sqlalchemy import delete

        from text2sql.persistence.models import QueryHistory, Session

        with self._session_factory() as session:
            existing = session.get(Session, session_id)
            if existing is None:
                return False
            # 显式删除历史再删会话，不依赖底层 FK 级联（SQLite 默认不开启外键）。
            session.execute(delete(QueryHistory).where(QueryHistory.session_id == session_id))
            session.delete(existing)
            session.commit()
            return True

    def delete_history(self, history_id: int) -> bool:
        from text2sql.persistence.models import QueryHistory, Session

        with self._session_factory() as session:
            row = session.get(QueryHistory, history_id)
            if row is None:
                return False
            session_id = row.session_id
            session.delete(row)
            session.flush()
            # 只删该条历史并同步 turn_count（可能归零）；会话元信息始终保留，
            # 清空整个会话请用 delete_session。
            remaining = (
                session.query(QueryHistory).filter(QueryHistory.session_id == session_id).count()
            )
            parent = session.get(Session, session_id)
            if parent is not None:
                parent.turn_count = remaining
            session.commit()
            return True

    @staticmethod
    def _to_record(row) -> HistoryRecord:
        return HistoryRecord(
            session_id=row.session_id,
            user_query=row.user_query,
            rewritten_query=row.rewritten_query,
            generated_sql=row.generated_sql,
            tables=list(row.tables or []),
            summary=row.summary,
            chart_type=row.chart_type,
            row_count=row.row_count,
            elapsed_ms=row.elapsed_ms,
            trace_id=row.trace_id,
            status=row.status,
            render_spec=row.render_spec,
            execution_result=row.execution_result,
            id=row.id,
            created_at=row.created_at,
        )


class EvalRunRepository(Protocol):
    """评测运行的持久化契约（聚合 + 逐 case trace）。"""

    def record_run(self, total: int, passed: int, pass_rate: float, metrics: dict) -> EvalRunRecord: ...

    def list_runs(self) -> list[EvalRunRecord]: ...

    def record_case_result(self, record: EvalCaseResultRecord) -> EvalCaseResultRecord: ...

    def list_case_results(self, run_id: int) -> list[EvalCaseResultRecord]: ...


class InMemoryEvalRunRepository:
    """内存实现：供测试与缺 MySQL 时降级。"""

    def __init__(self) -> None:
        self._runs: list[EvalRunRecord] = []
        self._case_results: list[EvalCaseResultRecord] = []
        self._id_seq = itertools.count(1)
        self._case_id_seq = itertools.count(1)

    def record_run(self, total: int, passed: int, pass_rate: float, metrics: dict) -> EvalRunRecord:
        record = EvalRunRecord(
            total=total,
            passed=passed,
            pass_rate=pass_rate,
            metrics=dict(metrics),
            id=next(self._id_seq),
            run_at=datetime.utcnow(),
        )
        self._runs.append(record)
        return record

    def list_runs(self) -> list[EvalRunRecord]:
        # 最近一次运行排最前，便于对比。
        return sorted(self._runs, key=lambda r: (r.run_at, r.id), reverse=True)

    def record_case_result(self, record: EvalCaseResultRecord) -> EvalCaseResultRecord:
        record.id = next(self._case_id_seq)
        record.created_at = record.created_at or datetime.utcnow()
        self._case_results.append(record)
        return record

    def list_case_results(self, run_id: int) -> list[EvalCaseResultRecord]:
        rows = [row for row in self._case_results if row.run_id == run_id]
        return sorted(rows, key=lambda row: row.id)


class SqlAlchemyEvalRunRepository:
    """ORM 实现：把评测聚合结果落到 eval_runs 表。"""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def record_run(self, total: int, passed: int, pass_rate: float, metrics: dict) -> EvalRunRecord:
        from text2sql.persistence.models import EvalRun

        with self._session_factory() as session:
            row = EvalRun(
                run_at=datetime.utcnow(),
                total=total,
                passed=passed,
                pass_rate=pass_rate,
                metrics=dict(metrics),
            )
            session.add(row)
            session.commit()
            return EvalRunRecord(
                total=row.total,
                passed=row.passed,
                pass_rate=row.pass_rate,
                metrics=row.metrics or {},
                id=row.id,
                run_at=row.run_at,
            )

    def list_runs(self) -> list[EvalRunRecord]:
        from sqlalchemy import select

        from text2sql.persistence.models import EvalRun

        with self._session_factory() as session:
            rows = session.execute(
                select(EvalRun).order_by(EvalRun.run_at.desc(), EvalRun.id.desc())
            ).scalars().all()
            return [
                EvalRunRecord(
                    total=row.total,
                    passed=row.passed,
                    pass_rate=row.pass_rate,
                    metrics=row.metrics or {},
                    id=row.id,
                    run_at=row.run_at,
                )
                for row in rows
            ]

    def record_case_result(self, record: EvalCaseResultRecord) -> EvalCaseResultRecord:
        from text2sql.persistence.models import EvalCaseResult

        with self._session_factory() as session:
            row = EvalCaseResult(
                run_id=record.run_id,
                case_id=record.case_id,
                query=record.query,
                rewritten_query=record.rewritten_query,
                passed=int(bool(record.passed)),
                retrieval_hits=record.retrieval_hits,
                table_relationship=record.table_relationship,
                few_shot_examples=record.few_shot_examples,
                prompt=record.prompt,
                generated_sql=record.generated_sql,
                execution_rows=record.execution_rows,
                row_count=record.row_count,
                clarification=record.clarification,
                metrics=record.metrics,
                errors=record.errors,
                created_at=record.created_at or datetime.utcnow(),
            )
            session.add(row)
            session.commit()
            record.id = row.id
            record.created_at = row.created_at
        return record

    def list_case_results(self, run_id: int) -> list[EvalCaseResultRecord]:
        from sqlalchemy import select

        from text2sql.persistence.models import EvalCaseResult

        with self._session_factory() as session:
            rows = session.execute(
                select(EvalCaseResult)
                .where(EvalCaseResult.run_id == run_id)
                .order_by(EvalCaseResult.id.asc())
            ).scalars().all()
            return [self._to_case_record(row) for row in rows]

    @staticmethod
    def _to_case_record(row) -> EvalCaseResultRecord:
        return EvalCaseResultRecord(
            run_id=row.run_id,
            case_id=row.case_id,
            query=row.query,
            rewritten_query=row.rewritten_query,
            passed=bool(row.passed),
            retrieval_hits=row.retrieval_hits or [],
            table_relationship=row.table_relationship or [],
            few_shot_examples=row.few_shot_examples or [],
            prompt=row.prompt,
            generated_sql=row.generated_sql,
            execution_rows=row.execution_rows or [],
            row_count=row.row_count,
            clarification=row.clarification,
            metrics=row.metrics or {},
            errors=row.errors or [],
            id=row.id,
            created_at=row.created_at,
        )
