from __future__ import annotations

"""执行结果总结。

总结器把 ExecutionResult 转成面向业务用户的自然语言说明。启用 LLM 时走
prompt 总结；否则用本地统计摘要，保证失败和空结果也有可读反馈。
"""

from statistics import mean

from text2sql.llm import LLMProvider
from text2sql.models import ExecutionResult


class DataInsightSummarizer:
    """LLM 优先、本地统计兜底的数据洞察总结器。"""

    def __init__(self, llm_provider: LLMProvider | None = None) -> None:
        self.llm_provider = llm_provider

    async def asummarize(self, query: str, result: ExecutionResult) -> str:
        # 执行失败时直接走本地总结，避免把错误结果再交给 LLM 发散解释。
        if not self.llm_provider or result.error:
            return self.summarize(query, result)
        prompt = self._build_prompt(query, result)
        try:
            response = await self.llm_provider.complete(prompt)
            return response.strip() or self.summarize(query, result)
        except Exception:
            return self.summarize(query, result)

    def summarize(self, query: str, result: ExecutionResult) -> str:
        # 本地摘要只做稳妥统计：行数、数值列合计/均值/极值和首末变化。
        if result.error:
            return f"查询执行失败：{result.error}"
        if not result.rows:
            return "查询成功，但结果为空；建议检查筛选条件或时间范围。"

        numeric_columns = self._numeric_columns(result)
        findings: list[str] = [f"共返回 {result.row_count} 行结果。"]
        for column in numeric_columns[:3]:
            values = [float(row[column]) for row in result.rows if row.get(column) is not None]
            if not values:
                continue
            findings.append(
                f"{column} 合计 {sum(values):.2f}，平均 {mean(values):.2f}，"
                f"最大 {max(values):.2f}，最小 {min(values):.2f}。"
            )
            if len(values) >= 3:
                delta = values[-1] - values[0]
                direction = "上升" if delta > 0 else "下降" if delta < 0 else "持平"
                findings.append(f"{column} 从首行到末行整体{direction}，变化 {delta:.2f}。")

        if len(findings) == 1:
            sample = result.rows[0]
            findings.append(f"首行样例：{sample}")
        return "\n".join(findings)

    def _numeric_columns(self, result: ExecutionResult) -> list[str]:
        """从实际返回值判断哪些列可做数值统计。"""

        columns: list[str] = []
        for column in result.columns:
            values = [row.get(column) for row in result.rows]
            if any(isinstance(value, (int, float)) for value in values):
                columns.append(column)
        return columns

    def _build_prompt(self, query: str, result: ExecutionResult) -> str:
        """限制样例行数，避免把大结果集完整塞进 LLM prompt。"""

        rows = list(result.rows[:30])
        return f"""你是一名数据趋势分析师。
请基于用户问题和 SQL 结果，输出结构化业务解读，包含关键发现、趋势、异常和可能的下一步分析。

用户问题: {query}
字段: {', '.join(result.columns)}
行数: {result.row_count}
样例数据: {rows}
"""
