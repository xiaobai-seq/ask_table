"""持久化子包：ORM 模型、引擎/会话工厂与 repository。

SQLAlchemy 为可选依赖。缺失时 `Base` 为 None、ORM 模型不定义，上层据此降级为内存
实现，保证测试与离线开发不依赖 MySQL。
"""

from text2sql.persistence.db import (
    Base,
    create_metadata_engine,
    create_session_factory,
    init_models,
)
# 导入 models 以触发 ORM 表注册到 Base.metadata（缺 SQLAlchemy 时为空模块，安全）。
from text2sql.persistence import models  # noqa: E402,F401

__all__ = [
    "Base",
    "create_metadata_engine",
    "create_session_factory",
    "init_models",
]
