from __future__ import annotations

"""结果渲染推荐。

这个模块不直接画图，而是根据 SQLPlan、用户问题和结果字段生成 RenderSpec。
前端可以用 RenderSpec 选择 ECharts/表格/KPI 等展示方式。
"""

from text2sql.config.domain_profile import DomainProfile, contains_any, get_domain_profile
from text2sql.core.models import ChartType, ExecutionResult, RenderSpec, SQLPlan


NUMERIC_HINTS = get_domain_profile().render_hints("numeric_hints")
TIME_HINTS = get_domain_profile().render_hints("time_hints")
DIMENSION_HINTS = get_domain_profile().render_hints("dimension_hints")


class ChartRecommender:
    """根据生成计划和结果字段推荐图表类型及轴字段。"""

    def __init__(self, domain_profile: DomainProfile | None = None) -> None:
        self.domain_profile = domain_profile or get_domain_profile()

    def recommend(self, query: str, plan: SQLPlan, result: ExecutionResult) -> RenderSpec:
        columns = list(result.columns)
        if result.error or not columns:
            return RenderSpec("table", title="查询结果")

        # SQL 生成阶段如果已经明确给出图表类型，优先尊重生成器判断。
        if plan.chart_type != "table":
            chart_type = plan.chart_type
        else:
            chart_type = self._infer_chart_type(query, columns, result)

        # x 优先时间/维度字段，y 选择数值字段；这些都是前端渲染的最小必要信息。
        x = first_matching(columns, self.domain_profile.render_hints("time_hints")) or first_matching(
            columns, self.domain_profile.render_hints("dimension_hints")
        )
        y_columns = tuple(
            column
            for column in columns
            if is_numeric_like(column, self.domain_profile.render_hints("numeric_hints")) and column != x
        )
        if not y_columns and len(columns) >= 2:
            y_columns = (columns[-1],)
        series = first_matching(columns, self.domain_profile.render_hints("series_hints"))
        title = build_title(query, chart_type)
        return RenderSpec(chart_type, x=x, y=y_columns, series=series, title=title)

    def _infer_chart_type(
        self, query: str, columns: list[str], result: ExecutionResult
    ) -> ChartType:
        # 先看用户明确意图，再用字段形态兜底。
        profile = self.domain_profile
        if contains_any(query, profile.chart_intent_terms("line")):
            return "line"
        if contains_any(query, profile.chart_intent_terms("ratio")):
            return "pie" if result.row_count <= 8 else "bar"
        if contains_any(query, profile.chart_intent_terms("funnel")):
            return "funnel"
        if contains_any(query, profile.chart_intent_terms("sankey")):
            return "sankey"
        if contains_any(query, profile.chart_intent_terms("heatmap")):
            return "heatmap"
        if contains_any(query, profile.chart_intent_terms("scatter")):
            return "scatter"
        if contains_any(query, profile.chart_intent_terms("histogram")):
            return "histogram"
        if result.row_count == 1 and len(columns) <= 3:
            return "kpi"
        if first_matching(columns, profile.render_hints("time_hints")):
            return "line"
        if len(columns) >= 2:
            return "bar"
        return "table"


def first_matching(columns: list[str], hints: tuple[str, ...]) -> str | None:
    """返回第一个名字命中 hint 的字段。"""

    for column in columns:
        lowered = column.lower()
        if any(hint in lowered for hint in hints):
            return column
    return None


def is_numeric_like(column: str, hints: tuple[str, ...] | None = None) -> bool:
    """根据字段名粗略判断是否可作为指标列。"""

    lowered = column.lower()
    return any(hint in lowered for hint in (hints or NUMERIC_HINTS))


def build_title(query: str, chart_type: str) -> str:
    """用用户问题生成图表标题，过长时截断。"""

    compact = query.strip().replace("\n", " ")
    if len(compact) > 40:
        compact = compact[:37] + "..."
    return compact or f"{chart_type} result"
