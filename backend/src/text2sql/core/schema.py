from __future__ import annotations

"""数据库 schema introspection。

该模块把真实数据库结构转换成 TableInfo/ColumnInfo/ForeignKeyInfo。
后续检索、关系分析、SQL 生成都只依赖这些统一模型，而不用关心底层是
SQLite 还是 SQLAlchemy 支持的生产数据库。
"""

import functools
import sqlite3
from pathlib import Path

from text2sql.config.domain_profile import get_domain_profile
from text2sql.core.models import ColumnInfo, ForeignKeyInfo, TableInfo


def load_schema(database_url_or_path: str) -> list[TableInfo]:
    """按连接串类型选择 SQLite 或 SQLAlchemy inspector。"""

    if database_url_or_path.startswith("sqlite:///"):
        return inspect_sqlite_database(database_url_or_path.replace("sqlite:///", "", 1))
    if database_url_or_path.endswith(".db") or database_url_or_path == ":memory:":
        return inspect_sqlite_database(database_url_or_path)
    return inspect_sqlalchemy_database(database_url_or_path)


@functools.lru_cache(maxsize=16)
def inspect_sqlite_database(path: str) -> list[TableInfo]:
    """读取 SQLite 表、字段、外键和行数，并补充轻量语义标签。"""

    db_path = Path(path)
    if path != ":memory:" and not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables: list[TableInfo] = []
        for row in rows:
            table_name = row["name"]
            # PRAGMA 是 SQLite 原生 schema introspection 入口。
            column_rows = connection.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
            fk_rows = connection.execute(f"PRAGMA foreign_key_list({quote_identifier(table_name)})").fetchall()
            columns = tuple(
                ColumnInfo(
                    name=column["name"],
                    data_type=column["type"] or "TEXT",
                    nullable=not bool(column["notnull"]),
                    primary_key=bool(column["pk"]),
                    semantic_tags=infer_column_tags(column["name"], column["type"] or ""),
                )
                for column in column_rows
            )
            foreign_keys = tuple(
                ForeignKeyInfo(
                    source_table=table_name,
                    source_column=fk["from"],
                    target_table=fk["table"],
                    target_column=fk["to"],
                )
                for fk in fk_rows
            )
            row_count = safe_count(connection, table_name)
            tables.append(
                TableInfo(
                    name=table_name,
                    comment=infer_table_comment(table_name),
                    columns=columns,
                    foreign_keys=foreign_keys,
                    semantic_tags=infer_table_tags(table_name),
                    row_count=row_count,
                )
            )
        return tables
    finally:
        connection.close()


def inspect_sqlalchemy_database(database_url: str) -> list[TableInfo]:
    """通过 SQLAlchemy inspector 适配 MySQL/Postgres 等非 SQLite 数据源。"""

    try:  # pragma: no cover - optional dependency
        from sqlalchemy import create_engine, inspect, text
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("SQLAlchemy is required for non-SQLite databases") from exc

    engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=1800)
    inspector = inspect(engine)
    tables: list[TableInfo] = []
    with engine.connect() as connection:
        for table_name in inspector.get_table_names():
            # inspector 返回的列结构会被规整到 ColumnInfo，供后续链路统一消费。
            columns = tuple(
                ColumnInfo(
                    name=column["name"],
                    data_type=str(column.get("type", "TEXT")),
                    comment=column.get("comment") or "",
                    nullable=bool(column.get("nullable", True)),
                    primary_key=column["name"] in inspector.get_pk_constraint(table_name).get(
                        "constrained_columns", []
                    ),
                    semantic_tags=infer_column_tags(column["name"], str(column.get("type", ""))),
                )
                for column in inspector.get_columns(table_name)
            )
            foreign_keys: list[ForeignKeyInfo] = []
            for fk in inspector.get_foreign_keys(table_name):
                referred_table = fk.get("referred_table")
                referred_columns = fk.get("referred_columns") or []
                constrained_columns = fk.get("constrained_columns") or []
                for source_column, target_column in zip(constrained_columns, referred_columns):
                    if referred_table and target_column:
                        foreign_keys.append(
                            ForeignKeyInfo(table_name, source_column, referred_table, target_column)
                        )
            row_count = None
            try:
                # 行数只是给检索/展示做参考，失败不应阻断 schema 加载。
                row_count = connection.execute(
                    text(f"SELECT COUNT(*) FROM {quote_identifier(table_name)}")
                ).scalar_one()
            except Exception:
                row_count = None
            tables.append(
                TableInfo(
                    name=table_name,
                    comment=infer_table_comment(table_name),
                    columns=columns,
                    foreign_keys=tuple(foreign_keys),
                    semantic_tags=infer_table_tags(table_name),
                    row_count=row_count,
                )
            )
    return tables


def quote_identifier(identifier: str) -> str:
    """安全引用表名/字段名，避免特殊字符破坏 introspection SQL。"""

    return '"' + identifier.replace('"', '""') + '"'


def safe_count(connection: sqlite3.Connection, table_name: str) -> int | None:
    """读取表行数；权限或数据源异常时返回 None。"""

    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(table_name)}").fetchone()[0])
    except Exception:
        return None


def infer_table_comment(name: str) -> str:
    """在没有数据字典时，根据领域配置推断一点业务描述用于召回。"""

    return get_domain_profile().schema_table_comment(name)


def infer_table_tags(name: str) -> tuple[str, ...]:
    """根据表名打粗粒度业务标签。"""

    return get_domain_profile().schema_table_tags(name)


def infer_column_tags(name: str, data_type: str) -> tuple[str, ...]:
    """根据字段名和类型打 time/metric/key/dimension 等语义标签。"""

    return get_domain_profile().schema_column_tags(name, data_type)


def clear_schema_cache() -> None:
    inspect_sqlite_database.cache_clear()
