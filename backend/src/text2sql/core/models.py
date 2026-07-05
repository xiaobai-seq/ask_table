from __future__ import annotations

"""跨模块共享的数据结构。

这里的 dataclass 是链路中各阶段交换的稳定载体：schema 检索产出 RetrievalHit，
SQL 生成产出 SQLPlan，执行产出 ExecutionResult，最终渲染产出 RenderSpec。
AgentState 则是工作流节点之间共享的“黑板”。
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, NotRequired, TypedDict


# 前端可支持的图表类型全集；生成 SQL 和渲染推荐都会引用同一组字面量。
ChartType = Literal[
    "line",
    "bar",
    "stacked_bar",
    "horizontal_bar",
    "pie",
    "donut",
    "scatter",
    "bubble",
    "heatmap",
    "treemap",
    "sankey",
    "funnel",
    "radar",
    "gauge",
    "table",
    "area",
    "stacked_area",
    "histogram",
    "boxplot",
    "waterfall",
    "map",
    "candlestick",
    "kpi",
]


@dataclass(frozen=True)
class ColumnInfo:
    """数据库字段的元信息，也是检索文档的一部分。"""

    name: str
    data_type: str = "TEXT"
    comment: str = ""
    nullable: bool = True
    primary_key: bool = False
    semantic_tags: tuple[str, ...] = ()

    def document(self) -> str:
        # document 是给 BM25/向量召回使用的扁平文本，越能表达业务语义越容易被召回。
        tags = " ".join(self.semantic_tags)
        return f"{self.name} {self.data_type} {self.comment} {tags}".strip()


@dataclass(frozen=True)
class ForeignKeyInfo:
    """一条外键边，用于关系图搜索和 JOIN 提示。"""

    source_table: str
    source_column: str
    target_table: str
    target_column: str

    def label(self) -> str:
        return (
            f"{self.source_table}.{self.source_column}"
            f" -> {self.target_table}.{self.target_column}"
        )


@dataclass(frozen=True)
class TableInfo:
    """一张表的 schema 摘要，是 schema_inspector 的核心输入和输出。"""

    name: str
    comment: str = ""
    columns: tuple[ColumnInfo, ...] = ()
    foreign_keys: tuple[ForeignKeyInfo, ...] = ()
    semantic_tags: tuple[str, ...] = ()
    row_count: int | None = None

    def column_names(self) -> set[str]:
        return {column.name for column in self.columns}

    def primary_keys(self) -> list[str]:
        return [column.name for column in self.columns if column.primary_key]

    def document(self) -> str:
        # 表名、注释、字段和外键会合成一个检索文档，供混合召回统一打分。
        column_doc = " ".join(column.document() for column in self.columns)
        fk_doc = " ".join(fk.label() for fk in self.foreign_keys)
        tags = " ".join(self.semantic_tags)
        return f"{self.name} {self.comment} {tags} {column_doc} {fk_doc}".strip()

    def brief_schema(self) -> str:
        # brief_schema 会进入 SQL prompt，保持短小可以减少 token 并降低 LLM 幻觉空间。
        columns = ", ".join(f"{c.name}:{c.data_type}" for c in self.columns)
        return f"{self.name}({columns}) -- {self.comment}".strip()


@dataclass(frozen=True)
class RelationshipPath:
    """候选表之间的一条可 JOIN 路径。"""

    source: str
    target: str
    joins: tuple[ForeignKeyInfo, ...] = ()

    def to_sql_hint(self) -> str:
        # SQL 生成器把这里的等值条件作为 JOIN 约束，不需要重新推断关系。
        if not self.joins:
            return ""
        return " AND ".join(
            f"{fk.source_table}.{fk.source_column} = {fk.target_table}.{fk.target_column}"
            for fk in self.joins
        )


@dataclass(frozen=True)
class RetrievalHit:
    """schema 检索阶段返回的候选表及其解释性分数。"""

    table: TableInfo
    score: float
    bm25_rank: int | None = None
    vector_rank: int | None = None
    rerank_score: float | None = None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Clarification:
    """当问题不足以生成可靠 SQL 时返回给用户的澄清问题。"""

    question: str
    options: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class SQLPlan:
    """SQL 生成阶段的产物：SQL 文本、图表建议和生成理由。"""

    sql: str | None
    chart_type: ChartType = "table"
    reasoning: str = ""
    confidence: float = 0.0
    advanced_features: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionResult:
    """SQL 执行后的结构化结果，错误也作为普通字段返回以便链路继续总结。"""

    columns: tuple[str, ...] = ()
    rows: tuple[dict[str, Any], ...] = ()
    row_count: int = 0
    elapsed_ms: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class RenderSpec:
    """前端渲染所需的最小图表配置。"""

    chart_type: ChartType
    x: str | None = None
    y: tuple[str, ...] = ()
    series: str | None = None
    title: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationTurn:
    """一轮已完成对话的摘要，用于后续追问改写。"""

    user_query: str
    rewritten_query: str
    generated_sql: str | None = None
    tables: tuple[str, ...] = ()
    summary: str = ""


@dataclass(frozen=True)
class EvalCase:
    """JSONL 评测用例，一条 case 对应一次完整工作流运行。"""

    case_id: str
    query: str
    expected_sql: str | None = None
    expected_tables: tuple[str, ...] = ()
    required_sql_keywords: tuple[str, ...] = ()
    allow_clarification: bool = False
    # 期望结果集（每行一个 dict）；提供时评测会做执行结果级比对（行/列/值）。
    expected_result: tuple[dict[str, Any], ...] | None = None


@dataclass(frozen=True)
class EvalResult:
    """评测结果，包含指标和失败原因，方便回归报告直接序列化。"""

    case_id: str
    passed: bool
    metrics: dict[str, float]
    generated_sql: str | None
    errors: tuple[str, ...] = ()
    # 逐 case 全环节 trace（检索/prompt/执行等），供报告与回溯落库；默认 None 保持向后兼容。
    trace: dict[str, Any] | None = None


class AgentState(TypedDict, total=False):
    """工作流节点间共享的状态。

    字段大致按链路出现顺序排列：输入问题 -> schema 召回 -> 关系路径 ->
    SQL 计划 -> 执行结果 -> 总结和渲染。
    """

    user_query: str
    rewritten_query: str
    session_id: str
    db_info: list[TableInfo]
    retrieval_hits: list[RetrievalHit]
    table_relationship: list[RelationshipPath]
    generated_sql: str | None
    sql_plan: SQLPlan
    # 生成 SQL 时喂给 LLM 的最终 prompt：仅用于评测 trace 落盘，经 SSE 出口剔除不影响线上。
    sql_prompt: str
    execution_result: ExecutionResult
    summary: str
    chart_type: ChartType
    render_spec: RenderSpec
    clarification: Clarification | None
    attempts: int
    errors: list[str]
    trace_id: str
    cancelled: NotRequired[bool]


def to_plain(value: Any) -> Any:
    """把 dataclass/tuple 等内部对象转换成 JSON/SSE 友好的普通结构。"""

    if hasattr(value, "__dataclass_fields__"):
        return {key: to_plain(val) for key, val in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_plain(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    return value


# 仅用于评测 trace 落盘的 state 字段：体积大且暴露 prompt，不应出现在线上 SSE 增量里。
TRACE_ONLY_STATE_FIELDS: tuple[str, ...] = ("sql_prompt",)


def strip_trace_only_fields(partial: dict[str, Any]) -> dict[str, Any]:
    """剔除仅供评测 trace 的 state 字段，保持线上 SSE 行为不变。

    不修改入参：命中时返回过滤后的浅拷贝，未命中则原样返回（零拷贝快路径）。
    """

    if not any(key in partial for key in TRACE_ONLY_STATE_FIELDS):
        return partial
    return {key: value for key, value in partial.items() if key not in TRACE_ONLY_STATE_FIELDS}
