from __future__ import annotations

"""元数据库 ORM 模型。

覆盖 5 张表：会话、查询历史、few-shot 示例、schema 语义元数据、评测结果。
仅在安装 SQLAlchemy 时定义；缺失时本模块为空，由上层降级为内存实现。
JSON 列在 SQLite/MySQL 上都可用，存储 tables / render_spec / 枚举值等结构化字段。
"""

from datetime import datetime

from text2sql.persistence.db import _HAS_SQLALCHEMY, Base

if _HAS_SQLALCHEMY:
    from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
    from sqlalchemy.orm import Mapped, mapped_column

    class Session(Base):
        """会话：一个 session_id 对应一组多轮查询。"""

        __tablename__ = "sessions"

        session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
        title: Mapped[str] = mapped_column(String(512), default="")
        created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
        updated_at: Mapped[datetime] = mapped_column(
            DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
        )
        turn_count: Mapped[int] = mapped_column(Integer, default=0)

    class QueryHistory(Base):
        """查询历史：一轮问答的完整可回看快照。"""

        __tablename__ = "query_history"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        session_id: Mapped[str] = mapped_column(
            String(128), ForeignKey("sessions.session_id", ondelete="CASCADE"), index=True
        )
        user_query: Mapped[str] = mapped_column(Text)
        rewritten_query: Mapped[str] = mapped_column(Text, default="")
        generated_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
        tables: Mapped[list] = mapped_column(JSON, default=list)
        summary: Mapped[str] = mapped_column(Text, default="")
        chart_type: Mapped[str] = mapped_column(String(64), default="table")
        row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
        elapsed_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
        trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
        status: Mapped[str] = mapped_column(String(32), default="success")
        # render_spec / execution_result 完整存档，供前端历史回看时重绘图表。
        render_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
        execution_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
        created_at: Mapped[datetime] = mapped_column(
            DateTime, default=datetime.utcnow, index=True
        )

    class FewShotExample(Base):
        """落库的 few-shot 示例，便于线上/评测回流优质样本。"""

        __tablename__ = "few_shot_examples"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        question: Mapped[str] = mapped_column(Text)
        sql: Mapped[str] = mapped_column(Text)
        chart_type: Mapped[str] = mapped_column(String(64), default="table")
        created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    class SchemaMetadata(Base):
        """schema 语义元数据的可入库形态（与 YAML 维护互为补充）。"""

        __tablename__ = "schema_metadata"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        table_name: Mapped[str] = mapped_column(String(128), index=True)
        # column_name 为空表示这是表级元数据。
        column_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
        alias: Mapped[str | None] = mapped_column(String(256), nullable=True)
        description: Mapped[str | None] = mapped_column(Text, nullable=True)
        enum_values: Mapped[list | None] = mapped_column(JSON, nullable=True)
        created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    class EvalRun(Base):
        """评测运行：聚合指标落库，支持多次对比与趋势。"""

        __tablename__ = "eval_runs"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        run_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
        total: Mapped[int] = mapped_column(Integer, default=0)
        passed: Mapped[int] = mapped_column(Integer, default=0)
        pass_rate: Mapped[float] = mapped_column(Float, default=0.0)
        metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
