from __future__ import annotations

"""会话上下文管理。

Text2SQL 很容易遇到“继续看同比”“换成按地区”这类追问。本模块保存每个
session 最近几轮的 SQL、候选表和改写后问题，用于补全指代和构造 prompt 历史。
"""

from text2sql.core.models import ConversationTurn
from text2sql.persistence.repository import HistoryRecord, InMemoryHistoryRepository


class ConversationMemory:
    """按 session_id 维护对话历史，并通过 repository 持久化。

    rewrite/context 只关心最近 max_turns 轮；底层存储交给 HistoryRepository：
    默认内存实现（测试/降级），可注入 SQLAlchemy 实现落 MySQL。这样追问改写所需的
    短窗口与可回看的长期历史共用同一份数据，避免双写不一致。
    """

    def __init__(self, max_turns: int = 8, repository=None) -> None:
        self.max_turns = max_turns
        # 缺省内存实现保证离线测试不依赖 MySQL；生产由 API 注入 DB 实现。
        self.repository = repository or InMemoryHistoryRepository()

    def add_turn(self, session_id: str, turn: ConversationTurn) -> None:
        # 兼容旧调用：用 ConversationTurn 的有限字段构造历史记录落库。
        self.add_record(
            HistoryRecord(
                session_id=session_id,
                user_query=turn.user_query,
                rewritten_query=turn.rewritten_query,
                generated_sql=turn.generated_sql,
                tables=list(turn.tables),
                summary=turn.summary,
            )
        )

    def add_record(self, record: HistoryRecord) -> HistoryRecord:
        # 落库完整记录（含 chart_type/trace_id/render_spec 等），供 REST 回看。
        return self.repository.add_turn(record)

    def get_turns(self, session_id: str) -> tuple[ConversationTurn, ...]:
        records = self.repository.get_session_history(session_id)[-self.max_turns :]
        return tuple(
            ConversationTurn(
                user_query=record.user_query,
                rewritten_query=record.rewritten_query,
                generated_sql=record.generated_sql,
                tables=tuple(record.tables),
                summary=record.summary,
            )
            for record in records
        )

    def rewrite_query(self, session_id: str, query: str) -> str:
        # 只有看起来像追问时才拼接上一轮上下文；独立问题保持原样，避免污染召回。
        turns = self.get_turns(session_id)
        if not turns:
            return query
        latest = turns[-1]
        if self._looks_contextual(query):
            parts = [f"上一轮问题: {latest.rewritten_query}"]
            if latest.tables:
                parts.append(f"上一轮涉及表: {', '.join(latest.tables)}")
            if latest.generated_sql:
                parts.append(f"上一轮SQL: {latest.generated_sql}")
            parts.append(f"当前追问: {query}")
            return "\n".join(parts)
        return query

    def build_context_block(self, session_id: str) -> str:
        # context_block 会被放进 SQL prompt，让 LLM/规则能看到历史 SQL 和表名。
        turns = self.get_turns(session_id)
        if not turns:
            return ""
        lines = ["对话历史:"]
        for index, turn in enumerate(turns[-self.max_turns :], start=1):
            tables = ", ".join(turn.tables) if turn.tables else "无"
            lines.append(
                f"{index}. 用户: {turn.user_query}; 改写: {turn.rewritten_query}; 表: {tables}; "
                f"SQL: {turn.generated_sql or 'NULL'}"
            )
        return "\n".join(lines)

    def _looks_contextual(self, query: str) -> bool:
        # 短句和含指代/改写词的问题，通常需要继承上一轮语义。
        lowered = query.lower()
        contextual_words = (
            "继续",
            "上面",
            "刚才",
            "上一",
            "这个",
            "这些",
            "它",
            "他们",
            "那",
            "其中",
            "再",
            "同比",
            "环比",
            "换成",
            "改为",
            "按照",
        )
        if len(query.strip()) <= 12:
            return True
        return any(word in lowered for word in contextual_words)
