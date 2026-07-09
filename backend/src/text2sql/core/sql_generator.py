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
    "结果列必须完整覆盖用户问题中要求输出/统计/展示的全部维度和指标；除非用户只要求单指标图表，禁止把多指标结果压缩成 dimension_value/metric_value 两列。",
    "涉及 TopN、排名、窗口函数或最近一笔时，ORDER BY 必须包含用户指定排序指标，并追加主键/名称等稳定 tie-breaker。",
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
        sql_dialect: str = "generic",
    ) -> None:
        # semantics 可选：注入枚举字典等业务语义，缺省时退化为纯结构化 prompt。
        # few_shot_store 可选：注入「问题→SQL」示例，仅影响 LLM prompt，规则路径不受影响。
        self.semantics = semantics
        self.few_shot_store = few_shot_store
        self.few_shot_top_k = few_shot_top_k
        self.domain_profile = domain_profile or get_domain_profile()
        self.sql_dialect = normalize_sql_dialect(sql_dialect)

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
        guidance_block = build_sql_generation_guidance(query, hits)
        dialect_block = sql_dialect_prompt(self.sql_dialect)
        return f"""你是一名严谨的企业 DBA 和数据分析工程师。
{context_block}

目标 SQL 方言:
{dialect_block}

硬约束:
{rules}

业务口径与结果契约:
{guidance_block or "无"}

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

        search_k = max(self.few_shot_top_k * 4, self.few_shot_top_k)
        examples = [
            example
            for example in self.few_shot_store.search(query, search_k)
            if is_sql_compatible_with_dialect(example.sql, self.sql_dialect)
        ][: self.few_shot_top_k]
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
        table_names = {hit.table.name for hit in hits}

        ecommerce_plan = _ecommerce_sales_finance_plan(query, table_names)
        if ecommerce_plan:
            return adapt_sql_plan_for_dialect(ecommerce_plan, self.sql_dialect)
        ecommerce_plan = _ecommerce_user_membership_plan(query, table_names)
        if ecommerce_plan:
            return adapt_sql_plan_for_dialect(ecommerce_plan, self.sql_dialect)
        ecommerce_plan = _ecommerce_product_supply_plan(query, table_names)
        if ecommerce_plan:
            return adapt_sql_plan_for_dialect(ecommerce_plan, self.sql_dialect)
        ecommerce_plan = _ecommerce_inventory_plan(query, table_names)
        if ecommerce_plan:
            return adapt_sql_plan_for_dialect(ecommerce_plan, self.sql_dialect)
        ecommerce_plan = _ecommerce_marketing_plan(query, table_names)
        if ecommerce_plan:
            return adapt_sql_plan_for_dialect(ecommerce_plan, self.sql_dialect)
        ecommerce_plan = _ecommerce_logistics_after_sales_plan(query, table_names)
        if ecommerce_plan:
            return adapt_sql_plan_for_dialect(ecommerce_plan, self.sql_dialect)
        ecommerce_plan = _ecommerce_behavior_plan(query, table_names)
        if ecommerce_plan:
            return adapt_sql_plan_for_dialect(ecommerce_plan, self.sql_dialect)
        ecommerce_plan = _ecommerce_data_quality_plan(query, table_names)
        if ecommerce_plan:
            return adapt_sql_plan_for_dialect(ecommerce_plan, self.sql_dialect)

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
        sql_dialect: str = "generic",
    ) -> None:
        super().__init__(
            semantics,
            few_shot_store,
            few_shot_top_k,
            domain_profile,
            sql_dialect=sql_dialect,
        )
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

    def build_quality_repair_prompt(
        self,
        original_prompt: str,
        generated_sql: str,
        issues: list[str],
    ) -> str:
        """让 LLM 在执行前修正高置信 SQL 质量问题。"""

        issue_lines = "\n".join(f"- {issue}" for issue in issues)
        return f"""{original_prompt}

上一次生成的 SQL 可以执行，但静态质量检查发现它很可能无法满足用户问题的业务口径。
请只修复下列问题，保留正确的表关系和过滤条件，重新输出 JSON。

待修复 SQL:
{generated_sql}

必须修复的问题:
{issue_lines}
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
        prompt: str | None = None,
    ) -> SQLPlan:
        if not self.llm_provider:
            # 默认本地运行不启用 LLM，保证无需 API Key 也能跑通测试。
            return self.generate(query, hits, relationships, context_block)
        # prompt 可由上层预构造并透传（用于评测 trace 落盘），避免 LLM 路径重复构造。
        if prompt is None:
            prompt = self.build_prompt(query, hits, relationships, context_block)
        try:
            response = await self.llm_provider.complete(prompt)
            plan = parse_llm_sql_plan(response)
            if plan.sql:
                quality_issues = inspect_sql_quality(
                    query, plan.sql, hits, sql_dialect=self.sql_dialect
                )
                if quality_issues:
                    repaired = await self._repair_quality_issues(
                        prompt,
                        plan,
                        query,
                        hits,
                        quality_issues,
                    )
                    template_fallback = self._quality_gate_template_fallback(
                        query,
                        hits,
                        relationships,
                        context_block,
                        quality_issues,
                    )
                    if template_fallback:
                        return template_fallback
                    if repaired:
                        return repaired
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

    async def _repair_quality_issues(
        self,
        original_prompt: str,
        plan: SQLPlan,
        query: str,
        hits: list[RetrievalHit],
        issues: list[str],
    ) -> SQLPlan | None:
        """执行前质量门禁：高置信口径问题触发一次 LLM 修正。"""

        assert self.llm_provider is not None
        prompt = self.build_quality_repair_prompt(original_prompt, plan.sql or "", issues)
        issue_warnings = tuple(_quality_issue_warning(issue) for issue in issues)
        try:
            response = await self.llm_provider.complete(prompt)
            repaired = parse_llm_sql_plan(response)
        except Exception:
            return SQLPlan(
                plan.sql,
                plan.chart_type,
                reasoning=plan.reasoning,
                confidence=plan.confidence,
                advanced_features=plan.advanced_features,
                warnings=(*plan.warnings, *issue_warnings, "quality_gate_repair_failed"),
            )
        if not repaired.sql:
            return SQLPlan(
                plan.sql,
                plan.chart_type,
                reasoning=plan.reasoning,
                confidence=plan.confidence,
                advanced_features=plan.advanced_features,
                warnings=(*plan.warnings, *issue_warnings, "quality_gate_repair_failed"),
            )
        remaining_issues = inspect_sql_quality(
            query, repaired.sql, hits, sql_dialect=self.sql_dialect
        )
        remaining_warnings = tuple(
            f"quality_gate_remaining_issue:{issue}" for issue in remaining_issues
        )
        unresolved_warning = ("quality_gate_repair_unresolved",) if remaining_issues else ()
        return SQLPlan(
            repaired.sql,
            repaired.chart_type,
            reasoning=repaired.reasoning,
            confidence=repaired.confidence,
            advanced_features=repaired.advanced_features,
            warnings=(
                *repaired.warnings,
                *issue_warnings,
                "quality_gate_repair",
                *remaining_warnings,
                *unresolved_warning,
            ),
        )

    def _quality_gate_template_fallback(
        self,
        query: str,
        hits: list[RetrievalHit],
        relationships: list[RelationshipPath],
        context_block: str,
        issues: list[str],
    ) -> SQLPlan | None:
        """LLM 修复仍不可靠时，用干净的高置信规则模板接管。"""

        fallback = self.generate(query, hits, relationships, context_block)
        if not fallback.sql or fallback.confidence < 0.89:
            return None
        if inspect_sql_quality(query, fallback.sql, hits, sql_dialect=self.sql_dialect):
            return None
        issue_warnings = tuple(_quality_issue_warning(issue) for issue in issues)
        return SQLPlan(
            fallback.sql,
            fallback.chart_type,
            reasoning=f"{fallback.reasoning} Quality gate template fallback.",
            confidence=fallback.confidence,
            advanced_features=fallback.advanced_features,
            warnings=(
                *fallback.warnings,
                *issue_warnings,
                "quality_gate_repair",
                "quality_gate_template_fallback",
            ),
        )


def _quality_issue_warning(issue: str) -> str:
    return f"quality_gate_issue:{issue}"


def adapt_sql_plan_for_dialect(plan: SQLPlan, dialect: str) -> SQLPlan:
    """把内置样例模板适配到目标方言；SQLite 仍保持原始模板。"""

    if not plan.sql or normalize_sql_dialect(dialect) != "mysql":
        return plan
    sql = sqlite_template_sql_to_mysql(plan.sql)
    if sql == plan.sql:
        return plan
    return SQLPlan(
        sql=sql,
        chart_type=plan.chart_type,
        reasoning=plan.reasoning,
        confidence=plan.confidence,
        advanced_features=plan.advanced_features,
        warnings=plan.warnings,
    )


def sqlite_template_sql_to_mysql(sql: str) -> str:
    """把电商内置模板中常见 SQLite 日期/字符串表达式转换为 MySQL 8.0。"""

    replacements = (
        (
            "((CAST(strftime('%m', order_date) AS INTEGER) - 1) / 3 + 1)",
            "(FLOOR((MONTH(order_date) - 1) / 3) + 1)",
        ),
        ("CAST(strftime('%Y', order_date) AS INTEGER)", "YEAR(order_date)"),
        ("CAST(strftime('%m', order_date) AS INTEGER)", "MONTH(order_date)"),
        ("cur.year || '-Q' || cur.quarter_no", "CONCAT(cur.year, '-Q', cur.quarter_no)"),
        ("strftime('%Y-%m', paid_at)", "DATE_FORMAT(paid_at, '%Y-%m')"),
        ("strftime('%Y-%m', refunded_at)", "DATE_FORMAT(refunded_at, '%Y-%m')"),
        ("strftime('%Y-%m', u.register_date)", "DATE_FORMAT(u.register_date, '%Y-%m')"),
        ("date(u.register_date, '+30 days')", "DATE_ADD(u.register_date, INTERVAL 30 DAY)"),
        (
            "strftime('%Y-%m', date(cur.month || '-01', '+1 month'))",
            "DATE_FORMAT(DATE_ADD(STR_TO_DATE(CONCAT(cur.month, '-01'), '%Y-%m-%d'), INTERVAL 1 MONTH), '%Y-%m')",
        ),
        ("strftime('%Y-%m', event_time)", "DATE_FORMAT(event_time, '%Y-%m')"),
        (
            "date(sp.listing_date, '+90 days')",
            "DATE_ADD(sp.listing_date, INTERVAL 90 DAY)",
        ),
        ("datetime(MAX(created_at))", "MAX(created_at)"),
        (
            "datetime(created_at) > datetime(params.max_ts, '-30 days')",
            "created_at > DATE_SUB(params.max_ts, INTERVAL 30 DAY)",
        ),
        (
            "datetime(created_at) > datetime(params.max_ts, '-90 days')",
            "created_at > DATE_SUB(params.max_ts, INTERVAL 90 DAY)",
        ),
        ("datetime(created_at) <= params.max_ts", "created_at <= params.max_ts"),
        (
            "CAST(julianday(end_date) - julianday(start_date) + 1 AS INTEGER)",
            "DATEDIFF(end_date, start_date) + 1",
        ),
        (
            "date(pw.start_date, '-' || pw.window_days || ' days')",
            "DATE_SUB(pw.start_date, INTERVAL pw.window_days DAY)",
        ),
        (
            "(julianday(s.delivered_at) - julianday(s.shipped_at)) * 24",
            "TIMESTAMPDIFF(SECOND, s.shipped_at, s.delivered_at) / 3600.0",
        ),
        (
            "(julianday(delivered_at) - julianday(shipped_at)) * 24",
            "TIMESTAMPDIFF(SECOND, shipped_at, delivered_at) / 3600.0",
        ),
        (
            "julianday(po.order_date) - julianday(po.prev_order_date)",
            "DATEDIFF(po.order_date, po.prev_order_date)",
        ),
        (
            "julianday(complete_date) - julianday(apply_date)",
            "DATEDIFF(complete_date, apply_date)",
        ),
        (
            "COUNT(DISTINCT user_id || '|' || session_id)",
            "COUNT(DISTINCT CONCAT(user_id, '|', session_id))",
        ),
        ("COUNT(DISTINCT date(event_time))", "COUNT(DISTINCT CAST(event_time AS DATE))"),
        ("date(uc.used_at) < o.order_date", "CAST(uc.used_at AS DATE) < o.order_date"),
    )
    adapted = sql
    for old, new in replacements:
        adapted = adapted.replace(old, new)
    return adapted


def normalize_sql_dialect(dialect: str | None) -> str:
    value = (dialect or "generic").lower()
    if value.startswith("mysql"):
        return "mysql"
    if value.startswith("sqlite"):
        return "sqlite"
    return "generic"


def infer_sql_dialect(database_url_or_path: str | None) -> str:
    if not database_url_or_path:
        return "generic"
    lowered = database_url_or_path.lower()
    if lowered.startswith("mysql"):
        return "mysql"
    if lowered.startswith("sqlite") or "://" not in lowered:
        return "sqlite"
    return "generic"


def sql_dialect_prompt(dialect: str) -> str:
    normalized = normalize_sql_dialect(dialect)
    if normalized == "mysql":
        return (
            "MySQL 8.0。必须使用 MySQL 函数和语法：DATE_FORMAT、TIMESTAMPDIFF、"
            "DATE_ADD/DATE_SUB、DATEDIFF、CONCAT、INTERVAL；禁止使用 SQLite 专属函数 "
            "strftime、julianday、date(x, '+N days')、datetime(x, '-N days')。"
        )
    if normalized == "sqlite":
        return (
            "SQLite。时间格式化和日期差可使用 strftime、julianday、date/datetime；"
            "不要使用 MySQL 专属的 DATE_FORMAT、TIMESTAMPDIFF、INTERVAL 语法。"
        )
    return "遵循目标数据库支持的 ANSI SQL；不确定函数兼容性时优先使用标准 SQL 表达。"


def is_sql_compatible_with_dialect(sql: str, dialect: str) -> bool:
    """判断 few-shot SQL 是否适合作为目标方言示例。"""

    normalized = normalize_sql_text(sql)
    target = normalize_sql_dialect(dialect)
    sqlite_only_patterns = (
        r"\bstrftime\s*\(",
        r"\bjulianday\s*\(",
        r"\bdate\s*\([^)]*,\s*['\"][+-]\d+\s+(?:day|days|month|months|year|years)",
        r"\bdatetime\s*\([^)]*,\s*['\"][+-]\d+\s+(?:day|days|month|months|year|years)",
        r"\|\|",
    )
    mysql_only_patterns = (
        r"\bdate_format\s*\(",
        r"\btimestampdiff\s*\(",
        r"\bdate_add\s*\(",
        r"\bdate_sub\s*\(",
        r"\bdatediff\s*\(",
        r"\bconcat\s*\(",
        r"\binterval\s+\d+",
    )
    if target == "mysql":
        return not any(re.search(pattern, normalized) for pattern in sqlite_only_patterns)
    if target == "sqlite":
        return not any(re.search(pattern, normalized) for pattern in mysql_only_patterns)
    return True


def build_sql_generation_guidance(query: str, hits: list[RetrievalHit]) -> str:
    """根据问题和候选表生成 LLM 可执行的业务口径提示。"""

    table_names = {hit.table.name for hit in hits}
    hints: list[str] = [
        "按用户问题的原始措辞设计 SELECT 列：问题中出现的实体 ID、名称、计数、金额、成本、差异、比例、拆分项都要输出。",
        "多指标分析不要只输出 dimension_value/metric_value；应使用业务别名，如 order_count、revenue、gross_profit、rate 等。",
        "涉及 SKU、SPU、品类、供应商、仓库、优惠券、物流公司、用户等实体时，同时输出实体主键和可读名称，便于复核和下游联动。",
        "涉及率/比例/SLA/达成率时，同时输出分子计数、分母计数和计算后的率，便于校验。",
    ]
    if "orders" in table_names and _mentions_paid_orders(query):
        hints.append("电商已支付订单默认包含 pay_status IN ('paid', 'partial_refund')，不要只写 pay_status = 'paid'。")
    if _query_requires_user_level_output(query, table_names):
        hints.append("高价值/沉默用户清单应 JOIN membership_levels，并输出 level_name、user_id、username、total_spent、last_login_at。")
    if _mentions_zero_buckets(query):
        hints.append("问题要求全部类别/固定组合/零值也输出时，先构造枚举或维表基准，再 LEFT JOIN 聚合结果。")
    if "最近" in query and "最大" in query and "created_at" in query:
        hints.append("以数据最大 created_at 为基准的最近 N 天窗口，先取 MAX(created_at)，使用 created_at > 下界 且 created_at <= max_ts。")
    if "观察窗完整" in query or "窗口完全" in query:
        hints.append("完整观察窗问题必须先取数据全局 min/max 日期，只保留观察窗完全落在数据范围内的实体。")
    if any(word in query for word in ("对账", "一致性", "检查")):
        hints.append("数据质量/对账类 SQL 先构造全量明细差异，再用 CASE WHEN 聚合；无异常时用 COALESCE/CASE 输出 0 而不是 NULL。")
    if any(word in query for word in ("相邻", "最近一笔", "Top", "top", "前10", "前20", "最高", "最大", "最慢")):
        hints.append("排序必须包含用户指定主指标，并追加实体主键/名称作为稳定 tie-breaker；窗口排序同样要追加主键。")
    return "\n".join(f"- {hint}" for hint in hints)


def inspect_sql_quality(
    query: str,
    sql: str | None,
    hits: list[RetrievalHit],
    *,
    sql_dialect: str = "generic",
) -> list[str]:
    """对 LLM SQL 做高置信静态质量检查，发现常见口径错误后触发二次生成。"""

    if not sql:
        return []
    normalized = normalize_sql_text(sql)
    table_names = {hit.table.name for hit in hits}
    issues: list[str] = []
    dialect = normalize_sql_dialect(sql_dialect)

    if not is_sql_compatible_with_dialect(sql, dialect):
        issues.append(f"SQL 方言错误：目标数据库是 {dialect}，不能使用该方言不支持的日期/字符串函数。")

    if (
        "orders" in table_names
        and _mentions_paid_orders(query)
        and _uses_paid_only_filter(normalized)
        and "partial_refund" not in normalized
    ):
        issues.append("已支付订单口径错误：必须使用 pay_status IN ('paid', 'partial_refund')，不能只保留 paid。")

    selected_columns = _final_select_columns(sql)
    final_select_text = " ".join(selected_columns).lower()
    if (
        _query_requires_multi_metric_output(query)
        and len(selected_columns) <= 2
        and ("dimension_value" in normalized or "metric_value" in normalized)
    ):
        issues.append("输出列不完整：用户要求多个维度/指标，不能压缩成 dimension_value/metric_value 两列。")

    missing_entity_ids = _missing_entity_id_outputs(query, sql, table_names)
    for entity_name, id_column in missing_entity_ids:
        issues.append(f"输出列不完整：{entity_name} 分析需要同时输出 {id_column} 和名称/指标。")

    if _query_requires_user_level_output(query, table_names) and not _select_outputs_user_level(
        final_select_text
    ):
        issues.append("输出列不完整：高价值/沉默用户清单需要输出 membership_levels.name AS level_name。")

    if _mentions_zero_buckets(query) and "left join" not in normalized and "union all" not in normalized:
        issues.append("需要保留固定类别或零值桶：请构造枚举/维表基准并 LEFT JOIN 聚合结果。")

    if _query_requests_count_output(query) and not _select_outputs_count(final_select_text):
        issues.append("输出列不完整：问题要求计数/数量，需要在 SELECT 中输出对应 count 指标。")

    if _query_requests_rate_with_counts(query) and not _select_outputs_rate_components(
        selected_columns
    ):
        issues.append("输出列不完整：率/比例类结果需要同时输出分子计数和分母计数，不能只输出 rate。")

    if "最近" in query and "最大" in query and "created_at" in query:
        if "max(created_at)" not in normalized or "<=" not in normalized or ">=" in normalized:
            issues.append("最近 N 天窗口边界不严谨：应基于 MAX(created_at)，使用严格下界 > 和上界 <=。")

    if ("观察窗完整" in query or "窗口完全" in query) and not (
        "min(" in normalized and "max(" in normalized
    ):
        issues.append("完整观察窗约束缺失：需要用全局 min/max 日期过滤观察窗完全落入数据范围的实体。")

    if "相邻" in query and "lag(" in normalized and "order_id" not in normalized:
        issues.append("相邻订单窗口排序不稳定：LAG 的 ORDER BY 需要追加 order_id 作为 tie-breaker。")

    if "最近一笔" in query and "row_number()" in normalized and "order_id" not in normalized:
        issues.append("最近一笔排序不稳定：ROW_NUMBER 的 ORDER BY 需要追加 order_id DESC。")

    if "截至" in query and "已过期优惠券" in query and "valid_to" not in normalized:
        issues.append("过期优惠券口径错误：截至日期应使用 coupons.valid_to 与日期比较，而不是只看 status。")

    if "不一致订单数" in query and "where" in normalized and re.search(r"\b(abs\(|<>\b|!=)", normalized):
        issues.append("对账汇总不应先过滤异常行：先构造全量差异，再用 CASE WHEN 聚合，避免无异常时 MAX/SUM 为 NULL。")

    if "成功支付记录一致性" in query and "unpaid" not in normalized and "pay_status in" not in normalized:
        issues.append("成功支付一致性需要区分 unpaid：缺成功支付记录只对 paid/partial_refund/refunded 等应支付状态计数。")

    return issues


def normalize_sql_text(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()


def _mentions_paid_orders(query: str) -> bool:
    return "已支付" in query or "paid order" in query.lower()


def _mentions_zero_buckets(query: str) -> bool:
    return any(word in query for word in ("都输出", "全部输出", "所有", "零值"))


def _query_requires_user_level_output(query: str, table_names: set[str]) -> bool:
    return (
        "users" in table_names
        and "membership_levels" in table_names
        and (
            any(keyword in query for keyword in ("高价值", "沉默用户"))
            or any(keyword in query for keyword in ("输出会员等级", "展示会员等级", "会员等级名称"))
        )
        and ("用户" in query or "users" in query.lower())
    )


def _query_requires_multi_metric_output(query: str) -> bool:
    output_markers = ("输出", "展示", "统计", "检查", "对比", "找出", "查询")
    metric_markers = (
        "订单数",
        "销售额",
        "客单价",
        "商品金额",
        "优惠金额",
        "优惠率",
        "成本",
        "毛利",
        "毛利率",
        "销量",
        "领取数",
        "张数",
        "核销率",
        "平均",
        "比例",
        "数量",
        "缺口",
        "金额",
        "差异",
        "计数",
        "率",
    )
    return any(marker in query for marker in output_markers) and sum(
        marker in query for marker in metric_markers
    ) >= 2


def _query_requests_count_output(query: str) -> bool:
    return any(
        marker in query
        for marker in (
            "订单数",
            "包裹数",
            "退货数",
            "用户数",
            "领取数",
            "张数",
            "笔数",
            "数量",
        )
    )


def _query_requests_rate_with_counts(query: str) -> bool:
    if "比例" in query and any(keyword in query for keyword in ("用户", "订单", "包裹", "记录")):
        return True
    return any(
        marker in query
        for marker in ("签收率", "完成率", "核销率", "未使用率", "留存率", "匹配率", "达成率", "SLA", "sla")
    )


def _select_outputs_count(final_select_text: str) -> bool:
    return any(
        re.search(pattern, final_select_text) is not None
        for pattern in (
            r"\bcount\s*\(",
            r"\b\w+_count\b",
            r"\b\w+_(?:users|orders|shipments|packages|items|coupons|skus|spus|products|records|rows)\b",
            r"\b\w+_(?:qty|quantity|num|number)\b",
        )
    )


def _select_outputs_rate_components(selected_columns: list[str]) -> bool:
    return sum(1 for column in selected_columns if _column_is_count_semantic(column)) >= 2


def _column_is_count_semantic(column: str) -> bool:
    lowered = column.lower()
    alias_match = re.search(r"\bas\s+([a-zA-Z_][\w]*)\b", lowered)
    alias_or_expr = alias_match.group(1) if alias_match else lowered
    return any(
        re.search(pattern, alias_or_expr) is not None
        for pattern in (
            r"\b\w+_count\b",
            r"\b\w+_(?:users|orders|shipments|packages|items|coupons|skus|spus|products|records|rows)\b",
            r"\b\w+_(?:qty|quantity|num|number)\b",
        )
    )


def _select_outputs_user_level(final_select_text: str) -> bool:
    return "level_name" in final_select_text or "membership" in final_select_text


def _uses_paid_only_filter(normalized_sql: str) -> bool:
    return bool(re.search(r"pay_status\s*=\s*['\"]paid['\"]", normalized_sql))


def _missing_entity_id_outputs(
    query: str,
    sql: str,
    table_names: set[str],
) -> list[tuple[str, str]]:
    final_select = " ".join(_final_select_columns(sql)).lower()
    specs = (
        ("SKU", ("SKU",), "skus", "sku_id"),
        ("SPU", ("spu", "SPU"), "spus", "spu_id"),
        ("品类", ("品类", "类目"), "categories", "category_id"),
        ("供应商", ("供应商",), "suppliers", "supplier_id"),
        ("仓库", ("仓库",), "warehouses", "warehouse_id"),
        ("物流公司", ("物流公司", "快递公司"), "logistics_companies", "company_id"),
    )
    missing: list[tuple[str, str]] = []
    for entity_name, keywords, table_name, id_column in specs:
        if table_name in table_names and any(keyword in query for keyword in keywords):
            if entity_name == "SKU" and not _query_targets_sku_entity(query):
                continue
            if id_column not in final_select:
                missing.append((entity_name, id_column))
    if (
        "coupons" in table_names
        and any(keyword in query for keyword in ("各券", "优惠券中各券"))
        and "coupon_id" not in final_select
    ):
        missing.append(("优惠券", "coupon_id"))
    return missing


def _query_targets_sku_entity(query: str) -> bool:
    if "SKU" not in query:
        return False
    if any(marker in query for marker in ("SKU数", "SKU数量", "参与SKU")):
        return False
    return any(
        marker in query
        for marker in (
            "按SKU",
            "各SKU",
            "每个SKU",
            "每款SKU",
            "个SKU",
            "SKU-",
            "SKU维度",
            "SKU中",
            "SKU的",
            "SKU毛利",
            "SKU净",
            "SKU兴趣",
            "低库存SKU",
        )
    )


def _final_select_columns(sql: str) -> list[str]:
    """粗解析最终 SELECT 列，足够支撑 prompt 质量门禁。"""

    select_start, from_start = _final_select_bounds(sql)
    if select_start < 0 or from_start < 0 or from_start <= select_start:
        return []
    clause = sql[select_start:from_start]
    return [part.strip() for part in _split_top_level_commas(clause) if part.strip()]


def _final_select_bounds(sql: str) -> tuple[int, int]:
    normalized = sql.lower()
    depth = 0
    last_select = -1
    for match in re.finditer(r"\b(select|from)\b|[()]", normalized):
        token = match.group(0)
        if token == "(":
            depth += 1
        elif token == ")":
            depth = max(0, depth - 1)
        elif token == "select" and depth == 0:
            last_select = match.end()
        elif token == "from" and depth == 0 and last_select >= 0:
            return last_select, match.start()
    return -1, -1


def _split_top_level_commas(clause: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(clause):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(clause[start:index])
            start = index + 1
    parts.append(clause[start:])
    return parts


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


def _ecommerce_sales_finance_plan(query: str, table_names: set[str]) -> SQLPlan | None:
    """内置电商样例的销售财务 SQL 模板。

    这些模板只在业务短语与所需表同时命中时触发，用于离线/无 LLM 环境下覆盖
    CTE、条件聚合、同比和多事实拼接等高频分析能力。
    """

    normalized = query.lower()

    def has_all(*tables: str) -> bool:
        return set(tables) <= table_names

    if has_all("orders") and "季度" in query and "同比" in query and "gmv" in normalized:
        return SQLPlan(
            """
WITH quarterly AS (
  SELECT
    CAST(strftime('%Y', order_date) AS INTEGER) AS year,
    ((CAST(strftime('%m', order_date) AS INTEGER) - 1) / 3 + 1) AS quarter_no,
    ROUND(SUM(total_amount), 2) AS gmv
  FROM orders
  WHERE pay_status IN ('paid', 'partial_refund')
  GROUP BY CAST(strftime('%Y', order_date) AS INTEGER),
           ((CAST(strftime('%m', order_date) AS INTEGER) - 1) / 3 + 1)
)
SELECT
  cur.year || '-Q' || cur.quarter_no AS quarter,
  cur.gmv,
  prev.gmv AS prev_year_gmv,
  ROUND((cur.gmv - prev.gmv) / NULLIF(prev.gmv, 0), 4) AS yoy_rate
FROM quarterly cur
LEFT JOIN quarterly prev
  ON prev.year = cur.year - 1
 AND prev.quarter_no = cur.quarter_no
ORDER BY cur.year, cur.quarter_no
""".strip(),
            chart_type="line",
            reasoning="Use ecommerce paid-order quarterly GMV template with prior-year self join.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if has_all("payments", "refunds") and "净收款" in query and "月份" in query:
        return SQLPlan(
            """
WITH paid AS (
  SELECT strftime('%Y-%m', paid_at) AS month, ROUND(SUM(amount), 2) AS paid_amount
  FROM payments
  WHERE status = 'success' AND paid_at IS NOT NULL
  GROUP BY strftime('%Y-%m', paid_at)
),
refunded AS (
  SELECT strftime('%Y-%m', refunded_at) AS month, ROUND(SUM(amount), 2) AS refund_amount
  FROM refunds
  WHERE status = 'success' AND refunded_at IS NOT NULL
  GROUP BY strftime('%Y-%m', refunded_at)
),
months AS (
  SELECT month FROM paid
  UNION
  SELECT month FROM refunded
)
SELECT
  months.month,
  COALESCE(paid.paid_amount, 0) AS paid_amount,
  COALESCE(refunded.refund_amount, 0) AS refund_amount,
  ROUND(COALESCE(paid.paid_amount, 0) - COALESCE(refunded.refund_amount, 0), 2) AS net_payment
FROM months
LEFT JOIN paid ON months.month = paid.month
LEFT JOIN refunded ON months.month = refunded.month
ORDER BY months.month
""".strip(),
            chart_type="line",
            reasoning="Combine monthly successful payments and successful refunds into net collection.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if (
        has_all("orders", "order_items", "skus", "spus", "categories")
        and "品类" in query
        and "毛利" in query
    ):
        return SQLPlan(
            """
SELECT
  c.category_id,
  c.name AS category_name,
  SUM(oi.quantity) AS units_sold,
  ROUND(SUM(oi.subtotal), 2) AS revenue,
  ROUND(SUM(s.cost * oi.quantity), 2) AS cost_amount,
  ROUND(SUM(oi.subtotal) - SUM(s.cost * oi.quantity), 2) AS gross_profit,
  ROUND((SUM(oi.subtotal) - SUM(s.cost * oi.quantity)) / NULLIF(SUM(oi.subtotal), 0), 4) AS gross_margin_rate
FROM orders o
JOIN order_items oi ON o.order_id = oi.order_id
JOIN skus s ON oi.sku_id = s.sku_id
JOIN spus sp ON s.spu_id = sp.spu_id
JOIN categories c ON sp.category_id = c.category_id
WHERE o.pay_status IN ('paid', 'partial_refund')
GROUP BY c.category_id, c.name
ORDER BY revenue DESC, c.category_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Aggregate paid order item revenue and SKU cost by category.",
            confidence=0.9,
        )

    if has_all("orders") and "收货省份" in query and "客单价" in query:
        return SQLPlan(
            """
SELECT
  receiver_province,
  COUNT(*) AS paid_order_count,
  ROUND(SUM(total_amount), 2) AS revenue,
  ROUND(AVG(total_amount), 2) AS avg_order_value
FROM orders
WHERE pay_status IN ('paid', 'partial_refund')
GROUP BY receiver_province
HAVING COUNT(*) >= 50
ORDER BY avg_order_value DESC, revenue DESC, receiver_province
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Compute paid-order AOV by receiver province with minimum sample size.",
            confidence=0.9,
        )

    if has_all("orders") and "优惠来源组合" in query:
        return SQLPlan(
            """
WITH discount_sources AS (
  SELECT 1 AS sort_key, 'coupon_and_promotion' AS discount_source
  UNION ALL SELECT 2, 'coupon_only'
  UNION ALL SELECT 3, 'promotion_only'
  UNION ALL SELECT 4, 'no_discount_source'
),
order_mix AS (
  SELECT
    CASE
      WHEN coupon_id IS NOT NULL AND promotion_id IS NOT NULL THEN 'coupon_and_promotion'
      WHEN coupon_id IS NOT NULL THEN 'coupon_only'
      WHEN promotion_id IS NOT NULL THEN 'promotion_only'
      ELSE 'no_discount_source'
    END AS discount_source,
    COUNT(*) AS order_count,
    SUM(product_amount) AS product_amount,
    SUM(discount_amount) AS discount_amount
  FROM orders
  WHERE pay_status IN ('paid', 'partial_refund')
  GROUP BY discount_source
)
SELECT
  ds.discount_source,
  COALESCE(om.order_count, 0) AS order_count,
  ROUND(COALESCE(om.product_amount, 0), 2) AS product_amount,
  ROUND(COALESCE(om.discount_amount, 0), 2) AS discount_amount,
  CASE
    WHEN COALESCE(om.product_amount, 0) = 0 THEN 0
    ELSE ROUND(COALESCE(om.discount_amount, 0) / om.product_amount, 4)
  END AS discount_rate
FROM discount_sources ds
LEFT JOIN order_mix om ON ds.discount_source = om.discount_source
ORDER BY ds.sort_key
""".strip(),
            chart_type="bar",
            reasoning="Bucket paid orders by coupon/promotion source and preserve empty buckets.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    return None


def _ecommerce_user_membership_plan(query: str, table_names: set[str]) -> SQLPlan | None:
    """内置电商样例的用户会员 SQL 模板。"""

    def has_all(*tables: str) -> bool:
        return set(tables) <= table_names

    if has_all("users", "orders") and "注册后30天" in query and "首笔已支付订单" in query:
        return SQLPlan(
            """
WITH first_paid_order AS (
  SELECT user_id, MIN(order_date) AS first_order_date
  FROM orders
  WHERE pay_status IN ('paid', 'partial_refund')
  GROUP BY user_id
),
eligible_users AS (
  SELECT *
  FROM users
  WHERE register_date >= '2023-01-01'
    AND register_date <= '2024-12-01'
)
SELECT
  strftime('%Y-%m', u.register_date) AS register_month,
  COUNT(*) AS new_users,
  SUM(CASE WHEN f.first_order_date BETWEEN u.register_date AND date(u.register_date, '+30 days') THEN 1 ELSE 0 END) AS first_purchase_30d_users,
  ROUND(SUM(CASE WHEN f.first_order_date BETWEEN u.register_date AND date(u.register_date, '+30 days') THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS first_purchase_30d_rate
FROM eligible_users u
LEFT JOIN first_paid_order f ON u.user_id = f.user_id
GROUP BY strftime('%Y-%m', u.register_date)
ORDER BY register_month
""".strip(),
            chart_type="line",
            reasoning="Use registration cohort and first paid order within 30 days.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if has_all("users", "membership_levels") and "高价值沉默用户" in query:
        return SQLPlan(
            """
SELECT
  u.user_id,
  u.username,
  ml.name AS level_name,
  ROUND(u.total_spent, 2) AS total_spent,
  u.last_login_at
FROM users u
JOIN membership_levels ml ON u.level_id = ml.level_id
WHERE u.status = 'active'
  AND u.total_spent > 50000
  AND u.last_login_at < '2024-07-01'
ORDER BY u.total_spent DESC, u.user_id
LIMIT 10
""".strip(),
            chart_type="table",
            reasoning="Filter active high-value users with stale last login and attach member level.",
            confidence=0.9,
        )

    if (
        has_all("orders", "users", "membership_levels")
        and "会员等级" in query
        and "复购间隔" in query
    ):
        return SQLPlan(
            """
WITH paid_orders AS (
  SELECT
    user_id,
    order_id,
    order_date,
    LAG(order_date) OVER (PARTITION BY user_id ORDER BY order_date, order_id) AS prev_order_date
  FROM orders
  WHERE pay_status IN ('paid', 'partial_refund')
)
SELECT
  ml.name AS level_name,
  COUNT(*) AS repeat_order_pairs,
  ROUND(AVG(julianday(po.order_date) - julianday(po.prev_order_date)), 2) AS avg_repurchase_interval_days
FROM paid_orders po
JOIN users u ON po.user_id = u.user_id
JOIN membership_levels ml ON u.level_id = ml.level_id
WHERE po.prev_order_date IS NOT NULL
GROUP BY ml.level_id, ml.name
ORDER BY ml.level_id
""".strip(),
            chart_type="bar",
            reasoning="Use LAG over paid orders to calculate repurchase intervals by member level.",
            confidence=0.9,
            advanced_features=("cte", "window_function"),
        )

    if (
        has_all("orders", "users", "membership_levels", "user_addresses")
        and "默认地址" in query
        and "最近一笔已支付订单" in query
    ):
        return SQLPlan(
            """
WITH latest_paid_order AS (
  SELECT
    user_id,
    receiver_city,
    ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY order_date DESC, order_id DESC) AS rn
  FROM orders
  WHERE pay_status IN ('paid', 'partial_refund')
),
default_address AS (
  SELECT user_id, city
  FROM user_addresses
  WHERE is_default = 1
)
SELECT
  ml.name AS level_name,
  COUNT(*) AS user_count,
  SUM(CASE WHEN da.city = lpo.receiver_city THEN 1 ELSE 0 END) AS city_match_users,
  ROUND(SUM(CASE WHEN da.city = lpo.receiver_city THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS city_match_rate
FROM latest_paid_order lpo
JOIN users u ON lpo.user_id = u.user_id
JOIN membership_levels ml ON u.level_id = ml.level_id
JOIN default_address da ON lpo.user_id = da.user_id
WHERE lpo.rn = 1
GROUP BY ml.level_id, ml.name
ORDER BY ml.level_id
""".strip(),
            chart_type="bar",
            reasoning="Compare each user's default address city with latest paid-order receiver city.",
            confidence=0.9,
            advanced_features=("cte", "window_function"),
        )

    if has_all("user_events") and "下月仍有行为事件" in query and "留存率" in query:
        return SQLPlan(
            """
WITH monthly_users AS (
  SELECT DISTINCT strftime('%Y-%m', event_time) AS month, user_id
  FROM user_events
  WHERE event_time >= '2024-01-01' AND event_time < '2025-01-01'
)
SELECT
  cur.month,
  COUNT(*) AS active_users,
  COUNT(nxt.user_id) AS retained_next_month_users,
  ROUND(COUNT(nxt.user_id) * 1.0 / COUNT(*), 4) AS next_month_retention_rate
FROM monthly_users cur
LEFT JOIN monthly_users nxt
  ON cur.user_id = nxt.user_id
 AND nxt.month = strftime('%Y-%m', date(cur.month || '-01', '+1 month'))
WHERE cur.month BETWEEN '2024-01' AND '2024-11'
GROUP BY cur.month
ORDER BY cur.month
""".strip(),
            chart_type="line",
            reasoning="Build monthly active-user cohorts and join to next-month activity.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    return None


def _ecommerce_product_supply_plan(query: str, table_names: set[str]) -> SQLPlan | None:
    """内置电商样例的商品供应链 SQL 模板。"""

    def has_all(*tables: str) -> bool:
        return set(tables) <= table_names

    if has_all("orders", "order_items", "skus") and "sku" in query.lower() and "毛利额" in query:
        return SQLPlan(
            """
SELECT
  s.sku_id,
  s.sku_name,
  SUM(oi.quantity) AS units_sold,
  ROUND(SUM(oi.subtotal), 2) AS revenue,
  ROUND(SUM(s.cost * oi.quantity), 2) AS cost_amount,
  ROUND(SUM(oi.subtotal) - SUM(s.cost * oi.quantity), 2) AS gross_profit,
  ROUND((SUM(oi.subtotal) - SUM(s.cost * oi.quantity)) / NULLIF(SUM(oi.subtotal), 0), 4) AS gross_margin_rate
FROM orders o
JOIN order_items oi ON o.order_id = oi.order_id
JOIN skus s ON oi.sku_id = s.sku_id
WHERE o.pay_status IN ('paid', 'partial_refund')
GROUP BY s.sku_id, s.sku_name
ORDER BY gross_profit DESC, s.sku_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Aggregate paid-order item revenue and SKU cost by SKU.",
            confidence=0.9,
        )

    if (
        has_all("spus", "skus", "order_items", "orders")
        and "上市后90天" in query
        and "观察窗" in query
    ):
        return SQLPlan(
            """
WITH params AS (
  SELECT MIN(order_date) AS min_order_date, MAX(order_date) AS max_order_date FROM orders
)
SELECT
  sp.spu_id,
  sp.spu_name,
  sp.listing_date,
  SUM(oi.quantity) AS units_sold_90d,
  ROUND(SUM(oi.subtotal), 2) AS revenue_90d
FROM spus sp
JOIN params
  ON sp.listing_date >= params.min_order_date
 AND date(sp.listing_date, '+90 days') <= params.max_order_date
JOIN skus s ON sp.spu_id = s.spu_id
JOIN order_items oi ON s.sku_id = oi.sku_id
JOIN orders o ON oi.order_id = o.order_id
WHERE o.pay_status IN ('paid', 'partial_refund')
  AND o.order_date BETWEEN sp.listing_date AND date(sp.listing_date, '+90 days')
GROUP BY sp.spu_id, sp.spu_name, sp.listing_date
ORDER BY revenue_90d DESC, sp.spu_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Restrict SPUs to complete 90-day observable windows and aggregate paid sales.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if (
        has_all("suppliers", "spus", "skus", "order_items", "orders")
        and "供应商" in query
        and "毛利率" in query
    ):
        return SQLPlan(
            """
SELECT
  sup.supplier_id,
  sup.name AS supplier_name,
  SUM(oi.quantity) AS units_sold,
  ROUND(SUM(oi.subtotal), 2) AS revenue,
  ROUND(SUM(s.cost * oi.quantity), 2) AS cost_amount,
  ROUND(SUM(oi.subtotal) - SUM(s.cost * oi.quantity), 2) AS gross_profit,
  ROUND((SUM(oi.subtotal) - SUM(s.cost * oi.quantity)) / NULLIF(SUM(oi.subtotal), 0), 4) AS gross_margin_rate
FROM suppliers sup
JOIN spus sp ON sup.supplier_id = sp.supplier_id
JOIN skus s ON sp.spu_id = s.spu_id
JOIN order_items oi ON s.sku_id = oi.sku_id
JOIN orders o ON oi.order_id = o.order_id
WHERE o.pay_status IN ('paid', 'partial_refund')
GROUP BY sup.supplier_id, sup.name
ORDER BY revenue DESC, sup.supplier_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Aggregate paid sales, cost and gross margin by supplier.",
            confidence=0.9,
        )

    if (
        has_all("sku_attributes", "order_items", "orders")
        and "颜色属性" in query
        and "销售额占比" in query
    ):
        return SQLPlan(
            """
WITH color_sales AS (
  SELECT
    sa.attr_value AS color,
    SUM(oi.quantity) AS units_sold,
    SUM(oi.subtotal) AS revenue
  FROM sku_attributes sa
  JOIN order_items oi ON sa.sku_id = oi.sku_id
  JOIN orders o ON oi.order_id = o.order_id
  WHERE sa.attr_name = '颜色'
    AND o.pay_status IN ('paid', 'partial_refund')
  GROUP BY sa.attr_value
),
total_sales AS (
  SELECT SUM(revenue) AS total_revenue FROM color_sales
)
SELECT
  color,
  units_sold,
  ROUND(revenue, 2) AS revenue,
  ROUND(revenue / NULLIF(total_revenue, 0), 4) AS revenue_share
FROM color_sales
CROSS JOIN total_sales
ORDER BY revenue DESC, color
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Calculate paid sales share by SKU color attribute.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if (
        has_all("categories", "spus", "skus", "order_items", "orders", "return_items", "return_orders")
        and "退货率" in query
        and "销售件数" in query
    ):
        return SQLPlan(
            """
WITH sales AS (
  SELECT
    c.category_id,
    c.name AS category_name,
    SUM(oi.quantity) AS sold_qty
  FROM categories c
  JOIN spus sp ON c.category_id = sp.category_id
  JOIN skus s ON sp.spu_id = s.spu_id
  JOIN order_items oi ON s.sku_id = oi.sku_id
  JOIN orders o ON oi.order_id = o.order_id
  WHERE o.pay_status IN ('paid', 'partial_refund')
  GROUP BY c.category_id, c.name
  HAVING SUM(oi.quantity) >= 100
),
returns AS (
  SELECT
    c.category_id,
    SUM(ri.quantity) AS returned_qty
  FROM return_orders ro
  JOIN orders o ON ro.order_id = o.order_id
  JOIN return_items ri ON ro.return_id = ri.return_id
  JOIN skus s ON ri.sku_id = s.sku_id
  JOIN spus sp ON s.spu_id = sp.spu_id
  JOIN categories c ON sp.category_id = c.category_id
  WHERE ro.status IN ('completed', 'refunded')
    AND o.pay_status IN ('paid', 'partial_refund')
  GROUP BY c.category_id
)
SELECT
  sales.category_id,
  sales.category_name,
  sales.sold_qty,
  COALESCE(returns.returned_qty, 0) AS returned_qty,
  ROUND(COALESCE(returns.returned_qty, 0) * 1.0 / NULLIF(sales.sold_qty, 0), 4) AS return_rate
FROM sales
LEFT JOIN returns ON sales.category_id = returns.category_id
ORDER BY return_rate DESC, sales.sold_qty DESC, sales.category_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Use aligned paid-order denominator and completed/refunded return numerator by category.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    return None


def _ecommerce_inventory_plan(query: str, table_names: set[str]) -> SQLPlan | None:
    """内置电商样例的库存仓储 SQL 模板。"""

    def has_all(*tables: str) -> bool:
        return set(tables) <= table_names

    if has_all("inventory", "skus", "warehouses") and "低于安全库存" in query and "缺口量" in query:
        return SQLPlan(
            """
SELECT
  w.warehouse_id,
  w.name AS warehouse_name,
  s.sku_id,
  s.sku_name,
  i.quantity,
  i.safety_stock,
  i.safety_stock - i.quantity AS shortage_qty
FROM inventory i
JOIN warehouses w ON i.warehouse_id = w.warehouse_id
JOIN skus s ON i.sku_id = s.sku_id
WHERE i.quantity < i.safety_stock
ORDER BY shortage_qty DESC, w.warehouse_id, s.sku_id
LIMIT 20
""".strip(),
            chart_type="table",
            reasoning="List current SKU-warehouse inventory below safety stock with shortage quantity.",
            confidence=0.9,
        )

    if has_all("warehouses", "inventory") and "库容利用率" in query:
        return SQLPlan(
            """
SELECT
  w.warehouse_id,
  w.name AS warehouse_name,
  w.capacity,
  COALESCE(SUM(i.quantity), 0) AS inventory_qty,
  ROUND(COALESCE(SUM(i.quantity), 0) * 1.0 / NULLIF(w.capacity, 0), 4) AS capacity_utilization,
  SUM(CASE WHEN i.quantity < i.safety_stock THEN 1 ELSE 0 END) AS low_stock_sku_count
FROM warehouses w
LEFT JOIN inventory i ON w.warehouse_id = i.warehouse_id
GROUP BY w.warehouse_id, w.name, w.capacity
ORDER BY capacity_utilization DESC, w.warehouse_id
""".strip(),
            chart_type="bar",
            reasoning="Aggregate current inventory quantity and low-stock count by warehouse capacity.",
            confidence=0.9,
        )

    if has_all("inventory_movements", "warehouses") and "最近30天" in query and "出库总量" in query:
        return SQLPlan(
            """
WITH params AS (
  SELECT datetime(MAX(created_at)) AS max_ts FROM inventory_movements
),
outbound AS (
  SELECT
    warehouse_id,
    SUM(quantity) AS outbound_qty_30d
  FROM inventory_movements, params
  WHERE movement_type = 'outbound'
    AND datetime(created_at) > datetime(params.max_ts, '-30 days')
    AND datetime(created_at) <= params.max_ts
  GROUP BY warehouse_id
)
SELECT
  w.warehouse_id,
  w.name AS warehouse_name,
  COALESCE(outbound.outbound_qty_30d, 0) AS outbound_qty_30d
FROM warehouses w
LEFT JOIN outbound ON w.warehouse_id = outbound.warehouse_id
ORDER BY outbound_qty_30d DESC, w.warehouse_id
""".strip(),
            chart_type="bar",
            reasoning="Use max inventory movement timestamp as rolling 30-day window anchor.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if has_all("inventory_movements", "skus") and "最近90天" in query and "净流入" in query:
        return SQLPlan(
            """
WITH params AS (
  SELECT datetime(MAX(created_at)) AS max_ts FROM inventory_movements
),
sku_flow AS (
  SELECT
    sku_id,
    SUM(CASE WHEN movement_type = 'inbound' THEN quantity ELSE 0 END) AS inbound_qty,
    SUM(CASE WHEN movement_type = 'outbound' THEN quantity ELSE 0 END) AS outbound_qty
  FROM inventory_movements, params
  WHERE datetime(created_at) > datetime(params.max_ts, '-90 days')
    AND datetime(created_at) <= params.max_ts
  GROUP BY sku_id
)
SELECT
  s.sku_id,
  s.sku_name,
  inbound_qty,
  outbound_qty,
  inbound_qty - outbound_qty AS net_inflow_qty
FROM sku_flow
JOIN skus s ON sku_flow.sku_id = s.sku_id
ORDER BY net_inflow_qty DESC, s.sku_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Compute SKU net inflow over rolling 90-day inventory movement window.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if (
        has_all("inventory", "skus", "spus", "suppliers")
        and "供应商" in query
        and "低库存风险" in query
    ):
        return SQLPlan(
            """
SELECT
  sup.supplier_id,
  sup.name AS supplier_name,
  COUNT(DISTINCT i.sku_id) AS low_stock_sku_count,
  COUNT(DISTINCT i.warehouse_id) AS affected_warehouse_count,
  SUM(i.safety_stock - i.quantity) AS total_shortage_qty
FROM inventory i
JOIN skus s ON i.sku_id = s.sku_id
JOIN spus sp ON s.spu_id = sp.spu_id
JOIN suppliers sup ON sp.supplier_id = sup.supplier_id
WHERE i.quantity < i.safety_stock
GROUP BY sup.supplier_id, sup.name
ORDER BY total_shortage_qty DESC, low_stock_sku_count DESC, sup.supplier_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Roll up current low-stock shortage risk to supplier.",
            confidence=0.9,
        )

    return None


def _ecommerce_marketing_plan(query: str, table_names: set[str]) -> SQLPlan | None:
    """内置电商样例的营销促销 SQL 模板。"""

    def has_all(*tables: str) -> bool:
        return set(tables) <= table_names

    if has_all("coupons", "user_coupons") and "优惠券类型" in query and "核销率" in query:
        return SQLPlan(
            """
SELECT
  c.type AS coupon_type,
  COUNT(*) AS received_count,
  SUM(CASE WHEN uc.status = 'used' THEN 1 ELSE 0 END) AS used_count,
  ROUND(SUM(CASE WHEN uc.status = 'used' THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS redeem_rate
FROM user_coupons uc
JOIN coupons c ON uc.coupon_id = c.coupon_id
GROUP BY c.type
ORDER BY redeem_rate DESC, received_count DESC, coupon_type
""".strip(),
            chart_type="bar",
            reasoning="Calculate coupon redeem rate by coupon type.",
            confidence=0.9,
        )

    if (
        has_all("coupons", "user_coupons", "orders")
        and "满减券" in query
        and "使用效率" in query
    ):
        return SQLPlan(
            """
SELECT
  c.coupon_id,
  c.name AS coupon_name,
  c.threshold,
  c.value,
  COUNT(uc.user_coupon_id) AS received_count,
  SUM(CASE WHEN uc.status = 'used' THEN 1 ELSE 0 END) AS used_count,
  ROUND(AVG(CASE WHEN uc.status = 'used' THEN o.total_amount END), 2) AS avg_used_order_amount,
  ROUND(AVG(CASE WHEN uc.status = 'used' THEN o.discount_amount END), 2) AS avg_used_order_discount
FROM coupons c
LEFT JOIN user_coupons uc ON c.coupon_id = uc.coupon_id
LEFT JOIN orders o ON uc.order_id = o.order_id
WHERE c.type = 'full_reduction'
GROUP BY c.coupon_id, c.name, c.threshold, c.value
ORDER BY used_count DESC, received_count DESC, c.coupon_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Evaluate full-reduction coupon receive/use counts and used-order averages.",
            confidence=0.9,
        )

    if (
        has_all("promotions", "promotion_products", "skus")
        and "促销类型" in query
        and "折扣深度" in query
    ):
        return SQLPlan(
            """
SELECT
  p.type AS promotion_type,
  COUNT(DISTINCT pp.sku_id) AS promo_sku_count,
  COUNT(*) AS promo_product_row_count,
  ROUND(AVG(s.price), 2) AS avg_list_price,
  ROUND(AVG(pp.promo_price), 2) AS avg_promo_price,
  ROUND(AVG(1 - pp.promo_price / NULLIF(s.price, 0)), 4) AS avg_discount_depth
FROM promotions p
JOIN promotion_products pp ON p.promotion_id = pp.promotion_id
JOIN skus s ON pp.sku_id = s.sku_id
GROUP BY p.type
ORDER BY avg_discount_depth DESC, promo_product_row_count DESC, promotion_type
""".strip(),
            chart_type="bar",
            reasoning="Measure promotion discount depth by promotion type using SKU list price.",
            confidence=0.9,
        )

    if (
        has_all("promotions", "promotion_products", "order_items", "orders")
        and "活动期" in query
        and "等长周期" in query
    ):
        return SQLPlan(
            """
WITH promo_windows AS (
  SELECT
    promotion_id,
    name,
    start_date,
    end_date,
    CAST(julianday(end_date) - julianday(start_date) + 1 AS INTEGER) AS window_days
  FROM promotions
),
sales AS (
  SELECT
    pw.promotion_id,
    pw.name,
    pw.window_days,
    SUM(CASE WHEN o.order_date BETWEEN pw.start_date AND pw.end_date THEN oi.subtotal ELSE 0 END) AS promo_revenue,
    SUM(CASE WHEN o.order_date >= date(pw.start_date, '-' || pw.window_days || ' days') AND o.order_date < pw.start_date THEN oi.subtotal ELSE 0 END) AS pre_revenue
  FROM promo_windows pw
  JOIN promotion_products pp ON pw.promotion_id = pp.promotion_id
  JOIN order_items oi ON pp.sku_id = oi.sku_id
  JOIN orders o ON oi.order_id = o.order_id
  WHERE o.pay_status IN ('paid', 'partial_refund')
    AND o.order_date >= date(pw.start_date, '-' || pw.window_days || ' days')
    AND o.order_date <= pw.end_date
  GROUP BY pw.promotion_id, pw.name, pw.window_days
)
SELECT
  promotion_id,
  name AS promotion_name,
  window_days,
  ROUND(promo_revenue, 2) AS promo_revenue,
  ROUND(pre_revenue, 2) AS pre_revenue,
  ROUND((promo_revenue - pre_revenue) / NULLIF(pre_revenue, 0), 4) AS lift_rate
FROM sales
ORDER BY promo_revenue DESC, promotion_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Compare promotion-window paid sales with an equal-length pre-promotion window.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if has_all("coupons", "user_coupons") and "过期未使用率" in query:
        return SQLPlan(
            """
SELECT
  c.coupon_id,
  c.name AS coupon_name,
  c.type AS coupon_type,
  c.valid_to,
  COUNT(*) AS received_count,
  SUM(CASE WHEN uc.status = 'expired' THEN 1 ELSE 0 END) AS expired_unused_count,
  ROUND(SUM(CASE WHEN uc.status = 'expired' THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS expired_unused_rate
FROM coupons c
JOIN user_coupons uc ON c.coupon_id = uc.coupon_id
WHERE c.valid_to < '2024-12-31'
GROUP BY c.coupon_id, c.name, c.type, c.valid_to
ORDER BY expired_unused_rate DESC, received_count DESC, c.coupon_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Calculate expired unused coupon rate as of the fixed analysis date.",
            confidence=0.9,
        )

    return None


def _ecommerce_logistics_after_sales_plan(query: str, table_names: set[str]) -> SQLPlan | None:
    """内置电商样例的物流售后 SQL 模板。"""

    def has_all(*tables: str) -> bool:
        return set(tables) <= table_names

    if has_all("shipments", "logistics_companies") and "72小时" in query and "物流公司" in query:
        return SQLPlan(
            """
SELECT
  lc.company_id,
  lc.name AS company_name,
  COUNT(*) AS signed_shipment_count,
  ROUND(AVG((julianday(s.delivered_at) - julianday(s.shipped_at)) * 24), 2) AS avg_delivery_hours,
  SUM(CASE WHEN (julianday(s.delivered_at) - julianday(s.shipped_at)) * 24 <= 72 THEN 1 ELSE 0 END) AS within_72h_count,
  ROUND(SUM(CASE WHEN (julianday(s.delivered_at) - julianday(s.shipped_at)) * 24 <= 72 THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS within_72h_rate
FROM shipments s
JOIN logistics_companies lc ON s.company_id = lc.company_id
WHERE s.status = 'signed'
  AND s.shipped_at IS NOT NULL
  AND s.delivered_at IS NOT NULL
GROUP BY lc.company_id, lc.name
ORDER BY within_72h_rate DESC, signed_shipment_count DESC, lc.company_id
""".strip(),
            chart_type="bar",
            reasoning="Calculate signed-shipment delivery SLA by logistics company.",
            confidence=0.9,
        )

    if has_all("shipments") and "收货省份" in query and "配送最慢" in query:
        return SQLPlan(
            """
SELECT
  receiver_province,
  COUNT(*) AS signed_shipment_count,
  ROUND(AVG((julianday(delivered_at) - julianday(shipped_at)) * 24), 2) AS avg_delivery_hours
FROM shipments
WHERE status = 'signed'
  AND shipped_at IS NOT NULL
  AND delivered_at IS NOT NULL
GROUP BY receiver_province
HAVING COUNT(*) >= 50
ORDER BY avg_delivery_hours DESC, signed_shipment_count DESC, receiver_province
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Rank receiver provinces by signed-shipment average delivery duration.",
            confidence=0.9,
        )

    if has_all("orders", "shipments") and "尚未有已签收物流记录" in query:
        return SQLPlan(
            """
SELECT
  o.status AS order_status,
  COUNT(*) AS order_count
FROM orders o
LEFT JOIN shipments s
  ON o.order_id = s.order_id
 AND s.status = 'signed'
WHERE o.pay_status IN ('paid', 'partial_refund')
  AND s.shipment_id IS NULL
GROUP BY o.status
ORDER BY order_count DESC, order_status
""".strip(),
            chart_type="bar",
            reasoning="Use anti-join to find paid orders without signed shipment records.",
            confidence=0.9,
        )

    if has_all("return_orders") and "3日内完成率" in query:
        return SQLPlan(
            """
SELECT
  reason,
  COUNT(*) AS return_count,
  ROUND(AVG(julianday(complete_date) - julianday(apply_date)), 2) AS avg_complete_days,
  SUM(CASE WHEN julianday(complete_date) - julianday(apply_date) <= 3 THEN 1 ELSE 0 END) AS within_3d_count,
  ROUND(SUM(CASE WHEN julianday(complete_date) - julianday(apply_date) <= 3 THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS within_3d_rate
FROM return_orders
WHERE status IN ('completed', 'refunded')
  AND complete_date IS NOT NULL
GROUP BY reason
ORDER BY return_count DESC, reason
""".strip(),
            chart_type="bar",
            reasoning="Calculate completed/refunded return processing SLA by reason.",
            confidence=0.9,
        )

    if has_all("refunds", "payments") and "退款原因" in query and "原支付方式" in query:
        return SQLPlan(
            """
SELECT
  r.reason,
  p.method AS payment_method,
  COUNT(*) AS refund_count,
  ROUND(SUM(r.amount), 2) AS refund_amount
FROM refunds r
JOIN payments p ON r.payment_id = p.payment_id
WHERE r.status = 'success'
GROUP BY r.reason, p.method
ORDER BY refund_amount DESC, refund_count DESC, r.reason, payment_method
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Aggregate successful refund count and amount by reason and original payment method.",
            confidence=0.9,
        )

    return None


def _ecommerce_behavior_plan(query: str, table_names: set[str]) -> SQLPlan | None:
    """内置电商样例的行为转化 SQL 模板。"""

    def has_all(*tables: str) -> bool:
        return set(tables) <= table_names

    if has_all("user_events") and "四类行为" in query and "去重用户数" in query:
        return SQLPlan(
            """
SELECT
  source,
  COUNT(DISTINCT CASE WHEN event_type = 'view' THEN user_id END) AS view_users,
  COUNT(DISTINCT CASE WHEN event_type = 'add_cart' THEN user_id END) AS add_cart_users,
  COUNT(DISTINCT CASE WHEN event_type = 'favorite' THEN user_id END) AS favorite_users,
  COUNT(DISTINCT CASE WHEN event_type = 'share' THEN user_id END) AS share_users
FROM user_events
WHERE event_time >= '2024-01-01' AND event_time < '2025-01-01'
GROUP BY source
ORDER BY view_users DESC, source
""".strip(),
            chart_type="bar",
            reasoning="Build source funnel with distinct users per behavior event type.",
            confidence=0.9,
        )

    if has_all("user_events") and "互动事件占比" in query:
        return SQLPlan(
            """
SELECT
  source,
  COUNT(*) AS event_count,
  COUNT(DISTINCT user_id) AS active_users,
  SUM(CASE WHEN sku_id IS NOT NULL THEN 1 ELSE 0 END) AS sku_event_count,
  SUM(CASE WHEN event_type IN ('add_cart', 'favorite', 'share') THEN 1 ELSE 0 END) AS interaction_event_count,
  ROUND(SUM(CASE WHEN event_type IN ('add_cart', 'favorite', 'share') THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS interaction_rate
FROM user_events
WHERE event_time >= '2024-01-01' AND event_time < '2025-01-01'
GROUP BY source
ORDER BY event_count DESC, source
""".strip(),
            chart_type="bar",
            reasoning="Summarize source-level event mix and interaction rate.",
            confidence=0.9,
        )

    if has_all("user_events") and "会话数" in query and "加购事件占比" in query:
        return SQLPlan(
            """
SELECT
  strftime('%Y-%m', event_time) AS month,
  COUNT(*) AS event_count,
  COUNT(DISTINCT user_id) AS active_users,
  COUNT(DISTINCT user_id || '|' || session_id) AS session_count,
  ROUND(SUM(CASE WHEN event_type = 'add_cart' THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS add_cart_event_rate
FROM user_events
WHERE event_time >= '2024-01-01' AND event_time < '2025-01-01'
GROUP BY strftime('%Y-%m', event_time)
ORDER BY month
""".strip(),
            chart_type="line",
            reasoning="Track monthly behavior volume, active users, distinct sessions and add-cart share.",
            confidence=0.9,
        )

    if has_all("user_events", "skus") and "兴趣分" in query:
        return SQLPlan(
            """
WITH sku_events AS (
  SELECT
    sku_id,
    SUM(CASE WHEN event_type = 'view' THEN 1 ELSE 0 END) AS view_events,
    SUM(CASE WHEN event_type = 'add_cart' THEN 1 ELSE 0 END) AS add_cart_events,
    SUM(CASE WHEN event_type = 'favorite' THEN 1 ELSE 0 END) AS favorite_events,
    SUM(CASE WHEN event_type = 'share' THEN 1 ELSE 0 END) AS share_events
  FROM user_events
  WHERE sku_id IS NOT NULL
    AND event_time >= '2024-01-01' AND event_time < '2025-01-01'
  GROUP BY sku_id
)
SELECT
  s.sku_id,
  s.sku_name,
  view_events,
  add_cart_events,
  favorite_events,
  share_events,
  view_events + add_cart_events * 3 + favorite_events * 2 + share_events * 2 AS interest_score
FROM sku_events se
JOIN skus s ON se.sku_id = s.sku_id
ORDER BY interest_score DESC, view_events DESC, s.sku_id
LIMIT 10
""".strip(),
            chart_type="bar",
            reasoning="Score SKU interest from weighted 2024 behavior events.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if has_all("user_events") and "活跃天数分布" in query:
        return SQLPlan(
            """
WITH user_source_days AS (
  SELECT
    source,
    user_id,
    COUNT(DISTINCT date(event_time)) AS active_days,
    COUNT(*) AS event_count
  FROM user_events
  WHERE event_time >= '2024-01-01' AND event_time < '2025-01-01'
  GROUP BY source, user_id
)
SELECT
  source,
  COUNT(*) AS active_user_count,
  ROUND(AVG(active_days), 2) AS avg_active_days,
  SUM(CASE WHEN active_days >= 3 THEN 1 ELSE 0 END) AS users_with_3plus_active_days,
  ROUND(SUM(CASE WHEN active_days >= 3 THEN 1 ELSE 0 END) * 1.0 / COUNT(*), 4) AS users_with_3plus_active_days_rate,
  ROUND(AVG(event_count), 2) AS avg_events_per_user
FROM user_source_days
GROUP BY source
ORDER BY active_user_count DESC, source
""".strip(),
            chart_type="bar",
            reasoning="Aggregate per-user active day distribution by behavior source.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    return None


def _ecommerce_data_quality_plan(query: str, table_names: set[str]) -> SQLPlan | None:
    """内置电商样例的数据质量 SQL 模板。"""

    def has_all(*tables: str) -> bool:
        return set(tables) <= table_names

    if has_all("orders", "order_items") and "订单商品金额" in query and "明细小计" in query:
        return SQLPlan(
            """
WITH item_amount AS (
  SELECT order_id, ROUND(SUM(subtotal), 2) AS item_subtotal
  FROM order_items
  GROUP BY order_id
),
diffs AS (
  SELECT
    o.order_id,
    ROUND(o.product_amount - COALESCE(item_amount.item_subtotal, 0), 2) AS amount_diff
  FROM orders o
  LEFT JOIN item_amount ON o.order_id = item_amount.order_id
)
SELECT
  SUM(CASE WHEN ABS(amount_diff) > 0.01 THEN 1 ELSE 0 END) AS mismatch_order_count,
  ROUND(MAX(ABS(amount_diff)), 2) AS max_abs_diff,
  ROUND(SUM(ABS(amount_diff)), 2) AS total_abs_diff
FROM diffs
""".strip(),
            chart_type="kpi",
            reasoning="Reconcile order product amount against summed order item subtotals.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if has_all("orders", "payments") and "成功支付记录一致性" in query:
        return SQLPlan(
            """
WITH success_payments AS (
  SELECT order_id, ROUND(SUM(amount), 2) AS success_amount
  FROM payments
  WHERE status = 'success'
  GROUP BY order_id
)
SELECT
  o.pay_status,
  COUNT(*) AS order_count,
  SUM(CASE WHEN o.pay_status IN ('paid', 'partial_refund', 'refunded') AND sp.success_amount IS NULL THEN 1 ELSE 0 END) AS missing_success_payment_orders,
  SUM(CASE WHEN sp.success_amount IS NOT NULL AND ABS(sp.success_amount - o.total_amount) > 0.01 THEN 1 ELSE 0 END) AS success_payment_amount_mismatch_orders
FROM orders o
LEFT JOIN success_payments sp ON o.order_id = sp.order_id
GROUP BY o.pay_status
ORDER BY o.pay_status
""".strip(),
            chart_type="bar",
            reasoning="Reconcile order pay status against successful payment records.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if has_all("users", "orders") and "users.total_spent" in query:
        return SQLPlan(
            """
WITH paid_spend AS (
  SELECT user_id, ROUND(SUM(total_amount), 2) AS paid_order_amount
  FROM orders
  WHERE pay_status IN ('paid', 'partial_refund')
  GROUP BY user_id
)
SELECT
  u.user_id,
  u.username,
  ROUND(u.total_spent, 2) AS recorded_total_spent,
  COALESCE(ps.paid_order_amount, 0) AS paid_order_amount,
  ROUND(u.total_spent - COALESCE(ps.paid_order_amount, 0), 2) AS amount_diff,
  ROUND(ABS(u.total_spent - COALESCE(ps.paid_order_amount, 0)), 2) AS abs_amount_diff
FROM users u
LEFT JOIN paid_spend ps ON u.user_id = ps.user_id
ORDER BY abs_amount_diff DESC, u.user_id
LIMIT 10
""".strip(),
            chart_type="table",
            reasoning="Compare recorded user total_spent to paid order total by user.",
            confidence=0.9,
            advanced_features=("cte",),
        )

    if has_all("product_reviews", "order_items", "orders") and "评价" in query and "订单状态" in query:
        return SQLPlan(
            """
SELECT
  o.status AS order_status,
  COUNT(*) AS review_count,
  SUM(CASE WHEN o.status <> 'completed' THEN 1 ELSE 0 END) AS non_completed_review_count
FROM product_reviews pr
JOIN order_items oi ON pr.order_item_id = oi.order_item_id
JOIN orders o ON oi.order_id = o.order_id
GROUP BY o.status
ORDER BY review_count DESC, order_status
""".strip(),
            chart_type="bar",
            reasoning="Check product review counts by originating order status.",
            confidence=0.9,
        )

    if has_all("user_coupons", "orders") and "已使用用户券" in query and "订单关联一致性" in query:
        return SQLPlan(
            """
SELECT
  COUNT(*) AS used_coupon_count,
  SUM(CASE WHEN o.order_id IS NULL THEN 1 ELSE 0 END) AS missing_order_count,
  SUM(CASE WHEN o.order_id IS NOT NULL AND (o.coupon_id IS NULL OR o.coupon_id <> uc.coupon_id) THEN 1 ELSE 0 END) AS coupon_mismatch_count,
  SUM(CASE WHEN o.order_id IS NOT NULL AND date(uc.used_at) < o.order_date THEN 1 ELSE 0 END) AS used_before_order_date_count
FROM user_coupons uc
LEFT JOIN orders o ON uc.order_id = o.order_id
WHERE uc.status = 'used'
""".strip(),
            chart_type="kpi",
            reasoning="Validate used coupon records against linked orders and order dates.",
            confidence=0.9,
        )

    return None
