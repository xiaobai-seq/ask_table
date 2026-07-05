from __future__ import annotations

"""评测 CLI。

评测从 JSONL 读取问题和期望项，逐条运行完整 Text2SQLWorkflow，
再计算表召回、SQL 精确匹配、关键词召回和执行成功率。
"""

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from text2sql.accuracy.few_shot import InMemoryFewShotStore
from text2sql.accuracy.schema_semantics import SchemaSemantics
from text2sql.config import Settings
from text2sql.core.clarification import AmbiguityDetector
from text2sql.core.graph import Text2SQLWorkflow
from text2sql.core.models import EvalCase, EvalResult, to_plain
from text2sql.core.sql_validator import normalize_sql

# 逐 case trace 落盘时执行结果最多保留的样例行数，避免大结果集撑爆存储。
_TRACE_MAX_ROWS = 20


def _row_signature(row: dict[str, Any]) -> frozenset[tuple[str, str]]:
    """把一行规整为可哈希签名：键 + 字符串化后的值，便于做多重集比对。

    用字符串化值规避 int/float（如 100 与 100.0）和数据库驱动类型差异带来的误判。
    """

    return frozenset((str(key), str(value)) for key, value in row.items())


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


def aggregate_metrics(results: list["EvalResult"]) -> dict[str, float]:
    """跨 case 聚合各指标的平均值，便于报告与趋势对比。"""

    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for result in results:
        for name, value in result.metrics.items():
            sums[name] += value
            counts[name] += 1
    return {name: sums[name] / counts[name] for name in sums}


class EvaluationRunner:
    """围绕 Text2SQLWorkflow 的回归评测执行器。"""

    def __init__(self, workflow: Text2SQLWorkflow) -> None:
        self.workflow = workflow

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

        expected_tables = set(case.expected_tables)
        retrieved_tables = {hit.table.name for hit in state.get("retrieval_hits", [])}
        if expected_tables:
            # 表召回检查发生在 SQL 之前，用来定位“选表错”还是“生成错”。
            metrics["table_recall"] = len(expected_tables & retrieved_tables) / len(expected_tables)
            if metrics["table_recall"] < 1.0:
                errors.append(f"Missing tables: {sorted(expected_tables - retrieved_tables)}")

        if case.expected_sql:
            metrics["exact_sql"] = float(normalize_sql(sql or "") == normalize_sql(case.expected_sql))
            if metrics["exact_sql"] < 1.0:
                errors.append("SQL exact match failed")

        if case.required_sql_keywords:
            normalized = normalize_sql(sql or "")
            matched = sum(1 for keyword in case.required_sql_keywords if keyword.lower() in normalized)
            metrics["keyword_recall"] = matched / len(case.required_sql_keywords)
            if metrics["keyword_recall"] < 1.0:
                errors.append("Required SQL keyword missing")

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

        passed = not errors and all(value >= 1.0 for value in metrics.values())
        return EvalResult(case.case_id, passed, metrics, sql, tuple(errors), trace=trace)

    def _build_case_trace(self, case: EvalCase, state: dict[str, Any]) -> dict[str, Any]:
        """从 workflow 最终 state 采集逐环节 trace（检索/prompt/执行样例等）。"""

        execution = state.get("execution_result")
        hits = state.get("retrieval_hits", [])
        has_exec = bool(execution and not execution.error)
        rewritten = state.get("rewritten_query", "")
        clarification = state.get("clarification")
        return {
            "query": case.query,
            "rewritten_query": rewritten,
            "retrieval_hits": [
                {"table": hit.table.name, "score": hit.score, "rerank_score": hit.rerank_score}
                for hit in hits
            ],
            "table_relationship": [to_plain(path) for path in state.get("table_relationship", [])],
            "few_shot_examples": self._collect_few_shot(rewritten or case.query),
            "prompt": state.get("sql_prompt"),
            "generated_sql": state.get("generated_sql"),
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

    async def run(self, cases: list[EvalCase]) -> list[EvalResult]:
        return [await self.run_case(case) for case in cases]


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


def write_report(path: str | Path, results: list[EvalResult]) -> None:
    """写出包含整体通过率和逐 case 详情的 JSON 报告。"""

    payload = {
        "summary": summarize_results(results),
        "results": [to_plain(result) for result in results],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
        record = EvalCaseResultRecord(
            run_id=run_id,
            case_id=result.case_id,
            query=trace.get("query", ""),
            rewritten_query=trace.get("rewritten_query", ""),
            passed=result.passed,
            retrieval_hits=trace.get("retrieval_hits", []),
            table_relationship=trace.get("table_relationship", []),
            few_shot_examples=trace.get("few_shot_examples", []),
            prompt=trace.get("prompt"),
            generated_sql=result.generated_sql,
            execution_rows=trace.get("execution_rows", []),
            row_count=trace.get("row_count"),
            clarification=trace.get("clarification"),
            metrics=dict(result.metrics),
            errors=list(result.errors),
        )
        records.append(repository.record_case_result(record))
    return records


def _build_eval_run_repository(settings: "Settings"):
    """构建 eval_runs repository：可用 SQLAlchemy 则落库，否则降级内存。"""

    try:
        from text2sql.persistence.db import (
            _HAS_SQLALCHEMY,
            create_metadata_engine,
            create_session_factory,
            init_models,
        )

        if _HAS_SQLALCHEMY and settings.metadata_database_url:
            from text2sql.persistence.repository import SqlAlchemyEvalRunRepository

            engine = create_metadata_engine(settings.metadata_database_url)
            init_models(engine)
            return SqlAlchemyEvalRunRepository(create_session_factory(engine))
    except Exception:  # pragma: no cover - 缺依赖/连接失败时降级
        pass
    from text2sql.persistence.repository import InMemoryEvalRunRepository

    return InMemoryEvalRunRepository()


def main() -> None:
    """命令行入口：构造 workflow，运行 cases，输出报告路径。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="SQLite db path or SQLAlchemy URL")
    parser.add_argument("--cases", required=True, help="JSONL evaluation cases")
    parser.add_argument("--report", default="eval_report.json")
    args = parser.parse_args()

    db = args.db if "://" in args.db else f"sqlite:///{args.db}"
    settings = Settings()
    semantics = SchemaSemantics.from_yaml(settings.schema_metadata_path)
    few_shot_store = InMemoryFewShotStore.from_jsonl(settings.few_shot_seed_path)
    workflow = Text2SQLWorkflow(
        database_url_or_path=db,
        schema_semantics=semantics,
        few_shot_store=few_shot_store,
        few_shot_top_k=settings.few_shot_top_k,
        sql_repair_max_retries=settings.sql_repair_max_retries,
        # 评测收紧数据域澄清触发，反映端到端生成能力；线上 API 仍用默认保守门槛。
        ambiguity_detector=AmbiguityDetector.for_evaluation(),
    )
    cases = load_cases(args.cases)
    results = asyncio.run(EvaluationRunner(workflow).run(cases))
    write_report(args.report, results)
    # 聚合结果落 eval_runs、逐 case trace 落 eval_case_results，便于趋势对比与逐环节回溯。
    repository = _build_eval_run_repository(settings)
    record = persist_eval_run(repository, results)
    persist_case_results(repository, record.id, results)
    passed = sum(1 for result in results if result.passed)
    print(f"pass_rate={passed}/{len(results)} report={args.report} eval_run_id={record.id}")


if __name__ == "__main__":
    main()
