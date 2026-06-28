from __future__ import annotations

"""Text2SQL 的总控编排层。

这个模块把用户问题加工成一条可执行链路：
1. 改写上下文问题并检索候选表；
2. 解析候选表之间的 JOIN 路径；
3. 生成 SQL；
4. 执行 SQL；
5. 总结结果并推荐渲染方式。

每个节点都只返回自己新增的 AgentState 字段，LangGraph 或手写 fallback
再把这些 partial state 合并成完整执行上下文。
"""

import asyncio
from pathlib import Path
from typing import AsyncIterator

from text2sql.clarification import AmbiguityDetector
from text2sql.context import ConversationMemory
from text2sql.executor import QueryExecutor
from text2sql.llm import LLMProvider, default_llm_provider
from text2sql.models import (
    AgentState,
    Clarification,
    ConversationTurn,
    RelationshipPath,
    RetrievalHit,
    TableInfo,
    to_plain,
)
from text2sql.observability import TraceRecorder
from text2sql.relationships import RelationshipResolver, default_relationship_resolver
from text2sql.render import ChartRecommender
from text2sql.retrieval import HybridTableRetriever
from text2sql.schema import load_schema
from text2sql.sql_generator import PromptedSQLGenerator
from text2sql.summarizer import DataInsightSummarizer

try:  # pragma: no cover - import path differs by langgraph version
    from langgraph.graph import END, START, StateGraph
except Exception:  # pragma: no cover
    END = START = None
    StateGraph = None


class Text2SQLWorkflow:
    """面向 API、测试和评测复用的 Text2SQL 入口。

    外部通常只需要调用 run/astream。构造函数负责把数据库 schema、召回器、
    关系解析器、SQL 生成器、执行器、总结器等组件接好。
    """

    def __init__(
        self,
        tables: list[TableInfo] | None = None,
        database_url_or_path: str | None = None,
        cache_dir: str | Path = ".text2sql_cache",
        memory: ConversationMemory | None = None,
        relationship_resolver: RelationshipResolver | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        # schema 可以由调用方直接注入，也可以从数据库连接动态 introspect。
        # 测试里常直接传 tables；API/CLI 则通常走 database_url_or_path。
        if tables is None:
            if not database_url_or_path:
                raise ValueError("Either tables or database_url_or_path is required")
            tables = load_schema(database_url_or_path)
        self.tables = tables
        self.database_url_or_path = database_url_or_path

        # 下面这些组件分别对应主链路中的一个阶段；默认实现都支持本地可测降级。
        self.memory = memory or ConversationMemory()
        self.retriever = HybridTableRetriever(tables, cache_dir=cache_dir)
        self.relationship_resolver = relationship_resolver or default_relationship_resolver(tables)
        llm_provider = llm_provider or default_llm_provider()
        self.sql_generator = PromptedSQLGenerator(llm_provider)
        self.ambiguity_detector = AmbiguityDetector()
        self.executor = QueryExecutor(database_url_or_path, tables) if database_url_or_path else None
        self.summarizer = DataInsightSummarizer(llm_provider)
        self.chart_recommender = ChartRecommender()
        self.graph = self._build_graph()

    def _build_graph(self):
        # 安装了 LangGraph 时使用声明式状态图；未安装时 astream 会用同样顺序手动执行，
        # 这样单元测试和离线开发不依赖完整生产依赖。
        if StateGraph is None:
            return None
        graph = StateGraph(AgentState)

        # 节点命名会直接出现在 SSE event 中，因此这里的名字也是前端可观察的阶段名。
        graph.add_node("schema_inspector", self.schema_inspector)
        graph.add_node("table_relationship", self.table_relationship)
        graph.add_node("sql_generator", self.generate_sql)
        graph.add_node("sql_executor", self.execute_sql)
        graph.add_node("summarize", self.summarize)
        graph.add_node("data_render", self.render)
        graph.add_edge(START, "schema_inspector")

        # 如果问题太模糊或候选表不明确，尽早返回澄清问题，不继续生成 SQL。
        graph.add_conditional_edges(
            "schema_inspector",
            lambda state: "end" if state.get("clarification") else "continue",
            {"end": END, "continue": "table_relationship"},
        )
        graph.add_edge("table_relationship", "sql_generator")

        # SQL 生成失败或选择返回 NULL 时同样提前结束，避免执行空 SQL。
        graph.add_conditional_edges(
            "sql_generator",
            lambda state: "end" if not state.get("generated_sql") else "continue",
            {"end": END, "continue": "sql_executor"},
        )
        graph.add_edge("sql_executor", "summarize")
        graph.add_edge("summarize", "data_render")
        graph.add_edge("data_render", END)
        return graph.compile()

    async def run(self, query: str, session_id: str = "default") -> AgentState:
        # run 返回最终完整状态，适合测试、评测 CLI 或一次性调用。
        state: AgentState = {"user_query": query, "session_id": session_id, "attempts": 0, "errors": []}
        if self.graph is not None:
            return await self.graph.ainvoke(state)
        async for _event, partial in self.astream(query, session_id):
            state.update(partial)
        return state

    async def astream(
        self,
        query: str,
        session_id: str = "default",
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[tuple[str, AgentState]]:
        # astream 按节点吐出增量状态，API 层会把这些 partial state 包成 SSE。
        state: AgentState = {"user_query": query, "session_id": session_id, "attempts": 0, "errors": []}
        if self.graph is not None:
            async for event in self.graph.astream(state, stream_mode="updates"):
                if cancel_event and cancel_event.is_set():
                    yield "cancelled", {"cancelled": True}
                    return
                for node_name, partial in event.items():
                    yield node_name, partial
            return

        for node_name, node in (
            ("schema_inspector", self.schema_inspector),
            ("table_relationship", self.table_relationship),
            ("sql_generator", self.generate_sql),
            ("sql_executor", self.execute_sql),
            ("summarize", self.summarize),
            ("data_render", self.render),
        ):
            # 手动 fallback 与 LangGraph 链路保持同一顺序和同一中断语义。
            if cancel_event and cancel_event.is_set():
                yield "cancelled", {"cancelled": True}
                return
            partial = await node(state)
            state.update(partial)
            yield node_name, partial
            if state.get("clarification") or (node_name == "sql_generator" and not state.get("generated_sql")):
                return

    async def schema_inspector(self, state: AgentState) -> AgentState:
        # 入口节点：结合会话历史改写问题，再用混合召回找到最可能相关的表。
        session_id = state.get("session_id", "default")
        query = state["user_query"]
        rewritten_query = self.memory.rewrite_query(session_id, query)
        hits = self.retriever.retrieve(rewritten_query, top_k=6)
        has_context = bool(self.memory.get_turns(session_id))

        # 澄清判断使用原始问题，避免“上一轮上下文”让真实含糊的追问被误判为清晰。
        clarification = self.ambiguity_detector.detect(query, hits, has_context)
        return {
            "rewritten_query": rewritten_query,
            "db_info": [hit.table for hit in hits],
            "retrieval_hits": hits,
            "clarification": clarification,
            "trace_id": TraceRecorder().trace_id,
        }

    async def table_relationship(self, state: AgentState) -> AgentState:
        # 把候选表投影成 JOIN 路径提示，后续 SQL 生成只能沿这些关系拼表。
        hits: list[RetrievalHit] = state.get("retrieval_hits", [])
        tables = [hit.table for hit in hits]
        paths = self.relationship_resolver.paths_for_tables(tables)
        return {"table_relationship": paths}

    async def generate_sql(self, state: AgentState) -> AgentState:
        if state.get("clarification"):
            return {"generated_sql": None}
        session_id = state.get("session_id", "default")
        context = self.memory.build_context_block(session_id)

        # 传入 rewritten_query、候选表、关系路径和对话上下文，生成器可以选择 LLM 或规则 fallback。
        plan = await self.sql_generator.agenerate(
            state.get("rewritten_query") or state["user_query"],
            state.get("retrieval_hits", []),
            state.get("table_relationship", []),
            context,
        )
        return {"sql_plan": plan, "generated_sql": plan.sql, "chart_type": plan.chart_type}

    async def execute_sql(self, state: AgentState) -> AgentState:
        # 执行前 QueryExecutor 会先做只读 SQL 和表字段校验。
        if not self.executor:
            return {"execution_result": None, "errors": ["No database configured"]}
        result = await self.executor.execute(state.get("generated_sql"))
        return {"execution_result": result}

    async def summarize(self, state: AgentState) -> AgentState:
        # 总结阶段只依赖执行结果；无数据库时保留已生成 SQL，给调用方自行执行。
        result = state.get("execution_result")
        if result is None:
            return {"summary": "未配置数据库，已生成 SQL 但未执行。"}
        summary = await self.summarizer.asummarize(state["user_query"], result)
        return {"summary": summary}

    async def render(self, state: AgentState) -> AgentState:
        # 渲染阶段只给前端一个结构化建议，不负责真正画图。
        result = state.get("execution_result")
        plan = state.get("sql_plan")
        if result is None or plan is None:
            return {}
        render_spec = self.chart_recommender.recommend(state["user_query"], plan, result)
        self._remember_turn(state, render_spec.title)
        return {"render_spec": render_spec, "chart_type": render_spec.chart_type}

    def _remember_turn(self, state: AgentState, summary: str) -> None:
        # 成功走到渲染阶段后，把本轮问题、SQL 和表名放入 session 记忆，供追问继承。
        session_id = state.get("session_id", "default")
        hits: list[RetrievalHit] = state.get("retrieval_hits", [])
        self.memory.add_turn(
            session_id,
            ConversationTurn(
                user_query=state["user_query"],
                rewritten_query=state.get("rewritten_query") or state["user_query"],
                generated_sql=state.get("generated_sql"),
                tables=tuple(hit.table.name for hit in hits),
                summary=state.get("summary") or summary,
            ),
        )

    @staticmethod
    def public_event(partial: AgentState) -> dict:
        return to_plain(partial)
