from __future__ import annotations

"""模糊问题检测。

在企业取数场景里，宁愿返回澄清问题，也不要在表/指标不明确时生成危险的
“看似合理”SQL。该模块根据召回结果、问题文本和会话上下文决定是否中断链路。
"""

from text2sql.models import Clarification, RetrievalHit


class AmbiguityDetector:
    """把空问题、无候选表、指标缺失和指代缺失转成 Clarification。"""

    def detect(self, query: str, hits: list[RetrievalHit], has_context: bool = False) -> Clarification | None:
        stripped = query.strip()
        if not stripped:
            return Clarification("请补充你想查询的业务问题。", reason="empty_query")

        if not hits:
            return Clarification(
                "没有找到足够相关的数据表，请说明业务对象或指标口径。",
                reason="no_schema_hit",
            )

        if self._too_vague(stripped) and not has_context:
            return Clarification(
                "这个问题还缺少明确的指标或分析维度，你想看哪个业务指标？",
                options=("订单金额", "订单数量", "客户数量", "商品销量"),
                reason="vague_metric",
            )

        top = hits[0]
        close_hits = [
            hit for hit in hits[1:4] if top.score > 0 and (top.score - hit.score) / top.score < 0.08
        ]
        if len(close_hits) >= 2:
            # top 分数非常接近时，让用户确认数据域，避免选错事实表。
            options = tuple(hit.table.name for hit in [top, *close_hits])
            return Clarification(
                "这个问题可能对应多张表，请确认要使用哪个数据域。",
                options=options,
                reason="close_schema_candidates",
            )

        if self._has_pronoun(stripped) and not has_context:
            return Clarification(
                "问题里有指代词，但当前会话没有可继承的上下文，请说明具体对象。",
                reason="missing_context",
            )

        return None

    def _too_vague(self, query: str) -> bool:
        # “看一下数据”这类问题没有指标词时，需要先问清楚口径。
        vague_words = ("情况", "数据", "看一下", "分析一下", "表现", "怎么样")
        metric_words = (
            "金额",
            "数量",
            "订单",
            "客户",
            "销售",
            "收入",
            "增长",
            "排名",
            "趋势",
            "转化",
        )
        return any(word in query for word in vague_words) and not any(
            word in query for word in metric_words
        )

    def _has_pronoun(self, query: str) -> bool:
        # 有指代但没有历史上下文时，生成器无法知道“这个/上面”指什么。
        return any(word in query for word in ("它", "这个", "这些", "其中", "上面", "刚才"))
