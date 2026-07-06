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
import logging
from pathlib import Path
from typing import AsyncIterator

from text2sql.accuracy.few_shot import FewShotStore
from text2sql.accuracy.schema_semantics import SchemaSemantics
from text2sql.config.domain_profile import DomainProfile, set_active_domain_profile
from text2sql.config.settings import Settings
from text2sql.core.clarification import AmbiguityDetector
from text2sql.core.context import ConversationMemory
from text2sql.core.executor import QueryExecutor
from text2sql.core.llm import LLMProvider, default_llm_provider
from text2sql.core.models import (
    AgentState,
    RetrievalHit,
    TableInfo,
    to_plain,
)
from text2sql.core.observability import TraceRecorder
from text2sql.core.relationships import RelationshipResolver, default_relationship_resolver
from text2sql.core.render import ChartRecommender
from text2sql.core.retrieval import HybridTableRetriever
from text2sql.core.schema import load_schema
from text2sql.core.sql_generator import PromptedSQLGenerator
from text2sql.core.summarizer import DataInsightSummarizer
from text2sql.persistence.repository import HistoryRecord

try:  # pragma: no cover - import path differs by langgraph version
    from langgraph.graph import END, START, StateGraph
except Exception:  # pragma: no cover
    END = START = None
    StateGraph = None

logger = logging.getLogger(__name__)


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
        schema_semantics: SchemaSemantics | None = None,
        few_shot_store: FewShotStore | None = None,
        few_shot_top_k: int = 3,
        sql_repair_max_retries: int = 2,
        ambiguity_detector: AmbiguityDetector | None = None,
        domain_profile: DomainProfile | None = None,
    ) -> None:
        self.domain_profile = domain_profile or DomainProfile.from_yaml(Settings().domain_profile_path)
        set_active_domain_profile(self.domain_profile)
        # schema 可以由调用方直接注入，也可以从数据库连接动态 introspect。
        # 测试里常直接传 tables；API/CLI 则通常走 database_url_or_path。
        if tables is None:
            if not database_url_or_path:
                raise ValueError("Either tables or database_url_or_path is required")
            tables = load_schema(database_url_or_path)
        self.tables = tables
        self.database_url_or_path = database_url_or_path

        # schema 语义元数据（中文别名/枚举字典）默认空，缺失不影响主链路；
        # 同时供检索语料增强与 SQL prompt 注入两处共享。
        self.schema_semantics = schema_semantics or SchemaSemantics.empty()

        # 下面这些组件分别对应主链路中的一个阶段；默认实现都支持本地可测降级。
        self.memory = memory or ConversationMemory()
        self.retriever = HybridTableRetriever(
            tables,
            cache_dir=cache_dir,
            semantics=self.schema_semantics,
            domain_profile=self.domain_profile,
        )
        self.relationship_resolver = relationship_resolver or default_relationship_resolver(tables)
        llm_provider = llm_provider or default_llm_provider()
        self.sql_generator = PromptedSQLGenerator(
            llm_provider,
            semantics=self.schema_semantics,
            few_shot_store=few_shot_store,
            few_shot_top_k=few_shot_top_k,
            domain_profile=self.domain_profile,
        )
        # 默认使用线上保守门槛；评测可注入 AmbiguityDetector.for_evaluation() 收紧触发。
        self.ambiguity_detector = ambiguity_detector or AmbiguityDetector(domain_profile=self.domain_profile)
        self.executor = QueryExecutor(database_url_or_path, tables) if database_url_or_path else None
        self.summarizer = DataInsightSummarizer(llm_provider)
        self.chart_recommender = ChartRecommender(self.domain_profile)
        # SQL 自修复重试上限：执行报错时最多回 LLM 重生成的次数，默认 2（取自 settings）。
        self.sql_repair_max_retries = sql_repair_max_retries
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
        graph.add_node("sql_repair", self.repair_sql)
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
        # 执行后判定：报错且未达重试上限则进入 sql_repair 自修复回环，否则继续总结。
        graph.add_conditional_edges(
            "sql_executor",
            self._route_after_execution,
            {"repair": "sql_repair", "continue": "summarize"},
        )
        # 修复后重新执行，形成 executor → repair → executor 的有界回环。
        graph.add_edge("sql_repair", "sql_executor")
        graph.add_edge("summarize", "data_render")
        graph.add_edge("data_render", END)
        return graph.compile()

    def _route_after_execution(self, state: AgentState) -> str:
        """判断执行结果是否需要自修复：有错误且未超重试上限才回环。"""

        result = state.get("execution_result")
        attempts = state.get("attempts", 0)
        if result is not None and result.error and attempts < self.sql_repair_max_retries:
            return "repair"
        return "continue"

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

        def _cancelled() -> bool:
            return bool(cancel_event and cancel_event.is_set())

        # 前置线性节点：召回 → 关系 → 生成；澄清或空 SQL 时提前结束。
        for node_name, node in (
            ("schema_inspector", self.schema_inspector),
            ("table_relationship", self.table_relationship),
            ("sql_generator", self.generate_sql),
        ):
            if _cancelled():
                yield "cancelled", {"cancelled": True}
                return
            partial = await node(state)
            state.update(partial)
            yield node_name, partial
            if state.get("clarification") or (node_name == "sql_generator" and not state.get("generated_sql")):
                return

        # 执行 + 自修复有界回环：与 LangGraph 的 sql_executor↔sql_repair 条件边保持同一语义。
        while True:
            if _cancelled():
                yield "cancelled", {"cancelled": True}
                return
            partial = await self.execute_sql(state)
            state.update(partial)
            yield "sql_executor", partial
            if self._route_after_execution(state) != "repair":
                break
            if _cancelled():
                yield "cancelled", {"cancelled": True}
                return
            partial = await self.repair_sql(state)
            state.update(partial)
            yield "sql_repair", partial

        # 后置线性节点：总结 → 渲染。
        for node_name, node in (
            ("summarize", self.summarize),
            ("data_render", self.render),
        ):
            if _cancelled():
                yield "cancelled", {"cancelled": True}
                return
            partial = await node(state)
            state.update(partial)
            yield node_name, partial

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
        query = state.get("rewritten_query") or state["user_query"]
        hits = state.get("retrieval_hits", [])
        relationships = state.get("table_relationship", [])
        # 预构造 prompt 落 state 供评测 trace 回溯，并透传给生成器复用（LLM 路径零额外构造）。
        prompt = self.sql_generator.build_prompt(query, hits, relationships, context)
        plan = await self.sql_generator.agenerate(query, hits, relationships, context, prompt=prompt)
        # 关键节点结构化日志：带 trace_id 贯穿，便于按链路回溯生成的 SQL。
        logger.info(
            "sql_generated",
            extra={"trace_id": state.get("trace_id"), "chart_type": plan.chart_type},
        )
        return {
            "sql_plan": plan,
            "generated_sql": plan.sql,
            "chart_type": plan.chart_type,
            "sql_prompt": prompt,
        }

    async def execute_sql(self, state: AgentState) -> AgentState:
        # 执行前 QueryExecutor 会先做只读 SQL 和表字段校验。
        if not self.executor:
            return {"execution_result": None, "errors": ["No database configured"]}
        result = await self.executor.execute(state.get("generated_sql"))
        logger.info(
            "sql_executed",
            extra={
                "trace_id": state.get("trace_id"),
                "row_count": result.row_count,
                "error": result.error,
            },
        )
        return {"execution_result": result}

    async def repair_sql(self, state: AgentState) -> AgentState:
        # 自修复节点：带着上一次执行报错回 LLM 重生成 SQL，attempts 记录已重试次数。
        attempts = state.get("attempts", 0) + 1
        result = state.get("execution_result")
        error = result.error if result and result.error else "unknown execution error"
        session_id = state.get("session_id", "default")
        logger.warning(
            "sql_repair_attempt",
            extra={"trace_id": state.get("trace_id"), "attempts": attempts, "error": error},
        )
        plan = await self.sql_generator.aregenerate_with_error(
            state.get("generated_sql"),
            error,
            state.get("rewritten_query") or state["user_query"],
            state.get("retrieval_hits", []),
            state.get("table_relationship", []),
            self.memory.build_context_block(session_id),
        )
        # 透出字段与契约一致：attempts / generated_sql / sql_plan（chart_type 一并刷新）。
        return {
            "attempts": attempts,
            "sql_plan": plan,
            "generated_sql": plan.sql,
            "chart_type": plan.chart_type,
        }

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
        self._remember_turn(state, render_spec)
        return {"render_spec": render_spec, "chart_type": render_spec.chart_type}

    def _remember_turn(self, state: AgentState, render_spec) -> None:
        # 成功走到渲染阶段后，落库完整一轮记录：既供追问改写，也供 REST 历史回看。
        session_id = state.get("session_id", "default")
        hits: list[RetrievalHit] = state.get("retrieval_hits", [])
        result = state.get("execution_result")
        # status 反映本轮端到端结果，便于历史列表区分成功/失败轮次。
        status = "success" if result is not None and not result.error else "error"
        self.memory.add_record(
            HistoryRecord(
                session_id=session_id,
                user_query=state["user_query"],
                rewritten_query=state.get("rewritten_query") or state["user_query"],
                generated_sql=state.get("generated_sql"),
                tables=[hit.table.name for hit in hits],
                summary=state.get("summary") or render_spec.title,
                chart_type=render_spec.chart_type,
                row_count=result.row_count if result else None,
                elapsed_ms=result.elapsed_ms if result else None,
                trace_id=state.get("trace_id"),
                status=status,
                render_spec=to_plain(render_spec),
                execution_result=to_plain(result),
            )
        )

    @staticmethod
    def public_event(partial: AgentState) -> dict:
        return to_plain(partial)
