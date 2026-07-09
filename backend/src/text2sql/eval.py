from __future__ import annotations

"""评测 CLI。

评测从 JSONL 读取问题和期望项，逐条运行完整 Text2SQLWorkflow，
再计算表召回、SQL 精确匹配、关键词召回和执行成功率。
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from text2sql.accuracy.few_shot import InMemoryFewShotStore
from text2sql.accuracy.schema_semantics import SchemaSemantics
from text2sql.config import Settings
from text2sql.config.domain_profile import DomainProfile, set_active_domain_profile
from text2sql.core.clarification import AmbiguityDetector
from text2sql.core.graph import Text2SQLWorkflow
from text2sql.core.models import EvalCase, EvalResult, RetrievalHit, to_plain
from text2sql.core.sql_validator import normalize_sql
from text2sql.core.summarizer import DataInsightSummarizer

# 逐 case trace 落盘时执行结果最多保留的样例行数，避免大结果集撑爆存储。
_TRACE_MAX_ROWS = 20

# pass 门槛按端到端口径只看：能执行 + 结果值集正确。
# table_recall / keyword_recall / exact_sql / column_set_match / row_count_match / value_set_recall
# 均降为诊断指标单独报告——LLM 常靠子查询/few-shot 补齐 JOIN、用等价写法，据此判 fail 会低估真实准确率。
_GATING_METRICS: tuple[str, ...] = (
    "execution_success",
    "value_set_exact",
)

AfterCaseCallback = Callable[[list["EvalResult"], "EvalResult", int, int], None]


def _normalize_cell(value: Any) -> str:
    """把单元格值归一为可比较字符串：数值容差到 2 位小数，其余原样字符串化。

    LLM 生成 SQL 是否 ROUND、浮点求和顺序都会带来末位差异，2 位小数容差可吸收这类
    精度噪声，同时保留真实的业务数值差异（如金额相差 1 元以上仍会被判为不同）。
    """

    if value is None:
        return "∅"
    if isinstance(value, bool):
        # bool 是 int 子类，需在数值分支前拦截，避免 True/False 被当成 1/0。
        return str(value)
    try:
        return f"{round(float(value), 2):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _row_signature(row: dict[str, Any]) -> tuple[str, ...]:
    """把一行规整为可哈希签名：忽略列名，只按归一后的值构成有序多重集。

    评测关注业务值是否正确，而 LLM 生成 SQL 的列别名（如 dimension_value 或中文名）
    不可控，故列名无关；行内多个值排序后成元组，列顺序同样不影响比对。
    """

    return tuple(sorted(_normalize_cell(value) for value in row.values()))


def compare_result_sets(
    expected: list[dict[str, Any]], actual: list[dict[str, Any]]
) -> dict[str, float]:
    """执行结果级比对：行数、列集、值集（精确与部分召回）。

    - row_count_match：行数是否一致；
    - column_set_match：列集合是否一致；
    - value_set_exact：作为多重集是否完全一致；
    - value_set_recall：期望行中有多少被实际结果覆盖（部分匹配）。
    """

    metrics: dict[str, float] = {}
    metrics["row_count_match"] = float(len(expected) == len(actual))

    expected_columns = set().union(*[row.keys() for row in expected]) if expected else set()
    actual_columns = set().union(*[row.keys() for row in actual]) if actual else set()
    metrics["column_set_match"] = float(expected_columns == actual_columns)

    # 用多重集比较行，避免顺序与重复行影响结果。
    expected_counter = Counter(_row_signature(row) for row in expected)
    actual_counter = Counter(_row_signature(row) for row in actual)
    matched = sum((expected_counter & actual_counter).values())
    metrics["value_set_exact"] = float(expected_counter == actual_counter)
    expected_total = sum(expected_counter.values())
    if expected_total == 0:
        # 期望为空时：实际也为空算完全召回，否则为 0。
        metrics["value_set_recall"] = 1.0 if not actual_counter else 0.0
    else:
        metrics["value_set_recall"] = matched / expected_total
    return metrics


def compare_table_retrieval(
    expected_tables: list[str] | tuple[str, ...],
    retrieved_tables: list[str] | tuple[str, ...],
) -> dict[str, float]:
    """表级检索指标。

    公式：
    - table_recall@K = |G ∩ R_K| / |G|；
    - table_precision@K = |G ∩ R_K| / |R_K|；
    - table_f1@K = 2PR / (P + R)；
    - table_accuracy = 1{set(R_|G|) == G}，即前金标数量个候选是否刚好命中金标表集。

    其中 G 是金标表集合，R_K 是检索器返回的 top K 表集合。
    """

    gold = set(expected_tables)
    retrieved = list(retrieved_tables)
    metrics: dict[str, float] = {}
    if not gold:
        return metrics

    retrieved_set = set(retrieved)
    matched = len(gold & retrieved_set)
    recall = matched / len(gold)
    precision = matched / len(retrieved_set) if retrieved_set else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if precision + recall else 0.0
    top_gold_count = set(retrieved[: len(gold)])

    metrics["table_recall"] = recall
    metrics["table_precision"] = precision
    metrics["table_f1"] = f1
    metrics["table_accuracy"] = float(top_gold_count == gold)
    metrics["table_top1_hit"] = float(bool(retrieved and retrieved[0] in gold))
    return metrics


def _keyword_recall(sql: str | None, keywords: tuple[str, ...]) -> float:
    """计算 SQL 关键词召回，作为生成诊断指标。"""

    if not keywords:
        return 1.0
    normalized = normalize_sql(sql or "")
    matched = sum(1 for keyword in keywords if keyword.lower() in normalized)
    return matched / len(keywords)


def aggregate_metrics(results: list["EvalResult"]) -> dict[str, float]:
    """跨 case 聚合各指标的平均值，便于报告与趋势对比。"""

    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for result in results:
        for name, value in result.metrics.items():
            sums[name] += float(value) if isinstance(value, Decimal) else value
            counts[name] += 1
    return {name: sums[name] / counts[name] for name in sums}


class EvaluationRunner:
    """围绕 Text2SQLWorkflow 的回归评测执行器。"""

    def __init__(
        self,
        workflow: Text2SQLWorkflow,
        *,
        progress: bool = False,
        mode_name: str = "e2e",
    ) -> None:
        self.workflow = workflow
        self.progress = progress
        self.mode_name = mode_name

    async def run_case(self, case: EvalCase) -> EvalResult:
        # 每个 case 使用独立 session，避免对话记忆在评测样例之间串场。
        state = await self.workflow.run(case.query, session_id=f"eval-{case.case_id}")
        sql = state.get("generated_sql")
        errors: list[str] = []
        metrics: dict[str, float] = {}
        # 全环节 trace 在跑完 workflow 后统一采集，供报告与 MySQL 回溯复用。
        trace = self._build_case_trace(case, state)

        if state.get("clarification"):
            # 有些 case 预期就是澄清问题，例如含糊输入，不应算失败。
            if case.allow_clarification:
                metrics["clarification"] = 1.0
                return EvalResult(case.case_id, True, metrics, sql, trace=trace)
            errors.append("Unexpected clarification")

        retrieved_tables = [hit.table.name for hit in state.get("retrieval_hits", [])]
        if case.expected_tables:
            # 表级指标降为诊断：反映检索召回/排序质量（尤其多跳桥接表 skus/spus），但不阻断 pass——
            # LLM 常靠子查询/few-shot 补齐 JOIN 仍能得到正确结果。
            metrics.update(compare_table_retrieval(case.expected_tables, retrieved_tables))

        if case.expected_sql:
            # SQL 文本精确匹配对 LLM 不公平（等价写法众多），仅作诊断，不纳入 pass 门槛。
            metrics["exact_sql"] = float(normalize_sql(sql or "") == normalize_sql(case.expected_sql))

        if case.required_sql_keywords:
            # keyword_recall 降为诊断：等价 SQL 写法众多（如 AVG(子查询) 替代 SUM/COUNT），不阻断 pass。
            metrics["keyword_recall"] = _keyword_recall(sql, case.required_sql_keywords)

        execution = state.get("execution_result")
        # 执行成功率是端到端可用性的最后一道指标。
        metrics["execution_success"] = float(bool(execution and not execution.error))
        if execution and execution.error:
            errors.append(f"Execution failed: {execution.error}")

        if case.expected_result is not None:
            # 结果级比对：把期望结果集与真实执行结果按行/列/值对齐打分。
            actual_rows = list(execution.rows) if execution and not execution.error else []
            result_metrics = compare_result_sets(list(case.expected_result), actual_rows)
            metrics.update(result_metrics)
            if result_metrics["value_set_exact"] < 1.0:
                errors.append("Result set mismatch")

        # 只以硬指标（_GATING_METRICS）判定 pass；缺失的指标按满分处理（如未配 expected_result）。
        passed = not errors and all(metrics.get(name, 1.0) >= 1.0 for name in _GATING_METRICS)
        return EvalResult(case.case_id, passed, metrics, sql, tuple(errors), trace=trace)

    def _build_case_trace(self, case: EvalCase, state: dict[str, Any]) -> dict[str, Any]:
        """从 workflow 最终 state 采集逐环节 trace（检索/prompt/执行样例等）。"""

        execution = state.get("execution_result")
        plan = state.get("sql_plan")
        plan_warnings = list(getattr(plan, "warnings", ()) or ())
        quality_gate_issues = [
            warning.split(":", 1)[1]
            for warning in plan_warnings
            if warning.startswith("quality_gate_issue:")
        ]
        quality_gate_remaining_issues = [
            warning.split(":", 1)[1]
            for warning in plan_warnings
            if warning.startswith("quality_gate_remaining_issue:")
        ]
        hits = state.get("retrieval_hits", [])
        has_exec = bool(execution and not execution.error)
        rewritten = state.get("rewritten_query", "")
        clarification = state.get("clarification")
        return {
            "query": case.query,
            "rewritten_query": rewritten,
            "retrieval_hits": [
                {
                    "table": hit.table.name,
                    "score": hit.score,
                    "rerank_score": hit.rerank_score,
                    "reasons": list(hit.reasons),
                }
                for hit in hits
            ],
            "table_relationship": [to_plain(path) for path in state.get("table_relationship", [])],
            "few_shot_examples": self._collect_few_shot(rewritten or case.query),
            "prompt": state.get("sql_prompt"),
            "generated_sql": state.get("generated_sql"),
            "sql_reasoning": getattr(plan, "reasoning", None),
            "sql_confidence": getattr(plan, "confidence", None),
            "sql_advanced_features": list(getattr(plan, "advanced_features", ()) or ()),
            "sql_warnings": plan_warnings,
            "quality_gate_issues": quality_gate_issues,
            "quality_gate_remaining_issues": quality_gate_remaining_issues,
            "quality_gate_repaired": "quality_gate_repair" in plan_warnings,
            "quality_gate_repair_failed": "quality_gate_repair_failed" in plan_warnings,
            "quality_gate_repair_unresolved": "quality_gate_repair_unresolved" in plan_warnings,
            "quality_gate_template_fallback": "quality_gate_template_fallback" in plan_warnings,
            # 只截断落前若干行，避免大结果集撑爆存储；总行数另由 row_count 记录。
            "execution_rows": (
                [dict(row) for row in execution.rows[:_TRACE_MAX_ROWS]] if has_exec else []
            ),
            "row_count": execution.row_count if execution else None,
            "execution_error": execution.error if execution else None,
            "clarification": to_plain(clarification) if clarification else None,
        }

    def _collect_few_shot(self, query: str) -> list[Any]:
        """复用生成器的 few-shot 检索，记录本 case 命中的示例（幂等，仅评测使用）。"""

        generator = getattr(self.workflow, "sql_generator", None)
        store = getattr(generator, "few_shot_store", None)
        if store is None:
            return []
        try:
            top_k = getattr(generator, "few_shot_top_k", 3)
            return [to_plain(example) for example in store.search(query, top_k)]
        except Exception:  # pragma: no cover - few-shot 检索失败不应中断评测
            return []

    async def run(
        self,
        cases: list[EvalCase],
        after_case: AfterCaseCallback | None = None,
    ) -> list[EvalResult]:
        results: list[EvalResult] = []
        total = len(cases)
        for index, case in enumerate(cases, start=1):
            if self.progress:
                print(
                    f"[eval] {self.mode_name} {index}/{total} {case.case_id} start",
                    file=sys.stderr,
                    flush=True,
                )
            result = await self.run_case(case)
            if self.progress:
                status = "PASS" if result.passed else "FAIL"
                print(
                    f"[eval] {self.mode_name} {index}/{total} {case.case_id} {status}",
                    file=sys.stderr,
                    flush=True,
                )
            results.append(result)
            if after_case is not None:
                after_case(results, result, index, total)
        return results


class TableRetrievalEvaluationRunner(EvaluationRunner):
    """只评测 schema 检索，不进入关系解析、SQL 生成和执行。"""

    def __init__(
        self,
        workflow: Text2SQLWorkflow,
        top_k: int = 6,
        *,
        progress: bool = False,
    ) -> None:
        super().__init__(workflow, progress=progress, mode_name="retrieval")
        self.top_k = top_k

    async def run_case(self, case: EvalCase) -> EvalResult:
        hits = self.workflow.retriever.retrieve(case.query, top_k=self.top_k)
        retrieved_tables = [hit.table.name for hit in hits]
        metrics = compare_table_retrieval(case.expected_tables, retrieved_tables)
        errors: list[str] = []

        if not case.expected_tables:
            errors.append("Missing expected_tables for retrieval evaluation")
        else:
            missing = [table for table in case.expected_tables if table not in retrieved_tables]
            if missing:
                errors.append(f"Missing tables: {missing}")
            if metrics.get("table_accuracy", 0.0) < 1.0:
                errors.append("Top table set mismatch")

        state = {
            "rewritten_query": case.query,
            "retrieval_hits": hits,
            "table_relationship": [],
            "generated_sql": None,
            "execution_result": None,
            "clarification": None,
        }
        trace = self._build_case_trace(case, state)
        trace["mode"] = "retrieval"
        trace["top_k"] = self.top_k
        passed = not errors and metrics.get("table_recall", 0.0) >= 1.0 and metrics.get(
            "table_accuracy", 0.0
        ) >= 1.0
        return EvalResult(case.case_id, passed, metrics, None, tuple(errors), trace=trace)


class FixedTableEvaluationRunner(EvaluationRunner):
    """跳过 schema 召回，使用 case.fixed_tables 测 SQL 生成/执行能力。"""

    def __init__(self, workflow: Text2SQLWorkflow, *, progress: bool = False) -> None:
        super().__init__(workflow, progress=progress, mode_name="fixed-tables")

    async def run_case(self, case: EvalCase) -> EvalResult:
        fixed_table_names = case.fixed_tables or case.expected_tables
        metrics: dict[str, float] = {}
        errors: list[str] = []

        if case.expected_tables:
            fixed_metrics = compare_table_retrieval(case.expected_tables, fixed_table_names)
            metrics.update({f"fixed_{name}": value for name, value in fixed_metrics.items()})
        if not fixed_table_names:
            errors.append("No fixed_tables configured")

        table_map = {table.name: table for table in getattr(self.workflow, "tables", [])}
        missing_fixed = [name for name in fixed_table_names if name not in table_map]
        if missing_fixed:
            errors.append(f"Fixed tables not found: {missing_fixed}")

        fixed_tables = [table_map[name] for name in fixed_table_names if name in table_map]
        hits = [
            RetrievalHit(table=table, score=1.0, reasons=("fixed",))
            for table in fixed_tables
        ]
        relationships = self.workflow.relationship_resolver.paths_for_tables(fixed_tables)
        prompt = self.workflow.sql_generator.build_prompt(case.query, hits, relationships, "")
        plan = await self.workflow.sql_generator.agenerate(
            case.query, hits, relationships, "", prompt=prompt
        )
        execution = None
        if self.workflow.executor and plan.sql:
            execution = await self.workflow.executor.execute(plan.sql)

        state = {
            "rewritten_query": case.query,
            "retrieval_hits": hits,
            "table_relationship": relationships,
            "sql_prompt": prompt,
            "sql_plan": plan,
            "generated_sql": plan.sql,
            "execution_result": execution,
            "clarification": None,
        }
        trace = self._build_case_trace(case, state)
        trace["mode"] = "fixed-tables"
        trace["fixed_tables"] = list(fixed_table_names)

        if case.expected_sql:
            metrics["exact_sql"] = float(normalize_sql(plan.sql or "") == normalize_sql(case.expected_sql))
        if case.required_sql_keywords:
            metrics["keyword_recall"] = _keyword_recall(plan.sql, case.required_sql_keywords)

        metrics["execution_success"] = float(bool(execution and not execution.error))
        if execution and execution.error:
            errors.append(f"Execution failed: {execution.error}")
        elif not execution:
            errors.append("Execution was not run")

        if case.expected_result is None:
            metrics["llm_generation_accuracy"] = 0.0
            errors.append("Missing expected_result for fixed-tables accuracy")
        else:
            actual_rows = list(execution.rows) if execution and not execution.error else []
            result_metrics = compare_result_sets(list(case.expected_result), actual_rows)
            metrics.update(result_metrics)
            metrics["llm_generation_accuracy"] = result_metrics["value_set_exact"]
            metrics["llm_value_recall"] = result_metrics["value_set_recall"]
            if result_metrics["value_set_exact"] < 1.0:
                errors.append("Result set mismatch")

        passed = (
            not errors
            and all(metrics.get(name, 0.0) >= 1.0 for name in _GATING_METRICS)
            and metrics.get("llm_generation_accuracy", 0.0) >= 1.0
        )
        return EvalResult(case.case_id, passed, metrics, plan.sql, tuple(errors), trace=trace)


def load_cases(path: str | Path) -> list[EvalCase]:
    """读取 JSONL 格式的评测用例。"""

    cases: list[EvalCase] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        cases.append(
            EvalCase(
                case_id=payload["case_id"],
                query=payload["query"],
                expected_sql=payload.get("expected_sql"),
                expected_tables=tuple(payload.get("expected_tables", ())),
                required_sql_keywords=tuple(payload.get("required_sql_keywords", ())),
                allow_clarification=bool(payload.get("allow_clarification", False)),
                fixed_tables=tuple(payload.get("fixed_tables", ())),
                expected_result=(
                    tuple(payload["expected_result"])
                    if payload.get("expected_result") is not None
                    else None
                ),
            )
        )
    return cases


def summarize_results(results: list[EvalResult]) -> dict[str, Any]:
    """汇总整体通过率与各指标均值，供报告与落库复用。"""

    passed = sum(1 for result in results if result.passed)
    return {
        "total": len(results),
        "passed": passed,
        "pass_rate": passed / max(1, len(results)),
        # 各指标的跨 case 平均值，便于结果级准确率的横向/趋势对比。
        "metrics": aggregate_metrics(results),
    }


def _json_report_default(value: Any) -> Any:
    """兜底转换 MySQL 驱动返回的非 JSON 标量，避免长跑结束后报告写盘失败。"""

    plain = to_plain(value)
    if plain is not value:
        return plain
    if isinstance(value, set):
        return [to_plain(item) for item in sorted(value, key=str)]
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def write_report(
    path: str | Path,
    results: list[EvalResult],
    metadata: dict[str, Any] | None = None,
) -> None:
    """写出包含整体通过率和逐 case 详情的 JSON 报告。"""

    payload = {
        "metadata": metadata or {},
        "summary": summarize_results(results),
        "results": [to_plain(result) for result in results],
    }
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_report_default),
        encoding="utf-8",
    )


def checkpoint_report_path(report_path: str | Path, explicit_path: str | Path | None = None) -> Path:
    """返回逐 case checkpoint 路径；默认紧挨最终报告，便于崩溃后直接恢复分析。"""

    if explicit_path:
        return Path(explicit_path)
    return Path(f"{report_path}.checkpoint.json")


def write_checkpoint_report(
    path: str | Path,
    results: list[EvalResult],
    metadata: dict[str, Any],
    *,
    completed: int,
    total: int,
) -> None:
    """写出可随时读取的部分 eval 报告；失败只影响 checkpoint，不影响主 eval。"""

    checkpoint_metadata = {
        **metadata,
        "checkpoint": True,
        "completed": completed,
        "total": total,
    }
    write_report(path, results, metadata=checkpoint_metadata)


def persist_eval_run(repository, results: list[EvalResult]):
    """把一次评测的聚合结果写入 eval_runs，返回落库记录（支持多次对比）。"""

    summary = summarize_results(results)
    return repository.record_run(
        total=summary["total"],
        passed=summary["passed"],
        pass_rate=summary["pass_rate"],
        metrics=summary["metrics"],
    )


def persist_case_results(repository, run_id: int, results: list[EvalResult]):
    """把逐 case trace 落 eval_case_results（关联 run_id），返回落库记录列表。"""

    from text2sql.persistence.repository import EvalCaseResultRecord

    records = []
    for result in results:
        trace = result.trace or {}
        metrics = to_plain(dict(result.metrics))
        sql_generation = sql_generation_trace_metrics(trace)
        if sql_generation:
            metrics["sql_generation"] = sql_generation
        record = EvalCaseResultRecord(
            run_id=run_id,
            case_id=result.case_id,
            query=trace.get("query", ""),
            rewritten_query=trace.get("rewritten_query", ""),
            passed=result.passed,
            retrieval_hits=to_plain(trace.get("retrieval_hits", [])),
            table_relationship=to_plain(trace.get("table_relationship", [])),
            few_shot_examples=to_plain(trace.get("few_shot_examples", [])),
            prompt=trace.get("prompt"),
            generated_sql=result.generated_sql,
            execution_rows=to_plain(trace.get("execution_rows", [])),
            row_count=trace.get("row_count"),
            clarification=to_plain(trace.get("clarification")),
            metrics=metrics,
            errors=list(result.errors),
        )
        records.append(repository.record_case_result(record))
    return records


def sql_generation_trace_metrics(trace: dict[str, Any]) -> dict[str, Any]:
    """把 SQL 生成诊断折叠进 case metrics，便于无需迁移表结构也能落库查询。"""

    fields = (
        "sql_reasoning",
        "sql_confidence",
        "sql_advanced_features",
        "sql_warnings",
        "quality_gate_issues",
        "quality_gate_remaining_issues",
        "quality_gate_repaired",
        "quality_gate_repair_failed",
        "quality_gate_repair_unresolved",
        "quality_gate_template_fallback",
    )
    return {field: to_plain(trace[field]) for field in fields if field in trace}


def _redact_database_url(url: str) -> str:
    """隐藏 URL 密码，避免连接失败时把凭据打印到终端。"""

    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    userinfo, host = rest.rsplit("@", 1)
    if ":" not in userinfo:
        return url
    username, _password = userinfo.split(":", 1)
    return f"{scheme}://{username}:***@{host}"


def _build_eval_run_repository(settings: "Settings", *, allow_inmemory: bool = False):
    """构建 eval repository；默认必须落元数据库，只有显式允许才用内存兜底。"""

    def _inmemory_repository():
        from text2sql.persistence.repository import InMemoryEvalRunRepository

        return InMemoryEvalRunRepository()

    try:
        from text2sql.persistence.db import (
            _HAS_SQLALCHEMY,
            create_metadata_engine,
            create_session_factory,
            init_models,
        )

        if not settings.metadata_database_url:
            raise RuntimeError("TEXT2SQL_METADATA_DATABASE_URL is not configured")
        if not _HAS_SQLALCHEMY:
            raise RuntimeError("SQLAlchemy is not installed")

        from text2sql.persistence.repository import SqlAlchemyEvalRunRepository

        engine = create_metadata_engine(settings.metadata_database_url)
        init_models(engine)
        return SqlAlchemyEvalRunRepository(create_session_factory(engine))
    except Exception as exc:
        if allow_inmemory:
            return _inmemory_repository()
        url = _redact_database_url(getattr(settings, "metadata_database_url", "") or "")
        detail = f" for {url}" if url else ""
        raise RuntimeError(
            "Eval persistence requires a writable metadata database"
            f"{detail}. Set TEXT2SQL_METADATA_DATABASE_URL to MySQL/SQLite,"
            " or pass --no-persist for a local-only report."
        ) from exc


def validate_external_ai_eval_consent(
    settings: "Settings",
    *,
    mode: str,
    allow_external_ai_eval: bool = False,
    allow_external_llm_eval: bool = False,
) -> None:
    """真实 eval 使用外部 AI 服务前，要求显式授权。

    eval 的外部调用不止 SQL 生成 LLM：只要配置了 DASHSCOPE_API_KEY，
    schema embedding 和 rerank 也会把 schema 文档 / case query 发送到 DashScope。
    因此这里必须在 workflow 构造前统一拦截，避免长跑中途才卡到网络调用。
    """

    services = _external_ai_eval_services(settings, mode=mode)
    if not services:
        return
    if (
        allow_external_ai_eval
        or allow_external_llm_eval
        or _env_flag_enabled("TEXT2SQL_ALLOW_EXTERNAL_AI_EVAL")
        or _env_flag_enabled("TEXT2SQL_ALLOW_EXTERNAL_LLM_EVAL")
    ):
        return
    service_names = ", ".join(service["name"] for service in services)
    endpoints = "; ".join(f'{service["name"]}: {service["endpoint"]}' for service in services)
    raise RuntimeError(
        "Eval would send local schema, case queries, few-shot examples, SQL prompts, or schema "
        f"documents to external AI services ({service_names}; {endpoints}). Re-run with "
        "--allow-external-ai-eval or set TEXT2SQL_ALLOW_EXTERNAL_AI_EVAL=1 only after explicit "
        "approval. The legacy --allow-external-llm-eval flag is still accepted as an alias."
    )


def validate_external_llm_eval_consent(
    settings: "Settings",
    *,
    mode: str,
    allow_external_llm_eval: bool = False,
) -> None:
    """向后兼容旧函数名；真实检查已覆盖 LLM/embedding/rerank。"""

    validate_external_ai_eval_consent(
        settings,
        mode=mode,
        allow_external_llm_eval=allow_external_llm_eval,
    )


def _external_ai_eval_services(settings: "Settings", *, mode: str) -> list[dict[str, str]]:
    services: list[dict[str, str]] = []
    if (
        mode != "retrieval"
        and _eval_uses_configured_llm(settings)
        and _is_external_endpoint(getattr(settings, "dashscope_http_base_url", None))
    ):
        services.append(
            {
                "name": "llm",
                "endpoint": getattr(settings, "dashscope_http_base_url", None)
                or "DashScope default endpoint",
            }
        )
    if _eval_uses_dashscope_retrieval_services(settings, mode=mode):
        services.extend(
            [
                {"name": "embedding", "endpoint": "DashScope TextEmbedding endpoint"},
                {"name": "rerank", "endpoint": "DashScope TextReRank endpoint"},
            ]
        )
    return services


def _eval_uses_configured_llm(settings: "Settings") -> bool:
    return bool(getattr(settings, "use_llm", False) and getattr(settings, "dashscope_api_key", None))


def _eval_uses_dashscope_retrieval_services(settings: "Settings", *, mode: str) -> bool:
    # Text2SQLWorkflow 当前在所有 eval 模式下都会构造 HybridTableRetriever；
    # 构造时可能重建 schema 向量缓存，retrieval/e2e 查询时还会做 query embedding 和 rerank。
    return mode in {"e2e", "retrieval", "fixed-tables"} and bool(
        getattr(settings, "dashscope_api_key", None) or os.getenv("DASHSCOPE_API_KEY")
    )


def _is_external_endpoint(url: str | None) -> bool:
    if not url:
        # DashScope SDK default endpoint is remote.
        return True
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return host not in {"", "localhost", "127.0.0.1", "::1"}


def _env_flag_enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def main() -> None:
    """命令行入口：构造 workflow，运行 cases，输出报告路径。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="SQLite db path or SQLAlchemy URL")
    parser.add_argument("--cases", required=True, help="JSONL evaluation cases")
    parser.add_argument("--report", default="eval_report.json")
    parser.add_argument(
        "--mode",
        choices=("e2e", "retrieval", "fixed-tables"),
        default="e2e",
        help=(
            "e2e=完整链路；retrieval=只测表召回/表准确；"
            "fixed-tables=跳过召回，用 fixed_tables/expected_tables 测 SQL 生成"
        ),
    )
    parser.add_argument("--top-k", type=int, default=8, help="retrieval 模式的表召回 top K")
    parser.add_argument("--cache-dir", default=".text2sql_cache", help="schema 向量缓存目录")
    parser.add_argument("--no-persist", action="store_true", help="只写 JSON report，不落 eval_runs")
    parser.add_argument(
        "--checkpoint-report",
        help="逐 case checkpoint JSON 路径；默认写到 <report>.checkpoint.json",
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="关闭逐 case checkpoint 写入；长耗时 LLM eval 不建议关闭",
    )
    parser.add_argument(
        "--allow-inmemory-persist",
        action="store_true",
        help="开发/测试兜底：元数据库不可用时允许把 eval 结果写入内存仓库",
    )
    parser.add_argument("--quiet", action="store_true", help="不输出逐 case 进度")
    parser.add_argument(
        "--llm-summary",
        action="store_true",
        help="e2e 模式也使用 LLM 生成自然语言总结；默认关闭以避免总结阶段影响 SQL 评测",
    )
    parser.add_argument(
        "--allow-external-llm-eval",
        action="store_true",
        help="兼容旧参数：等同于 --allow-external-ai-eval。",
    )
    parser.add_argument(
        "--allow-external-ai-eval",
        action="store_true",
        help=(
            "明确允许 eval 将本地 schema/case/few-shot prompt/schema 文档发送到外部 "
            "LLM/embedding/rerank endpoint；真实 AI 评测需要显式开启。"
        ),
    )
    args = parser.parse_args()

    db = args.db if "://" in args.db else f"sqlite:///{args.db}"
    settings = Settings()
    validate_external_ai_eval_consent(
        settings,
        mode=args.mode,
        allow_external_ai_eval=args.allow_external_ai_eval,
        allow_external_llm_eval=args.allow_external_llm_eval,
    )
    domain_profile = DomainProfile.from_yaml(settings.domain_profile_path)
    set_active_domain_profile(domain_profile)
    semantics = SchemaSemantics.from_yaml(settings.schema_metadata_path)
    few_shot_store = InMemoryFewShotStore.from_jsonl(settings.few_shot_seed_path)
    workflow = Text2SQLWorkflow(
        database_url_or_path=db,
        cache_dir=args.cache_dir,
        schema_semantics=semantics,
        few_shot_store=few_shot_store,
        few_shot_top_k=settings.few_shot_top_k,
        schema_retrieval_top_k=settings.schema_retrieval_top_k,
        sql_repair_max_retries=settings.sql_repair_max_retries,
        # 评测收紧数据域澄清触发，反映端到端生成能力；线上 API 仍用默认保守门槛。
        ambiguity_detector=AmbiguityDetector.for_evaluation(domain_profile),
        domain_profile=domain_profile,
    )
    if not args.llm_summary:
        # Eval 指标只依赖 SQL 执行结果；总结阶段默认走本地摘要，避免 LLM 总结耗时/超时污染评测。
        workflow.summarizer = DataInsightSummarizer(None)
    cases = load_cases(args.cases)
    progress = not args.quiet
    if args.mode == "retrieval":
        runner = TableRetrievalEvaluationRunner(workflow, top_k=args.top_k, progress=progress)
    elif args.mode == "fixed-tables":
        runner = FixedTableEvaluationRunner(workflow, progress=progress)
    else:
        runner = EvaluationRunner(workflow, progress=progress)
    repository = None
    if not args.no_persist:
        # 先校验元数据库可用性，避免真实 LLM eval 跑完后才发现无法落库。
        repository = _build_eval_run_repository(
            settings, allow_inmemory=args.allow_inmemory_persist
        )
    report_metadata = {
        "mode": args.mode,
        "top_k": args.top_k,
        "cases": args.cases,
        "db": args.db,
        "cache_dir": args.cache_dir,
    }
    checkpoint_path = (
        checkpoint_report_path(args.report, args.checkpoint_report)
        if not args.no_checkpoint
        else None
    )

    def checkpoint(results: list[EvalResult], _result: EvalResult, completed: int, total: int) -> None:
        if checkpoint_path is None:
            return
        try:
            write_checkpoint_report(
                checkpoint_path,
                results,
                report_metadata,
                completed=completed,
                total=total,
            )
        except Exception as exc:
            # checkpoint 是防长跑丢结果的保险，不应反过来中断真实 LLM eval。
            print(
                f"[eval] checkpoint write failed: {exc}",
                file=sys.stderr,
                flush=True,
            )

    results = asyncio.run(
        runner.run(cases, after_case=None if checkpoint_path is None else checkpoint)
    )

    record = None
    if repository is not None:
        # 先落 MySQL，再写最终 JSON；若最终报告序列化/磁盘异常，trace 也不会整轮丢失。
        record = persist_eval_run(repository, results)
        persist_case_results(repository, record.id, results)
    write_report(args.report, results, metadata=report_metadata)
    passed = sum(1 for result in results if result.passed)
    run_suffix = f" eval_run_id={record.id}" if record else ""
    checkpoint_suffix = f" checkpoint={checkpoint_path}" if checkpoint_path else ""
    print(
        f"mode={args.mode} pass_rate={passed}/{len(results)} report={args.report}"
        f"{checkpoint_suffix}{run_suffix}"
    )


if __name__ == "__main__":
    main()
