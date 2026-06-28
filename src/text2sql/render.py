from __future__ import annotations

"""结果渲染推荐。

这个模块不直接画图，而是根据 SQLPlan、用户问题和结果字段生成 RenderSpec。
前端可以用 RenderSpec 选择 ECharts/表格/KPI 等展示方式。
"""

from text2sql.models import ChartType, ExecutionResult, RenderSpec, SQLPlan


NUMERIC_HINTS = ("amount", "value", "count", "rate", "price", "metric", "total", "qty", "quantity")
TIME_HINTS = ("date", "time", "month", "year", "period", "day")
DIMENSION_HINTS = ("name", "category", "type", "status", "region", "city", "dimension")


class ChartRecommender:
    """根据生成计划和结果字段推荐图表类型及轴字段。"""

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
        x = first_matching(columns, TIME_HINTS) or first_matching(columns, DIMENSION_HINTS)
        y_columns = tuple(column for column in columns if is_numeric_like(column) and column != x)
        if not y_columns and len(columns) >= 2:
            y_columns = (columns[-1],)
        series = first_matching(columns, ("series", "category", "type", "status"))
        title = build_title(query, chart_type)
        return RenderSpec(chart_type, x=x, y=y_columns, series=series, title=title)

    def _infer_chart_type(
        self, query: str, columns: list[str], result: ExecutionResult
    ) -> ChartType:
        # 先看用户明确意图，再用字段形态兜底。
        lowered = query.lower()
        if any(word in lowered for word in ("趋势", "环比", "同比", "走势", "时间")):
            return "line"
        if any(word in lowered for word in ("占比", "比例")):
            return "pie" if result.row_count <= 8 else "bar"
        if any(word in lowered for word in ("漏斗", "转化")):
            return "funnel"
        if any(word in lowered for word in ("桑基", "流向")):
            return "sankey"
        if any(word in lowered for word in ("热力", "矩阵")):
            return "heatmap"
        if any(word in lowered for word in ("散点", "相关")):
            return "scatter"
        if any(word in lowered for word in ("分布", "直方")):
            return "histogram"
        if result.row_count == 1 and len(columns) <= 3:
            return "kpi"
        if first_matching(columns, TIME_HINTS):
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


def is_numeric_like(column: str) -> bool:
    """根据字段名粗略判断是否可作为指标列。"""

    lowered = column.lower()
    return any(hint in lowered for hint in NUMERIC_HINTS)


def build_title(query: str, chart_type: str) -> str:
    """用用户问题生成图表标题，过长时截断。"""

    compact = query.strip().replace("\n", " ")
    if len(compact) > 40:
        compact = compact[:37] + "..."
    return compact or f"{chart_type} result"
