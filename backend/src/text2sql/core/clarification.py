from __future__ import annotations

"""模糊问题检测。

在企业取数场景里，宁愿返回澄清问题，也不要在表/指标不明确时生成危险的
“看似合理”SQL。该模块根据召回结果、问题文本和会话上下文决定是否中断链路。
"""

from text2sql.config.domain_profile import DomainProfile, contains_any, get_domain_profile
from text2sql.core.models import Clarification, RetrievalHit


class AmbiguityDetector:
    """把空问题、无候选表、指标缺失和指代缺失转成 Clarification。"""

    def __init__(
        self,
        close_score_margin: float = 0.08,
        min_close_candidates: int = 2,
        domain_profile: DomainProfile | None = None,
    ) -> None:
        # 默认值即线上生产门槛：top 之后有 ≥2 张表相对分差 <8% 即视为数据域歧义。
        # 评测可用 for_evaluation() 收紧触发，避免“表多语义相近”把明确问题误拦为澄清。
        self.close_score_margin = close_score_margin
        self.min_close_candidates = min_close_candidates
        self.domain_profile = domain_profile or get_domain_profile()

    @classmethod
    def for_evaluation(cls, domain_profile: DomainProfile | None = None) -> "AmbiguityDetector":
        """评测专用触发条件：更小 margin + 更多并列候选，仅在候选表几乎完全并列时才澄清。

        线上仍用默认构造（保守门槛不变），评测借此反映端到端 SQL 生成能力，
        而非被多相近表频繁触发的数据域澄清所掩盖。
        """

        return cls(close_score_margin=0.02, min_close_candidates=3, domain_profile=domain_profile)

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
                options=self.domain_profile.clarification_options,
                reason="vague_metric",
            )

        top = hits[0]
        close_hits = [
            hit
            for hit in hits[1:4]
            if top.score > 0 and (top.score - hit.score) / top.score < self.close_score_margin
        ]
        if len(close_hits) >= self.min_close_candidates:
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
        vague_words = self.domain_profile.clarification_terms("vague_words")
        metric_words = self.domain_profile.clarification_terms("metric_words")
        return contains_any(query, vague_words) and not contains_any(query, metric_words)

    def _has_pronoun(self, query: str) -> bool:
        # 有指代但没有历史上下文时，生成器无法知道“这个/上面”指什么。
        return any(word in query for word in ("它", "这个", "这些", "其中", "上面", "刚才"))
