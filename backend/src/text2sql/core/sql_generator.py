from __future__ import annotations

"""SQL 生成层。

生产环境可由 LLM 根据 prompt 生成 SQL；离线或测试环境则走确定性规则 fallback。
两条路径都返回 SQLPlan，让后续执行、总结、渲染不用关心 SQL 来自哪里。
"""

import json
import re
from typing import TYPE_CHECKING, Protocol

from text2sql.config.domain_profile import DomainProfile, contains_any, get_domain_profile
from text2sql.core.models import RelationshipPath, RetrievalHit, SQLPlan, TableInfo

if TYPE_CHECKING:  # pragma: no cover - 仅类型注解使用
    from text2sql.accuracy.few_shot import FewShotStore
    from text2sql.accuracy.schema_semantics import SchemaSemantics


SQL_GENERATION_HARD_RULES = (
    # 这些规则会被写进 prompt，也体现了本地规则生成器遵守的边界。
    "只能使用已提供的表和字段，禁止编造表名或列名。",
    "所有 JOIN 必须遵守给定外键或关系路径。",
    "必须遵守字段数据类型，时间过滤使用时间字段，数值计算使用数值字段。",
    "只生成只读 SELECT / WITH SQL，禁止 INSERT、UPDATE、DELETE、DROP、ALTER。",
    "信息不足或问题有歧义时返回 NULL，并给出需要澄清的问题。",
    "聚合查询不要滥加 LIMIT；只有明示 top/rank/前 N 时才使用 LIMIT。",
    "排名、TopN、占比、同比、环比、滚动统计优先使用窗口函数或 CTE。",
    "层级、上下级、组织路径类问题优先使用 WITH RECURSIVE。",
    "SQL 与推荐图表类型必须协同输出，图表字段应来自 SQL 结果。",
)


class LLMProvider(Protocol):
    async def complete(self, prompt: str) -> str:
        ...


class DeterministicSQLGenerator:
    """规则 fallback：保证测试稳定，也展示复杂 SQL 的模板化生成方式。"""

    def __init__(
        self,
        semantics: "SchemaSemantics | None" = None,
        few_shot_store: "FewShotStore | None" = None,
        few_shot_top_k: int = 3,
        domain_profile: DomainProfile | None = None,
    ) -> None:
        # semantics 可选：注入枚举字典等业务语义，缺省时退化为纯结构化 prompt。
        # few_shot_store 可选：注入「问题→SQL」示例，仅影响 LLM prompt，规则路径不受影响。
        self.semantics = semantics
        self.few_shot_store = few_shot_store
        self.few_shot_top_k = few_shot_top_k
        self.domain_profile = domain_profile or get_domain_profile()

    def build_prompt(
        self,
        query: str,
        hits: list[RetrievalHit],
        relationships: list[RelationshipPath],
        context_block: str = "",
    ) -> str:
        # prompt 只暴露候选表和关系路径，刻意不把全库 schema 塞给 LLM，
        # 这样可以降低 token 成本，也减少编造不相关表字段的概率。
        schema = "\n".join(hit.table.brief_schema() for hit in hits)
        relation_hints = "\n".join(path.to_sql_hint() for path in relationships if path.to_sql_hint())
        rules = "\n".join(f"{index + 1}. {rule}" for index, rule in enumerate(SQL_GENERATION_HARD_RULES))
        # 枚举字典提示：告诉生成器各状态/品类字段的合法取值，避免编造不存在的值。
        enum_hints = ""
        if self.semantics:
            enum_hints = self.semantics.prompt_hints([hit.table.name for hit in hits])
        # few-shot 示例：检索与当前问题最相似的优质「问题→SQL」，引导 LLM 模仿写法。
        few_shot_block = self._few_shot_block(query)
        return f"""你是一名严谨的企业 DBA 和数据分析工程师。
{context_block}

硬约束:
{rules}

候选表:
{schema}

关系路径:
{relation_hints or "无"}

字段枚举字典:
{enum_hints or "无"}

参考示例:
{few_shot_block or "无"}

用户问题:
{query}

请输出 JSON: {{"sql": "... or null", "chart_type": "...", "reasoning": "..."}}
"""

    def _few_shot_block(self, query: str) -> str:
        if not self.few_shot_store:
            return ""
        from text2sql.accuracy.few_shot import format_examples_block

        examples = self.few_shot_store.search(query, self.few_shot_top_k)
        return format_examples_block(examples)

    def generate(
        self,
        query: str,
        hits: list[RetrievalHit],
        relationships: list[RelationshipPath],
        context_block: str = "",
    ) -> SQLPlan:
        if not hits:
            return SQLPlan(None, reasoning="No schema candidates", confidence=0.0)
        lowered = query.lower()

        # 先从候选表中挑一张事实表，再按字段标签/字段名找时间、指标和维度列。
        # 这些探测结果决定后续生成趋势、排名、分组或 KPI SQL。
        profile = self.domain_profile
        table = select_table_for_query(query, [hit.table for hit in hits], profile)
        date_col = find_first_column(table, ("time",), name_contains=profile.column_hints("time"))
        metric_col = find_first_column(
            table,
            ("metric",),
            name_contains=profile.column_hints("metric"),
        )
        dimension_col = find_first_column(
            table,
            ("dimension",),
            name_contains=profile.column_hints("dimension"),
        )
        pk = table.primary_keys()[0] if table.primary_keys() else table.columns[0].name

        # 组织树、上下级等层级问题优先走递归 CTE。
        if profile.has_intent(lowered, "hierarchy"):
            recursive_plan = self._recursive_cte(table)
            if recursive_plan:
                return recursive_plan

        # 如果用户问的是“按客户/商品/地区”等关联维度，优先用关系路径生成 JOIN SQL。
        join_plan = self._join_dimension_plan(query, table, [hit.table for hit in hits], relationships)
        if join_plan:
            return join_plan

        # 趋势/同比/环比场景需要先聚合到周期，再用窗口函数比较上一期。
        if profile.has_intent(lowered, "growth"):
            if date_col and metric_col:
                period = date_expression(date_col)
                sql = f"""
WITH metric_by_period AS (
  SELECT {period} AS period, SUM({metric_col.name}) AS metric_value
  FROM {table.name}
  GROUP BY {period}
)
SELECT
  period,
  metric_value,
  LAG(metric_value) OVER (ORDER BY period) AS previous_metric_value,
  CASE
    WHEN LAG(metric_value) OVER (ORDER BY period) IS NULL
      OR LAG(metric_value) OVER (ORDER BY period) = 0 THEN NULL
    ELSE ROUND((metric_value - LAG(metric_value) OVER (ORDER BY period)) * 1.0
      / LAG(metric_value) OVER (ORDER BY period), 4)
  END AS growth_rate
FROM metric_by_period
ORDER BY period
""".strip()
                return SQLPlan(
                    sql,
                    chart_type="line",
                    reasoning="Use CTE plus LAG window function for period growth.",
                    confidence=0.82,
                    advanced_features=("window_function", "cte"),
                )

        # TopN/排名场景用 RANK，而不是简单 LIMIT，避免并列名次被截断得不清楚。
        if profile.has_intent(lowered, "ranking"):
            if metric_col:
                dim = dimension_col.name if dimension_col else pk
                limit = extract_limit(query) or 10
                sql = f"""
SELECT *
FROM (
  SELECT
    {dim},
    SUM({metric_col.name}) AS metric_value,
    RANK() OVER (ORDER BY SUM({metric_col.name}) DESC) AS metric_rank
  FROM {table.name}
  GROUP BY {dim}
) ranked_metrics
WHERE metric_rank <= {limit}
ORDER BY metric_rank
""".strip()
                return SQLPlan(
                    sql,
                    chart_type="bar",
                    reasoning="Use RANK window function for TopN request.",
                    confidence=0.84,
                    advanced_features=("window_function",),
                )

        # 常规“按维度看分布/占比”退化为 GROUP BY，指标不存在时用 COUNT(*)。
        if profile.has_intent(lowered, "grouping") and dimension_col:
            metric_expr = f"SUM({metric_col.name})" if metric_col else "COUNT(*)"
            metric_alias = "metric_value" if metric_col else "row_count"
            sql = f"""
SELECT {dimension_col.name} AS dimension_value, {metric_expr} AS {metric_alias}
FROM {table.name}
GROUP BY {dimension_col.name}
ORDER BY {metric_alias} DESC
""".strip()
            return SQLPlan(
                sql,
                chart_type="pie" if contains_any(query, profile.chart_intent_terms("ratio")) else "bar",
                reasoning="Group by detected business dimension.",
                confidence=0.78,
            )

        # 单指标查询直接返回 KPI 聚合。
        if metric_col and profile.has_intent(lowered, "kpi"):
            sql = f"SELECT SUM({metric_col.name}) AS metric_value FROM {table.name}"
            return SQLPlan(sql, chart_type="kpi", reasoning="Aggregate primary metric.", confidence=0.76)

        # 最后兜底只预览表数据，避免在意图不足时生成看似精确但其实武断的聚合。
        sql = f"SELECT * FROM {table.name} LIMIT 100"
        return SQLPlan(sql, chart_type="table", reasoning="Fallback table preview.", confidence=0.55)

    def _join_dimension_plan(
        self,
        query: str,
        fact_table: TableInfo,
        tables: list[TableInfo],
        relationships: list[RelationshipPath],
    ) -> SQLPlan | None:
        """生成事实表到维表的 JOIN 聚合 SQL。"""

        lowered = query.lower()
        profile = self.domain_profile
        wants_related_dimension = contains_any(lowered, profile.related_dimension_terms)
        if not wants_related_dimension:
            return None
        metric_col = find_first_column(
            fact_table,
            ("metric",),
            profile.column_hints("metric"),
        )
        if not metric_col:
            return None
        table_map = {table.name: table for table in tables}
        for path in relationships:
            # 只使用包含当前事实表的路径，避免跨无关候选表随意拼接。
            if not path.joins:
                continue
            names = {path.source, path.target}
            if fact_table.name not in names:
                continue
            other_name = next(name for name in names if name != fact_table.name)
            other_table = table_map.get(other_name)
            if not other_table:
                continue
            dimension_col = pick_query_dimension(query, other_table, profile)
            if not dimension_col:
                continue
            join_clauses = build_join_clauses(fact_table.name, list(path.joins))
            sql = f"""
SELECT {other_name}.{dimension_col.name} AS dimension_value,
       SUM({fact_table.name}.{metric_col.name}) AS metric_value
FROM {fact_table.name}
{' '.join(join_clauses)}
GROUP BY {other_name}.{dimension_col.name}
ORDER BY metric_value DESC
""".strip()
            return SQLPlan(
                sql,
                chart_type="bar",
                reasoning="Use relationship path to join fact table with requested dimension table.",
                confidence=0.83,
        )
        return None

    def _recursive_cte(self, table: TableInfo) -> SQLPlan | None:
        """识别自关联表，生成层级遍历 SQL。"""

        self_fk = next(
            (
                fk
                for fk in table.foreign_keys
                if fk.source_table == table.name and fk.target_table == table.name
            ),
            None,
        )
        id_col = table.primary_keys()[0] if table.primary_keys() else "id"
        profile = self.domain_profile
        parent_hints = profile.column_hints("hierarchy_parent")
        name_hints = profile.column_hints("display_name")
        parent_col = self_fk.source_column if self_fk else next(
            (col.name for col in table.columns if any(hint in col.name.lower() for hint in parent_hints)),
            None,
        )
        name_col = next(
            (col.name for col in table.columns if any(hint in col.name.lower() for hint in name_hints)),
            id_col,
        )
        if not parent_col:
            return None
        sql = f"""
WITH RECURSIVE hierarchy AS (
  SELECT
    {id_col},
    {parent_col},
    {name_col},
    CAST({name_col} AS TEXT) AS path,
    1 AS depth
  FROM {table.name}
  WHERE {parent_col} IS NULL
  UNION ALL
  SELECT
    child.{id_col},
    child.{parent_col},
    child.{name_col},
    hierarchy.path || ' > ' || child.{name_col} AS path,
    hierarchy.depth + 1 AS depth
  FROM {table.name} child
  JOIN hierarchy ON child.{parent_col} = hierarchy.{id_col}
)
SELECT * FROM hierarchy ORDER BY path
""".strip()
        return SQLPlan(
            sql,
            chart_type="table",
            reasoning="Use recursive CTE for hierarchy traversal.",
            confidence=0.8,
            advanced_features=("recursive_cte",),
        )


class PromptedSQLGenerator(DeterministicSQLGenerator):
    """LLM 优先、规则兜底的 SQL 生成器。"""

    def __init__(
        self,
        llm_provider: LLMProvider | None = None,
        semantics: "SchemaSemantics | None" = None,
        few_shot_store: "FewShotStore | None" = None,
        few_shot_top_k: int = 3,
        domain_profile: DomainProfile | None = None,
    ) -> None:
        super().__init__(semantics, few_shot_store, few_shot_top_k, domain_profile)
        self.llm_provider = llm_provider

    def build_repair_prompt(
        self,
        failed_sql: str | None,
        error: str,
        query: str,
        hits: list[RetrievalHit],
        relationships: list[RelationshipPath],
        context_block: str = "",
    ) -> str:
        # 在原始生成 prompt 基础上附加失败 SQL 与执行报错，引导 LLM 定向修复。
        base = self.build_prompt(query, hits, relationships, context_block)
        return f"""{base}

上一次生成的 SQL 执行失败，请基于候选表与字段修复后重新输出（仍只输出 JSON）。
失败 SQL:
{failed_sql or "NULL"}

执行错误:
{error}
"""

    async def aregenerate_with_error(
        self,
        failed_sql: str | None,
        error: str,
        query: str,
        hits: list[RetrievalHit],
        relationships: list[RelationshipPath],
        context_block: str = "",
    ) -> SQLPlan:
        """SQL 自修复：带着执行报错重新生成。无 LLM 时退化为规则生成器。"""

        if not self.llm_provider:
            # 规则生成器无法利用报错信息，返回规则计划；由工作流的重试上限兜底，避免空转。
            return self.generate(query, hits, relationships, context_block)
        prompt = self.build_repair_prompt(failed_sql, error, query, hits, relationships, context_block)
        try:
            response = await self.llm_provider.complete(prompt)
            plan = parse_llm_sql_plan(response)
            if plan.sql:
                return plan
            return self.generate(query, hits, relationships, context_block)
        except Exception as exc:
            fallback = self.generate(query, hits, relationships, context_block)
            return SQLPlan(
                fallback.sql,
                fallback.chart_type,
                reasoning=f"{fallback.reasoning} repair fallback because: {exc}",
                confidence=min(fallback.confidence, 0.6),
                advanced_features=fallback.advanced_features,
                warnings=(*fallback.warnings, "repair_fallback"),
            )

    async def agenerate(
        self,
        query: str,
        hits: list[RetrievalHit],
        relationships: list[RelationshipPath],
        context_block: str = "",
    ) -> SQLPlan:
        if not self.llm_provider:
            # 默认本地运行不启用 LLM，保证无需 API Key 也能跑通测试。
            return self.generate(query, hits, relationships, context_block)
        prompt = self.build_prompt(query, hits, relationships, context_block)
        try:
            response = await self.llm_provider.complete(prompt)
            plan = parse_llm_sql_plan(response)
            if plan.sql:
                return plan
            # LLM 主动返回 null 时，仍尝试规则 fallback 给出可用结果。
            return self.generate(query, hits, relationships, context_block)
        except Exception as exc:
            # 生产适配器失败不让链路整体失败，保留 warning 方便调用方观察降级。
            fallback = self.generate(query, hits, relationships, context_block)
            return SQLPlan(
                fallback.sql,
                fallback.chart_type,
                reasoning=f"{fallback.reasoning} LLM fallback because: {exc}",
                confidence=min(fallback.confidence, 0.65),
                advanced_features=fallback.advanced_features,
                warnings=(*fallback.warnings, "llm_fallback"),
            )


def find_first_column(
    table: TableInfo,
    tags: tuple[str, ...] = (),
    name_contains: tuple[str, ...] = (),
):
    """按字段名片段优先、语义标签其次，寻找最符合意图的字段。"""

    for fragment in name_contains:
        for column in table.columns:
            if fragment in column.name.lower():
                return column
    for column in table.columns:
        if tags and any(tag in column.semantic_tags for tag in tags):
            return column
    return None


def build_join_clauses(start_table: str, joins) -> list[str]:
    """把 RelationshipPath 中的外键边转换成连续 JOIN 子句。"""

    joined_tables = {start_table}
    clauses: list[str] = []
    for fk in joins:
        if fk.source_table in joined_tables and fk.target_table not in joined_tables:
            join_table = fk.target_table
        elif fk.target_table in joined_tables and fk.source_table not in joined_tables:
            join_table = fk.source_table
        else:
            join_table = fk.target_table
        clauses.append(
            f"JOIN {join_table} ON {fk.source_table}.{fk.source_column} = "
            f"{fk.target_table}.{fk.target_column}"
        )
        joined_tables.add(join_table)
    return clauses


def pick_query_dimension(query: str, table: TableInfo, profile: DomainProfile | None = None):
    """根据用户问题中的业务词，在维表中挑选 GROUP BY 字段。"""

    profile = profile or get_domain_profile()
    candidates: list[str] = []
    for group in profile.dimension_candidate_groups:
        if contains_any(query, group["terms"]):
            candidates.extend(group["columns"])
    for wanted in candidates:
        for column in table.columns:
            if wanted in column.name.lower():
                return column
    if candidates:
        return None
    return find_first_column(table, ("dimension",), profile.column_hints("dimension"))


def select_table_for_query(
    query: str, tables: list[TableInfo], profile: DomainProfile | None = None
) -> TableInfo:
    """在召回结果中挑主表：层级优先，其次时间指标事实表，再其次指标表。"""

    profile = profile or get_domain_profile()
    lowered = query.lower()
    if profile.has_intent(lowered, "hierarchy"):
        parent_hints = profile.column_hints("hierarchy_parent")
        for table in tables:
            if any(fk.source_table == table.name and fk.target_table == table.name for fk in table.foreign_keys):
                return table
            if any(
                any(hint in col.name.lower() for hint in parent_hints)
                for col in table.columns
            ):
                return table

    needs_time_metric = profile.has_intent(lowered, "time_metric")
    if needs_time_metric:
        for table in tables:
            has_time = bool(find_first_column(table, ("time",), profile.column_hints("time")))
            has_metric = bool(
                find_first_column(
                    table,
                    ("metric",),
                    profile.column_hints("metric"),
                )
            )
            if has_time and has_metric:
                return table

    needs_metric = profile.has_intent(lowered, "metric")
    if needs_metric:
        for table in tables:
            if find_first_column(
                table,
                ("metric",),
                profile.column_hints("metric"),
            ):
                return table
    return tables[0]


def date_expression(column) -> str:
    """把日期字段规整到月粒度；样例库用文本日期，因此 SQLite 下用 substr。"""

    name = column.name
    lowered = name.lower()
    if "month" in lowered:
        return name
    return f"substr({name}, 1, 7)"


def extract_limit(query: str) -> int | None:
    """解析 top/前 N，并限制在安全范围内。"""

    match = re.search(r"(?:top|前)\s*(\d+)", query.lower())
    if match:
        return max(1, min(100, int(match.group(1))))
    return None


def parse_llm_sql_plan(response: str) -> SQLPlan:
    """解析 LLM 返回的 JSON 或 ```json 代码块。"""

    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    payload = json.loads(text)
    sql = payload.get("sql")
    return SQLPlan(
        sql=sql,
        chart_type=payload.get("chart_type") or "table",
        reasoning=payload.get("reasoning") or "Generated by LLM provider.",
        confidence=float(payload.get("confidence", 0.75)),
        advanced_features=tuple(payload.get("advanced_features", ())),
        warnings=tuple(payload.get("warnings", ())),
    )
