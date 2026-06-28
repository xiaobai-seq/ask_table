import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# 让 alembic 能 import 到 text2sql 包（src 布局）。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from text2sql.config import Settings  # noqa: E402
from text2sql.persistence.db import Base  # noqa: E402
import text2sql.persistence.models  # noqa: E402,F401  触发表注册

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# 用集中式 Settings 的元数据库 URL 覆盖 ini 中的占位 URL（生产 MySQL / 测试 SQLite）。
config.set_main_option("sqlalchemy.url", Settings().metadata_database_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 自动迁移的目标元数据：所有 ORM 模型已注册到 Base.metadata。
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
