from __future__ import annotations

"""集中式应用配置。

所有可调参数集中在 Settings，统一从环境变量加载（前缀 TEXT2SQL_），
取代散落在各处的 os.getenv，便于后续阶段接 MySQL 元数据库、Redis 与限流。
"""

import os

try:  # pragma: no cover - 安装 pydantic-settings 时走主路径
    from pydantic import Field
    from pydantic_settings import BaseSettings, SettingsConfigDict

    _HAS_PYDANTIC_SETTINGS = True
except Exception:  # pragma: no cover - 缺依赖时仅提供最小回退，保证导入安全
    _HAS_PYDANTIC_SETTINGS = False


if _HAS_PYDANTIC_SETTINGS:

    class Settings(BaseSettings):
        """应用级配置；字段名加前缀 TEXT2SQL_ 即为对应环境变量名。"""

        model_config = SettingsConfigDict(env_prefix="TEXT2SQL_", extra="ignore")

        # 查询目标库：默认指向样例 SQLite，生产通过 TEXT2SQL_DATABASE_URL 接真实数据源。
        database_url: str = "sqlite:///./examples/demo.db"
        # 元数据库：当前用 SQLite 占位，后续阶段接 MySQL 存储 schema/血缘等元数据。
        metadata_database_url: str = "sqlite:///./examples/metadata.db"
        redis_url: str | None = None

        # LLM 相关沿用既有环境变量名（无 TEXT2SQL_ 前缀），用 alias 保持兼容。
        use_llm: bool = False
        dashscope_api_key: str | None = Field(default=None, alias="DASHSCOPE_API_KEY")
        dashscope_llm_model: str = Field(default="qwen3.7-plus", alias="DASHSCOPE_LLM_MODEL")
        dashscope_http_base_url: str | None = Field(default=None, alias="DASHSCOPE_HTTP_BASE_URL")
        # 单次 LLM 请求超时；DashScope SDK 默认约 300 秒，评测时可调小避免单 case 长时间卡住。
        llm_request_timeout_seconds: int = 300

        sql_repair_max_retries: int = 2
        rate_limit_per_minute: int = 60
        # Redis 限流运行期异常时的策略：True=fail-open（放行，保可用性），False=fail-closed（拒绝，保后端）。
        rate_limit_fail_open: bool = True
        cors_origins: list[str] = ["*"]
        few_shot_top_k: int = 3
        # schema 召回默认取 8，覆盖多跳电商问题（最多 7 张金标表）同时控制 prompt 体积。
        schema_retrieval_top_k: int = 8
        # 准确率增强资源：schema 语义元数据与 few-shot 种子库（缺失时安全降级）。
        schema_metadata_path: str = "./examples/schema_metadata.yaml"
        few_shot_seed_path: str = "./examples/few_shot_seed.jsonl"
        # 领域配置：业务同义词、字段角色、规则意图词、澄清选项和前端示例。
        domain_profile_path: str = "./examples/domain_profile.yaml"

else:

    class Settings:  # type: ignore[no-redef]
        """无 pydantic-settings 时的最小回退：直接读环境变量，保持同名字段与默认值。"""

        def __init__(self) -> None:
            self.database_url = os.getenv("TEXT2SQL_DATABASE_URL", "sqlite:///./examples/demo.db")
            self.metadata_database_url = os.getenv(
                "TEXT2SQL_METADATA_DATABASE_URL", "sqlite:///./examples/metadata.db"
            )
            self.redis_url = os.getenv("TEXT2SQL_REDIS_URL")
            self.use_llm = os.getenv("TEXT2SQL_USE_LLM", "0") in ("1", "true", "True")
            self.dashscope_api_key = os.getenv("DASHSCOPE_API_KEY")
            self.dashscope_llm_model = os.getenv("DASHSCOPE_LLM_MODEL", "qwen3.7-plus")
            self.dashscope_http_base_url = os.getenv("DASHSCOPE_HTTP_BASE_URL")
            self.llm_request_timeout_seconds = int(
                os.getenv("TEXT2SQL_LLM_REQUEST_TIMEOUT_SECONDS", "300")
            )
            self.sql_repair_max_retries = int(os.getenv("TEXT2SQL_SQL_REPAIR_MAX_RETRIES", "2"))
            self.rate_limit_per_minute = int(os.getenv("TEXT2SQL_RATE_LIMIT_PER_MINUTE", "60"))
            self.rate_limit_fail_open = os.getenv("TEXT2SQL_RATE_LIMIT_FAIL_OPEN", "1") in ("1", "true", "True")
            self.cors_origins = ["*"]
            self.few_shot_top_k = int(os.getenv("TEXT2SQL_FEW_SHOT_TOP_K", "3"))
            self.schema_retrieval_top_k = int(
                os.getenv("TEXT2SQL_SCHEMA_RETRIEVAL_TOP_K", "8")
            )
            self.schema_metadata_path = os.getenv(
                "TEXT2SQL_SCHEMA_METADATA_PATH", "./examples/schema_metadata.yaml"
            )
            self.few_shot_seed_path = os.getenv(
                "TEXT2SQL_FEW_SHOT_SEED_PATH", "./examples/few_shot_seed.jsonl"
            )
            self.domain_profile_path = os.getenv(
                "TEXT2SQL_DOMAIN_PROFILE_PATH", "./examples/domain_profile.yaml"
            )
