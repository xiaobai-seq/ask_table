"""Text2SQL 工作流核心子包。

把工作流内部模块（检索、生成、执行、总结等）集中在 core 下，
并在此 re-export 对外公共符号，让上层（api/eval/tests）可直接
`from text2sql.core import ...`，无需感知内部模块拆分。
"""

from text2sql.core.clarification import AmbiguityDetector
from text2sql.core.context import ConversationMemory
from text2sql.core.graph import Text2SQLWorkflow
from text2sql.core.models import (
    AgentState,
    ChartType,
    Clarification,
    ColumnInfo,
    ConversationTurn,
    EvalCase,
    EvalResult,
    ExecutionResult,
    ForeignKeyInfo,
    RelationshipPath,
    RenderSpec,
    RetrievalHit,
    SQLPlan,
    TableInfo,
    to_plain,
)
from text2sql.core.retrieval import HybridTableRetriever, schema_fingerprint
from text2sql.core.sample_data import create_sample_database
from text2sql.core.sql_generator import (
    DeterministicSQLGenerator,
    PromptedSQLGenerator,
    parse_llm_sql_plan,
)
from text2sql.core.sql_validator import SQLValidationError, SQLValidator, normalize_sql

__all__ = [
    "AgentState",
    "AmbiguityDetector",
    "ChartType",
    "Clarification",
    "ColumnInfo",
    "ConversationMemory",
    "ConversationTurn",
    "DeterministicSQLGenerator",
    "EvalCase",
    "EvalResult",
    "ExecutionResult",
    "ForeignKeyInfo",
    "HybridTableRetriever",
    "PromptedSQLGenerator",
    "RelationshipPath",
    "RenderSpec",
    "RetrievalHit",
    "SQLPlan",
    "SQLValidationError",
    "SQLValidator",
    "TableInfo",
    "Text2SQLWorkflow",
    "create_sample_database",
    "normalize_sql",
    "parse_llm_sql_plan",
    "schema_fingerprint",
    "to_plain",
]
