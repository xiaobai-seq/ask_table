from __future__ import annotations

"""Eval 报告分析工具。

这个模块不调用 LLM、不执行目标 SQL。它用于长耗时真实 eval 结束后快速定位：
失败 case、表召回/结果准确指标、SQL 质量门禁触发和残留问题。

输入可以是已经生成的 JSON report，也可以是 eval_runs / eval_case_results 中持久化的
run。后者用于最终 JSON 写盘失败时，从元数据库继续复盘。
"""

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from text2sql.config import Settings
from text2sql.core.models import RetrievalHit, TableInfo, to_plain
from text2sql.core.sql_generator import infer_sql_dialect, inspect_sql_quality
from text2sql.persistence.repository import (
    EvalCaseResultRecord,
    EvalRunRecord,
    EvalRunRepository,
)


def load_case_queries(path: str | Path | None) -> dict[str, str]:
    """读取 JSONL cases，返回 case_id -> query；path 为空时安全降级为空字典。"""

    if not path:
        return {}
    queries: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        queries[payload["case_id"]] = payload["query"]
    return queries


def load_report(path: str | Path) -> dict[str, Any]:
    """读取 eval JSON report。"""

    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_eval_run_repository(metadata_database_url: str | None = None) -> EvalRunRepository:
    """按元数据库 URL 构建 eval repository，供 report 分析读取历史 run。"""

    url = metadata_database_url or Settings().metadata_database_url
    if not url:
        raise RuntimeError("TEXT2SQL_METADATA_DATABASE_URL is not configured")

    try:
        from text2sql.persistence.db import (
            _HAS_SQLALCHEMY,
            create_metadata_engine,
            create_session_factory,
            init_models,
        )

        if not _HAS_SQLALCHEMY:
            raise RuntimeError("SQLAlchemy is not installed")

        from text2sql.persistence.repository import SqlAlchemyEvalRunRepository

        engine = create_metadata_engine(url)
        init_models(engine)
        return SqlAlchemyEvalRunRepository(create_session_factory(engine))
    except Exception as exc:
        raise RuntimeError(
            "Cannot read eval runs from metadata database"
            f" {_redact_database_url(url)}"
        ) from exc


def load_report_from_repository(
    repository: EvalRunRepository,
    *,
    run_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从 eval repository 读取 run/cases，并转成 JSON report 兼容 payload。"""

    run = _select_eval_run(repository, run_id)
    if run.id is None:
        raise RuntimeError("Selected eval run has no id")
    return report_payload_from_eval_run(
        run,
        repository.list_case_results(run.id),
        metadata=metadata,
    )


def report_payload_from_eval_run(
    run: EvalRunRecord,
    case_results: list[EvalCaseResultRecord],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把持久化 run/case records 转成与 write_report 一致的 payload。"""

    payload_metadata = {
        "source": "repository",
        "eval_run_id": run.id,
        "run_at": run.run_at,
        **(metadata or {}),
    }
    return to_plain(
        {
            "metadata": payload_metadata,
            "summary": {
                "total": run.total,
                "passed": run.passed,
                "pass_rate": run.pass_rate,
                "metrics": run.metrics or {},
            },
            "results": [_case_record_to_report_result(record) for record in case_results],
        }
    )


def analyze_report(payload: dict[str, Any], case_queries: dict[str, str] | None = None) -> dict[str, Any]:
    """分析 eval report，返回可 JSON 序列化的汇总与逐 case 诊断。"""

    case_queries = case_queries or {}
    results = payload.get("results", [])
    metadata = payload.get("metadata") or {}
    sql_dialect = infer_sql_dialect(metadata.get("db"))
    total = len(results)
    passed = sum(1 for result in results if result.get("passed"))
    failed = total - passed

    issue_counts: Counter[str] = Counter()
    error_counts: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []
    passed_flagged: list[dict[str, Any]] = []

    trace_issue_cases = 0
    repaired_cases = 0
    repair_failed_cases = 0
    repair_unresolved_cases = 0
    template_fallback_cases = 0
    remaining_issue_cases = 0
    offline_failed_flagged = 0
    offline_passed_flagged = 0
    offline_unflagged_failures: list[str] = []

    for result in results:
        case_id = result.get("case_id", "")
        trace = result.get("trace") or {}
        query = case_queries.get(case_id) or trace.get("query") or ""
        generated_sql = result.get("generated_sql") or trace.get("generated_sql") or ""
        hits = _trace_hits_to_retrieval_hits(trace.get("retrieval_hits", []))
        offline_issues = (
            inspect_sql_quality(query, generated_sql, hits, sql_dialect=sql_dialect)
            if query
            else []
        )

        trace_issues = list(trace.get("quality_gate_issues") or [])
        remaining_issues = list(trace.get("quality_gate_remaining_issues") or [])
        if trace_issues:
            trace_issue_cases += 1
        if trace.get("quality_gate_repaired"):
            repaired_cases += 1
        if trace.get("quality_gate_repair_failed"):
            repair_failed_cases += 1
        if trace.get("quality_gate_repair_unresolved"):
            repair_unresolved_cases += 1
        if trace.get("quality_gate_template_fallback"):
            template_fallback_cases += 1
        if remaining_issues:
            remaining_issue_cases += 1

        for error in result.get("errors") or []:
            error_counts[error] += 1
        for issue in trace_issues or offline_issues:
            issue_counts[issue] += 1

        case_diag = {
            "case_id": case_id,
            "passed": bool(result.get("passed")),
            "query": query,
            "errors": list(result.get("errors") or []),
            "metrics": result.get("metrics") or {},
            "quality_gate_issues": trace_issues,
            "quality_gate_remaining_issues": remaining_issues,
            "quality_gate_repaired": bool(trace.get("quality_gate_repaired")),
            "quality_gate_repair_failed": bool(trace.get("quality_gate_repair_failed")),
            "quality_gate_repair_unresolved": bool(trace.get("quality_gate_repair_unresolved")),
            "quality_gate_template_fallback": bool(trace.get("quality_gate_template_fallback")),
            "offline_quality_issues": offline_issues,
            "generated_sql_preview": _preview_sql(generated_sql),
        }

        if result.get("passed"):
            if offline_issues:
                offline_passed_flagged += 1
                passed_flagged.append(case_diag)
        else:
            if offline_issues:
                offline_failed_flagged += 1
            else:
                offline_unflagged_failures.append(case_id)
            failures.append(case_diag)

    summary = payload.get("summary") or {}
    return {
        "metadata": metadata,
        "sql_dialect": sql_dialect,
        "summary": summary,
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": passed / max(1, total),
        "metrics": summary.get("metrics") or {},
        "quality_gate": {
            "trace_issue_cases": trace_issue_cases,
            "repaired_cases": repaired_cases,
            "repair_failed_cases": repair_failed_cases,
            "repair_unresolved_cases": repair_unresolved_cases,
            "template_fallback_cases": template_fallback_cases,
            "remaining_issue_cases": remaining_issue_cases,
            "offline_failed_flagged": offline_failed_flagged,
            "offline_unflagged_failures": offline_unflagged_failures,
            "offline_passed_flagged": offline_passed_flagged,
            "offline_failure_coverage": offline_failed_flagged / max(1, failed),
            "offline_pass_false_positive_rate": offline_passed_flagged / max(1, passed),
        },
        "issue_counts": _counter_rows(issue_counts),
        "error_counts": _counter_rows(error_counts),
        "failures": failures,
        "passed_flagged": passed_flagged,
    }


def render_markdown(analysis: dict[str, Any]) -> str:
    """把分析结果渲染成便于贴到复盘文档里的 Markdown。"""

    metrics = analysis.get("metrics") or {}
    quality = analysis.get("quality_gate") or {}
    lines = [
        "# Eval Report Analysis",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| total | {analysis['total']} |",
        f"| passed | {analysis['passed']} |",
        f"| failed | {analysis['failed']} |",
        f"| pass_rate | {_format_float(analysis['pass_rate'])} |",
        f"| sql_dialect | {analysis.get('sql_dialect', 'generic')} |",
    ]
    for name in (
        "table_recall",
        "table_accuracy",
        "execution_success",
        "value_set_exact",
        "value_set_recall",
        "exact_sql",
        "keyword_recall",
    ):
        if name in metrics:
            lines.append(f"| {name} | {_format_float(metrics[name])} |")

    lines.extend(
        [
            "",
            "## Quality Gate",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            f"| trace_issue_cases | {quality.get('trace_issue_cases', 0)} |",
            f"| repaired_cases | {quality.get('repaired_cases', 0)} |",
            f"| repair_failed_cases | {quality.get('repair_failed_cases', 0)} |",
            f"| repair_unresolved_cases | {quality.get('repair_unresolved_cases', 0)} |",
            f"| template_fallback_cases | {quality.get('template_fallback_cases', 0)} |",
            f"| remaining_issue_cases | {quality.get('remaining_issue_cases', 0)} |",
            (
                "| offline_failed_flagged | "
                f"{quality.get('offline_failed_flagged', 0)}/{analysis['failed']} |"
            ),
            (
                "| offline_passed_flagged | "
                f"{quality.get('offline_passed_flagged', 0)}/{analysis['passed']} |"
            ),
        ]
    )

    if quality.get("offline_unflagged_failures"):
        lines.extend(["", "Unflagged failures:"])
        lines.extend(f"- `{case_id}`" for case_id in quality["offline_unflagged_failures"])

    lines.extend(["", "## Issue Counts", "", "| Issue | Count |", "| --- | ---: |"])
    for row in analysis.get("issue_counts", [])[:20]:
        lines.append(f"| {row['name']} | {row['count']} |")
    if not analysis.get("issue_counts"):
        lines.append("| none | 0 |")

    lines.extend(["", "## Failed Cases", "", "| Case | Errors | Quality Issues |", "| --- | --- | --- |"])
    for case in analysis.get("failures", []):
        errors = "<br>".join(case["errors"]) or "-"
        issues = case["quality_gate_issues"] or case["offline_quality_issues"]
        issue_text = "<br>".join(issues) or "-"
        lines.append(f"| `{case['case_id']}` | {errors} | {issue_text} |")
    if not analysis.get("failures"):
        lines.append("| none | - | - |")

    if analysis.get("passed_flagged"):
        lines.extend(
            [
                "",
                "## Passed Cases Flagged By Current Rules",
                "",
                "| Case | Issues |",
                "| --- | --- |",
            ]
        )
        for case in analysis["passed_flagged"]:
            lines.append(
                f"| `{case['case_id']}` | {'<br>'.join(case['offline_quality_issues'])} |"
            )
    return "\n".join(lines) + "\n"


def _trace_hits_to_retrieval_hits(items: list[dict[str, Any]]) -> list[RetrievalHit]:
    hits: list[RetrievalHit] = []
    for item in items:
        table_name = item.get("table")
        if table_name:
            hits.append(RetrievalHit(TableInfo(table_name), float(item.get("score") or 0.0)))
    return hits


def _select_eval_run(repository: EvalRunRepository, run_id: int | None) -> EvalRunRecord:
    runs = repository.list_runs()
    if not runs:
        raise RuntimeError("No eval runs found in metadata database")
    if run_id is None:
        return runs[0]
    for run in runs:
        if run.id == run_id:
            return run
    raise RuntimeError(f"Eval run {run_id} was not found in metadata database")


def _case_record_to_report_result(record: EvalCaseResultRecord) -> dict[str, Any]:
    metrics = to_plain(record.metrics or {})
    sql_generation = metrics.get("sql_generation") if isinstance(metrics, dict) else {}
    if not isinstance(sql_generation, dict):
        sql_generation = {}

    trace = {
        "query": record.query,
        "rewritten_query": record.rewritten_query,
        "retrieval_hits": record.retrieval_hits or [],
        "table_relationship": record.table_relationship or [],
        "few_shot_examples": record.few_shot_examples or [],
        "prompt": record.prompt,
        "generated_sql": record.generated_sql,
        "execution_rows": record.execution_rows or [],
        "row_count": record.row_count,
        "clarification": record.clarification,
        **sql_generation,
    }
    return {
        "case_id": record.case_id,
        "passed": bool(record.passed),
        "metrics": metrics,
        "generated_sql": record.generated_sql,
        "errors": list(record.errors or []),
        "trace": to_plain(trace),
    }


def _counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [{"name": name, "count": count} for name, count in counter.most_common()]


def _preview_sql(sql: str, limit: int = 500) -> str:
    compact = " ".join((sql or "").split())
    return compact if len(compact) <= limit else f"{compact[:limit]}..."


def _format_float(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _json_default(value: Any) -> Any:
    plain = to_plain(value)
    if plain is not value:
        return plain
    if isinstance(value, set):
        return [to_plain(item) for item in sorted(value, key=str)]
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _redact_database_url(url: str) -> str:
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    userinfo, host = rest.rsplit("@", 1)
    if ":" not in userinfo:
        return url
    username, _password = userinfo.split(":", 1)
    return f"{scheme}://{username}:***@{host}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze text2sql eval report/case traces")
    parser.add_argument("--report", help="eval JSON report path")
    parser.add_argument("--metadata-db", help="metadata DB URL; defaults to TEXT2SQL_METADATA_DATABASE_URL")
    parser.add_argument("--run-id", type=int, help="eval_run id to analyze from metadata DB")
    parser.add_argument(
        "--latest-run",
        action="store_true",
        help="analyze latest eval_run from metadata DB; default when --report is omitted",
    )
    parser.add_argument(
        "--db",
        help="target SQL DB URL/path used by eval; helps replay dialect-specific SQL checks",
    )
    parser.add_argument("--cases", help="optional JSONL cases path for query text")
    parser.add_argument("--output", help="write Markdown analysis to this path")
    parser.add_argument("--json-output", help="write structured JSON analysis to this path")
    args = parser.parse_args()

    if args.report:
        payload = load_report(args.report)
        if args.db:
            payload.setdefault("metadata", {})["db"] = args.db
    else:
        payload = load_report_from_repository(
            build_eval_run_repository(args.metadata_db),
            run_id=args.run_id,
            metadata={"db": args.db} if args.db else None,
        )

    analysis = analyze_report(payload, load_case_queries(args.cases))
    markdown = render_markdown(analysis)
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
    else:
        print(markdown, end="")
    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
