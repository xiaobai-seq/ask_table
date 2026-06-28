from __future__ import annotations

"""持久化引擎与会话工厂。

元数据库（sessions/query_history/...）默认走 MySQL，测试用 SQLite。SQLAlchemy 为
可选依赖：未安装时本模块提供 `Base = None` 占位，让上层（repository）能据此降级为
内存实现，保证「缺依赖可离线跑」。
"""

try:  # pragma: no cover - SQLAlchemy 缺失时降级
    from sqlalchemy import create_engine
    from sqlalchemy.orm import DeclarativeBase, sessionmaker
    from sqlalchemy.pool import StaticPool

    _HAS_SQLALCHEMY = True
except Exception:  # pragma: no cover
    _HAS_SQLALCHEMY = False


if _HAS_SQLALCHEMY:

    class Base(DeclarativeBase):
        """所有 ORM 模型的声明基类。"""

    def create_metadata_engine(url: str, echo: bool = False):
        """按 URL 类型创建 engine。

        纯内存 SQLite（sqlite:// 或 sqlite:///:memory:）用 StaticPool 让多个连接共享
        同一内存库，便于测试；文件 SQLite 关闭 check_same_thread；其余（MySQL 等）启用
        生产连接池参数。
        """

        if url in ("sqlite://", "sqlite:///:memory:"):
            return create_engine(
                "sqlite://",
                echo=echo,
                future=True,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        if url.startswith("sqlite"):
            return create_engine(
                url, echo=echo, future=True, connect_args={"check_same_thread": False}
            )
        return create_engine(
            url, echo=echo, future=True, pool_pre_ping=True, pool_recycle=1800
        )

    def create_session_factory(engine):
        return sessionmaker(bind=engine, expire_on_commit=False, future=True)

    def init_models(engine) -> None:
        """直接建表（测试/首次启动用）；生产环境应使用 alembic 迁移。"""

        # 触发模型注册，确保 Base.metadata 已包含全部表，再建表。
        from text2sql.persistence import models  # noqa: F401

        Base.metadata.create_all(engine)

else:  # pragma: no cover - 缺依赖占位

    Base = None  # type: ignore[assignment]

    def create_metadata_engine(url: str, echo: bool = False):  # type: ignore[misc]
        raise RuntimeError("SQLAlchemy is not installed")

    def create_session_factory(engine):  # type: ignore[misc]
        raise RuntimeError("SQLAlchemy is not installed")

    def init_models(engine) -> None:  # type: ignore[misc]
        raise RuntimeError("SQLAlchemy is not installed")
