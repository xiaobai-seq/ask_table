from __future__ import annotations

"""评测 CLI。

评测从 JSONL 读取问题和期望项，逐条运行完整 Text2SQLWorkflow，
再计算表召回、SQL 精确匹配、关键词召回和执行成功率。
"""

import argparse
import asyncio
import json
from pathlib import Path

from text2sql.core.graph import Text2SQLWorkflow
from text2sql.core.models import EvalCase, EvalResult, to_plain
from text2sql.core.sql_validator import normalize_sql


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

        if state.get("clarification"):
            # 有些 case 预期就是澄清问题，例如含糊输入，不应算失败。
            if case.allow_clarification:
                metrics["clarification"] = 1.0
                return EvalResult(case.case_id, True, metrics, sql)
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

        passed = not errors and all(value >= 1.0 for value in metrics.values())
        return EvalResult(case.case_id, passed, metrics, sql, tuple(errors))

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
            )
        )
    return cases


def write_report(path: str | Path, results: list[EvalResult]) -> None:
    """写出包含整体通过率和逐 case 详情的 JSON 报告。"""

    payload = {
        "summary": {
            "total": len(results),
            "passed": sum(1 for result in results if result.passed),
            "pass_rate": sum(1 for result in results if result.passed) / max(1, len(results)),
        },
        "results": [to_plain(result) for result in results],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    """命令行入口：构造 workflow，运行 cases，输出报告路径。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="SQLite db path or SQLAlchemy URL")
    parser.add_argument("--cases", required=True, help="JSONL evaluation cases")
    parser.add_argument("--report", default="eval_report.json")
    args = parser.parse_args()

    db = args.db if "://" in args.db else f"sqlite:///{args.db}"
    workflow = Text2SQLWorkflow(database_url_or_path=db)
    cases = load_cases(args.cases)
    results = asyncio.run(EvaluationRunner(workflow).run(cases))
    write_report(args.report, results)
    passed = sum(1 for result in results if result.passed)
    print(f"pass_rate={passed}/{len(results)} report={args.report}")


if __name__ == "__main__":
    main()
