from __future__ import annotations

"""会话上下文管理。

Text2SQL 很容易遇到“继续看同比”“换成按地区”这类追问。本模块保存每个
session 最近几轮的 SQL、候选表和改写后问题，用于补全指代和构造 prompt 历史。
"""

from collections import defaultdict, deque

from text2sql.core.models import ConversationTurn


class ConversationMemory:
    """按 session_id 保存有限窗口的对话历史。"""

    def __init__(self, max_turns: int = 8) -> None:
        self.max_turns = max_turns
        self._sessions: dict[str, deque[ConversationTurn]] = defaultdict(lambda: deque(maxlen=max_turns))

    def add_turn(self, session_id: str, turn: ConversationTurn) -> None:
        self._sessions[session_id].append(turn)

    def get_turns(self, session_id: str) -> tuple[ConversationTurn, ...]:
        return tuple(self._sessions.get(session_id, ()))

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
